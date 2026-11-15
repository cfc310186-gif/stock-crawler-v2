import json
from pathlib import Path

from .delivery import business_delivery_key
from .parsers import number
from .rules import LIMITATION, _lots_summary

SOURCE_NOTE = "來源：2026-11-11 起金額為富邦分點重播；之前金額為舊表 Stock_Data 淨額、未重新抓取；張數僅含 2026-11-11 起真實重播值，更早日期只有金額、沒有張數；累積張數僅加總有值日期，張數與金額缺值欄位省略。"

RANKING_RULES = {
    "R4": (3, "buy"),
    "R5": (3, "sell"),
    "R6": (10, "buy"),
    "R7": (10, "sell"),
}


def _ranking_groups(events):
    """Use the configured horizon, never the length of partial history."""
    groups = []
    for window in (3, 10):
        for direction, label in (("buy", "買超"), ("sell", "賣超")):
            selected = []
            for event in events:
                rid = event["rule_id"]
                if rid not in RANKING_RULES:
                    continue
                fallback_window, fallback_direction = RANKING_RULES[rid]
                rule = event.get("thresholds", {})
                if (
                    rule.get("window", fallback_window),
                    rule.get("direction", fallback_direction),
                ) != (window, direction):
                    continue
                selected.append(event)

            def order(event, direction=direction):
                amount = number(event["inputs"].get("net_amount_k"))
                return (
                    amount is None,
                    (-amount if direction == "buy" else amount)
                    if amount is not None
                    else 0,
                    event["code"],
                )

            selected = sorted(selected, key=order)[:3]
            if selected:
                groups.append(
                    (f"{label}・近{window}日", selected)
                )
    return groups


def _fmt(value):
    n = number(value)
    return (
        f"{n:,.0f}"
        if n is not None and n == n.to_integral_value()
        else f"{n:,}"
        if n is not None
        else "—"
    )


def _line(
    code,
    i,
    direction,
    trigger,
    estimated=False,
    *,
    ranking=False,
    name=None,
):
    lots = (
        i.get("estimated_net_lots")
        if estimated
        else i.get("latest_net_lots", i.get("actual_net_lots"))
    )
    if all(v is None for v in (lots, i.get("cum_net_lots"), i.get("net_amount_k"))):
        direction = "方向未明"
    prefix = " ".join(part for part in (code, name, direction) if part)
    if lots is not None:
        prefix += f" {'估算' if estimated else ''}{_fmt(lots)}張"
    fields = [prefix]
    for key, label, unit in (
        ("cum_net_lots", "累積", "張"),
        ("net_amount_k", "金額", "千元"),
    ):
        value = i.get(key)
        if value is not None:
            fields.append(f"{label} {_fmt(value)}{unit}")
    if trigger and not ranking:
        fields.append(trigger)
    return "｜".join(fields)


def watchlist_dynamics(history, date, watchlist):
    """Summarize current-day members over the latest three trading dates."""
    seen = set()
    for r in history:
        identity = (r["trade_date"], r["code"])
        if identity in seen:
            raise ValueError(f"duplicate history row: {identity}")
        seen.add(identity)
    window = sorted({r["trade_date"] for r in history if r["trade_date"] <= date})[-3:]
    members = {
        r["code"] for r in history if r["trade_date"] == date and r["code"] in watchlist
    }
    rows = []
    for code in members:
        amounts = [
            number(r.get("net_amount_ntd"))
            for r in history
            if r["code"] == code and r["trade_date"] in window
        ]
        amounts = [a for a in amounts if a is not None]
        total = sum(amounts) / 1000 if amounts else None
        direction = (
            "方向未明"
            if total is None
            else "買超"
            if total > 0
            else "賣超"
            if total < 0
            else "持平"
        )
        rows.append(
            {
                "code": code,
                "net_amount_k": total,
                "n_days_present": len(amounts),
                "direction": direction,
                **_lots_summary(history, code, window),
            }
        )
    return sorted(
        rows,
        key=lambda r: (
            r["net_amount_k"] is None,
            -abs(r["net_amount_k"]) if r["net_amount_k"] is not None else 0,
            r["code"],
        ),
    )


def build(date, publication, events, correction=False, watchlist=None, *, names=None):
    names = {} if names is None else names
    rows = [] if watchlist is None else watchlist
    key = business_delivery_key(date, correction)
    sections = [
        ("高優先訊號", {"R1"}),
        ("一般動向", {"R2", "R3"}),
        ("全分點排行", set(RANKING_RULES)),
    ]

    text = [
        ("更正：" if correction else "") + f"{date} 9A91 日報 草稿未發送",
        LIMITATION,
        SOURCE_NOTE,
    ]
    text.append("\nWatchlist 動態")
    text.extend(
        _line(r["code"], r, r["direction"], None, name=names.get(r["code"]))
        for r in rows
    )
    if not rows:
        text.append("無")
    for title, ids in sections:
        text.append("\n" + title)
        selected = [e for e in events if e["rule_id"] in ids]
        groups = (
            _ranking_groups(selected)
            if ids == set(RANKING_RULES)
            else [(None, selected)]
            if selected
            else []
        )
        if not groups:
            text.append("無")
        headed_events = [
            (heading if index == 0 else None, event)
            for heading, group in groups
            for index, event in enumerate(group)
        ]
        for heading, e in headed_events:
            if heading:
                text.append(heading)
            i, rid = e["inputs"], e["rule_id"]
            trigger = {
                "R1": (
                    f"集中度 {_fmt(i['evaluated_concentration'])}% ≥ 5%"
                    if i.get("evaluated_concentration") is not None
                    else None
                ),
                "R2": f"連續買超 {i.get('streak', 0)} 日 ≥ 3 日",
                "R4": "區間累積買超前3大",
                "R5": "區間累積賣超前3大",
            }.get(rid, e["reason"])
            direction = "賣超" if rid in {"R5", "R7"} else "買超"
            text.append(
                _line(
                    e["code"],
                    i,
                    direction,
                    trigger,
                    rid == "R1" and e.get("basis") == "estimated",
                    ranking=rid in RANKING_RULES,
                    name=names.get(e["code"]),
                )
            )
    return {
        "delivery_key": key,
        "date": date,
        "publication_version": publication,
        "correction": correction,
        "text": "\n".join(text),
        "events": events,
        "watchlist": rows,
        "state": "draft",
    }


def save(draft, root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    suffix = "-correction-" + draft["delivery_key"][:10] if draft["correction"] else ""
    path = root / f"{draft['date']}-line-draft{suffix}"
    path.with_suffix(".json").write_text(
        json.dumps(draft, ensure_ascii=False, indent=2, default=str)
    )
    path.with_suffix(".md").write_text(draft["text"])
