"""FastAPI 装配（设计稿 §3.5 / §7）。

- 所有 ``/api/*`` 与 ``/ws`` 校验启动 token（query ``token`` 或
  ``Authorization: Bearer``）；静态资源与 ``/`` 不校验（页面本身无数据）。
- 若 ``mp_harvest/frontend/dist`` 存在则挂载到 ``/``（生产模式，D2）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from mp_harvest.infra.platform import paths
from mp_harvest.server import get_token
from mp_harvest.server.routes import ALL_ROUTERS
from mp_harvest.server.ws import hub
from mp_harvest.server.ws import router as ws_router


def _extract_token(scope: dict) -> str:
    qs = parse_qs((scope.get("query_string") or b"").decode("utf-8", "ignore"))
    if qs.get("token"):
        return qs["token"][0]
    for name, value in scope.get("headers") or []:
        if name.lower() == b"authorization":
            text = value.decode("utf-8", "ignore")
            if text.lower().startswith("bearer "):
                return text[7:].strip()
    return ""


class TokenAuthMiddleware:
    """纯 ASGI 中间件：http 与 websocket scope 都校验 /api/* 与 /ws。"""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] in ("http", "websocket"):
            path: str = scope.get("path") or ""
            if path.startswith("/api") or path == "/ws":
                if _extract_token(scope) != get_token():
                    if scope["type"] == "websocket":
                        await send({"type": "websocket.close", "code": 4401})
                    else:
                        response = JSONResponse(
                            status_code=401, content={"detail": "无效的访问 token"}
                        )
                        await response(scope, receive, send)
                    return
        await self.app(scope, receive, send)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    import asyncio
    from mp_harvest.server.credential_watcher import CredentialWatcher

    hub.bind_loop(asyncio.get_running_loop())
    watcher = CredentialWatcher()
    watcher.start()
    try:
        yield
    finally:
        watcher.stop()


def create_app() -> FastAPI:
    app = FastAPI(
        title="MP Harvest", version="2.0.0", docs_url=None, redoc_url=None, lifespan=_lifespan
    )

    @app.exception_handler(Exception)
    async def _unhandled(request, exc):  # noqa: ANN001
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    for r in ALL_ROUTERS:
        app.include_router(r)
    app.include_router(ws_router)

    dist = paths.package_root() / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="frontend")
    else:

        @app.get("/")
        def _index() -> dict:
            return {
                "app": "MP Harvest",
                "version": "2.0.0",
                "hint": "frontend/dist 不存在；开发模式请用 --dev 指向 Vite dev server",
            }

    app.add_middleware(TokenAuthMiddleware)
    return app


__all__ = ["create_app", "TokenAuthMiddleware"]
