"""通知的持久化。所有 SQL 都收在这个模块里，换存储只改这一层。

可靠性要点：每个连接 synchronous=FULL，事务 COMMIT 返回即已 fsync，
调用方只有在 insert_or_get 返回之后才能对外回"已受理"。
"""

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 5000

STATUS_PENDING = "pending"

_MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE notifications (
        id              TEXT PRIMARY KEY,
        caller          TEXT NOT NULL,
        idempotency_key TEXT NOT NULL,
        request_hash    TEXT NOT NULL,
        url             TEXT NOT NULL,
        method          TEXT NOT NULL,
        headers_json    TEXT NOT NULL,
        body            TEXT,
        expires_at      TEXT,
        status          TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        UNIQUE (caller, idempotency_key)
    );
    """,
}


@dataclass(frozen=True)
class NewNotification:
    caller: str
    idempotency_key: str
    request_hash: str
    url: str
    method: str
    headers: dict[str, str]
    body: str | None
    expires_at: datetime | None


@dataclass(frozen=True)
class Notification:
    id: str
    caller: str
    idempotency_key: str
    request_hash: str
    url: str
    method: str
    headers: dict[str, str]
    body: str | None
    expires_at: datetime | None
    status: str
    created_at: datetime


@dataclass(frozen=True)
class InsertResult:
    id: str
    request_hash: str
    created: bool


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def init_schema(db_path: Path) -> None:
    """首次启动建表；已有库则按版本号追加 DDL。版本号记在 PRAGMA user_version。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for version in range(current + 1, SCHEMA_VERSION + 1):
            with conn:
                conn.executescript(f"BEGIN; {_MIGRATIONS[version]} PRAGMA user_version={version}; COMMIT;")
    finally:
        conn.close()


def insert_or_get(conn: sqlite3.Connection, n: NewNotification) -> InsertResult:
    """插入新通知；(caller, idempotency_key) 已存在时返回已有记录，不新建。

    并发提交同一个 key 时由唯一约束裁决，不做应用层先查后写。
    """
    new_id = str(uuid.uuid4())
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO notifications
                    (id, caller, idempotency_key, request_hash, url, method,
                     headers_json, body, expires_at, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id,
                    n.caller,
                    n.idempotency_key,
                    n.request_hash,
                    n.url,
                    n.method,
                    json.dumps(n.headers, ensure_ascii=False),
                    n.body,
                    _to_text(n.expires_at),
                    STATUS_PENDING,
                    _to_text(datetime.now(timezone.utc)),
                ),
            )
        return InsertResult(id=new_id, request_hash=n.request_hash, created=True)
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT id, request_hash FROM notifications WHERE caller = ? AND idempotency_key = ?",
            (n.caller, n.idempotency_key),
        ).fetchone()
        if row is None:
            # 不是幂等冲突，而是别的约束失败，原样抛出
            raise
        return InsertResult(id=row["id"], request_hash=row["request_hash"], created=False)


def get(conn: sqlite3.Connection, notification_id: str) -> Notification | None:
    row = conn.execute("SELECT * FROM notifications WHERE id = ?", (notification_id,)).fetchone()
    if row is None:
        return None
    return Notification(
        id=row["id"],
        caller=row["caller"],
        idempotency_key=row["idempotency_key"],
        request_hash=row["request_hash"],
        url=row["url"],
        method=row["method"],
        headers=json.loads(row["headers_json"]),
        body=row["body"],
        expires_at=_from_text(row["expires_at"]),
        status=row["status"],
        created_at=_from_text(row["created_at"]),
    )


def _to_text(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def _from_text(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)
