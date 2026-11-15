from decimal import ROUND_HALF_UP, Decimal

from .parsers import number

LIMITATION = "僅當期可取得 Top-50 買超＋Top-50 賣超範圍，非全分點排行；未上榜日不計加總"


def _lots_summary(history, code, window):
    rows = [r for r in history if r["code"] == code and r["trade_date"] in window]
    lots = [number(r.get("net_lots")) for r in rows]
    actual = [n for n in lots if n is not None]
    latest = next(
        (
            number(r.get("net_lots"))
            for r in rows
            if window and r["trade_date"] == window[-1]
        ),
        None,
    )
    return {
        "cum_net_lots": sum(actual, Decimal(0)) if actual else None,
        "n_days_with_lots": len(actual),
        "n_days_missing_lots": len(rows) - len(actual),
        "latest_net_lots": latest,
    }


def grouped_ranking(history, window_days, direction):
    """Return coverage-keyed groups over explicit dates; callers choose presentation grouping.

    Ranking n_days_present counts only days with a nonmissing amount.
    Lots summaries count missing lots over B/E union appearance days instead;
    those days can differ from the amount days used for ranking groups.
    """
    totals, coverage = {}, {}
    for row in history:
        if row["trade_date"] not in window_days:
            continue
        code = row["code"]
        amount = number(row.get("net_amount_ntd"))
        if amount is not None:
            coverage.setdefault(code, set()).add(row["trade_date"])
            totals[code] = totals.get(code, Decimal(0)) + amount / 1000
    groups = {}
    buy = direction == "buy"
    for code, amount in totals.items():
        if amount > 0 if buy else amount < 0:
            n = len(coverage[code])
            groups.setdefault(n, []).append(
                dict(
                    code=code,
                    net_amount_k=amount,
                    n_days_present=n,
                    **_lots_summary(history, code, window_days),
                )
            )
    return {
        n: sorted(
            groups[n],
            key=lambda r: (
                (-r["net_amount_k"] if buy else r["net_amount_k"]),
                r["code"],
            ),
        )
        for n in sorted(groups, reverse=True)
    }


def metrics(history, date, watchlist):
    days = sorted({r["trade_date"] for r in history if r["trade_date"] <= date})
    by = {(r["trade_date"], r["code"]): r for r in history}
    today = {r["code"]: r for r in history if r["trade_date"] == date}
    result = {}
    for code in sorted(set(today) | set(watchlist)):
        r = today.get(code, {})
        actual, amount = number(r.get("net_lots")), number(r.get("net_amount_ntd"))
        close, volume = number(r.get("close")), number(r.get("volume_shares"))
        estimated = (
            amount / close / 1000
            if amount is not None and close and close > 0
            else None
        )
        concentration = (
            (100 * actual / (volume / 1000)).quantize(
                Decimal(".1"), rounding=ROUND_HALF_UP
            )
            if actual is not None and volume and volume > 0
            else None
        )
        streak = 0
        for day in reversed(days):
            n = number(by.get((day, code), {}).get("net_lots"))
            if n is None or n <= 0:
                break
            streak += 1
        result[code] = {
            "actual_net_lots": actual,
            "estimated_net_lots": estimated,
            "concentration": concentration,
            "volume_shares": volume,
            "close": close,
            "streak": streak,
            "present": code in today,
        }
    return result


def evaluate(history, date, watchlist, rules):
    ms = metrics(history, date, watchlist)
    days = sorted({r["trade_date"] for r in history if r["trade_date"] <= date})
    refs = sorted({r["publication_id"] for r in history})
    events, unknown = [], []
    for rule in rules:
        if not rule["enabled"]:
            continue
        rid = rule["rule_id"]

        def event(code, inputs, reason, rid=rid, rule=rule):
            events.append(
                {
                    "rule_id": rid,
                    "rule_version": rule["version"],
                    "code": code,
                    "inputs": inputs,
                    "thresholds": rule,
                    "reason": reason,
                    "basis": rule.get("basis", "actual"),
                    "publication_refs": refs,
                }
            )

        if rule["kind"] == "ranking":
            if rule["metric"] != "net_amount_k":
                raise ValueError("unknown metric")
            window = days[-rule["window"] :]
            for row in history:
                if (
                    row["trade_date"] in window
                    and number(row.get("net_amount_ntd")) is None
                ):
                    unknown.append(
                        {
                            "rule_id": rid,
                            "code": row["code"],
                            "reason": "missing amount",
                        }
                    )
            groups = grouped_ranking(history, window, rule["direction"])
            # Total grouping ranks across all coverage groups, including historical definitions.
            if rule.get("grouping") != "same-coverage":
                buy = rule["direction"] == "buy"
                groups = {
                    0: sorted(
                        [r for rows in groups.values() for r in rows],
                        key=lambda r: (
                            (-r["net_amount_k"] if buy else r["net_amount_k"]),
                            r["code"],
                        ),
                    )
                }
            for rows in groups.values():
                for row in rows[: rule["top_n"]]:
                    event(
                        row["code"],
                        {
                            k: v
                            for k, v in dict(row, window=window).items()
                            if k != "code"
                        },
                        LIMITATION,
                    )
        else:
            for code in sorted(watchlist):
                m = dict(ms[code])
                window = days[-3:]
                target = next(
                    (
                        r
                        for r in history
                        if r["trade_date"] == date and r["code"] == code
                    ),
                    {},
                )
                amount = number(target.get("net_amount_ntd"))
                m.update(
                    _lots_summary(history, code, window),
                    window=window,
                    n_days_present=sum(
                        r["code"] == code and r["trade_date"] in window for r in history
                    ),
                    net_amount_k=amount / 1000 if amount is not None else None,
                )
                if not m["present"] or m["close"] is None:
                    unknown.append(
                        {
                            "rule_id": rid,
                            "code": code,
                            "reason": "missing flow/close; stock rule skipped",
                        }
                    )
                    continue
                if rule["kind"] == "concentration":
                    n = (
                        m["actual_net_lots"]
                        if rule.get("basis") != "estimated"
                        else m["estimated_net_lots"]
                    )
                    v = m["volume_shares"]
                    if n is None or not v or v <= 0:
                        unknown.append(
                            {
                                "rule_id": rid,
                                "code": code,
                                "reason": "missing lots/volume",
                            }
                        )
                        continue
                    concentration = (100 * n / (v / 1000)).quantize(
                        Decimal(".1"), rounding=ROUND_HALF_UP
                    )
                    if n > 0 and concentration >= rule["threshold"]:
                        event(
                            code,
                            dict(**m, evaluated_concentration=concentration),
                            "集中度達門檻且淨買超",
                        )
                elif rule["kind"] == "streak":
                    if m["streak"] >= rule["days"]:
                        event(code, m, "實際淨張數連續買超達門檻")
                    elif m["streak"] < rule["days"] and (
                        len(days) < rule["days"]
                        or any(
                            not any(
                                r["trade_date"] == d
                                and r["code"] == code
                                and number(r["net_lots"]) is not None
                                for r in history
                            )
                            for d in days[-rule["days"] :]
                        )
                    ):
                        unknown.append(
                            {
                                "rule_id": rid,
                                "code": code,
                                "reason": "insufficient history",
                            }
                        )
                else:
                    raise ValueError("unknown rule kind")
    return events, unknown, ms
