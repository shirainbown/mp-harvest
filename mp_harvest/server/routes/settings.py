"""网络设置 + 代理连通测试（设计稿 §7.1）。

对应旧模块：settings。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from mp_harvest.server.schemas import TestProxyIn

router = APIRouter(tags=["settings"])


@router.get("/api/settings")
def get_settings() -> dict:
    from mp_harvest.core import settings as settings_mod

    return {"settings": settings_mod.load_settings()}


@router.put("/api/settings")
def put_settings(body: dict[str, Any]) -> dict:
    from mp_harvest.core import settings as settings_mod

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="settings 必须是 JSON 对象")
    settings_mod.save_settings(None, body)
    return {"ok": True}


@router.post("/api/settings/test-proxy")
def test_proxy(body: TestProxyIn) -> dict:
    """对代理地址做 TCP 连通测试（毫秒~秒级，同步返回）。"""
    import socket
    from urllib.parse import urlparse

    proxy = (body.proxy or "").strip()
    if not proxy:
        from mp_harvest.core import settings as settings_mod

        proxy = str(settings_mod.load_settings().get("proxy") or "").strip()
    if not proxy:
        raise HTTPException(status_code=400, detail="未配置代理地址")
    parsed = urlparse(proxy if "://" in proxy else f"http://{proxy}")
    host, port = parsed.hostname, parsed.port
    if not host or not port:
        raise HTTPException(status_code=400, detail=f"代理地址无法解析：{proxy}")
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError as exc:
        return {"ok": False, "message": f"代理不可达 {host}:{port}：{exc}"}
    return {"ok": True, "message": f"代理可达 {host}:{port}"}
