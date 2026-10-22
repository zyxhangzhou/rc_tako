import random
from dataclasses import replace
from datetime import datetime, timezone

from notifier import store
from notifier.config import WorkerSettings
from notifier.delivery import Deps, make_client


class FakeClock:
    def __init__(self):
        # 比真实时间快 1s：之后插入的通知（next_attempt_at = 真实受理时间）在测试一开始就已到期
        self.ms = store.to_ms(datetime.now(timezone.utc)) + 1000

    def __call__(self) -> int:
        return self.ms

    def advance(self, seconds: float) -> None:
        self.ms += int(seconds * 1000)

    def set(self, ms: int) -> None:
        self.ms = ms


def allow_all(url: str) -> None:
    """测试专用：不做地址校验，才能投递到 127.0.0.1 的假下游。"""


def make_cfg(db_path, **kw):
    base = WorkerSettings(db_path=db_path, request_timeout_s=2.0, lease_s=5.0, poll_interval_s=0.05)
    return replace(base, **kw)


def make_deps(cfg, clock=None, guard=allow_all):
    return Deps(cfg=cfg, client=make_client(cfg), guard=guard, clock=clock or FakeClock(), rng=random.Random(0))


def insert(conn, url, key="evt-1", method="POST", headers=None, body='{"e":1}', expires_at=None):
    return store.insert_or_get(
        conn,
        store.NewNotification(
            caller="crm", idempotency_key=key, request_hash="h", url=url, method=method,
            headers=headers if headers is not None else {"Content-Type": "application/json"},
            body=body, expires_at=expires_at,
        ),
    ).id


def claim_one(conn, clock, cfg):
    claimed = store.claim_due(conn, now_ms=clock(), limit=1, lease_ms=int(cfg.lease_s * 1000))
    assert len(claimed) == 1, claimed
    return claimed[0]
