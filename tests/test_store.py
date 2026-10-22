from datetime import datetime, timedelta, timezone

import pytest

from notifier import store


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "data" / "n.db"
    store.init_schema(path)
    return path


@pytest.fixture
def conn(db_path):
    c = store.connect(db_path)
    yield c
    c.close()


def _new(**overrides):
    fields = dict(
        caller="crm-service",
        idempotency_key="evt-1",
        request_hash="h1",
        url="https://api.example.com/hook",
        method="POST",
        headers={"Content-Type": "application/json", "X-Api-Key": "密钥"},
        body='{"user": "张三", "n": 1}',
        expires_at=datetime(2030, 1, 1, 8, 0, tzinfo=timezone(timedelta(hours=8))),
    )
    fields.update(overrides)
    return store.NewNotification(**fields)


def test_first_start_initializes_schema(tmp_path):
    path = tmp_path / "fresh" / "n.db"
    assert not path.exists()
    store.init_schema(path)
    c = store.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION
    assert c.execute("SELECT count(*) FROM notifications").fetchone()[0] == 0


def test_init_schema_is_idempotent(db_path):
    store.init_schema(db_path)
    store.init_schema(db_path)


def test_connection_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    # 2 == FULL
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == store.BUSY_TIMEOUT_MS


def test_insert_saves_content_verbatim_with_pending_status(conn):
    n = _new()
    r = store.insert_or_get(conn, n)
    assert r.created
    got = store.get(conn, r.id)
    assert (got.caller, got.idempotency_key, got.request_hash) == (n.caller, n.idempotency_key, n.request_hash)
    assert (got.url, got.method, got.headers, got.body) == (n.url, n.method, n.headers, n.body)
    assert got.expires_at == n.expires_at
    assert got.status == store.STATUS_PENDING
    assert got.created_at.tzinfo is not None


def test_null_body_and_expiry_roundtrip(conn):
    r = store.insert_or_get(conn, _new(body=None, expires_at=None, headers={}))
    got = store.get(conn, r.id)
    assert got.body is None and got.expires_at is None and got.headers == {}


def test_duplicate_key_returns_existing_without_new_row(conn):
    first = store.insert_or_get(conn, _new())
    second = store.insert_or_get(conn, _new(request_hash="h2", body="different"))
    assert not second.created
    assert second.id == first.id
    # 返回的是首次的指纹，供上层判断 409
    assert second.request_hash == "h1"
    assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == 1


def test_same_key_different_callers_are_independent(conn):
    a = store.insert_or_get(conn, _new(caller="a"))
    b = store.insert_or_get(conn, _new(caller="b"))
    assert a.created and b.created and a.id != b.id


def test_get_unknown_id(conn):
    assert store.get(conn, "nope") is None
