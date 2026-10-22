"""按目标隔离：下游 X 卡死时，下游 Y 的投递不受影响。

对照组（不限 host，即上限 = 并发数）证明效果来自限流本身，而不是测试条件宽松。
"""

import threading
import time

import pytest

from notifier import store
from notifier.delivery import system_clock
from notifier.worker import Worker
from tests.delivery_helpers import insert, make_cfg, make_deps
from tests.fake_downstream import FakeDownstream, Reply


def _run(tmp_path, per_host_limit):
    db = tmp_path / "n.db"
    store.init_schema(db)
    conn = store.connect(db)
    hold = threading.Event()
    with FakeDownstream([Reply(200, block=hold)]) as x, FakeDownstream([Reply(200)]) as y:
        # x 的通知先受理（更早到期），没有隔离时会先占满所有线程
        x_ids = [insert(conn, x.url.replace("127.0.0.1", "localhost") + f"/{i}", key=f"x{i}") for i in range(10)]
        time.sleep(0.01)
        y_ids = [insert(conn, y.url + f"/{i}", key=f"y{i}") for i in range(5)]
        assert {store.get(conn, i).target_host for i in x_ids} == {"localhost"}
        assert {store.get(conn, i).target_host for i in y_ids} == {"127.0.0.1"}

        cfg = make_cfg(db, concurrency=4, per_host_limit=per_host_limit, request_timeout_s=10, lease_s=30, poll_interval_s=0.05)
        w = Worker(cfg, make_deps(cfg, clock=system_clock))
        t = threading.Thread(target=w.run)
        t.start()
        max_x = 0
        start = time.monotonic()
        y_done_at = None
        try:
            while time.monotonic() - start < 3:
                max_x = max(max_x, x.count())
                if all(store.get(conn, i).status == store.STATUS_DELIVERED for i in y_ids):
                    y_done_at = time.monotonic() - start
                    break
                time.sleep(0.02)
            time.sleep(0.2)
            max_x = max(max_x, x.count())
        finally:
            hold.set()
            w.stop()
            t.join(15)
            conn.close()
    return y_done_at, max_x


def test_slow_downstream_does_not_block_others(tmp_path):
    y_done_at, max_x = _run(tmp_path, per_host_limit=2)
    assert y_done_at is not None and y_done_at < 3, "Y 的通知没有在 3s 内全部送达"
    assert max_x == 2, f"X 同时收到 {max_x} 个请求，超过上限 2"


def test_control_without_host_limit_y_is_starved(tmp_path):
    y_done_at, max_x = _run(tmp_path, per_host_limit=4)
    assert y_done_at is None, "对照组：不限 host 时 Y 应被 X 堵住"
    assert max_x == 4
