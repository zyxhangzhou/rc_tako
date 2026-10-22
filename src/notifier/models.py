"""提交请求的契约：字段校验与请求指纹。"""

import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictStr, field_validator

ALLOWED_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE", "GET"})
# 小写比较；这些 Header 由 HTTP 客户端负责，或属于 hop-by-hop
FORBIDDEN_HEADERS = frozenset({"host", "content-length", "transfer-encoding", "connection"})
MAX_BODY_BYTES = 1024 * 1024


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: StrictStr
    method: StrictStr
    headers: dict[StrictStr, StrictStr] = Field(default_factory=dict)
    body: StrictStr | None = None
    expires_at: AwareDatetime | None = None
    idempotency_key: StrictStr = Field(min_length=1, max_length=200)

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        parts = urlsplit(v)
        if parts.scheme.lower() not in ("http", "https"):
            raise ValueError("url scheme 只允许 http 或 https")
        if not parts.hostname:
            raise ValueError("url 必须带 host")
        return v

    @field_validator("method")
    @classmethod
    def _check_method(cls, v: str) -> str:
        m = v.upper()
        if m not in ALLOWED_METHODS:
            raise ValueError(f"method 只允许 {sorted(ALLOWED_METHODS)}")
        return m

    @field_validator("headers")
    @classmethod
    def _check_headers(cls, v: dict[str, str]) -> dict[str, str]:
        bad = sorted(k for k in v if k.lower() in FORBIDDEN_HEADERS)
        if bad:
            raise ValueError(f"不允许提交这些 Header: {bad}")
        return v

    @field_validator("body")
    @classmethod
    def _check_body(cls, v: str | None) -> str | None:
        if v is not None and len(v.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValueError("body 经 UTF-8 编码后不能超过 1MB")
        return v

    @field_validator("expires_at")
    @classmethod
    def _check_expiry(cls, v: datetime | None) -> datetime | None:
        if v is not None and v <= datetime.now(timezone.utc):
            raise ValueError("expires_at 必须晚于当前时间")
        return v


def request_hash(req: SubmitRequest) -> str:
    """同一幂等 key 下判断"是不是同一个请求"。键排序，所以 headers 顺序不影响结果。"""
    canonical = json.dumps(
        {
            "url": req.url,
            "method": req.method,
            "headers": req.headers,
            "body": req.body,
            "expires_at": None if req.expires_at is None else req.expires_at.astimezone(timezone.utc).isoformat(),
        },
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
