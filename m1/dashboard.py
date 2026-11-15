"""Private dashboard. Launch with --server.address 127.0.0.1."""

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import altair as alt
import pandas as pd
import streamlit as st

from m1 import outbox, store
from m1.parsers import number
from m1.perf import measure
from m1.rules import LIMITATION, _lots_summary, grouped_ranking


def publication_keys(db):
    c = store.connect(db, readonly=True)
    try:
        return tuple(
            tuple(r)
            for r in c.execute(
                "SELECT id,trade_date,published_at FROM publications WHERE current=1 ORDER BY trade_date DESC"
            )
        )
    finally:
        c.close()


@st.cache_data
def load(db, versions, date):
    c = store.connect(db, readonly=True)
    try:
        pid = next(p[0] for p in versions if p[1] == date)
        return {
            "history": store.history(c, date),
            "events": [
                json.loads(r[0])
                for r in c.execute(
                    "SELECT event_json FROM alert_events WHERE publication_id=?", (pid,)
                )
            ],
        }
    finally:
        c.close()


def watchlist(db):
    c = store.connect(db, readonly=True)
    try:
        return [
            dict(r)
            for r in c.execute("SELECT * FROM watchlist_entries ORDER BY category,code")
        ]
    finally:
        c.close()


def name_index(history, date, watch):
    names = {w["code"]: w["name"] for w in watch if w.get("name")}
    for row in sorted(history, key=lambda r: r["trade_date"]):
        if row["trade_date"] <= date and row.get("name"):
            names[row["code"]] = row["name"]
    return names


def context(show_date=True, allow_empty=False):
    db = st.session_state.get(
        "db_path", os.environ.get("DASHBOARD_DB", "data/trial-20261113-v2.5.sqlite")
    )
    with measure(st.session_state, "load"):
        versions = publication_keys(db)
        watch = watchlist(db)
        dates = [p[1] for p in versions]
        requested = st.query_params.get("trade_date")
        date = requested if requested in dates else dates[0] if dates else ""
        if requested and requested not in dates:
            st.warning("指定交易日未發布；目前顯示最新已發布交易日")
        if show_date and dates:
            with st.expander(
                "更換交易日", key="date_picker", on_change="rerun"
            ) as picker:
                if picker.open:
                    date = st.selectbox("交易日", dates, index=dates.index(date))
                    st.query_params["trade_date"] = date
        data = load(db, versions, date) if dates else {"history": [], "events": []}
        data.update(db=db, versions=versions, date=date, watch=watch)
        c = store.connect(db, readonly=True)
        try:
            data["runs"] = [
                dict(r)
                for r in c.execute(
                    "SELECT target_date,status FROM runs ORDER BY rowid DESC LIMIT 10"
                )
            ]
            data["last_success"] = c.execute(
                "SELECT MAX(target_date) FROM runs WHERE status='success'"
            ).fetchone()[0]
        finally:
            c.close()
        data["names"] = name_index(data["history"], date, watch)
    if not dates and not allow_empty:
        st.info("尚無已發布資料；可到「我的」管理共用名單")
        st.stop()
    return data


def fmt(n):
    return "未提供" if n is None else f"{number(n):,}"


def format_published_at(value, *, now=None):
    if not value:
        return "發布時間未提供"
    try:
        timestamp = datetime.fromisoformat(value)
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            return "發布時間待確認"
        local = timestamp.astimezone(ZoneInfo("Asia/Taipei"))
    except (ValueError, TypeError, OverflowError):
        return "發布時間待確認"
    current = (now or datetime.now(ZoneInfo("Asia/Taipei"))).astimezone(
        ZoneInfo("Asia/Taipei")
    )
    pattern = "%m/%d %H:%M" if local.year == current.year else "%Y/%m/%d %H:%M"
    return f"發布於 {local.strftime(pattern)} 台北時間"


def choose(kind, label, options, key, default, **kw):
    previous = st.session_state.get(key + "_selected", default)
    value = kind(label, options, default=previous, key=key, **kw)
    value = previous if value is None else value
    st.session_state[key + "_selected"] = value
    return value


def period_window(dates, anchor, days=3, start=None, end=None):
    available = sorted({date for date in dates if date <= anchor})
    if days == "自訂":
        return [date for date in available if start <= date <= end]
    return available[-days:]


def period_selector(d, prefix):
    dates = sorted({p[1] for p in d["versions"] if p[1] <= d["date"]})
    days = choose(
        st.pills,
        "排行期間" if prefix == "rank" else "個股期間",
        [3, 5, 10, 20, "自訂"],
        prefix + "_period",
        3,
        format_func=lambda n: "自訂" if n == "自訂" else f"近{n}日",
    )
    start = end = None
    if days == "自訂":
        defaults = dates[-3:]
        # Anchor changes can remove a previously selected date from the options.
        for suffix in ("start", "end"):
            key = f"{prefix}_{suffix}"
            if st.session_state.get(key) not in dates:
                st.session_state.pop(key, None)
        start = st.selectbox(
            "開始交易日", dates, index=dates.index(defaults[0]), key=prefix + "_start"
        )
        end = st.selectbox(
            "結束交易日", dates, index=len(dates) - 1, key=prefix + "_end"
        )
        if start > end:
            st.warning("開始交易日不可晚於結束交易日")
    window = period_window(dates, d["date"], days, start, end)
    st.caption(
        f"區間：{window[0] if window else start} ~ {window[-1] if window else end}（{len(window)} 個交易日）"
    )
    return window, "自訂" if days == "自訂" else f"近{days}日"


def ranking_chips():
    side = choose(
        st.segmented_control, "方向", ["買超", "賣超"], "rank_direction", "買超"
    )
    minimum = 0
    with st.expander("進階篩選：自訂排行，不影響日報摘要"):
        threshold = choose(
            st.pills, "金額門檻（千元）", [0, 1000, 5000, 10000], "rank_minimum", 0
        )
        custom = st.checkbox("使用自訂門檻", key="rank_custom")
        value = st.number_input(
            "自訂金額（千元）", min_value=0, value=0, key="rank_amount"
        )
        minimum = value if custom else threshold
    return side, minimum


def amount_chart_data(history, code, window):
    amounts = {
        r["trade_date"]: number(r.get("net_amount_ntd"))
        for r in history
        if r["code"] == code and r["trade_date"] in window
    }
    frame = pd.DataFrame(
        [
            {
                "日期": date,
                "每日淨金額": float(amounts[date] / 1000)
                if amounts.get(date) is not None
                else None,
            }
            for date in sorted(window)
        ],
        columns=["日期", "每日淨金額"],
    )
    frame["累積淨金額"] = frame["每日淨金額"].cumsum()
    return frame


def render_amount_chart(history, code, window):
    if not any(r["code"] == code and r["trade_date"] in window for r in history):
        st.info("此區間無資料")
        return
    frame = amount_chart_data(history, code, window)
    base = alt.Chart(frame).encode(
        x=alt.X("日期:O", title="交易日", axis=alt.Axis(labelAngle=-45))
    )
    bars = base.mark_bar().encode(
        y=alt.Y("每日淨金額:Q", title="淨金額（千元）"),
        color=alt.condition(
            alt.datum["每日淨金額"] >= 0, alt.value("#E67F75"), alt.value("#6CB097")
        ),
        tooltip=["日期:O", "每日淨金額:Q", "累積淨金額:Q"],
    )
    line = base.mark_line(point=True, color="#537CAD").encode(y="累積淨金額:Q")
    st.altair_chart(
        (bars + line).properties(height=300),
        width="stretch",
        alt=f"{code} 每日淨金額長條與累積淨金額折線，單位千元",
    )
    st.caption(
        "金額為實際淨金額；實際張數僅自 2026-11-11 起提供，且限 Top-50 張數榜代號。缺少個股資料的交易日留空，不補零。"
    )


def stock_url(code, date):
    return "stock?" + urlencode({"code": str(code), "trade_date": date})


def card_lines(code, name, row, *, estimated=False):
    amount = number(row.get("net_amount_k"))
    direction = (
        "方向未明"
        if amount is None
        else "買超"
        if amount > 0
        else "賣超"
        if amount < 0
        else "持平"
    )
    lots = row.get("estimated_net_lots") if estimated else row.get("latest_net_lots")
    if lots is None and not estimated:
        lots = row.get("actual_net_lots")
    fields = []
    if lots is not None:
        fields.append(f"{'估算' if estimated else '當日'} {fmt(lots)}張")
    if row.get("cum_net_lots") is not None:
        fields.append(f"累積 {fmt(row['cum_net_lots'])}張")
    if not fields and amount is None:
        fields.append("張數與金額未提供")
    return (
        f"{code} {name or '名稱未提供'}　{direction}",
        "｜".join(fields),
        f"金額 {fmt(amount)}千元" if amount is not None else "",
    )


def render_stock_card(d, row, *, event=None, link=True):
    code = row["code"]
    lines = card_lines(
        code,
        d["names"].get(code),
        row,
        estimated=bool(event and event.get("basis") == "estimated"),
    )
    with st.container(border=True):
        st.markdown(f"**{lines[0]}**")
        if lines[1]:
            st.write(lines[1])
        with st.container(horizontal=True, vertical_alignment="center"):
            if lines[2]:
                st.write(lines[2])
            if link:
                st.link_button("查看個股", stock_url(code, d["date"]))
        if event:
            reason = event.get("reason", "").replace(LIMITATION, "").strip("；; ")
            if reason:
                st.write(reason)
            with st.expander("訊號細節"):
                st.json(event)


def flat_rank_rows(history, window, direction):
    rows = [
        row
        for group in grouped_ranking(history, window, direction).values()
        for row in group
    ]
    return sorted(
        rows,
        key=lambda r: (
            -r["net_amount_k"] if direction == "buy" else r["net_amount_k"],
            r["code"],
        ),
    )


def top3_groups(history, date):
    dates = sorted({r["trade_date"] for r in history if r["trade_date"] <= date})
    return [
        (
            days,
            direction,
            dates[-days:],
            flat_rank_rows(history, dates[-days:], direction)[:3],
        )
        for days in (3, 10)
        for direction in ("buy", "sell")
    ]


@st.cache_data
def summary_data(db, versions, date, _history):
    # Publications are immutable; their version keys invalidate this presentation
    # cache. Avoid re-running four ranking calculations before the first card.
    return top3_groups(_history, date)


def ranking_summary(d):
    st.caption("Top50可取得範圍；金額與累積張數為區間合計")
    stored = {e["rule_id"] for e in d["events"]}
    if stored & {"R4", "R5"} and not stored & {"R6", "R7"}:
        st.caption(
            "既有發布事件可能使用舊版規則；以下為現有資料的3／10日摘要，未修改已發布事件"
        )
    with measure(st.session_state, "compute"):
        groups = summary_data(d["db"], d["versions"], d["date"], d["history"])
    for days, direction, window, rows in groups:
        st.subheader(f"近{days}日{'買超' if direction == 'buy' else '賣超'}前3")
        if len(window) < days:
            st.caption(
                f"可用 {len(window)} 個交易日：{window[0] if window else '無'} 至 {window[-1] if window else '無'}"
            )
        for row in rows:
            render_stock_card(d, row)
        if not rows:
            st.write("無符合資料")


def source_notes():
    with st.expander("資料範圍與來源"):
        st.write(LIMITATION)
        st.write(outbox.SOURCE_NOTE)


def edit(db, code, name, category, remove=False):
    try:
        c = store.connect(db)
        try:
            store.edit_watchlist(c, code, name, category, remove)
        finally:
            c.close()
        if not remove:
            st.session_state["last_category"] = category
        st.success(
            f"{code} 已{'移除' if remove else '儲存'}・{category}；分類可在「我的」更改"
        )
        return True
    except (ValueError, sqlite3.Error) as exc:
        st.error(f"儲存失敗：{exc}")
        return False


def today_page():
    st.subheader("今日")
    d = context()
    pub = next(p for p in d["versions"] if p[1] == d["date"])
    st.caption(f"交易日 {d['date']}｜{format_published_at(pub[2])}")
    latest = d["runs"][0] if d["runs"] else {}
    if latest.get("status") not in (None, "success"):
        st.warning("最近執行失敗；目前顯示最後已發布資料")
    with measure(st.session_state, "compute"):
        dynamics = outbox.watchlist_dynamics(
            d["history"], d["date"], {w["code"] for w in d["watch"]}
        )
    with measure(st.session_state, "render"):
        st.subheader("Watchlist 動態")
        st.caption("金額與累積張數為近3日；單日張數為所選交易日。缺值欄位省略。")
        for row in dynamics:
            render_stock_card(d, row)
        if not dynamics:
            st.write("自選股當日無可取得資料")
        signals = [e for e in d["events"] if e["rule_id"] in {"R1", "R2", "R3"}]
        if not signals:
            st.write("今日無觸發訊號")
        for title, ids in (("高優先訊號", {"R1"}), ("一般動向", {"R2", "R3"})):
            selected = [e for e in signals if e["rule_id"] in ids]
            if selected:
                st.subheader(title)
                for event in selected:
                    render_stock_card(
                        d, dict(event["inputs"], code=event["code"]), event=event
                    )
        st.subheader("全分點排行")
        ranking_summary(d)
        source_notes()


@st.cache_data
def rank_data(db, versions, history, window, direction, minimum, page):
    rows = [
        r
        for r in flat_rank_rows(history, window, direction)
        if abs(r["net_amount_k"]) >= minimum
    ]
    return rows[page * 20 : (page + 1) * 20], len(rows)


@st.fragment
def ranking_cards(d, window):
    side, minimum = ranking_chips()
    signature = (d["versions"], d["date"], side, tuple(window), minimum)
    if st.session_state.get("rank_signature") != signature:
        st.session_state.update(rank_signature=signature, rank_page=0)
    page = st.session_state["rank_page"]
    with measure(st.session_state, "compute"):
        rows, total = rank_data(
            d["db"],
            d["versions"],
            d["history"],
            window,
            "buy" if side == "買超" else "sell",
            minimum,
            page,
        )
    with measure(st.session_state, "render"):
        st.write(
            f"第 {page + 1} 頁・{page * 20 + 1 if total else 0}–{min((page + 1) * 20, total)}／{total} 檔"
        )
        for row in rows:
            render_stock_card(d, row)
        if not rows:
            st.info("無符合資料")
        with st.container(horizontal=True):
            if st.button("上一頁", disabled=page == 0):
                st.session_state["rank_page"] = page - 1
                st.rerun(scope="fragment")
            if st.button("下一頁", disabled=(page + 1) * 20 >= total):
                st.session_state["rank_page"] = page + 1
                st.rerun(scope="fragment")


def ranking_page():
    st.subheader("排行")
    d = context()
    st.caption(f"交易日 {d['date']}")
    window, label = period_selector(d, "rank")
    st.caption("Top50可取得範圍；金額與累積張數為區間合計")
    for direction, title in (("buy", "買超"), ("sell", "賣超")):
        st.subheader(f"{label}{title}前3")
        rows = flat_rank_rows(d["history"], window, direction)[:3]
        for row in rows:
            render_stock_card(d, row)
        if not rows:
            st.info("此區間無資料")
    full = st.expander("完整排行", key="full_ranking", on_change="rerun")
    if full.open:
        with full:
            ranking_cards(d, window)
    source_notes()


def confirm_remove(d, w, key):
    confirm = st.checkbox("確認從共用 Watchlist 移除", key=f"confirm_{key}")
    if st.button("移除", key=f"remove_{key}", disabled=not confirm) and edit(
        d["db"], w["code"], w["name"], w["category"], True
    ):
        st.rerun()


def stock_page():
    query = st.text_input(
        "代號／名稱搜尋", value=st.query_params.get("code", ""), key="stock_search"
    )
    d = context(show_date=False)
    if not query.strip():
        st.info("輸入代號或名稱搜尋個股（例如 0050）")
        return
    linked = st.query_params.get("code")
    if linked and query == linked:
        code = linked
        if code not in d["names"] and not any(r["code"] == code for r in d["history"]):
            st.warning("指定代號無可取得資料；請重新搜尋")
            return
    else:
        options = sorted(
            {
                r["code"]
                for r in d["history"]
                if query.lower() in (r["code"] + d["names"].get(r["code"], "")).lower()
            }
        )[:10]
        if not options:
            st.info("無符合個股")
            return
        code = st.selectbox(
            "建議（最多 10 筆）",
            options,
            format_func=lambda c: f"{c} {d['names'].get(c, '名稱未提供')}",
        )
    with measure(st.session_state, "render"):
        st.subheader(f"{code} {d['names'].get(code, '名稱未提供')}")
        st.caption(f"交易日 {d['date']}")
        daily = next(
            (
                r
                for r in d["history"]
                if r["code"] == code and r["trade_date"] == d["date"]
            ),
            {},
        )
        st.write("當日資料")
        amount = number(daily.get("net_amount_ntd"))
        render_stock_card(
            d,
            {
                "code": code,
                "latest_net_lots": daily.get("net_lots"),
                "net_amount_k": amount / 1000 if amount is not None else None,
            },
            link=False,
        )
        window, label = period_selector(d, "stock")
        with measure(st.session_state, "compute"):
            rows = [
                r
                for r in d["history"]
                if r["code"] == code and r["trade_date"] in window
            ]
            amounts = [
                number(r.get("net_amount_ntd"))
                for r in rows
                if number(r.get("net_amount_ntd")) is not None
            ]
            summary = _lots_summary(d["history"], code, window)
        st.write(f"{label}區間合計")
        render_stock_card(
            d,
            dict(
                summary,
                code=code,
                latest_net_lots=None,
                net_amount_k=sum(amounts) / 1000 if amounts else None,
            ),
            link=False,
        )
        present = next((w for w in d["watch"] if w["code"] == code), None)
        if present:
            with st.expander("管理 Watchlist"):
                confirm_remove(d, present, "stock")
        elif st.button("⭐ 加入 Watchlist"):
            category = st.session_state.get("last_category") or next(
                (w["category"] for w in d["watch"]), "未分類"
            )
            if edit(d["db"], code, d["names"].get(code, ""), category):
                st.rerun()
        render_amount_chart(d["history"], code, window)
        st.subheader("訊號原因")
        for event in d["events"]:
            if event["code"] == code and event["rule_id"] in {"R1", "R2", "R3"}:
                render_stock_card(d, dict(event["inputs"], code=code), event=event)
        detail = st.expander("詳細資料", key="stock_detail", on_change="rerun")
        if detail.open:
            with detail:
                estimates = [
                    number(r["net_amount_ntd"]) / number(r["close"]) / 1000
                    for r in rows
                    if number(r.get("net_amount_ntd")) is not None
                    and number(r.get("close"))
                    and number(r["close"]) > 0
                ]
                st.write(
                    f"估算淨張數（非實際）：{fmt(sum(estimates) if estimates else None)}張"
                )
                st.write("成本：不可用（無法驗證）")
                st.write(
                    f"區間缺張數 {summary['n_days_missing_lots']} 日；累積僅含有值日期"
                )
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "日期": r["trade_date"],
                                "代號": r["code"],
                                "名稱": r.get("name"),
                                "實際淨張數（張）": r.get("net_lots"),
                                "淨金額（千元）": number(r["net_amount_ntd"]) / 1000
                                if number(r.get("net_amount_ntd")) is not None
                                else None,
                                "未還原收盤價（元）": r.get("close"),
                            }
                            for r in rows
                        ]
                    ),
                    alt=f"{code} 每日實際張數、金額及未還原收盤價",
                )
        source_notes()


def diagnostic_rows(db, query, args=()):
    c = store.connect(db, readonly=True)
    try:
        return [dict(r) for r in c.execute(query, args)]
    finally:
        c.close()


def mine_page():
    d = context(show_date=False, allow_empty=True)
    with measure(st.session_state, "render"):
        st.subheader("我的 Watchlist")
        categories = sorted({w["category"] for w in d["watch"]})
        st.caption(
            f"{len(d['watch'])} 檔／{len(categories)} 分類・名單由所有使用者共用"
        )
        for category in categories:
            st.write(category)
            for w in d["watch"]:
                if w["category"] != category:
                    continue
                with st.container(border=True):
                    st.markdown(
                        f"**{w['code']} {d['names'].get(w['code'], '名稱未提供')}**"
                    )
                    st.link_button("查看個股", stock_url(w["code"], d["date"]))
                    management = st.expander(
                        "管理", key=f"manage_{w['code']}", on_change="rerun"
                    )
                    if management.open:
                        with management:
                            new_category = st.text_input(
                                "分類", w["category"], key=f"category_{w['code']}"
                            )
                            if st.button("儲存分類", key=f"save_{w['code']}") and edit(
                                d["db"], w["code"], w["name"], new_category
                            ):
                                st.rerun()
                            confirm_remove(d, w, w["code"])
        if not d["watch"]:
            st.info("名單尚無個股；請到「個股」搜尋並加入")
        health = st.expander("資料健康", key="mine_health", on_change="rerun")
        if health.open:
            with health:
                st.write(
                    "最新已發布交易日：",
                    d["versions"][0][1] if d["versions"] else "尚無",
                )
                st.write("最後成功日：", d["last_success"] or "無")
                st.write("目前交易日：", d["date"] or "尚無")
                for row in diagnostic_rows(
                    d["db"],
                    "SELECT source,quality_status FROM source_batches WHERE target_date=?",
                    (d["date"],),
                ):
                    st.write(f"來源 {row['source']}：{row['quality_status']}")
        notifications = st.expander(
            "規則與通知（唯讀）", key="mine_rules", on_change="rerun"
        )
        if notifications.open:
            with notifications:
                st.dataframe(
                    pd.DataFrame(
                        diagnostic_rows(
                            d["db"],
                            "SELECT rule_id AS 規則,version AS 版本,effective_from AS 生效日,enabled AS 啟用 FROM rule_versions ORDER BY rule_id,effective_from",
                        )
                    ),
                    alt="既有規則版本與生效日",
                )
                st.write("全部通知（全庫統計）；accepted 不代表收件者已收到")
                for row in diagnostic_rows(
                    d["db"],
                    "SELECT state,COUNT(*) count FROM notification_outbox GROUP BY state",
                ):
                    st.write(f"狀態 {row['state']}：{row['count']} 筆")
        diagnostics = st.expander("技術診斷", key="mine_diagnostics", on_change="rerun")
        if diagnostics.open:
            with diagnostics:
                st.write("最近執行狀態")
                st.dataframe(
                    pd.DataFrame(d["runs"]).rename(
                        columns={"target_date": "交易日", "status": "狀態"}
                    ),
                    alt="最近十次執行狀態",
                )
                st.write("發布版本與原始發布時間")
                st.dataframe(
                    pd.DataFrame(
                        d["versions"], columns=["發布版本", "交易日", "原始發布時間"]
                    ),
                    alt="發布版本及原始時間",
                )
                st.write("伺服器端效能計時（秒；不含瀏覽器與網路）")
                for stage, value in st.session_state.get("perf_timings", {}).items():
                    st.write(
                        f"{ {'load': '讀取', 'compute': '計算', 'render': '元件建立'}.get(stage, stage) }：{value:.4f}"
                    )
                raw = st.toggle("顯示原始診斷 JSON")
                if raw:
                    st.json({"runs": d["runs"], "publications": d["versions"]})


def main():
    st.session_state.setdefault(
        "db_path", os.environ.get("DASHBOARD_DB", "data/trial-20261113-v2.5.sqlite")
    )
    st.set_page_config(page_title="分點日報", layout="centered")
    pages = [
        st.Page(today_page, title="今日", url_path="today", default=True),
        st.Page(ranking_page, title="排行", url_path="ranking"),
        st.Page(stock_page, title="個股", url_path="stock"),
        st.Page(mine_page, title="我的", url_path="mine"),
    ]
    st.session_state["stock_page"] = pages[2]
    selected = st.navigation(pages, position="top")
    # Native top navigation collapses into the drawer at phone widths (verified
    # at 390px). Keep all four destinations visible using native page links.
    st.html("""<style>
    .st-key-mobile_navigation a {min-height:44px; display:flex; align-items:center;}
    .st-key-dashboard_surface input:not([type="checkbox"]):not([type="radio"]),
    .st-key-dashboard_surface button,
    .st-key-dashboard_surface summary,
    .st-key-dashboard_surface a[href*="code="] {min-height:44px;}
    .st-key-dashboard_surface input {font-size:16px !important;}
    .st-key-dashboard_surface h3 {font-size:1.5rem;}
    @media (min-width:768px) {.st-key-mobile_navigation {display:none;}}
    </style>""")
    with st.container(key="mobile_navigation", horizontal=True):
        for page, label in zip(pages, ("今日", "排行", "個股", "我的")):
            params = (
                {"trade_date": st.query_params["trade_date"]}
                if "trade_date" in st.query_params
                else {}
            )
            if label == "個股" and st.query_params.get("code"):
                params["code"] = st.query_params["code"]
            st.page_link(page, label=label, query_params=params)
    if selected.title == "今日" and st.query_params.get("code"):
        # Preserve legacy /?code=0050 links without changing registered routes.
        st.switch_page(pages[2], query_params=dict(st.query_params))
    with st.container(key="dashboard_surface"):
        selected.run()


if __name__ == "__main__":
    main()
