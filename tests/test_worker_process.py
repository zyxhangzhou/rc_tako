"""worker 作为独立进程：入口、SIGTERM 优雅退出、SIGKILL 后重投。"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from notifier import store
from tests.delivery_helpers import insert
from tests.fake_downstream import FakeDownstream, Reply

ENTRY = Path(__file__).with_name("worker_entry.py")


def _env(db, **extra):
    return {**os.environ, "NOTIFIER_DB_PATH": str(db), **extra}


def _wait(pred, timeout, msg):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(0.05)
    raise AssertionError(msg)


def test_module_entry_exits_cleanly_on_sigterm(tmp_path):
    proc = subprocess.Popen([sys.executable, "-m", "notifier.worker"], env=_env(tmp_path / "n.db"))
    try:
        _wait(lambda: (tmp_path / "n.db").exists(), 10, "worker 未启动")
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=5) == 0
    finally:
        if proc.poll() is None:
            proc.kill()


def test_missing_config_exits_with_error():
    env = {k: v for k, v in os.environ.items() if k != "NOTIFIER_DB_PATH"}
    r = subprocess.run([sys.executable, "-m", "notifier.worker"], env=env, capture_output=True, text=True, timeout=10)
    assert r.returncode == 2 and "NOTIFIER_DB_PATH" in r.stderr


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="需要 SIGKILL")
def test_sigkill_after_request_sent_is_redelivered(tmp_path):
    """7.1：规则 3 + 规则 5 的证据。请求已发出、结果未记录时 worker 被 SIGKILL，重启后占用到期重投。"""
    db = tmp_path / "n.db"
    store.init_schema(db)
    conn = store.connect(db)
    env = _env(
        db,
        NOTIFIER_WORKER_LEASE_S="2",
        NOTIFIER_WORKER_REQUEST_TIMEOUT_S="1",
        NOTIFIER_WORKER_POLL_INTERVAL_S="0.1",
    )
    hold = __import__("threading").Event()
    try:
        with FakeDownstream([Reply(200, block=hold), Reply(200)]) as ds:
            nid = insert(conn, ds.url + "/hook")
            first = subprocess.Popen([sys.executable, str(ENTRY)], env=env)
            try:
                _wait(lambda: ds.count() == 1, 15, "第一次请求没有到达下游")
                os.kill(first.pid, signal.SIGKILL)
                first.wait(5)
            finally:
                if first.poll() is None:
                    first.kill()
            assert first.returncode == -signal.SIGKILL
            hold.set()
            assert store.get(conn, nid).status == store.STATUS_DELIVERING

            second = subprocess.Popen([sys.executable, str(ENTRY)], env=env)
            try:
                _wait(lambda: store.get(conn, nid).status == store.STATUS_DELIVERED, 15, "重启后没有重投成功")
            finally:
                second.send_signal(signal.SIGTERM)
                second.wait(5)

        assert ds.count() == 2
        assert [r.headers["X-Notification-Id"] for r in ds.received] == [nid, nid]
        assert [r.headers["X-Notification-Attempt"] for r in ds.received] == ["1", "2"]
        # 第一次的请求发出了但结果没记录下来（进程被杀），所以只有第二次的尝试记录
        assert [a.attempt_no for a in store.list_attempts(conn, nid)] == [2]
    finally:
        hold.set()
        conn.close()
