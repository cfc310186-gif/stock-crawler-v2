"""Explicit outbox delivery. Importing this module never reads credentials."""

import argparse
import hashlib
import http.client
import importlib
import importlib.util
import json
import os
import sys
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.error import URLError

DEFAULT_HELPER_PATH = (
    "/home/hatch/workspace/custom-connectors/custom.line-messaging-api/bin/line_api.py"
)
_HELPER_CACHE = {}


def _load_helper(path):
    path = os.path.abspath(os.path.expanduser(path))
    if path in _HELPER_CACHE:
        return _HELPER_CACHE[path]
    try:
        name = "_line_helper_" + hashlib.sha256(path.encode()).hexdigest()[:12]
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise RuntimeError("missing loader")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, "call", None)):
            raise TypeError("missing call")
        try:
            credentials = importlib.import_module("dynamic_credentials")
        except (ModuleNotFoundError, ImportError):
            skill_bin = "/opt/muse/skills/custom-connector-setup/bin"
            if skill_bin not in sys.path:
                sys.path.insert(0, skill_bin)
            credentials = importlib.import_module("dynamic_credentials")
        error = credentials.DynamicCredentialError
        if not isinstance(error, type) or not issubclass(error, Exception):
            raise TypeError("invalid credential error")
        result = module.call, error
    except Exception as exc:
        raise RuntimeError("line helper unavailable") from exc
    _HELPER_CACHE[path] = result
    return result


class MessageTooLong(ValueError):
    pass


def check_length(text):
    if len(text.encode("utf-16-le")) // 2 > 5000:
        raise MessageTooLong("message exceeds 5000 UTF-16 code units")


def _now(now=None):
    if now is None:
        return datetime.now(UTC)
    if isinstance(now, str):
        now = datetime.fromisoformat(now)
    return now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)


@dataclass(frozen=True)
class Result:
    kind: str
    # Only a fixed, non-sensitive annotation, never a provider response body.
    note: str = ""


class Transport(Protocol):
    def push(self, *, retry_key: str, recipient_ref: str, text: str) -> Result: ...


class LineTransport:
    def __init__(self):
        from linebot.v3.messaging import ApiClient, Configuration, MessagingApi

        missing = [
            name
            for name in ("LINE_ACCESS_TOKEN", "LINE_USER_ID")
            if not os.environ.get(name)
        ]
        if missing:
            raise ValueError("missing environment variables: " + ", ".join(missing))
        configuration = Configuration(access_token=os.environ["LINE_ACCESS_TOKEN"])
        configuration.retries = 0
        self._client = ApiClient(configuration)
        self._api = MessagingApi(self._client)
        self._recipient = os.environ["LINE_USER_ID"]

    @classmethod
    def from_env(cls):
        return cls()

    def push(self, *, retry_key, recipient_ref, text):
        from linebot.v3.messaging import PushMessageRequest, TextMessage
        from linebot.v3.messaging.exceptions import ApiException
        from urllib3.exceptions import HTTPError, TimeoutError

        check_length(text)
        if recipient_ref != "line:primary":
            raise ValueError("unsupported recipient_ref")
        try:
            self._api.push_message(
                PushMessageRequest(
                    to=self._recipient, messages=[TextMessage(text=text)]
                ),
                x_line_retry_key=retry_key,
                _request_timeout=(5, 30),
            )
            return Result("accepted")
        except ApiException as exc:
            return classify_status(exc.status, exc.body)
        except (TimeoutError, HTTPError, OSError):
            return Result("unknown")
        except (ValueError, TypeError):
            # SDK validation/transport exceptions can contain recipient or credentials.
            # Keep the uncertain outcome conservative and never expose raw errors.
            return Result("unknown")


def _error_text(payload):
    if isinstance(payload, dict):
        payload = payload.get("error")
        if isinstance(payload, dict):
            payload = payload.get("message")
    return payload if isinstance(payload, str) else ""


class ConnectorTransport:
    def __init__(self, recipient=None, helper_path=None):
        self._recipient = (
            os.environ.get("LINE_USER_ID") if recipient is None else recipient
        )
        if not self._recipient:
            raise ValueError("missing environment variables: LINE_USER_ID")
        self._call, self._credential_error = _load_helper(
            DEFAULT_HELPER_PATH if helper_path is None else helper_path
        )

    def push(self, *, retry_key, recipient_ref, text):
        check_length(text)
        if recipient_ref != "line:primary":
            raise ValueError("unsupported recipient_ref")
        try:
            response = self._call(
                "POST",
                "/v2/bot/message/push",
                body={
                    "to": self._recipient,
                    "messages": [{"type": "text", "text": text}],
                },
                headers={"X-Line-Retry-Key": retry_key},
            )
        except (
            URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
            ValueError,
            TypeError,
            self._credential_error,
        ):
            return Result("unknown")
        if (
            not isinstance(response, tuple)
            or len(response) != 2
            or type(response[0]) is not int
            or not isinstance(response[1], dict)
        ):
            return Result("unknown")
        status, payload = response
        return classify_status(status, _error_text(payload))


def classify_status(status, body=None):
    if not status:
        return Result("unknown")
    if 200 <= status < 300:
        return Result("accepted")
    if status == 409:
        return Result("already_accepted", "already accepted by provider")
    if status == 429:
        # Heuristic only: monthly/quota wording needs verification against real API.
        body = str(body or "").lower()
        return Result(
            "quota_exhausted"
            if "monthly" in body or "quota" in body
            else "rate_limited"
        )
    if status >= 500:
        return Result("transient_error")
    if 400 <= status < 500:
        return Result("permanent_error")
    return Result("unknown")


class FakeTransport:
    """Offline scripted receiver; accepted keys suppress duplicate acceptance."""

    def __init__(self, outcomes=None):
        self._outcomes = iter(outcomes or [])
        self.requests = []
        self.accepted = {}
        self._lock = threading.Lock()

    def push(self, *, retry_key, recipient_ref, text):
        check_length(text)
        with self._lock:
            outcome = next(self._outcomes, "success")
            kinds = {
                "success": "accepted",
                "timeout_unknown": "unknown",
                "quota_429": "quota_exhausted",
                "rate_429": "rate_limited",
                "error_500": "transient_error",
                "conflict_409": "already_accepted",
                "error_400": "permanent_error",
            }
            result = Result(
                "already_accepted" if retry_key in self.accepted else kinds[outcome]
            )
            if outcome == "timeout_unknown" or result.kind in (
                "accepted",
                "already_accepted",
            ):
                self.accepted.setdefault(retry_key, (recipient_ref, text))
            self.requests.append(
                {
                    "retry_key": retry_key,
                    "recipient_ref": recipient_ref,
                    "text": text,
                    "timestamp": _now().isoformat(),
                    "result": result,
                }
            )
            return result


def make_transport(kind=None, recipient=None, outcomes=None):
    if kind is None or kind == "connector":
        return ConnectorTransport(recipient)
    if kind == "sdk":
        return LineTransport.from_env()
    if kind == "fake":
        return FakeTransport(outcomes)
    raise ValueError("unsupported transport")


def business_delivery_key(date, correction=False, recipient_ref="line:primary"):
    kind = "daily-correction" if correction else "daily"
    return f"{kind}|{date}|9A91|{recipient_ref}"


def queue_for_delivery(conn, delivery_key, now=None):
    now = _now(now).isoformat()
    with conn:
        conn.execute(
            """UPDATE notification_outbox SET state='queued',
            retry_key=COALESCE(retry_key,?),retry_key_created_at=COALESCE(retry_key_created_at,?),
            next_retry_at=? WHERE delivery_key=? AND state='draft'""",
            (str(uuid.uuid4()), now, now, delivery_key),
        )
    row = conn.execute(
        "SELECT retry_key FROM notification_outbox WHERE delivery_key=?",
        (delivery_key,),
    ).fetchone()
    return row[0] if row else None


def claim_next(conn, now=None):
    now = _now(now)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            """SELECT * FROM notification_outbox WHERE state IN ('queued','retrying')
            AND julianday(next_retry_at)<=julianday(?) ORDER BY rowid LIMIT 1""",
            (now.isoformat(),),
        )
        row = cursor.fetchone()
        if row is None:
            conn.commit()
            return None
        item = dict(zip([d[0] for d in cursor.description], row))
        deadline = (now + timedelta(seconds=300)).isoformat()
        conn.execute(
            "UPDATE notification_outbox SET state='sending',next_retry_at=? WHERE delivery_key=?",
            (deadline, item["delivery_key"]),
        )
        conn.commit()
        item.update(state="sending", next_retry_at=deadline)
        return item
    except Exception:
        conn.rollback()
        raise


def deliver_one(conn, transport, now=None):
    now = _now(now)
    row = claim_next(conn, now)
    if row is None:
        return None
    key = row["delivery_key"]
    created = row["retry_key_created_at"]
    if (
        not row["retry_key"]
        or not created
        or now - _now(created) >= timedelta(hours=24)
    ):
        with conn:
            conn.execute(
                "UPDATE notification_outbox SET state='stopped',next_retry_at=NULL WHERE delivery_key=?",
                (key,),
            )
        return key, "stopped"
    try:
        text = json.loads(row["draft_json"])["text"]
        check_length(text)
    except (MessageTooLong, ValueError, KeyError, TypeError):
        with conn:
            conn.execute(
                "UPDATE notification_outbox SET state='failed-permanent',next_retry_at=NULL WHERE delivery_key=?",
                (key,),
            )
        raise
    result = transport.push(
        retry_key=row["retry_key"], recipient_ref=row["recipient_ref"], text=text
    )
    attempts = row["attempts"] + 1
    sent_at, next_retry = None, None
    if result.kind in ("accepted", "already_accepted"):
        state, sent_at = "accepted", now.isoformat()
    elif result.kind in ("rate_limited", "transient_error"):
        state = "retrying" if attempts < 4 else "failed-permanent"
        if state == "retrying":
            next_retry = (
                now + timedelta(seconds=min(3600, 60 * 2 ** (attempts - 1)))
            ).isoformat()
    else:
        state = {
            "quota_exhausted": "stopped",
            "permanent_error": "failed-permanent",
            "unknown": "unknown",
        }.get(result.kind, "unknown")
    with conn:
        conn.execute(
            """UPDATE notification_outbox SET state=?,attempts=?,next_retry_at=?,sent_at=?
                     WHERE delivery_key=?""",
            (state, attempts, next_retry, sent_at, key),
        )
    return key, result.kind


def reclaim_stale(conn, now=None, lease_seconds=300):
    """next_retry_at already stores the lease deadline (lease_seconds is compatibility)."""
    with conn:
        cursor = conn.execute(
            """UPDATE notification_outbox SET state='retrying'
            WHERE state='sending' AND julianday(next_retry_at)<=julianday(?)""",
            (_now(now).isoformat(),),
        )
    return cursor.rowcount


def get_state(conn, delivery_key):
    row = conn.execute(
        "SELECT state FROM notification_outbox WHERE delivery_key=?", (delivery_key,)
    ).fetchone()
    return row[0] if row else None


def main(argv=None) -> int:
    from m1 import store

    parser = argparse.ArgumentParser(description="Deliver queued LINE notifications")
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--transport", choices=("connector", "sdk"), default="connector"
    )
    parser.add_argument("--max", type=int, default=5)
    parser.add_argument("--recipient")
    args = parser.parse_args(argv)
    conn = None
    try:
        if args.max < 0:
            raise ValueError("invalid maximum")
        conn = store.connect(args.db)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(notification_outbox)")
        }
        if not {"retry_key", "retry_key_created_at"} <= columns:
            raise ValueError("outbox migration required")
        transport = make_transport(args.transport, recipient=args.recipient)
        for _ in range(args.max):
            now = _now()
            row = conn.execute(
                """SELECT delivery_key FROM notification_outbox
                WHERE state IN ('queued','retrying')
                AND julianday(next_retry_at)<=julianday(?) ORDER BY rowid LIMIT 1""",
                (now.isoformat(),),
            ).fetchone()
            if row is None:
                break
            try:
                result = deliver_one(conn, transport, now)
            except Exception:  # noqa: BLE001 - inspect persisted state after any delivery failure
                result = row[0], get_state(conn, row[0])
            if result is not None:
                key, state = result
                print(f"{key} -> {state}")
        return 0
    except Exception:  # noqa: BLE001 - keep configuration and DB errors private
        print("delivery configuration or database unavailable", file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
