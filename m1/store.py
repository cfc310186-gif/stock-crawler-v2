import json
import sqlite3
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_metadata(key TEXT PRIMARY KEY,value TEXT);
INSERT OR IGNORE INTO schema_metadata VALUES('schema_version','2');
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,target_date TEXT,status TEXT,report TEXT);
CREATE TABLE IF NOT EXISTS source_batches(id TEXT PRIMARY KEY,run_id TEXT,source TEXT,target_date TEXT,payload_hash TEXT,snapshot_ref TEXT,row_count INTEGER,quality_status TEXT);
CREATE TABLE IF NOT EXISTS publications(id TEXT PRIMARY KEY,trade_date TEXT,scope TEXT,revision INTEGER,current INTEGER,published_at TEXT,content_hash TEXT,UNIQUE(trade_date,scope,revision));
CREATE UNIQUE INDEX IF NOT EXISTS current_publication ON publications(trade_date,scope) WHERE current=1;
CREATE TABLE IF NOT EXISTS publication_context(publication_id TEXT PRIMARY KEY,watchlist_json TEXT,rules_json TEXT);
CREATE TABLE IF NOT EXISTS branch_flows(publication_id TEXT,code TEXT,name TEXT,branch TEXT DEFAULT '9A91',buy_lots TEXT,sell_lots TEXT,net_lots TEXT,net_amount_ntd INTEGER,raw_json TEXT,PRIMARY KEY(publication_id,code));
CREATE TABLE IF NOT EXISTS daily_prices(publication_id TEXT,code TEXT,close TEXT,volume_shares TEXT,price_basis TEXT,PRIMARY KEY(publication_id,code));
CREATE TABLE IF NOT EXISTS daily_metrics(publication_id TEXT,code TEXT,metrics_json TEXT,PRIMARY KEY(publication_id,code));
CREATE TABLE IF NOT EXISTS watchlist_entries(code TEXT PRIMARY KEY,name TEXT,category TEXT);
CREATE TABLE IF NOT EXISTS rule_versions(rule_id TEXT,version TEXT,definition TEXT,definition_hash TEXT,effective_from TEXT,enabled INTEGER,PRIMARY KEY(rule_id,version));
CREATE TABLE IF NOT EXISTS alert_events(id TEXT PRIMARY KEY,publication_id TEXT,rule_id TEXT,code TEXT,event_json TEXT,UNIQUE(publication_id,rule_id,code));
CREATE TABLE IF NOT EXISTS notification_outbox(
notification_id TEXT PRIMARY KEY,delivery_key TEXT UNIQUE,publication_id TEXT,
channel TEXT,recipient_ref TEXT,event_ids TEXT,template_version TEXT,
state TEXT CHECK(state IN ('draft','queued','sending','retrying','accepted','failed-permanent','stopped','unknown')),
attempts INTEGER DEFAULT 0,next_retry_at TEXT,provider_request_id TEXT,sent_at TEXT,
draft_json TEXT,retry_key TEXT,retry_key_created_at TEXT);
CREATE INDEX IF NOT EXISTS outbox_ready ON notification_outbox(state,next_retry_at);
"""


def connect(path, readonly=False):
    if readonly:
        conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
    else:
        p = Path(path).resolve()
        if p.name == "stock-crawler-v2-m0-isolated.sqlite" or "m0" in p.parts:
            raise ValueError("protected path")
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(p, timeout=10)
        existing = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='notification_outbox'"
        ).fetchone()
        if not existing:
            conn.executescript(SCHEMA)
    conn.row_factory = sqlite3.Row
    return conn


def seed(conn, source="data/stock-crawler-v2-m0-isolated.sqlite"):
    if conn.execute(
        "SELECT 1 FROM schema_metadata WHERE key='watchlist_seeded'"
    ).fetchone():
        return
    src = sqlite3.connect(f"file:{Path(source).resolve()}?mode=ro", uri=True)
    try:
        rows = src.execute("SELECT id,name,category FROM watchlist").fetchall()
    finally:
        src.close()
    if len(rows) != 35 or len({r[2] for r in rows}) != 5:
        raise ValueError("unexpected watchlist seed")
    with conn:
        conn.executemany("INSERT OR IGNORE INTO watchlist_entries VALUES(?,?,?)", rows)
        conn.execute(
            "INSERT INTO schema_metadata VALUES('watchlist_seeded','m0-ro-35-5')"
        )


def edit_watchlist(conn, code, name="", category="", remove=False):
    if not code or not code.isalnum() or not 4 <= len(code) <= 6:
        raise ValueError("invalid stock code")
    with conn:
        if remove:
            conn.execute("DELETE FROM watchlist_entries WHERE code=?", (code,))
        else:
            if not category.strip():
                raise ValueError("category required")
            conn.execute(
                "INSERT INTO watchlist_entries VALUES(?,?,?) ON CONFLICT(code) DO UPDATE SET name=excluded.name,category=excluded.category",
                (code, name, category),
            )


def history(conn, date):
    return [
        dict(r)
        for r in conn.execute(
            """SELECT p.trade_date,p.id publication_id,f.*,d.close,d.volume_shares FROM publications p JOIN branch_flows f ON f.publication_id=p.id LEFT JOIN daily_prices d ON d.publication_id=p.id AND d.code=f.code WHERE p.current=1 AND p.trade_date<=? ORDER BY p.trade_date,f.code""",
            (date,),
        )
    ]


def migrate_outbox(conn):
    """Explicit, atomic v1 migration; never invoked by connect()."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(notification_outbox)")}
        if "notification_id" in columns:
            conn.commit()
            return
        rows = conn.execute(
            "SELECT delivery_key,publication_id,state,draft_json FROM notification_outbox"
        ).fetchall()
        conn.execute("ALTER TABLE notification_outbox RENAME TO outbox_v1")
        for statement in SCHEMA.split(";"):
            if (
                "CREATE TABLE IF NOT EXISTS notification_outbox" in statement
                or "CREATE INDEX IF NOT EXISTS outbox_ready" in statement
            ):
                conn.execute(statement)
        for key, publication, state, draft_json in rows:
            draft = json.loads(draft_json)
            ids = [e["id"] for e in draft.get("events", []) if "id" in e]
            conn.execute(
                """INSERT INTO notification_outbox
                (notification_id,delivery_key,publication_id,channel,recipient_ref,
                 event_ids,template_version,state,attempts,draft_json)
                VALUES(?,?,?,'line','line:primary',?,'v2.5',?,0,?)""",
                (
                    uuid.uuid4().hex,
                    key,
                    publication,
                    json.dumps(ids),
                    state,
                    draft_json,
                ),
            )
        conn.execute("DROP TABLE outbox_v1")
        conn.execute("UPDATE schema_metadata SET value='2' WHERE key='schema_version'")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
