"""通知的持久化。所有 SQL 都收在这个模块里，换存储只改这一层。

可靠性要点：每个连接 synchronous=FULL，事务 COMMIT 返回即已 fsync，
调用方只有在 insert_or_get 返回之后才能对外回"已受理"。
"""

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 2
BUSY_TIMEOUT_MS = 5000

STATUS_PENDING = "pending"
STATUS_DELIVERING = "delivering"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"

REASON_PERMANENT_ERROR = "permanent_error"
REASON_RETRIES_EXHAUSTED = "retries_exhausted"
REASON_EXPIRED = "expired"
REASON_BLOCKED_TARGET = "blocked_target"

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
    # 调度字段存整数毫秒（UTC epoch）：ISO 文本在微秒为 0 时省略小数部分，按字符串比较会排错序
    2: """
    ALTER TABLE notifications ADD COLUMN next_attempt_at INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE notifications ADD COLUMN lease_token TEXT;
    ALTER TABLE notifications ADD COLUMN lease_until INTEGER;
    ALTER TABLE notifications ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE notifications ADD COLUMN failure_reason TEXT;
    ALTER TABLE notifications ADD COLUMN finished_at TEXT;
    CREATE INDEX idx_notifications_due ON notifications (status, next_attempt_at);
    CREATE TABLE delivery_attempts (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        notification_id  TEXT NOT NULL REFERENCES notifications (id),
        attempt_no       INTEGER NOT NULL,
        started_at       TEXT NOT NULL,
        duration_ms      INTEGER NOT NULL,
        outcome          TEXT NOT NULL,
        status_code      INTEGER,
        error            TEXT,
        response_excerpt TEXT
    );
    CREATE INDEX idx_attempts_notification ON delivery_attempts (notification_id, id);
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
    next_attempt_at: int
    attempt_count: int
    failure_reason: str | None
    finished_at: datetime | None


@dataclass(frozen=True)
class Claimed:
    notification: Notification
    lease_token: str


@dataclass(frozen=True)
class Attempt:
    started_at: datetime
    duration_ms: int
    outcome: str
    status_code: int | None = None
    error: str | None = None
    response_excerpt: str | None = None


@dataclass(frozen=True)
class AttemptRecord:
    attempt_no: int
    started_at: datetime
    duration_ms: int
    outcome: str
    status_code: int | None
    error: str | None
    response_excerpt: str | None


@dataclass(frozen=True)
class InsertResult:
    id: str
    request_hash: str
    created: bool


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    # journal_mode=WAL 会持久化写在数据库文件里，由 init_schema 设置一次；这里只设置连接级参数
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return conn


def init_schema(db_path: Path) -> None:
    """首次启动建表；已有库则按版本号追加 DDL。版本号记在 PRAGMA user_version。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    conn.isolation_level = None  # 手动管理事务
    try:
        _enable_wal(conn)
        # API 和 worker 可能同时冷启动：先拿写锁，再在锁内读版本号，只补还没做的迁移
        conn.execute("BEGIN IMMEDIATE")
        try:
            current = conn.execute("PRAGMA user_version").fetchone()[0]
            for version in range(current + 1, SCHEMA_VERSION + 1):
                for stmt in _MIGRATIONS[version].split(";"):
                    if stmt.strip():
                        conn.execute(stmt)
                conn.execute(f"PRAGMA user_version={version}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.close()


def _enable_wal(conn: sqlite3.Connection) -> None:
    """切换日志模式时 SQLite 不调用 busy handler，并发冷启动会直接报 locked，所以手动重试。"""
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if mode != "wal":
                raise sqlite3.OperationalError(f"无法启用 WAL，当前日志模式: {mode}")
            return
        except sqlite3.OperationalError as e:
            if "locked" not in str(e) or time.monotonic() >= deadline:
                raise
            time.sleep(0.02)


def insert_or_get(conn: sqlite3.Connection, n: NewNotification) -> InsertResult:
    """插入新通知；(caller, idempotency_key) 已存在时返回已有记录，不新建。

    并发提交同一个 key 时由唯一约束裁决，不做应用层先查后写。
    """
    new_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO notifications
                    (id, caller, idempotency_key, request_hash, url, method,
                     headers_json, body, expires_at, status, created_at, next_attempt_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _to_text(now),
                    to_ms(now),
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
    return None if row is None else _row_to_notification(row)


def claim_due(conn: sqlite3.Connection, now_ms: int, limit: int, lease_ms: int) -> list[Claimed]:
    """取出到期的通知并占用。

    两类通知可被取出：到期的 pending；占用已过期的 delivering（持有者崩溃或卡住）。
    每行写入新的随机 lease_token，写回时凭它做 fencing。attempt_count 在占用时就加 1，
    这样请求发出后崩溃的那次尝试也被计入。
    """
    with conn:
        rows = conn.execute(
            """
            UPDATE notifications
               SET status = ?,
                   lease_token = lower(hex(randomblob(16))),
                   lease_until = ?,
                   attempt_count = attempt_count + 1
             WHERE id IN (
                   SELECT id FROM notifications
                    WHERE (status = ? AND next_attempt_at <= ?)
                       OR (status = ? AND lease_until <= ?)
                    ORDER BY next_attempt_at
                    LIMIT ?)
            RETURNING *
            """,
            (STATUS_DELIVERING, now_ms + lease_ms, STATUS_PENDING, now_ms, STATUS_DELIVERING, now_ms, limit),
        ).fetchall()
    return [Claimed(notification=_row_to_notification(r), lease_token=r["lease_token"]) for r in rows]


def mark_delivered(conn: sqlite3.Connection, claimed: Claimed, attempt: Attempt, now: datetime) -> bool:
    return _finish(
        conn, claimed, attempt,
        "status = ?, finished_at = ?", (STATUS_DELIVERED, _to_text(now)),
    )


def mark_failed(
    conn: sqlite3.Connection, claimed: Claimed, attempt: Attempt | None, reason: str, now: datetime
) -> bool:
    """attempt 为 None 表示没有发出请求（过期、目标被禁止），此时撤销占用时加的那次计数。"""
    uncount = ", attempt_count = attempt_count - 1" if attempt is None else ""
    return _finish(
        conn, claimed, attempt,
        "status = ?, failure_reason = ?, finished_at = ?" + uncount, (STATUS_FAILED, reason, _to_text(now)),
    )


def schedule_retry(conn: sqlite3.Connection, claimed: Claimed, attempt: Attempt, next_attempt_ms: int) -> bool:
    return _finish(
        conn, claimed, attempt,
        "status = ?, next_attempt_at = ?", (STATUS_PENDING, next_attempt_ms),
    )


def list_attempts(conn: sqlite3.Connection, notification_id: str) -> list[AttemptRecord]:
    rows = conn.execute(
        "SELECT * FROM delivery_attempts WHERE notification_id = ? ORDER BY id", (notification_id,)
    ).fetchall()
    return [
        AttemptRecord(
            attempt_no=r["attempt_no"],
            started_at=_from_text(r["started_at"]),
            duration_ms=r["duration_ms"],
            outcome=r["outcome"],
            status_code=r["status_code"],
            error=r["error"],
            response_excerpt=r["response_excerpt"],
        )
        for r in rows
    ]


def _finish(
    conn: sqlite3.Connection, claimed: Claimed, attempt: Attempt | None, set_sql: str, params: tuple
) -> bool:
    """在同一事务里写尝试记录并更新状态。返回 False 表示占用已丢失，状态更新被丢弃。

    尝试记录无论占用是否还在都会写入：请求确实发出过。
    """
    n = claimed.notification
    with conn:
        if attempt is not None:
            conn.execute(
                """
                INSERT INTO delivery_attempts
                    (notification_id, attempt_no, started_at, duration_ms, outcome, status_code, error, response_excerpt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    n.id,
                    n.attempt_count,
                    _to_text(attempt.started_at),
                    attempt.duration_ms,
                    attempt.outcome,
                    attempt.status_code,
                    attempt.error,
                    attempt.response_excerpt,
                ),
            )
        cur = conn.execute(
            f"UPDATE notifications SET {set_sql}, lease_token = NULL, lease_until = NULL "
            "WHERE id = ? AND lease_token = ?",
            (*params, n.id, claimed.lease_token),
        )
    return cur.rowcount == 1


def to_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _row_to_notification(row: sqlite3.Row) -> Notification:
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
        next_attempt_at=row["next_attempt_at"],
        attempt_count=row["attempt_count"],
        failure_reason=row["failure_reason"],
        finished_at=_from_text(row["finished_at"]),
    )


def _to_text(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def _from_text(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)
