"""单次投递：发送前检查 → 地址校验 → 发送 → 分类 → 写回。

规则 2：只有 2xx 才 mark_delivered。规则 5：超时、断连等结果不明的情况按临时失败重试。
规则 6：每次请求都带 X-Notification-Id。
"""

import logging
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from notifier import policy, store
from notifier.config import WorkerSettings
from notifier.netguard import BlockedTarget, TargetUnresolvable, check_target

log = logging.getLogger(__name__)

HEADER_ID = "X-Notification-Id"
HEADER_ATTEMPT = "X-Notification-Attempt"
EXCERPT_BYTES = 1024

Clock = Callable[[], int]  # 返回 UTC 毫秒
Guard = Callable[[str], None]  # 目标被禁止时抛 BlockedTarget，解析失败时抛 TargetUnresolvable


def system_clock() -> int:
    return int(time.time() * 1000)


def strict_guard(url: str) -> None:
    check_target(url, strict=True)


def make_client(cfg: WorkerSettings) -> httpx.Client:
    # trust_env=False：不读取环境变量里的代理配置，出站地址只由通知决定
    return httpx.Client(timeout=cfg.request_timeout_s, follow_redirects=False, trust_env=False)


@dataclass
class Deps:
    cfg: WorkerSettings
    client: httpx.Client
    guard: Guard = strict_guard
    clock: Clock = system_clock
    rng: random.Random | None = None

    def __post_init__(self):
        if self.rng is None:
            self.rng = random.Random()


def deliver_one(conn: sqlite3.Connection, claimed: store.Claimed, deps: Deps) -> str:
    """投递一条已占用的通知，返回最终写入的状态（占用丢失时返回 "lease_lost"）。"""
    n = claimed.notification
    now_ms = deps.clock()
    deadline_ms, deadline_reason = policy.deadline(n.retry_window_start, n.expires_at, deps.cfg)

    # 发送前检查：两个期限哪个先到以哪个为准，到期即不再发送
    if now_ms >= deadline_ms:
        return _fail(conn, claimed, None, deadline_reason, now_ms)

    try:
        deps.guard(n.url)
    except BlockedTarget as e:
        log.warning("目标被禁止 id=%s: %s", n.id, e)
        return _fail(conn, claimed, None, store.REASON_BLOCKED_TARGET, now_ms)
    except TargetUnresolvable as e:
        attempt = store.Attempt(store.from_ms(now_ms), 0, policy.TEMPORARY, error=f"dns: {e}")
        return _retry(conn, claimed, attempt, None, now_ms, deadline_ms, deps)

    headers = {k: v for k, v in n.headers.items() if k.lower() not in (HEADER_ID.lower(), HEADER_ATTEMPT.lower())}
    headers[HEADER_ID] = n.id
    headers[HEADER_ATTEMPT] = str(n.attempt_count)
    content = None if n.body is None else n.body.encode("utf-8")

    started = time.monotonic()
    try:
        with deps.client.stream(n.method, n.url, headers=headers, content=content) as resp:
            excerpt = _read_excerpt(resp)
            code = resp.status_code
            retry_after = resp.headers.get("Retry-After")
    except httpx.HTTPError as e:
        attempt = store.Attempt(store.from_ms(now_ms), _since(started), policy.TEMPORARY, error=_describe(e))
        return _retry(conn, claimed, attempt, None, deps.clock(), deadline_ms, deps)

    outcome = policy.classify_status(code)
    attempt = store.Attempt(store.from_ms(now_ms), _since(started), outcome, status_code=code, response_excerpt=excerpt)
    done_ms = deps.clock()
    if outcome == policy.SUCCESS:
        ok = store.mark_delivered(conn, claimed, attempt, store.from_ms(done_ms))
        return store.STATUS_DELIVERED if ok else _lease_lost(n)
    if outcome == policy.PERMANENT:
        return _fail(conn, claimed, attempt, store.REASON_PERMANENT_ERROR, done_ms)
    return _retry(conn, claimed, attempt, policy.parse_retry_after(retry_after, done_ms), done_ms, deadline_ms, deps)


def _retry(conn, claimed, attempt, retry_after_s, now_ms, deadline_ms, deps) -> str:
    nxt = policy.next_attempt_ms(claimed.notification.attempt_count, now_ms, retry_after_s, deadline_ms, deps.cfg, deps.rng)
    ok = store.schedule_retry(conn, claimed, attempt, nxt)
    return store.STATUS_PENDING if ok else _lease_lost(claimed.notification)


def _fail(conn, claimed, attempt, reason, now_ms) -> str:
    ok = store.mark_failed(conn, claimed, attempt, reason, store.from_ms(now_ms))
    return store.STATUS_FAILED if ok else _lease_lost(claimed.notification)


def _lease_lost(n: store.Notification) -> str:
    log.warning("占用已过期，结果被丢弃 id=%s attempt=%s", n.id, n.attempt_count)
    return "lease_lost"


def _read_excerpt(resp: httpx.Response) -> str:
    buf = b""
    for chunk in resp.iter_bytes():
        buf += chunk
        if len(buf) >= EXCERPT_BYTES:
            break
    return buf[:EXCERPT_BYTES].decode("utf-8", errors="replace")


def _since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _describe(e: httpx.HTTPError) -> str:
    return f"{type(e).__name__}: {e}"[:500]
