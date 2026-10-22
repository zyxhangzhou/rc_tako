"""投递 worker：独立进程，与 API 共用同一个 SQLite 文件。

运行：python -m notifier.worker（或 notifier-worker）
"""

import logging
import signal
import sys
import threading
from concurrent.futures import Future, ThreadPoolExecutor, wait

from notifier import store
from notifier.config import ConfigError, WorkerSettings, load_worker_settings
from notifier.delivery import Deps, Guard, deliver_one, make_client, strict_guard

log = logging.getLogger(__name__)


class Worker:
    def __init__(self, cfg: WorkerSettings, deps: Deps | None = None):
        self.cfg = cfg
        self.deps = deps if deps is not None else Deps(cfg=cfg, client=make_client(cfg))
        self._pool = ThreadPoolExecutor(max_workers=cfg.concurrency, thread_name_prefix="deliver")
        self._inflight: set[Future] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        store.init_schema(cfg.db_path)

    def run_once(self, wait_for_completion: bool = False) -> int:
        """按空闲名额占用一批到期通知并提交投递，返回本轮占用的数量。"""
        with self._lock:
            free = self.cfg.concurrency - len(self._inflight)
        if free <= 0:
            return 0
        conn = store.connect(self.cfg.db_path)
        try:
            claimed = store.claim_due(
                conn, self.deps.clock(), free, int(self.cfg.lease_s * 1000), per_host_limit=self.cfg.per_host_limit
            )
        finally:
            conn.close()
        futures = [self._pool.submit(self._deliver, c) for c in claimed]
        with self._lock:
            self._inflight.update(futures)
        for f in futures:
            f.add_done_callback(self._done)
        if wait_for_completion:
            wait(futures)
        return len(claimed)

    def run(self) -> None:
        log.info("worker 启动 db=%s concurrency=%s", self.cfg.db_path, self.cfg.concurrency)
        while not self._stop.is_set():
            try:
                claimed = self.run_once()
            except Exception:
                # 存储暂时不可用（如锁超时）不应让 worker 退出；下一轮再试
                log.exception("占用通知失败")
                claimed = 0
            if claimed == 0:
                self._stop.wait(self.cfg.poll_interval_s)
        self.close()

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        """停止占用新通知，等正在投递的完成。"""
        self._pool.shutdown(wait=True)
        self.deps.client.close()
        log.info("worker 已退出")

    def _deliver(self, claimed: store.Claimed) -> str:
        conn = store.connect(self.cfg.db_path)
        try:
            return deliver_one(conn, claimed, self.deps)
        except Exception:
            # 意外异常：不写回，占用到期后会被重新取出（至少一次）
            log.exception("投递异常 id=%s", claimed.notification.id)
            return "error"
        finally:
            conn.close()

    def _done(self, f: Future) -> None:
        with self._lock:
            self._inflight.discard(f)


def main(guard: Guard = strict_guard) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load_worker_settings()
    except ConfigError as e:
        print(f"配置错误: {e}", file=sys.stderr)
        return 2
    worker = Worker(cfg, Deps(cfg=cfg, client=make_client(cfg), guard=guard))

    def on_signal(signum, frame):
        log.info("收到信号 %s，停止占用新通知", signum)
        worker.stop()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    worker.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
