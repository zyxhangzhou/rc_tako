"""对外展示：查询结果的 JSON 与 Prometheus 指标文本。"""

from notifier import store

# 白名单内的 Header 显示原值，其余只保留名字（isolation-ops design D3）
VISIBLE_HEADERS = frozenset({"content-type", "accept", "user-agent", "content-encoding", "accept-language"})
REDACTED = "***"

STATUSES = (store.STATUS_PENDING, store.STATUS_DELIVERING, store.STATUS_DELIVERED, store.STATUS_FAILED)
REASONS = (
    store.REASON_PERMANENT_ERROR,
    store.REASON_RETRIES_EXHAUSTED,
    store.REASON_EXPIRED,
    store.REASON_BLOCKED_TARGET,
)
OUTCOMES = ("success", "permanent", "temporary")


def redact_headers(headers: dict[str, str]) -> dict[str, str]:
    return {k: (v if k.lower() in VISIBLE_HEADERS else REDACTED) for k, v in headers.items()}


def _iso(dt):
    return None if dt is None else dt.isoformat()


def notification_view(n: store.Notification, attempts: list[store.AttemptRecord]) -> dict:
    return {
        "id": n.id,
        "status": n.status,
        "failure_reason": n.failure_reason,
        "attempt_count": n.attempt_count,
        "created_at": _iso(n.created_at),
        "expires_at": _iso(n.expires_at),
        "finished_at": _iso(n.finished_at),
        "next_attempt_at": _iso(store.from_ms(n.next_attempt_at)) if n.status == store.STATUS_PENDING else None,
        "request": {
            "url": n.url,
            "method": n.method,
            "headers": redact_headers(n.headers),
            "body": n.body,
        },
        "attempts": [
            {
                "attempt_no": a.attempt_no,
                "started_at": _iso(a.started_at),
                "duration_ms": a.duration_ms,
                "outcome": a.outcome,
                "status_code": a.status_code,
                "error": a.error,
                "response_excerpt": a.response_excerpt,
            }
            for a in attempts
        ],
    }


def prometheus_text(s: store.Stats) -> str:
    lines: list[str] = []

    def metric(name, kind, help_text, samples):
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            label_str = "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}" if labels else ""
            lines.append(f"{name}{label_str} {value}")

    metric("notifier_notifications", "gauge", "Notifications by status.",
           [({"status": st}, s.by_status.get(st, 0)) for st in STATUSES])
    metric("notifier_failed_notifications", "gauge", "Failed notifications by reason.",
           [({"reason": r}, s.failed_by_reason.get(r, 0)) for r in REASONS])
    metric("notifier_backlog_due", "gauge", "Pending notifications that are due but not yet claimed.",
           [({}, s.backlog_due)])
    metric("notifier_oldest_due_age_seconds", "gauge", "Seconds the oldest due pending notification has waited.",
           [({}, round(s.oldest_due_age_s, 3))])
    metric("notifier_delivery_attempts_total", "counter", "Delivery attempts by outcome.",
           [({"outcome": o}, s.attempts_by_outcome.get(o, 0)) for o in OUTCOMES])
    return "\n".join(lines) + "\n"
