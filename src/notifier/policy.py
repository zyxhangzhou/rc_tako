"""投递策略：结果分类、退避、截止时间。全部是纯函数，时间统一用 UTC 毫秒。"""

import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from notifier import store
from notifier.config import WorkerSettings

SUCCESS = "success"
PERMANENT = "permanent"
TEMPORARY = "temporary"

JITTER = 0.2
RETRY_AFTER_MIN_S = 1.0


def classify_status(code: int) -> str:
    if 200 <= code < 300:
        return SUCCESS
    if code in (408, 429) or code >= 500:
        return TEMPORARY
    # 3xx（不跟随重定向）与其余 4xx：重试也不会成功
    return PERMANENT


def parse_retry_after(value: str | None, now_ms: int) -> float | None:
    """返回秒数；支持秒数和 HTTP-date 两种格式，无法解析时返回 None。"""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (store.to_ms(dt) - now_ms) / 1000


def backoff_delay_s(n_failures: int, cfg: WorkerSettings, rng: random.Random) -> float:
    base = min(cfg.backoff_base_s * 2 ** (n_failures - 1), cfg.backoff_cap_s)
    return min(base * rng.uniform(1 - JITTER, 1 + JITTER), cfg.backoff_cap_s)


def deadline(window_start_ms: int, expires_at: datetime | None, cfg: WorkerSettings) -> tuple[int, str]:
    """有效截止时间 = min(重试窗口起点 + 最大重试时长, expires_at)，并返回届时的失败原因。

    重试窗口起点是受理时间，或最近一次人工重放的时间。

    两者同时到时按已过期处理：业务约束优先于我方的重试预算。
    """
    retry_deadline = window_start_ms + int(cfg.max_retry_duration_s * 1000)
    if expires_at is not None and store.to_ms(expires_at) <= retry_deadline:
        return store.to_ms(expires_at), store.REASON_EXPIRED
    return retry_deadline, store.REASON_RETRIES_EXHAUSTED


def next_attempt_ms(
    n_failures: int,
    now_ms: int,
    retry_after_s: float | None,
    deadline_ms: int,
    cfg: WorkerSettings,
    rng: random.Random,
) -> int:
    """按正常规则算下一次重试时间；来不及时返回截止时间，届时由发送前检查置为失败。

    不为"最后再试一次"做任何提前或压缩（见 design D6）。
    """
    if retry_after_s is not None:
        delay = min(max(retry_after_s, RETRY_AFTER_MIN_S), cfg.backoff_cap_s)
    else:
        delay = backoff_delay_s(n_failures, cfg, rng)
    return min(now_ms + int(delay * 1000), deadline_ms)
