"""存储层：按调用方读取、Header 脱敏、重放。"""

import threading
from datetime import datetime, timedelta, timezone

import pytest

from notifier import store, views
from tests.delivery_helpers import insert

LEASE = 60_000


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "n.db"
    store.init_schema(path)
    return path


@pytest.fixture
def conn(db):
    c = store.connect(db)
    yield c
    c.close()


def _fail(conn, nid, reason):
    t = conn.execute("SELECT next_attempt_at FROM notifications WHERE id=?", (nid,)).fetchone()[0]
    [c] = [x for x in store.claim_due(conn, t + 10 * LEASE, 100, LEASE) if x.notification.id == nid]
    store.mark_failed(conn, c, None, reason, datetime.now(timezone.utc))


def _now_ms():
    return store.to_ms(datetime.now(timezone.utc))


# --- 按调用方读取 ---

def test_get_for_caller_hides_others(conn):
    nid = insert(conn, "https://a.example.com/", caller="crm")
    assert store.get_for_caller(conn, nid, "crm").id == nid
    assert store.get_for_caller(conn, nid, "ads") is None
    assert store.get_for_caller(conn, "nope", "crm") is None


# --- Header 脱敏 ---

def test_redact_headers_allowlist_case_insensitive():
    original = {
        "Content-Type": "application/json",
        "accept": "*/*",
        "User-Agent": "biz/1.0",
        "X-Api-Key": "secret",
        "authorization": "Bearer t",
        "X-Sign": "abc",
    }
    redacted = views.redact_headers(original)
    assert redacted == {
        "Content-Type": "application/json",
        "accept": "*/*",
        "User-Agent": "biz/1.0",
        "X-Api-Key": "***",
        "authorization": "***",
        "X-Sign": "***",
    }
    assert original["X-Api-Key"] == "secret"


# --- 重放 ---

@pytest.mark.parametrize(
    "reason", [store.REASON_PERMANENT_ERROR, store.REASON_RETRIES_EXHAUSTED, store.REASON_BLOCKED_TARGET]
)
def test_replayable_reasons(conn, reason):
    nid = insert(conn, "https://a.example.com/")
    _fail(conn, nid, reason)
    now = _now_ms() + 5000
    assert store.replay(conn, nid, "crm", now) == store.REPLAYED
    n = store.get(conn, nid)
    assert (n.status, n.failure_reason, n.finished_at) == (store.STATUS_PENDING, None, None)
    assert n.next_attempt_at == now and n.retry_window_start == now


def test_expired_reason_not_replayable(conn):
    nid = insert(conn, "https://a.example.com/")
    _fail(conn, nid, store.REASON_EXPIRED)
    assert store.replay(conn, nid, "crm", _now_ms()) == store.NOT_REPLAYABLE
    assert store.get(conn, nid).status == store.STATUS_FAILED


def test_past_expires_at_not_replayable(conn):
    exp = datetime.now(timezone.utc) + timedelta(minutes=5)
    nid = insert(conn, "https://a.example.com/", expires_at=exp)
    _fail(conn, nid, store.REASON_PERMANENT_ERROR)
    assert store.replay(conn, nid, "crm", store.to_ms(exp)) == store.NOT_REPLAYABLE
    assert store.replay(conn, nid, "crm", store.to_ms(exp) - 1) == store.REPLAYED


def test_delivered_pending_delivering_not_replayable(conn):
    pending = insert(conn, "https://a.example.com/", key="p")
    assert store.replay(conn, pending, "crm", _now_ms()) == store.NOT_REPLAYABLE

    delivering = insert(conn, "https://b.example.com/", key="d")
    t = store.get(conn, delivering).next_attempt_at
    claimed = [c for c in store.claim_due(conn, t, 10, LEASE) if c.notification.id == delivering]
    assert store.replay(conn, delivering, "crm", _now_ms()) == store.NOT_REPLAYABLE

    store.mark_delivered(
        conn, claimed[0], store.Attempt(datetime.now(timezone.utc), 1, "success", 200), datetime.now(timezone.utc)
    )
    assert store.replay(conn, delivering, "crm", _now_ms()) == store.NOT_REPLAYABLE


def test_replay_others_not_found(conn):
    nid = insert(conn, "https://a.example.com/", caller="crm")
    _fail(conn, nid, store.REASON_PERMANENT_ERROR)
    assert store.replay(conn, nid, "ads", _now_ms()) == store.NOT_FOUND
    assert store.get(conn, nid).status == store.STATUS_FAILED


def test_concurrent_replay_only_one_wins(db, conn):
    nid = insert(conn, "https://a.example.com/")
    _fail(conn, nid, store.REASON_PERMANENT_ERROR)
    n = 8
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def go():
        c = store.connect(db)
        try:
            barrier.wait()
            r = store.replay(c, nid, "crm", _now_ms())
            with lock:
                results.append(r)
        finally:
            c.close()

    threads = [threading.Thread(target=go) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results).count(store.REPLAYED) == 1, results
    assert results.count(store.NOT_REPLAYABLE) == n - 1
