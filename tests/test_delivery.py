import socket
from datetime import timedelta

import pytest

from notifier import store
from notifier.delivery import deliver_one
from notifier.netguard import BlockedTarget, TargetUnresolvable
from tests.delivery_helpers import FakeClock, claim_one, insert, make_cfg, make_deps
from tests.fake_downstream import FakeDownstream, Reply


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "n.db"
    store.init_schema(db)
    conn = store.connect(db)
    cfg = make_cfg(db)
    clock = FakeClock()
    deps = make_deps(cfg, clock)
    yield conn, cfg, clock, deps
    deps.client.close()
    conn.close()


def _deliver(conn, cfg, clock, deps):
    return deliver_one(conn, claim_one(conn, clock, cfg), deps)


def test_200_delivered_and_request_forwarded_verbatim(env):
    conn, cfg, clock, deps = env
    with FakeDownstream([Reply(200)]) as ds:
        nid = insert(conn, ds.url + "/hook?x=1", method="PUT", headers={"X-Api-Key": "k", "X-Notification-Id": "spoof"}, body='{"名":"张"}')
        assert _deliver(conn, cfg, clock, deps) == store.STATUS_DELIVERED
    [req] = ds.received
    assert (req.method, req.path, req.body) == ("PUT", "/hook?x=1", '{"名":"张"}'.encode())
    assert req.headers["X-Api-Key"] == "k"
    # 系统的值覆盖提交时的同名 Header
    assert req.headers["X-Notification-Id"] == nid
    assert req.headers["X-Notification-Attempt"] == "1"
    n = store.get(conn, nid)
    assert n.status == store.STATUS_DELIVERED and n.finished_at is not None
    [a] = store.list_attempts(conn, nid)
    assert (a.status_code, a.outcome, a.response_excerpt) == (200, "success", "ok")


def test_400_permanent_single_request(env):
    conn, cfg, clock, deps = env
    with FakeDownstream([Reply(400)]) as ds:
        nid = insert(conn, ds.url)
        assert _deliver(conn, cfg, clock, deps) == store.STATUS_FAILED
        clock.advance(86400 * 2)
        assert store.claim_due(conn, clock(), 10, 5000) == []
    assert ds.count() == 1
    assert store.get(conn, nid).failure_reason == store.REASON_PERMANENT_ERROR


def test_302_not_followed(env):
    conn, cfg, clock, deps = env
    with FakeDownstream([Reply(200)]) as target, FakeDownstream([Reply(302, {"Location": "/elsewhere"})]) as ds:
        # Location 指向另一个假下游，验证没有被请求
        ds.replies = [Reply(302, {"Location": target.url + "/redirected"})]
        nid = insert(conn, ds.url)
        assert _deliver(conn, cfg, clock, deps) == store.STATUS_FAILED
    assert ds.count() == 1 and target.count() == 0
    assert store.get(conn, nid).failure_reason == store.REASON_PERMANENT_ERROR


@pytest.mark.parametrize("status", [429, 500, 503, 408])
def test_temporary_status_scheduled_for_retry(env, status):
    conn, cfg, clock, deps = env
    with FakeDownstream([Reply(status)]) as ds:
        nid = insert(conn, ds.url)
        assert _deliver(conn, cfg, clock, deps) == store.STATUS_PENDING
    n = store.get(conn, nid)
    assert n.status == store.STATUS_PENDING and n.next_attempt_at > clock()


def test_retry_after_respected(env):
    conn, cfg, clock, deps = env
    with FakeDownstream([Reply(503, {"Retry-After": "120"})]) as ds:
        nid = insert(conn, ds.url)
        _deliver(conn, cfg, clock, deps)
    assert store.get(conn, nid).next_attempt_at == clock() + 120_000


def _closed_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_connection_refused_retried(env):
    conn, cfg, clock, deps = env
    nid = insert(conn, f"http://127.0.0.1:{_closed_port()}/hook")
    assert _deliver(conn, cfg, clock, deps) == store.STATUS_PENDING
    [a] = store.list_attempts(conn, nid)
    assert a.outcome == "temporary" and a.status_code is None and "ConnectError" in a.error


def test_blocked_at_delivery_not_sent(env):
    conn, cfg, clock, _ = env

    def blocked(url):
        raise BlockedTarget("解析到 10.0.0.5")

    deps = make_deps(cfg, clock, guard=blocked)
    with FakeDownstream() as ds:
        nid = insert(conn, ds.url)
        assert _deliver(conn, cfg, clock, deps) == store.STATUS_FAILED
    assert ds.count() == 0
    n = store.get(conn, nid)
    assert (n.failure_reason, n.attempt_count) == (store.REASON_BLOCKED_TARGET, 0)


def test_dns_failure_at_delivery_retried(env):
    conn, cfg, clock, _ = env

    def unresolvable(url):
        raise TargetUnresolvable("无法解析")

    deps = make_deps(cfg, clock, guard=unresolvable)
    nid = insert(conn, "https://flaky.example.com/hook")
    assert _deliver(conn, cfg, clock, deps) == store.STATUS_PENDING
    [a] = store.list_attempts(conn, nid)
    assert a.error.startswith("dns:")


def test_expired_not_sent(env):
    conn, cfg, clock, deps = env
    with FakeDownstream() as ds:
        nid = insert(conn, ds.url, expires_at=store.from_ms(clock()) + timedelta(minutes=5))
        clock.advance(300)  # 恰好到 expires_at
        assert _deliver(conn, cfg, clock, deps) == store.STATUS_FAILED
    assert ds.count() == 0
    assert store.get(conn, nid).failure_reason == store.REASON_EXPIRED


def test_retry_budget_exhausted_not_sent(env):
    conn, cfg, clock, deps = env
    with FakeDownstream() as ds:
        nid = insert(conn, ds.url)
        clock.advance(cfg.max_retry_duration_s + 1)
        assert _deliver(conn, cfg, clock, deps) == store.STATUS_FAILED
    assert ds.count() == 0
    assert store.get(conn, nid).failure_reason == store.REASON_RETRIES_EXHAUSTED


def test_processed_but_timed_out_is_redelivered_with_same_id(env):
    """5.2：下游已处理但响应超过超时 → 重试，下游收到 2 次相同 ID。"""
    conn, cfg, clock, _ = env
    cfg = make_cfg(cfg.db_path, request_timeout_s=0.3)
    deps = make_deps(cfg, clock)
    with FakeDownstream([Reply(200, delay_s=1.0), Reply(200)]) as ds:
        nid = insert(conn, ds.url)
        assert _deliver(conn, cfg, clock, deps) == store.STATUS_PENDING
        [a1] = store.list_attempts(conn, nid)
        assert "Timeout" in a1.error
        clock.set(store.get(conn, nid).next_attempt_at)
        assert _deliver(conn, cfg, clock, deps) == store.STATUS_DELIVERED
    assert ds.count() == 2
    assert [r.headers["X-Notification-Id"] for r in ds.received] == [nid, nid]
    assert [r.headers["X-Notification-Attempt"] for r in ds.received] == ["1", "2"]
    deps.client.close()


def test_large_response_excerpt_truncated(env):
    conn, cfg, clock, deps = env
    with FakeDownstream([Reply(500, body=b"x" * 100_000)]) as ds:
        nid = insert(conn, ds.url)
        _deliver(conn, cfg, clock, deps)
    [a] = store.list_attempts(conn, nid)
    assert len(a.response_excerpt) == 1024


def test_deadline_counts_from_retry_window_start(env):
    """重试窗口起点被重置（重放）后，受理 25 小时的通知不会被判为重试耗尽。"""
    conn, cfg, clock, deps = env
    with FakeDownstream([Reply(200)]) as ds:
        nid = insert(conn, ds.url)
        clock.advance(25 * 3600)
        conn.execute("UPDATE notifications SET retry_window_start = ? WHERE id = ?", (clock(), nid))
        conn.commit()
        assert _deliver(conn, cfg, clock, deps) == store.STATUS_DELIVERED
    assert ds.count() == 1
