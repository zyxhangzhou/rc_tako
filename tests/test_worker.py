import threading
import time
from datetime import timedelta

import pytest

from notifier import store
from notifier.worker import Worker
from tests.delivery_helpers import FakeClock, insert, make_cfg, make_deps
from tests.fake_downstream import FakeDownstream, Reply


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


def _worker(db, clock, **cfg_kw):
    cfg = make_cfg(db, **cfg_kw)
    return Worker(cfg, make_deps(cfg, clock))


def _step_to_next(conn, clock, nid):
    clock.set(store.get(conn, nid).next_attempt_at)


def test_fail_then_succeed(db, conn):
    clock = FakeClock()
    with FakeDownstream([Reply(500), Reply(200)]) as ds:
        nid = insert(conn, ds.url)
        w = _worker(db, clock)
        assert w.run_once(wait_for_completion=True) == 1
        assert store.get(conn, nid).status == store.STATUS_PENDING
        _step_to_next(conn, clock, nid)
        assert w.run_once(wait_for_completion=True) == 1
        w.close()
    assert store.get(conn, nid).status == store.STATUS_DELIVERED
    assert [a.status_code for a in store.list_attempts(conn, nid)] == [500, 200]
    assert ds.count() == 2


def test_intervals_grow_under_persistent_500(db, conn):
    clock = FakeClock()
    with FakeDownstream([Reply(500)]) as ds:
        nid = insert(conn, ds.url)
        w = _worker(db, clock)
        intervals = []
        for _ in range(6):
            w.run_once(wait_for_completion=True)
            before = clock()
            _step_to_next(conn, clock, nid)
            intervals.append(clock() - before)
        w.close()
    assert all(b > a for a, b in zip(intervals, intervals[1:])), intervals


def test_retries_exhausted_and_nothing_sent_after_deadline(db, conn):
    clock = FakeClock()
    with FakeDownstream([Reply(500)]) as ds:
        nid = insert(conn, ds.url)
        w = _worker(db, clock, max_retry_duration_s=120, backoff_base_s=10, backoff_cap_s=40)
        for _ in range(50):
            if store.get(conn, nid).status == store.STATUS_FAILED:
                break
            w.run_once(wait_for_completion=True)
            _step_to_next(conn, clock, nid)
        sent = ds.count()
        clock.advance(3600)
        assert w.run_once(wait_for_completion=True) == 0
        w.close()
    n = store.get(conn, nid)
    assert (n.status, n.failure_reason) == (store.STATUS_FAILED, store.REASON_RETRIES_EXHAUSTED)
    assert ds.count() == sent
    deadline = store.to_ms(n.created_at) + 120_000
    assert all(store.to_ms(a.started_at) < deadline for a in store.list_attempts(conn, nid))


def test_candidate_example_expiry_before_next_retry(db, conn):
    """10:00 失败、正常算出 10:10、expires_at 10:05：之后不再发请求，10:05 后一个 poll 周期内变为已过期。"""
    clock = FakeClock()
    with FakeDownstream([Reply(503, {"Retry-After": "600"})]) as ds:
        nid = insert(conn, ds.url, expires_at=store.from_ms(clock()) + timedelta(minutes=5))
        w = _worker(db, clock)
        w.run_once(wait_for_completion=True)
        expires_ms = store.to_ms(store.get(conn, nid).expires_at)
        # 失败当时没有被提前标记为已过期
        n = store.get(conn, nid)
        assert n.status == store.STATUS_PENDING and n.next_attempt_at == expires_ms
        clock.set(expires_ms - 1)
        assert w.run_once(wait_for_completion=True) == 0
        clock.set(expires_ms + int(w.cfg.poll_interval_s * 1000))
        assert w.run_once(wait_for_completion=True) == 1
        w.close()
    n = store.get(conn, nid)
    assert (n.status, n.failure_reason) == (store.STATUS_FAILED, store.REASON_EXPIRED)
    assert ds.count() == 1


def test_restart_does_not_retry_early(db, conn):
    """6.2：worker A 失败 3 次后停止；worker B 在原定时间前不投递，之后恰好投递 1 次。"""
    clock = FakeClock()
    with FakeDownstream([Reply(500)]) as ds:
        nid = insert(conn, ds.url)
        a = _worker(db, clock)
        for i in range(3):
            a.run_once(wait_for_completion=True)
            if i < 2:
                _step_to_next(conn, clock, nid)
        a.close()
        assert ds.count() == 3
        due = store.get(conn, nid).next_attempt_at
        assert due - clock() >= 30_000  # 第 3 次失败后的退避约 40s

        b = _worker(db, clock)
        clock.set(due - 1)
        assert b.run_once(wait_for_completion=True) == 0
        assert ds.count() == 3
        clock.set(due)
        assert b.run_once(wait_for_completion=True) == 1
        b.close()
    assert ds.count() == 4


def test_stop_waits_for_inflight(db, conn):
    clock = FakeClock()
    release = threading.Event()
    with FakeDownstream([Reply(200, block=release)]) as ds:
        nid = insert(conn, ds.url)
        w = _worker(db, clock)
        t = threading.Thread(target=w.run)
        t.start()
        deadline = time.monotonic() + 5
        while ds.count() == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ds.count() == 1
        w.stop()
        time.sleep(0.2)
        assert t.is_alive(), "正在投递时不应退出"
        release.set()
        t.join(5)
        assert not t.is_alive()
    assert store.get(conn, nid).status == store.STATUS_DELIVERED
