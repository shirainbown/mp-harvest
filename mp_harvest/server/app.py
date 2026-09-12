"""FastAPI 装配（设计稿 §3.5 / §7）。

- 所有 ``/api/*`` 与 ``/ws`` 校验启动 token（query ``token`` 或
  ``Authorization: Bearer``）；静态资源与 ``/`` 不校验（页面本身无数据）。
- 若 ``mp_harvest/frontend/dist`` 存在则挂载到 ``/``（生产模式，D2）。
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

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


def _check_local_data() -> None:
    """启动时体检本地数据（2026-09）。**绝不抛异常** —— 这是启动路径。

    做两件事：

    1. **清理历史更新包**：原先只增不减，实测一个用户的 data/ 里积了 16 个包
       共 806MB（其余全部数据加起来不到 9MB）。应用后的包没有任何用处。
    2. **数据文件缺失要出声**：`settings.json` / `weekly/prompts.json` 被删掉后
       会**静默回退默认值** —— 用户改过的打分标准、默认目录会无声消失，
       只会觉得「我明明改过」。这里在**事实成立**时记一笔，说清后果与恢复方式。

       判据带上「目录里还有别的东西」：全新安装同样没有这两个文件，那不是异常。
       对提示词用 `weekly/cache.json` 作证 —— 它每生成一期就会写，说明这目录用过。
    """
    from mp_harvest.infra.platform import paths as _paths

    try:
        from mp_harvest.core.event_log import log_event
    except Exception:  # noqa: BLE001
        return

    try:
        from mp_harvest.infra.platform import base as platform_base

        removed = platform_base.prune_update_packages()
        if removed:
            log_event("info", "storage", f"清理了 {removed} 个历史更新包（只保留最新一个）",
                      {"removed": removed})
    except Exception:  # noqa: BLE001
        pass

    data = _paths.data_dir()
    try:
        if not (data / "settings.json").is_file() and (data / "accounts.json").is_file():
            log_event(
                "warn", "storage",
                "设置文件缺失（data/settings.json），本次全部使用默认值 ——"
                "若你此前改过默认导出目录、周报标题、批大小等，需要重新设置。",
                {"missing": str(data / "settings.json")},
            )
        prompts = data / "weekly" / "prompts.json"
        if not prompts.is_file() and (data / "weekly" / "cache.json").is_file():
            log_event(
                "warn", "storage",
                "自定义提示词缺失（data/weekly/prompts.json），本次按内置默认标准打分 ——"
                "若你此前改过打分/解读/洞察标准，需要重新填写（周报页 → 提示词）。",
                {"missing": str(prompts)},
            )
    except Exception:  # noqa: BLE001
        pass


@asynccontextmanager
async def _lifespan(app: FastAPI):
    import asyncio
    from mp_harvest.server.credential_watcher import CredentialWatcher

    _check_local_data()
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

    @app.middleware("http")
    async def _log_actions(request, call_next):  # noqa: ANN001
        """把**写操作**记进执行日志（2026-09）。

        用中间件而不是逐个路由加埋点：一处覆盖全部用户动作（拉取 / 筛选 / 导出 /
        生成 / 保存设置…），以后新增的路由也自动在内。只记 POST/PUT/PATCH/DELETE ——
        GET 是浏览不是动作，记下来只会把日志淹掉。

        ⚠️ 查询串里带**启动 token**（`?token=…`），一律经 `event_log._redact`
        按 `token` 键名打码后才落库（有测试钉住）。
        """
        method = request.method.upper()
        if method not in ("POST", "PUT", "PATCH", "DELETE"):
            return await call_next(request)
        # 清空日志这个动作本身不记 —— 否则点完「清空」还剩一条，用户会以为按钮坏了
        # （本项目对「点了没反应 / 看着像坏了」有过多轮返工，这点直觉值得照顾）。
        if request.url.path == "/api/logs":
            return await call_next(request)
        started = time.time()

        def _write(level: str, message: str, status: int = 0, detail: str = "") -> None:
            try:
                from mp_harvest.core.event_log import log_event

                log_event(level, "action", message, {
                    "method": method,
                    "path": request.url.path,
                    "status": status,
                    "detail": detail,
                    "query": dict(request.query_params),
                    "elapsed_ms": int((time.time() - started) * 1000),
                })
            except Exception:  # noqa: BLE001
                pass

        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            _write("error", f"{method} {request.url.path} 抛异常：{exc}")
            raise
        if response.status_code < 400:
            _write("info", f"{method} {request.url.path} → {response.status_code}",
                   status=response.status_code)
            return response
        # 失败一定要带上**原因**（FastAPI 把原因放在 body 的 `detail` 里）——
        # 「POST → 400」这种记录对排查毫无帮助，用户来看的就是为什么。
        #
        # 但 `call_next` 给的是 `_StreamingResponse`（**没有 `.body`**），只能把
        # 迭代器读完再原样造一个响应回去。本应用没有流式 HTTP 响应；WebSocket
        # 不走 HTTP 中间件，不受影响。
        chunks: list[bytes] = []
        try:
            async for c in response.body_iterator:
                chunks.append(c if isinstance(c, bytes) else str(c).encode("utf-8"))
        except Exception:  # noqa: BLE001 —— 读不出来就只记状态码，别把响应弄坏
            pass
        raw = b"".join(chunks)
        detail = ""
        try:
            detail = str(json.loads(raw.decode("utf-8", "ignore")).get("detail") or "")
        except Exception:  # noqa: BLE001
            detail = ""
        _write("warn", f"{method} {request.url.path} → {response.status_code}"
                       + (f"：{detail}" if detail else ""),
               status=response.status_code, detail=detail)
        # 原样返回。只丢 content-length（body 换了、长度必须重算）；
        # **content-type 一定要留着** —— 流式包装的 `media_type` 是空的，
        # 一起丢掉的话客户端会收到一个没有类型的响应（有测试钉住）。
        headers = {
            k: v for k, v in response.headers.items() if k.lower() != "content-length"
        }
        return Response(content=raw, status_code=response.status_code, headers=headers)

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
