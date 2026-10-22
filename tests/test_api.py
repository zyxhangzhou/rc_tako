import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from notifier import store
from tests.conftest import auth, payload, row_count


# --- 认证 ---

def test_missing_token_401_and_nothing_written(client, db):
    r = client.post("/notifications", json=payload())
    assert r.status_code == 401
    assert row_count(db) == 0


def test_invalid_token_401(client, db):
    r = client.post("/notifications", json=payload(), headers=auth("nope"))
    assert r.status_code == 401
    assert row_count(db) == 0


def test_auth_checked_before_validation(client):
    r = client.post("/notifications", json={"url": "ftp://x"})
    assert r.status_code == 401


def test_caller_recorded(client, db):
    r = client.post("/notifications", json=payload(), headers=auth("t-crm"))
    assert store.get(db, r.json()["id"]).caller == "crm-service"


# --- 正常受理 ---

def test_accept_returns_202_and_persists_pending(client, db):
    p = payload(headers={"X-Api-Key": "k"}, expires_at="2099-01-01T00:00:00+08:00")
    r = client.post("/notifications", json=p, headers=auth())
    assert r.status_code == 202
    n = store.get(db, r.json()["id"])
    assert n.status == store.STATUS_PENDING
    assert (n.url, n.method, n.headers, n.body) == (p["url"], p["method"], p["headers"], p["body"])
    assert n.expires_at == datetime(2099, 1, 1, tzinfo=timezone(timedelta(hours=8)))


# --- 校验与地址防护经接口返回 422 ---

@pytest.mark.parametrize(
    "overrides",
    [
        {"url": "ftp://example.com/x"},
        {"method": "TRACE"},
        {"headers": {"Host": "x"}},
        {"expires_at": "2020-01-01T00:00:00Z"},
        {"url": "http://127.0.0.1:8080/hook"},
        {"url": "http://169.254.169.254/latest/meta-data"},
    ],
)
def test_rejected_with_422(client, db, overrides):
    r = client.post("/notifications", json=payload(**overrides), headers=auth())
    assert r.status_code == 422
    assert row_count(db) == 0


def test_missing_idempotency_key_422(client):
    p = payload()
    del p["idempotency_key"]
    assert client.post("/notifications", json=p, headers=auth()).status_code == 422


# --- 幂等 ---

def test_same_content_resubmit_returns_200_same_id(client, db):
    first = client.post("/notifications", json=payload(), headers=auth())
    second = client.post("/notifications", json=payload(), headers=auth())
    assert (first.status_code, second.status_code) == (202, 200)
    assert first.json()["id"] == second.json()["id"]
    assert row_count(db) == 1


def test_same_key_different_content_409_original_unchanged(client, db):
    first = client.post("/notifications", json=payload(body="a"), headers=auth())
    second = client.post("/notifications", json=payload(body="b"), headers=auth())
    assert second.status_code == 409
    assert store.get(db, first.json()["id"]).body == "a"
    assert row_count(db) == 1


def test_same_key_different_callers_independent(client, db):
    a = client.post("/notifications", json=payload(), headers=auth("t-crm"))
    b = client.post("/notifications", json=payload(), headers=auth("t-ads"))
    assert (a.status_code, b.status_code) == (202, 202)
    assert a.json()["id"] != b.json()["id"]


# --- 持久化失败 ---

def test_store_error_returns_503_no_row(client, db, monkeypatch):
    def boom(conn, n):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(store, "insert_or_get", boom)
    r = client.post("/notifications", json=payload(), headers=auth())
    assert r.status_code == 503
    assert row_count(db) == 0


def test_lock_timeout_returns_503_then_retry_succeeds(client, settings, monkeypatch):
    # 真实的锁超时：另一个连接持有写锁
    monkeypatch.setattr(store, "BUSY_TIMEOUT_MS", 100)
    locker = sqlite3.connect(settings.db_path, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    try:
        r = client.post("/notifications", json=payload(), headers=auth())
        assert r.status_code == 503
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    # 调用方带同一个 key 重试
    r = client.post("/notifications", json=payload(), headers=auth())
    assert r.status_code == 202


# --- 并发幂等 ---

def test_concurrent_duplicate_submits_create_one_row(app, db):
    n = 16
    barrier = threading.Barrier(n)
    results = []
    lock = threading.Lock()

    def worker():
        with TestClient(app) as c:
            barrier.wait()
            r = c.post("/notifications", json=payload(idempotency_key="race"), headers=auth())
            with lock:
                results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    codes = sorted(r.status_code for r in results)
    assert codes.count(202) == 1 and codes.count(200) == n - 1, codes
    assert len({r.json()["id"] for r in results}) == 1
    assert row_count(db) == 1
