"""查询、重放、指标接口（TestClient + 进程内 worker + 假下游）。"""

import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from notifier import store
from notifier.app import create_app
from notifier.worker import Worker
from tests.conftest import TOKENS, auth, public_resolver
from tests.delivery_helpers import FakeClock, insert, make_cfg, make_deps
from tests.fake_downstream import FakeDownstream, Reply

CRM = TOKENS["t-crm"]  # "crm-service"


@pytest.fixture
def ops(settings):
    clock = FakeClock()
    app = create_app(settings, resolver=public_resolver, clock=clock)
    conn = store.connect(settings.db_path)
    cfg = make_cfg(settings.db_path)
    worker = Worker(cfg, make_deps(cfg, clock))
    with TestClient(app) as client:
        yield client, conn, clock, worker
    worker.close()
    conn.close()


def _run_until_settled(worker, conn, clock, nid, max_rounds=20):
    for _ in range(max_rounds):
        n = store.get(conn, nid)
        if n.status in (store.STATUS_DELIVERED, store.STATUS_FAILED):
            return n
        clock.set(max(clock(), n.next_attempt_at))
        worker.run_once(wait_for_completion=True)
    return store.get(conn, nid)


# --- 查询 ---

def test_query_delivered_with_attempts(ops):
    client, conn, clock, worker = ops
    with FakeDownstream([Reply(500), Reply(200)]) as ds:
        nid = insert(conn, ds.url, caller=CRM)
        _run_until_settled(worker, conn, clock, nid)
    r = client.get(f"/notifications/{nid}", headers=auth())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "delivered" and body["next_attempt_at"] is None
    assert [a["status_code"] for a in body["attempts"]] == [500, 200]
    assert [a["attempt_no"] for a in body["attempts"]] == [1, 2]
    assert body["request"]["url"] == ds.url


def test_query_pending_shows_next_attempt(ops):
    client, conn, clock, worker = ops
    with FakeDownstream([Reply(503, {"Retry-After": "120"})]) as ds:
        nid = insert(conn, ds.url, caller=CRM)
        worker.run_once(wait_for_completion=True)
    body = client.get(f"/notifications/{nid}", headers=auth()).json()
    assert body["status"] == "pending"
    assert store.to_ms(__import__("datetime").datetime.fromisoformat(body["next_attempt_at"])) == clock() + 120_000


def test_query_failed_shows_reason(ops):
    client, conn, clock, worker = ops
    with FakeDownstream([Reply(400)]) as ds:
        nid = insert(conn, ds.url, caller=CRM)
        worker.run_once(wait_for_completion=True)
    body = client.get(f"/notifications/{nid}", headers=auth()).json()
    assert (body["status"], body["failure_reason"]) == ("failed", "permanent_error")


def test_query_access_control(ops):
    client, conn, clock, worker = ops
    nid = insert(conn, "https://a.example.com/", caller=CRM)
    assert client.get(f"/notifications/{nid}", headers=auth("t-ads")).status_code == 404
    assert client.get("/notifications/does-not-exist", headers=auth()).status_code == 404
    assert client.get(f"/notifications/{nid}").status_code == 401


def test_query_redacts_headers_but_delivery_sends_originals(ops):
    client, conn, clock, worker = ops
    headers = {"Content-Type": "application/json", "X-Api-Key": "secret", "authorization": "Bearer t"}
    with FakeDownstream([Reply(200)]) as ds:
        nid = insert(conn, ds.url, caller=CRM, headers=headers)
        worker.run_once(wait_for_completion=True)
    shown = client.get(f"/notifications/{nid}", headers=auth()).json()["request"]["headers"]
    assert shown == {"Content-Type": "application/json", "X-Api-Key": "***", "authorization": "***"}
    [req] = ds.received
    assert req.headers["X-Api-Key"] == "secret" and req.headers["authorization"] == "Bearer t"


# --- 重放 ---

def test_replay_after_permanent_failure_redelivers_same_id(ops):
    client, conn, clock, worker = ops
    with FakeDownstream([Reply(500), Reply(400), Reply(200)]) as ds:
        nid = insert(conn, ds.url, caller=CRM)
        n = _run_until_settled(worker, conn, clock, nid)
        assert (n.status, n.failure_reason, n.attempt_count) == ("failed", "permanent_error", 2)
        # 下游修正（脚本里下一个回复是 200）后重放
        r = client.post(f"/notifications/{nid}/replay", headers=auth())
        assert r.status_code == 202 and r.json() == {"id": nid, "status": "pending"}
        n = _run_until_settled(worker, conn, clock, nid)
    assert n.status == "delivered"
    assert [r.headers["X-Notification-Id"] for r in ds.received] == [nid] * 3
    assert [r.headers["X-Notification-Attempt"] for r in ds.received] == ["1", "2", "3"]


def test_replay_after_retries_exhausted_gets_full_window(ops):
    client, conn, clock, worker = ops
    with FakeDownstream([Reply(500)]) as ds:
        nid = insert(conn, ds.url, caller=CRM)
        n = _run_until_settled(worker, conn, clock, nid, max_rounds=60)
        assert (n.status, n.failure_reason) == ("failed", "retries_exhausted")
        sent_before = ds.count()
        assert client.post(f"/notifications/{nid}/replay", headers=auth()).status_code == 202
        n = store.get(conn, nid)
        assert n.retry_window_start == clock()
        # 重放后能再次投递，不会因为受理时间早于 24 小时而立即失败
        worker.run_once(wait_for_completion=True)
        assert ds.count() == sent_before + 1
        assert store.get(conn, nid).status == "pending"


def test_replay_rejections_leave_state_unchanged(ops):
    client, conn, clock, worker = ops
    with FakeDownstream([Reply(200)]) as ok, FakeDownstream([Reply(503)]) as busy:
        delivered = insert(conn, ok.url, key="d", caller=CRM)
        pending = insert(conn, busy.url, key="p", caller=CRM)
        expiring = insert(conn, busy.url, key="e", caller=CRM, expires_at=store.from_ms(clock()) + timedelta(seconds=30))
        worker.run_once(wait_for_completion=True)
        clock.advance(31)
        worker.run_once(wait_for_completion=True)
    assert store.get(conn, expiring).failure_reason == "expired"

    for nid in (delivered, pending, expiring):
        before = store.get(conn, nid)
        assert client.post(f"/notifications/{nid}/replay", headers=auth()).status_code == 409
        assert store.get(conn, nid) == before
    assert client.post(f"/notifications/{delivered}/replay", headers=auth("t-ads")).status_code == 404
    assert client.post("/notifications/nope/replay", headers=auth()).status_code == 404
    assert client.post(f"/notifications/{delivered}/replay").status_code == 401


# --- 指标 ---

def _set(conn, nid, **cols):
    sets = ", ".join(f"{k} = ?" for k in cols)
    conn.execute(f"UPDATE notifications SET {sets} WHERE id = ?", (*cols.values(), nid))
    conn.commit()


def _parse(text):
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            out[name] = float(value)
    return out


def test_metrics_counts_backlog_and_no_content(ops):
    client, conn, clock, worker = ops
    now = clock()
    ids = [insert(conn, f"https://h{i}.example.com/secret-path", key=f"k{i}", caller=CRM) for i in range(6)]
    _set(conn, ids[0], status="delivered")
    _set(conn, ids[1], status="delivered")
    _set(conn, ids[2], status="failed", failure_reason="permanent_error")
    _set(conn, ids[3], next_attempt_at=now - 120_000)  # 已到期 120s
    _set(conn, ids[4], next_attempt_at=now + 60_000)
    _set(conn, ids[5], next_attempt_at=now + 60_000)
    conn.execute(
        "INSERT INTO delivery_attempts (notification_id, attempt_no, started_at, duration_ms, outcome) VALUES (?,1,'2026-10-22T00:00:00+00:00',1,'temporary')",
        (ids[3],),
    )
    conn.commit()

    r = client.get("/metrics")  # 不带认证
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    m = _parse(r.text)
    assert m['notifier_notifications{status="delivered"}'] == 2
    assert m['notifier_notifications{status="failed"}'] == 1
    assert m['notifier_notifications{status="pending"}'] == 3
    assert m['notifier_notifications{status="delivering"}'] == 0
    assert m['notifier_failed_notifications{reason="permanent_error"}'] == 1
    assert m['notifier_failed_notifications{reason="expired"}'] == 0
    assert m["notifier_backlog_due"] == 1
    assert m["notifier_oldest_due_age_seconds"] == pytest.approx(120, abs=0.01)
    assert m['notifier_delivery_attempts_total{outcome="temporary"}'] == 1
    assert "example.com" not in r.text and not any(i in r.text for i in ids)


def test_metrics_no_backlog_zero(ops):
    client, conn, clock, worker = ops
    m = _parse(client.get("/metrics").text)
    assert m["notifier_backlog_due"] == 0 and m["notifier_oldest_due_age_seconds"] == 0


def test_metrics_survive_app_restart(ops, settings):
    client, conn, clock, worker = ops
    with FakeDownstream([Reply(500), Reply(200)]) as ds:
        nid = insert(conn, ds.url, caller=CRM)
        _run_until_settled(worker, conn, clock, nid)
    before = client.get("/metrics").text
    with TestClient(create_app(settings, resolver=public_resolver, clock=clock)) as fresh:
        after = fresh.get("/metrics").text
    assert before == after
    assert _parse(after)['notifier_delivery_attempts_total{outcome="success"}'] == 1
