"""规则 1 的测试证据：收到 202 之后进程被 SIGKILL，重启后通知仍在。

用真实的 uvicorn 子进程，不用 TestClient，确保覆盖进程级的崩溃。
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time

import httpx
import pytest

from notifier import store

N = 20


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(env, port):
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "notifier.app:create_app", "--factory", "--port", str(port), "--log-level", "warning"],
        env=env,
    )
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"服务启动失败，退出码 {proc.returncode}")
        try:
            httpx.get(f"http://127.0.0.1:{port}/docs", timeout=0.5)
            return proc
        except httpx.TransportError:
            time.sleep(0.1)
    proc.kill()
    raise RuntimeError("服务启动超时")


@pytest.mark.skipif(not hasattr(signal, "SIGKILL"), reason="需要 SIGKILL")
def test_accepted_notifications_survive_sigkill(tmp_path):
    db_path = tmp_path / "n.db"
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"t-crm": "crm-service"}))
    env = {**os.environ, "NOTIFIER_DB_PATH": str(db_path), "NOTIFIER_TOKENS_FILE": str(tokens)}
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    proc = _start(env, port)
    submitted = {}
    try:
        for i in range(N):
            p = {
                "url": "https://93.184.216.34/hook",  # 公网 IP 字面量，不依赖 DNS
                "method": "POST",
                "headers": {"Content-Type": "application/json", "X-Seq": str(i)},
                "body": json.dumps({"seq": i, "名字": "张三"}, ensure_ascii=False),
                "idempotency_key": f"evt-{i}",
            }
            r = httpx.post(f"{base}/notifications", json=p, headers={"Authorization": "Bearer t-crm"})
            assert r.status_code == 202, r.text
            submitted[r.json()["id"]] = p
    finally:
        # 最后一个 202 返回之后立即强杀，不给进程任何清理机会
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait()
    assert proc.returncode == -signal.SIGKILL

    proc = _start(env, port)
    try:
        conn = store.connect(db_path)
        try:
            for nid, p in submitted.items():
                n = store.get(conn, nid)
                assert n is not None, f"通知 {nid} 丢失"
                assert n.status == store.STATUS_PENDING
                assert (n.url, n.method, n.headers, n.body) == (p["url"], p["method"], p["headers"], p["body"])
            assert conn.execute("SELECT count(*) FROM notifications").fetchone()[0] == N
        finally:
            conn.close()
        # 重启后的服务仍能识别重复提交
        r = httpx.post(
            f"{base}/notifications",
            json=next(iter(submitted.values())),
            headers={"Authorization": "Bearer t-crm"},
        )
        assert r.status_code == 200
        assert r.json()["id"] == next(iter(submitted))
    finally:
        proc.terminate()
        proc.wait()
