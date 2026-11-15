"""Private dashboard. Launch with --server.address 127.0.0.1."""

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pandas as pd
import streamlit as st

from m1 import store
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
            "sources": [
                dict(r)
                for r in c.execute(
                    "SELECT source,quality_status FROM source_batches WHERE target_date=?",
                    (date,),
                )
            ],
            "runs": [
                dict(r)
                for r in c.execute(
                    "SELECT target_date,status FROM runs ORDER BY rowid DESC LIMIT 10"
                )
            ],
            "rules": [
                dict(r)
                for r in c.execute(
                    "SELECT rule_id,version,effective_from,enabled FROM rule_versions ORDER BY rule_id,effective_from"
                )
            ],
            "notifications": [
                dict(r)
                for r in c.execute(
                    "SELECT state,COUNT(*) count FROM notification_outbox GROUP BY state"
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


def context(show_date=True):
    db = st.session_state["db_path"]
    with measure(st.session_state, "load"):
        versions = publication_keys(db)
        if not versions:
            st.info("尚無已發布資料")
            st.stop()
        dates = [p[1] for p in versions]
        initial = st.query_params.get("trade_date", dates[0])
        date = initial if initial in dates else dates[0]
        if show_date:
            date = st.selectbox("交易日", dates, index=dates.index(date))
        data = load(db, versions, date)
        data.update(db=db, versions=versions, date=date, watch=watchlist(db))
        c = store.connect(db, readonly=True)
        try:
            # Failed runs need to be visible even when publications did not change.
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
    return data


def fmt(n):
    return "—（缺張數）" if n is None else f"{number(n):,}"


def chips():
    def choose(kind, label, options, key, default, **kw):
        previous = st.session_state.get(key + "_selected", default)

        def retain_selection():
            if st.session_state.get(key) is None:
                st.session_state[key] = st.session_state.get(key + "_selected", default)

        value = kind(
            label, options, default=previous, key=key, on_change=retain_selection, **kw
        )
        if value is None:
            value = previous
        st.session_state[key + "_selected"] = value
        return value

    side = choose(st.segmented_control, "方向", ["買超", "賣超"], "direction", "買超")
    days = choose(
        st.pills,
        "期間",
        [3, 5, 10, 20],
        "period",
        3,
        format_func=lambda n: f"近 {n} 日",
    )
    minimum = choose(
        st.pills,
        "金額門檻（千元）",
        [0, 1000, 5000, 10000],
        "minimum",
        0,
        format_func=lambda n: f"{n:,}",
    )
    with st.expander("進階篩選"):
        custom = st.checkbox("使用自訂門檻")
        value = st.number_input("自訂金額（千元）", min_value=0, value=0)
    return side, days, value if custom else minimum


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
    except (ValueError, sqlite3.Error) as exc:
        st.error(f"儲存失敗：{exc}")


def today_page():
    d = context()
    with measure(st.session_state, "compute"):
        today = [r for r in d["history"] if r["trade_date"] == d["date"]]
        partial = any(r["close"] is None or r["net_lots"] is None for r in today)
        pub = next(p for p in d["versions"] if p[1] == d["date"])
    with measure(st.session_state, "render"):
        st.write("資料狀態：", "部分" if partial else "已發布", "發布時間：", pub[2])
        latest = d["runs"][0] if d["runs"] else {}
        st.write("最近執行狀態", latest)
        if latest.get("status") not in (None, "success"):
            st.error("最近執行失敗；目前顯示最後已發布資料")
        st.write("通知：僅草稿，未發送")
        st.subheader("今日觸發告警")
        for e in d["events"]:
            with st.container(border=True):
                st.write(e["rule_id"], e["code"], e["reason"])
        if not d["events"]:
            st.write("（無）")
        for rid, label in [("R4", "買超"), ("R5", "賣超")]:
            groups = sorted(
                {
                    e["inputs"]["n_days_present"]
                    for e in d["events"]
                    if e["rule_id"] == rid
                },
                reverse=True,
            )
            for n in groups:
                st.subheader(f"{label}・覆蓋{n}天")
                for e in d["events"]:
                    if e["rule_id"] == rid and e["inputs"]["n_days_present"] == n:
                        st.write(
                            e["code"], f"{number(e['inputs']['net_amount_k']):,}千元"
                        )
        st.subheader("Watchlist 今日動態")
        active = {e["code"] for e in d["events"]}
        inactive = 0
        for w in d["watch"]:
            if w["code"] in active:
                with st.container(border=True):
                    st.write(
                        w["code"],
                        w["name"],
                        [e["rule_id"] for e in d["events"] if e["code"] == w["code"]],
                    )
            else:
                inactive += 1
        st.write(f"{inactive} 檔今日無動態")
        st.caption(LIMITATION)
    with st.expander("伺服器端效能計時"):
        st.write(st.session_state.get("perf_timings", {}))


@st.cache_data
def rank_data(db, versions, history, window, direction, minimum, coverage, page, limit):
    groups = grouped_ranking(history, window, direction)
    rows = [r for r in groups.get(coverage, []) if abs(r["net_amount_k"]) >= minimum]
    return rows[page * 20 : page * 20 + limit], len(rows)


@st.fragment
def ranking_cards(d, side, days, minimum):
    window = sorted({r["trade_date"] for r in d["history"]})[-days:]
    with measure(st.session_state, "compute"):
        groups = grouped_ranking(
            d["history"], window, "buy" if side == "買超" else "sell"
        )
    if not groups:
        st.info("無符合資料")
        return
    signature = (d["versions"], d["date"], side, days, minimum)
    if st.session_state.get("rank_signature") != signature:
        st.session_state.update(rank_signature=signature, rank_page=0, rank_limit=20)
        st.session_state.pop("coverage", None)
    options = list(groups)
    coverage = st.pills(
        "覆蓋天數組別",
        options,
        default=options[0],
        key="coverage",
        format_func=lambda n: f"{n}天組",
    )
    coverage = coverage or st.session_state.get("coverage_selected", options[0])
    if coverage not in options:
        coverage = options[0]
    if st.session_state.get("coverage_selected") != coverage:
        st.session_state.update(rank_page=0, rank_limit=20)
    st.session_state["coverage_selected"] = coverage
    page, limit = st.session_state["rank_page"], st.session_state["rank_limit"]
    rows, total = rank_data(
        d["db"],
        d["versions"],
        d["history"],
        window,
        "buy" if side == "買超" else "sell",
        minimum,
        coverage,
        page,
        limit,
    )
    with measure(st.session_state, "render"):
        st.write(f"第 {page + 1} 頁，共 {total} 檔")
        for row in rows:
            code = row["code"]
            name = next(
                (
                    r.get("name", "")
                    for r in reversed(d["history"])
                    if r["code"] == code
                ),
                "",
            )
            with st.container(border=True):
                st.write(name, code)
                st.write(
                    f"實際淨張數 {fmt(row['cum_net_lots'])}；缺張數 {row['n_days_missing_lots']} 日"
                )
                st.write(
                    f"淨金額 {row['net_amount_k']:,}千元｜出現／覆蓋 {coverage} 天"
                )
                st.write(
                    "觸發規則：",
                    " ".join(
                        e["rule_id"]
                        for e in d["events"]
                        if e["code"] == code and e["rule_id"] in {"R4", "R5"}
                    )
                    or "無",
                )
                if st.button("查看個股", key=f"view_{code}"):
                    st.query_params.update(code=code, trade_date=d["date"])
                    st.session_state.pop("stock_search", None)
                    st.switch_page(
                        st.session_state["stock_page"],
                        query_params={"code": code, "trade_date": d["date"]},
                    )
        if st.button("載入更多", disabled=limit >= 60 or page * 20 + limit >= total):
            st.session_state["rank_limit"] = min(60, limit + 20)
            st.rerun(scope="fragment")
        if total > 60:
            st.caption("每次累計顯示上限 60 檔；其餘請使用下一頁")
        prev, nxt = st.columns(2)
        if prev.button("上一頁", disabled=page == 0):
            st.session_state.update(rank_page=page - 1, rank_limit=20)
            st.rerun(scope="fragment")
        if nxt.button("下一頁", disabled=(page + 1) * 20 >= total):
            st.session_state.update(rank_page=page + 1, rank_limit=20)
            st.rerun(scope="fragment")


def ranking_page():
    d = context()
    side, days, minimum = chips()
    st.caption(LIMITATION)
    ranking_cards(d, side, days, minimum)


def stock_page():
    query = st.text_input(
        "代號／名稱搜尋", value=st.query_params.get("code", ""), key="stock_search"
    )
    d = context(show_date=False)
    options = sorted(
        {
            r["code"] + " " + r.get("name", "")
            for r in d["history"]
            if query.lower() in (r["code"] + r.get("name", "")).lower()
        }
    )[:10]
    if not options:
        st.info("無符合個股")
        return
    code = st.selectbox("建議（最多 10 筆）", options).split()[0]
    _side, days, _minimum = chips()
    window = sorted({r["trade_date"] for r in d["history"]})[-days:]
    with measure(st.session_state, "compute"):
        rows = [
            r for r in d["history"] if r["code"] == code and r["trade_date"] in window
        ]
        summary = _lots_summary(d["history"], code, window)
    with measure(st.session_state, "render"):
        st.metric(
            f"區間實際淨張數（缺 {summary['n_days_missing_lots']} 日）",
            fmt(summary["cum_net_lots"]),
        )
        estimates = [
            number(r["net_amount_ntd"]) / number(r["close"]) / 1000
            for r in rows
            if number(r["net_amount_ntd"]) is not None
            and number(r["close"])
            and number(r["close"]) > 0
        ]
        st.metric("估算淨張數（非實際）", fmt(sum(estimates) if estimates else None))
        st.metric(
            "最近收盤價（未還原）",
            rows[-1]["close"] if rows and rows[-1]["close"] is not None else "不可用",
        )
        st.write("成本：不可用（無法驗證）")
        present = next((w for w in d["watch"] if w["code"] == code), None)
        categories = sorted({w["category"] for w in d["watch"]})
        category = st.session_state.get("last_category") or (
            categories[0] if categories else "未分類"
        )
        if st.button("⭐ 移除 Watchlist" if present else "⭐ 加入 Watchlist"):
            edit(
                d["db"],
                code,
                next((r.get("name", "") for r in rows), ""),
                present["category"] if present else category,
                bool(present),
            )
        st.subheader("觸發規則與理由")
        for e in d["events"]:
            if e["code"] == code:
                st.write(e["rule_id"], e["reason"])
        if rows:
            frame = pd.DataFrame(
                [
                    {
                        "日期": r["trade_date"],
                        "每日實際淨張數": float(r["net_lots"])
                        if r["net_lots"] is not None
                        else None,
                    }
                    for r in rows
                ]
            ).set_index("日期")
            st.line_chart(frame, height=300, width="stretch")
        with st.expander("詳細數據"):
            st.dataframe(rows)
        st.caption(LIMITATION)


def mine_page():
    d = context()
    with measure(st.session_state, "compute"):
        categories = sorted({w["category"] for w in d["watch"]})
    with measure(st.session_state, "render"):
        st.subheader(
            f"Watchlist：{len(d['watch'])} 檔／{len({w['category'] for w in d['watch']})} 分類"
        )
        for category in categories:
            st.subheader(category)
            for w in d["watch"]:
                if w["category"] != category:
                    continue
                with st.container(border=True):
                    st.write(w["code"], w["name"])
                    new_category = st.text_input(
                        "分類", w["category"], key=f"category_{w['code']}"
                    )
                    if st.button("更改分類", key=f"save_{w['code']}"):
                        edit(d["db"], w["code"], w["name"], new_category)
                    if st.button("移除", key=f"remove_{w['code']}"):
                        edit(d["db"], w["code"], w["name"], w["category"], True)
        st.subheader("資料健康")
        from datetime import date

        st.write("今日是否發布", any(p[1] == str(date.today()) for p in d["versions"]))
        st.write(
            "最後成功日",
            d["last_success"] or "無",
        )
        st.write("來源狀態", d["sources"])
        st.subheader("規則與通知（唯讀）")
        st.dataframe(d["rules"])
        st.write("通知：僅草稿，未發送", d["notifications"])


def main():
    # 公開版：允許任何綁定位址（Streamlit Cloud 綁 0.0.0.0）。
    # 私人版 m1/dashboard.py 仍保留 localhost 檢查，請勿把此檔複製回私人環境使用。
    st.sidebar.text_input("資料庫", "data/trial-20261113-v2.5.sqlite", key="db_path")
    has_code = bool(st.query_params.get("code"))
    pages = [
        st.Page(today_page, title="今日", url_path="today", default=not has_code),
        st.Page(ranking_page, title="排行", url_path="ranking"),
        st.Page(stock_page, title="個股", url_path="stock", default=has_code),
        st.Page(mine_page, title="我的", url_path="mine"),
    ]
    st.session_state["stock_page"] = pages[2]
    st.navigation(pages, position="top").run()


if __name__ == "__main__":
    main()
