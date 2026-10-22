from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from notifier.models import MAX_BODY_BYTES, SubmitRequest, request_hash


def _valid(**overrides):
    data = dict(
        url="https://api.example.com/hook",
        method="POST",
        headers={"Content-Type": "application/json"},
        body='{"a": 1}',
        idempotency_key="evt-1",
    )
    data.update(overrides)
    return data


def test_valid_request():
    req = SubmitRequest(**_valid(method="post"))
    assert req.method == "POST"


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"url": "ftp://example.com/x"}, id="ftp-scheme"),
        pytest.param({"url": "https:///no-host"}, id="no-host"),
        pytest.param({"url": "not a url"}, id="no-scheme"),
        pytest.param({"method": "TRACE"}, id="method-trace"),
        pytest.param({"headers": {"Host": "evil"}}, id="header-host"),
        pytest.param({"headers": {"content-length": "1"}}, id="header-content-length"),
        pytest.param({"headers": {"Transfer-Encoding": "chunked"}}, id="header-te"),
        pytest.param({"headers": {"Connection": "close"}}, id="header-connection"),
        pytest.param({"headers": {"X-N": 1}}, id="header-non-string"),
        pytest.param({"body": "x" * (MAX_BODY_BYTES + 1)}, id="body-too-large"),
        pytest.param({"body": "汉" * (MAX_BODY_BYTES // 3 + 1)}, id="body-too-large-utf8"),
        pytest.param({"body": {"a": 1}}, id="body-not-text"),
        pytest.param({"expires_at": "2020-01-01T00:00:00Z"}, id="expired"),
        pytest.param({"expires_at": "2099-01-01T00:00:00"}, id="expiry-naive"),
        pytest.param({"idempotency_key": ""}, id="empty-key"),
        pytest.param({"extra": 1}, id="unknown-field"),
    ],
)
def test_invalid_requests_rejected(overrides):
    with pytest.raises(ValidationError):
        SubmitRequest(**_valid(**overrides))


def test_missing_idempotency_key_rejected():
    data = _valid()
    del data["idempotency_key"]
    with pytest.raises(ValidationError):
        SubmitRequest(**data)


def test_body_at_limit_accepted():
    SubmitRequest(**_valid(body="x" * MAX_BODY_BYTES))


def test_hash_ignores_header_order():
    a = SubmitRequest(**_valid(headers={"A": "1", "B": "2"}))
    b = SubmitRequest(**_valid(headers={"B": "2", "A": "1"}))
    assert request_hash(a) == request_hash(b)


def test_hash_differs_on_body():
    assert request_hash(SubmitRequest(**_valid(body="x"))) != request_hash(SubmitRequest(**_valid(body="y")))


def test_hash_same_instant_different_timezone():
    t = datetime(2099, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
    a = SubmitRequest(**_valid(expires_at=t))
    b = SubmitRequest(**_valid(expires_at=t.astimezone(timezone.utc)))
    assert request_hash(a) == request_hash(b)


def test_hash_ignores_idempotency_key():
    # 指纹只比较请求内容，key 本身是去重的维度
    assert request_hash(SubmitRequest(**_valid(idempotency_key="a"))) == request_hash(
        SubmitRequest(**_valid(idempotency_key="b"))
    )
