"""schema v2：迁移、占用、fencing 写回、尝试记录。"""

import sqlite3
from datetime import datetime, timezone

import pytest

from notifier import store

LEASE = 60_000


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "n.db"
    store.init_schema(path)
    c = store.connect(path)
    yield c
    c.close()


def _insert(conn, key="evt-1", **kw):
    fields = dict(
        caller="crm", idempotency_key=key, request_hash="h", url="https://api.example.com/hook",
        method="POST", headers={}, body="{}", expires_at=None,
    )
    fields.update(kw)
    return store.insert_or_get(conn, store.NewNotification(**fields)).id


def _attempt(outcome="temporary", code=500):
    return store.Attempt(started_at=datetime.now(timezone.utc), duration_ms=5, outcome=outcome, status_code=code)


def _now(conn, nid):
    return store.get(conn, nid).next_attempt_at


# --- 迁移 ---

V1_DDL = store._MIGRATIONS[1]


def test_upgrade_from_v1_keeps_data_and_makes_pending_due(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(V1_DDL + "PRAGMA user_version=1;")
    old.execute(
        "INSERT INTO notifications VALUES ('n1','crm','k1','h','https://a.example/x','POST','{\"A\":\"1\"}','body',NULL,'pending','2026-10-22T10:00:00+00:00')"
    )
    old.commit()
    old.close()

    store.init_schema(path)
    c = store.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    n = store.get(c, "n1")
    assert (n.url, n.headers, n.body, n.status, n.attempt_count) == ("https://a.example/x", {"A": "1"}, "body", "pending", 0)
    claimed = store.claim_due(c, now_ms=store.to_ms(datetime.now(timezone.utc)), limit=10, lease_ms=LEASE)
    assert [cl.notification.id for cl in claimed] == ["n1"]


def test_new_notification_due_at_acceptance_time(conn):
    nid = _insert(conn)
    n = store.get(conn, nid)
    assert abs(n.next_attempt_at - store.to_ms(n.created_at)) <= 1


# --- 占用 ---

def test_claim_takes_due_marks_delivering_and_counts_attempt(conn):
    nid = _insert(conn)
    [c] = store.claim_due(conn, now_ms=_now(conn, nid), limit=10, lease_ms=LEASE)
    assert c.notification.id == nid and c.notification.status == store.STATUS_DELIVERING
    assert c.notification.attempt_count == 1
    assert len(c.lease_token) == 32


def test_not_yet_due_not_claimed(conn):
    nid = _insert(conn)
    assert store.claim_due(conn, now_ms=_now(conn, nid) - 1, limit=10, lease_ms=LEASE) == []


def test_held_lease_not_reclaimed_until_expiry(conn):
    nid = _insert(conn)
    t = _now(conn, nid)
    [first] = store.claim_due(conn, now_ms=t, limit=10, lease_ms=LEASE)
    # 同一时刻只能有一个有效占用
    assert store.claim_due(conn, now_ms=t + LEASE - 1, limit=10, lease_ms=LEASE) == []
    [second] = store.claim_due(conn, now_ms=t + LEASE, limit=10, lease_ms=LEASE)
    assert second.notification.id == nid
    assert second.lease_token != first.lease_token
    assert second.notification.attempt_count == 2


def test_terminal_not_claimed(conn):
    a, b = _insert(conn, "a"), _insert(conn, "b")
    t = max(_now(conn, a), _now(conn, b))
    ca, cb = store.claim_due(conn, now_ms=t, limit=10, lease_ms=LEASE)
    now = datetime.now(timezone.utc)
    assert store.mark_delivered(conn, ca, _attempt("success", 200), now)
    assert store.mark_failed(conn, cb, _attempt("permanent", 400), store.REASON_PERMANENT_ERROR, now)
    assert store.claim_due(conn, now_ms=t + 10 * LEASE, limit=10, lease_ms=LEASE) == []
    fb = store.get(conn, cb.notification.id)
    assert (fb.status, fb.failure_reason) == (store.STATUS_FAILED, store.REASON_PERMANENT_ERROR)
    assert fb.finished_at is not None


def test_claim_respects_limit_oldest_first(conn):
    ids = [_insert(conn, f"k{i}") for i in range(5)]
    t = _now(conn, ids[-1])
    got = store.claim_due(conn, now_ms=t, limit=2, lease_ms=LEASE)
    assert len(got) == 2


# --- 写回与 fencing ---

def test_retry_returns_to_pending_with_next_time(conn):
    nid = _insert(conn)
    [c] = store.claim_due(conn, now_ms=_now(conn, nid), limit=1, lease_ms=LEASE)
    assert store.schedule_retry(conn, c, _attempt(), next_attempt_ms=123_456)
    n = store.get(conn, nid)
    assert (n.status, n.next_attempt_at) == (store.STATUS_PENDING, 123_456)


def test_stale_lease_writeback_discarded(conn):
    nid = _insert(conn)
    t = _now(conn, nid)
    [old] = store.claim_due(conn, now_ms=t, limit=1, lease_ms=LEASE)
    [new] = store.claim_due(conn, now_ms=t + LEASE, limit=1, lease_ms=LEASE)
    # 旧持有者晚到的"已送达"不生效
    assert store.mark_delivered(conn, old, _attempt("success", 200), datetime.now(timezone.utc)) is False
    assert store.get(conn, nid).status == store.STATUS_DELIVERING
    # 新持有者的结果生效
    assert store.schedule_retry(conn, new, _attempt(), next_attempt_ms=t + 2 * LEASE)
    assert store.get(conn, nid).status == store.STATUS_PENDING
    # 旧持有者那次请求确实发出过，尝试记录保留
    assert [a.attempt_no for a in store.list_attempts(conn, nid)] == [1, 2]


def test_attempts_recorded_in_order(conn):
    nid = _insert(conn)
    t = _now(conn, nid)
    [c1] = store.claim_due(conn, now_ms=t, limit=1, lease_ms=LEASE)
    store.schedule_retry(conn, c1, _attempt("temporary", 500), next_attempt_ms=t + 1)
    [c2] = store.claim_due(conn, now_ms=t + 1, limit=1, lease_ms=LEASE)
    store.mark_delivered(conn, c2, store.Attempt(datetime.now(timezone.utc), 7, "success", 200, None, "ok"), datetime.now(timezone.utc))
    attempts = store.list_attempts(conn, nid)
    assert [(a.attempt_no, a.status_code, a.outcome) for a in attempts] == [(1, 500, "temporary"), (2, 200, "success")]
    assert attempts[1].response_excerpt == "ok" and attempts[1].duration_ms == 7


def test_failed_without_sending_uncounts_attempt(conn):
    nid = _insert(conn)
    [c] = store.claim_due(conn, now_ms=_now(conn, nid), limit=1, lease_ms=LEASE)
    store.mark_failed(conn, c, None, store.REASON_EXPIRED, datetime.now(timezone.utc))
    n = store.get(conn, nid)
    assert (n.status, n.failure_reason, n.attempt_count) == (store.STATUS_FAILED, store.REASON_EXPIRED, 0)
    assert store.list_attempts(conn, nid) == []


def test_concurrent_cold_start_init_schema(tmp_path):
    """API 与 worker 同时冷启动：迁移在写锁内进行，不会重复建表。"""
    import threading

    path = tmp_path / "cold.db"
    errors = []
    barrier = threading.Barrier(6)

    def init():
        barrier.wait()
        try:
            store.init_schema(path)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=init) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    c = store.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
