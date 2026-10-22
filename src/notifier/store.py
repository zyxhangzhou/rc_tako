"""通知的持久化。所有 SQL 都收在这个模块里，换存储只改这一层。

可靠性要点：每个连接 synchronous=FULL，事务 COMMIT 返回即已 fsync，
调用方只有在 insert_or_get 返回之后才能对外回"已受理"。
"""

import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 5000

STATUS_PENDING = "pending"
STATUS_DELIVERING = "delivering"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"

REASON_PERMANENT_ERROR = "permanent_error"
REASON_RETRIES_EXHAUSTED = "retries_exhausted"
REASON_EXPIRED = "expired"
REASON_BLOCKED_TARGET = "blocked_target"

def _migrate_v3(conn: sqlite3.Connection) -> None:
    conn.execute("ALTER TABLE notifications ADD COLUMN target_host TEXT NOT NULL DEFAULT ''")
    conn.execute("ALTER TABLE notifications ADD COLUMN retry_window_start INTEGER NOT NULL DEFAULT 0")
    rows = conn.execute("SELECT id, url, created_at FROM notifications").fetchall()
    for r in rows:
        conn.execute(
            "UPDATE notifications SET target_host = ?, retry_window_start = ? WHERE id = ?",
            (target_host(r["url"]), to_ms(_from_text(r["created_at"])), r["id"]),
        )
    conn.execute("CREATE INDEX idx_notifications_host ON notifications (target_host, status)")


_MIGRATIONS: dict[int, "str | Callable[[sqlite3.Connection], None]"] = {
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
    # 按 host 限流的分组键；重放时重置的重试窗口起点（毫秒）
    3: _migrate_v3,
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
    target_host: str
    retry_window_start: int


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
                migration = _MIGRATIONS[version]
                if callable(migration):
                    migration(conn)
                else:
                    for stmt in migration.split(";"):
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
                     headers_json, body, expires_at, status, created_at, next_attempt_at,
                     target_host, retry_window_start)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    target_host(n.url),
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


def claim_due(
    conn: sqlite3.Connection, now_ms: int, limit: int, lease_ms: int, per_host_limit: int | None = None
) -> list[Claimed]:
    """取出到期的通知并占用。

    两类通知可被取出：到期的 pending；占用已过期的 delivering（持有者崩溃或卡住）。
    每行写入新的随机 lease_token，写回时凭它做 fencing。attempt_count 在占用时就加 1，
    这样请求发出后崩溃的那次尝试也被计入。

    per_host_limit：同一 target_host 同时持有有效占用的通知数上限。已满的 host 本轮跳过，
    其余 host 照常取（按目标隔离，见 isolation-ops design D1）。None 表示不限。
    """
    cap = per_host_limit if per_host_limit is not None else limit
    with conn:
        rows = conn.execute(
            """
            WITH inflight AS (
                SELECT target_host, count(*) AS n
                  FROM notifications
                 WHERE status = :delivering AND lease_until > :now
                 GROUP BY target_host
            ),
            due AS (
                SELECT id, target_host, next_attempt_at,
                       ROW_NUMBER() OVER (PARTITION BY target_host ORDER BY next_attempt_at, id) AS rn
                  FROM notifications
                 WHERE (status = :pending AND next_attempt_at <= :now)
                    OR (status = :delivering AND lease_until <= :now)
            )
            UPDATE notifications
               SET status = :delivering,
                   lease_token = lower(hex(randomblob(16))),
                   lease_until = :lease_until,
                   attempt_count = attempt_count + 1
             WHERE id IN (
                   SELECT due.id
                     FROM due LEFT JOIN inflight ON inflight.target_host = due.target_host
                    WHERE due.rn <= :cap - coalesce(inflight.n, 0)
                    ORDER BY due.next_attempt_at
                    LIMIT :limit)
            RETURNING *
            """,
            {
                "delivering": STATUS_DELIVERING,
                "pending": STATUS_PENDING,
                "now": now_ms,
                "lease_until": now_ms + lease_ms,
                "cap": cap,
                "limit": limit,
            },
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


def target_host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


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
        target_host=row["target_host"],
        retry_window_start=row["retry_window_start"],
        attempt_count=row["attempt_count"],
        failure_reason=row["failure_reason"],
        finished_at=_from_text(row["finished_at"]),
    )


def _to_text(dt: datetime | None) -> str | None:
    return None if dt is None else dt.astimezone(timezone.utc).isoformat()


def _from_text(s: str | None) -> datetime | None:
    return None if s is None else datetime.fromisoformat(s)


# --- 运维：按调用方读取、重放、指标（isolation-ops）---

REPLAYED = "replayed"
NOT_FOUND = "not_found"
NOT_REPLAYABLE = "not_replayable"


def get_for_caller(conn: sqlite3.Connection, notification_id: str, caller: str) -> Notification | None:
    """他人的通知与不存在的通知一样返回 None，不暴露是否存在。"""
    row = conn.execute(
        "SELECT * FROM notifications WHERE id = ? AND caller = ?", (notification_id, caller)
    ).fetchone()
    return None if row is None else _row_to_notification(row)


def replay(conn: sqlite3.Connection, notification_id: str, caller: str, now_ms: int) -> str:
    """把失败终态的通知改回待投递，并从现在起重新计算重试窗口。

    检查和修改在同一个写事务里完成（BEGIN IMMEDIATE），并发重放至多一个成功。
    通知 ID 和 attempt_count 沿用，下游可以按 ID 去重。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT status, failure_reason, expires_at FROM notifications WHERE id = ? AND caller = ?",
            (notification_id, caller),
        ).fetchone()
        if row is None:
            conn.rollback()
            return NOT_FOUND
        expires_at = _from_text(row["expires_at"])
        if (
            row["status"] != STATUS_FAILED
            or row["failure_reason"] == REASON_EXPIRED
            or (expires_at is not None and to_ms(expires_at) <= now_ms)
        ):
            conn.rollback()
            return NOT_REPLAYABLE
        conn.execute(
            """
            UPDATE notifications
               SET status = ?, failure_reason = NULL, finished_at = NULL,
                   next_attempt_at = ?, retry_window_start = ?
             WHERE id = ? AND status = ?
            """,
            (STATUS_PENDING, now_ms, now_ms, notification_id, STATUS_FAILED),
        )
        conn.commit()
        return REPLAYED
    except BaseException:
        conn.rollback()
        raise


@dataclass(frozen=True)
class Stats:
    by_status: dict[str, int]
    failed_by_reason: dict[str, int]
    backlog_due: int
    oldest_due_age_s: float
    attempts_by_outcome: dict[str, int]


def stats(conn: sqlite3.Connection, now_ms: int) -> Stats:
    by_status = dict(conn.execute("SELECT status, count(*) FROM notifications GROUP BY status").fetchall())
    failed_by_reason = dict(
        conn.execute(
            "SELECT failure_reason, count(*) FROM notifications WHERE status = ? GROUP BY failure_reason",
            (STATUS_FAILED,),
        ).fetchall()
    )
    backlog, oldest = conn.execute(
        "SELECT count(*), min(next_attempt_at) FROM notifications WHERE status = ? AND next_attempt_at <= ?",
        (STATUS_PENDING, now_ms),
    ).fetchone()
    attempts = dict(conn.execute("SELECT outcome, count(*) FROM delivery_attempts GROUP BY outcome").fetchall())
    return Stats(
        by_status=by_status,
        failed_by_reason=failed_by_reason,
        backlog_due=backlog,
        oldest_due_age_s=0.0 if oldest is None else max(0.0, (now_ms - oldest) / 1000),
        attempts_by_outcome=attempts,
    )
