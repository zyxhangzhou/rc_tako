"""HTTP 入口。

运行：uvicorn notifier.app:create_app --factory
"""

import hmac
import logging
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from notifier import store
from notifier.config import Settings, load_settings
from notifier.models import SubmitRequest, request_hash
from notifier.netguard import BlockedTarget, Resolver, check_target, default_resolver

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, resolver: Resolver = default_resolver) -> FastAPI:
    settings = load_settings() if settings is None else settings
    store.init_schema(settings.db_path)

    app = FastAPI(title="notifier")
    bearer = HTTPBearer(auto_error=False)

    def authenticate(cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> str:
        if cred is not None:
            for token, caller in settings.tokens.items():
                if hmac.compare_digest(cred.credentials.encode(), token.encode()):
                    return caller
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少或无效的调用方 token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # 同步端点：FastAPI 放进线程池执行，sqlite3 和 DNS 解析不会阻塞事件循环
    @app.post("/notifications")
    def submit(req: SubmitRequest, caller: str = Depends(authenticate)) -> JSONResponse:
        try:
            check_target(req.url, resolver=resolver)
        except BlockedTarget as e:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(e)) from None

        fingerprint = request_hash(req)
        new = store.NewNotification(
            caller=caller,
            idempotency_key=req.idempotency_key,
            request_hash=fingerprint,
            url=req.url,
            method=req.method,
            headers=req.headers,
            body=req.body,
            expires_at=req.expires_at,
        )
        try:
            conn = store.connect(settings.db_path)
            try:
                result = store.insert_or_get(conn, new)
            finally:
                conn.close()
        except sqlite3.Error:
            # 没有确认落盘就不能回成功；调用方带同一个 key 重试不会产生重复
            log.exception("持久化失败 caller=%s key=%s", caller, req.idempotency_key)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="暂时无法受理，请重试") from None

        if result.created:
            return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"id": result.id})
        if result.request_hash == fingerprint:
            return JSONResponse(status_code=status.HTTP_200_OK, content={"id": result.id})
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"幂等 key 已用于内容不同的请求（通知 {result.id}）",
        )

    return app
