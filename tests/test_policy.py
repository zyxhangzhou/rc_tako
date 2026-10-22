import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from notifier import policy, store
from notifier.config import WorkerSettings

CFG = WorkerSettings(db_path=Path("x.db"))
T0 = datetime(2026, 10, 22, 10, 0, tzinfo=timezone.utc)
MS = store.to_ms


@pytest.mark.parametrize(
    "code,expected",
    [
        (200, policy.SUCCESS), (204, policy.SUCCESS), (299, policy.SUCCESS),
        (301, policy.PERMANENT), (302, policy.PERMANENT), (304, policy.PERMANENT),
        (400, policy.PERMANENT), (401, policy.PERMANENT), (404, policy.PERMANENT), (422, policy.PERMANENT),
        (408, policy.TEMPORARY), (429, policy.TEMPORARY),
        (500, policy.TEMPORARY), (502, policy.TEMPORARY), (503, policy.TEMPORARY), (599, policy.TEMPORARY),
    ],
)
def test_classify_status(code, expected):
    assert policy.classify_status(code) == expected


def test_parse_retry_after():
    now = MS(T0)
    assert policy.parse_retry_after("120", now) == 120
    assert policy.parse_retry_after("Thu, 22 Oct 2026 10:02:00 GMT", now) == 120
    assert policy.parse_retry_after("soon", now) is None
    assert policy.parse_retry_after(None, now) is None


def test_backoff_grows_then_caps():
    rng = random.Random(0)
    delays = [policy.backoff_delay_s(n, CFG, rng) for n in range(1, 15)]
    assert 8 <= delays[0] <= 12
    # 无抖动的基准翻倍，抖动 ±20% 下相邻两次仍然递增，直到上限
    for a, b in zip(delays[:7], delays[1:8]):
        assert b > a
    assert all(d <= CFG.backoff_cap_s for d in delays)
    assert delays[-1] >= CFG.backoff_cap_s * 0.8


def test_retry_after_used_and_clamped():
    rng = random.Random(0)
    far = MS(T0) + 10 * 86400_000
    assert policy.next_attempt_ms(1, MS(T0), 120, far, CFG, rng) == MS(T0) + 120_000
    assert policy.next_attempt_ms(1, MS(T0), 99999, far, CFG, rng) == MS(T0) + 3600_000
    assert policy.next_attempt_ms(1, MS(T0), -5, far, CFG, rng) == MS(T0) + 1000


def test_deadline_retry_budget_first():
    d, reason = policy.deadline(MS(T0), None, CFG)
    assert (d, reason) == (MS(T0 + timedelta(hours=24)), store.REASON_RETRIES_EXHAUSTED)
    d, reason = policy.deadline(MS(T0), T0 + timedelta(hours=30), CFG)
    assert reason == store.REASON_RETRIES_EXHAUSTED


def test_deadline_expiry_first_or_tie():
    d, reason = policy.deadline(MS(T0), T0 + timedelta(minutes=5), CFG)
    assert (d, reason) == (MS(T0 + timedelta(minutes=5)), store.REASON_EXPIRED)
    _, reason = policy.deadline(MS(T0), T0 + timedelta(hours=24), CFG)
    assert reason == store.REASON_EXPIRED


def test_candidate_example_next_retry_after_expiry():
    """候选人的例子：10:00 失败，正常算出 10:10，expires_at 10:05 → 等到 10:05，不提前标过期、不提前重试。"""
    created = T0 - timedelta(minutes=1)
    expires = T0 + timedelta(minutes=5)
    d, reason = policy.deadline(MS(created), expires, CFG)
    nxt = policy.next_attempt_ms(1, MS(T0), 600, d, CFG, random.Random(0))
    assert nxt == MS(expires)
    assert reason == store.REASON_EXPIRED


def test_normal_retry_not_pulled_earlier():
    d = MS(T0) + 3600_000
    nxt = policy.next_attempt_ms(1, MS(T0), 120, d, CFG, random.Random(0))
    assert nxt == MS(T0) + 120_000
