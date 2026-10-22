"""HTTP 入口。

运行：uvicorn notifier.app:create_app --factory
"""

import hmac
import logging
import sqlite3

from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from notifier import store, views
from notifier.config import Settings, load_settings
from notifier.delivery import Clock, system_clock
from notifier.models import SubmitRequest, request_hash
from notifier.netguard import BlockedTarget, Resolver, check_target, default_resolver

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None, resolver: Resolver = default_resolver, clock: Clock = system_clock
) -> FastAPI:
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

    def not_found() -> HTTPException:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="通知不存在")

    @app.get("/notifications/{notification_id}")
    def query(notification_id: str, caller: str = Depends(authenticate)) -> dict:
        conn = store.connect(settings.db_path)
        try:
            n = store.get_for_caller(conn, notification_id, caller)
            if n is None:
                raise not_found()
            return views.notification_view(n, store.list_attempts(conn, n.id))
        finally:
            conn.close()

    @app.post("/notifications/{notification_id}/replay")
    def replay(notification_id: str, caller: str = Depends(authenticate)) -> JSONResponse:
        conn = store.connect(settings.db_path)
        try:
            result = store.replay(conn, notification_id, caller, clock())
        finally:
            conn.close()
        if result == store.NOT_FOUND:
            raise not_found()
        if result == store.NOT_REPLAYABLE:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="只有失败终态、且未过期的通知可以重放",
            )
        log.info("重放 id=%s caller=%s", notification_id, caller)
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content={"id": notification_id, "status": "pending"})

    @app.get("/metrics")
    def metrics() -> PlainTextResponse:
        conn = store.connect(settings.db_path)
        try:
            text = views.prometheus_text(store.stats(conn, clock()))
        finally:
            conn.close()
        return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")

    return app
