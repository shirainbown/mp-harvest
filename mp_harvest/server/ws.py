"""WebSocket 广播中心（设计稿 §7.2）。

- 事件循环运行在 uvicorn 线程；任务池 / mitm 线程通过
  :func:`broadcast_event` 线程安全地推送事件。
- 事件格式：``{"type": <str>, ...payload}``
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()


class BroadcastHub:
    """连接集合 + 线程安全广播。"""

    def __init__(self) -> None:
        self._conns: set[WebSocket] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """记录 uvicorn 事件循环（app startup 时调用）。"""
        self._loop = loop or asyncio.get_event_loop()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        with self._lock:
            self._conns.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        with self._lock:
            self._conns.discard(ws)

    @property
    def connection_count(self) -> int:
        with self._lock:
            return len(self._conns)

    async def _send_all(self, text: str) -> None:
        with self._lock:
            conns = list(self._conns)
        dead: list[WebSocket] = []
        for ws in conns:
            try:
                await ws.send_text(text)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        if dead:
            with self._lock:
                for ws in dead:
                    self._conns.discard(ws)

    def broadcast(self, message: dict[str, Any]) -> None:
        """任意线程调用；无连接/无 loop 时安全丢弃。"""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        if self.connection_count == 0:
            return
        text = json.dumps(message, ensure_ascii=False, default=str)
        asyncio.run_coroutine_threadsafe(self._send_all(text), loop)


hub = BroadcastHub()


def broadcast_event(event_type: str, payload: dict[str, Any] | None = None) -> None:
    """供任务注册表 / mitm 回调 / 剪贴板监听使用的事件入口（§7.2）。"""
    message: dict[str, Any] = {"type": event_type}
    if payload:
        message.update(payload)
    hub.broadcast(message)


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket) -> None:
    # token 校验由 ASGI 中间件完成（见 app.py）
    await hub.connect(ws)
    try:
        while True:
            # 客户端目前只收不发；收包仅为检测断开
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        hub.disconnect(ws)
