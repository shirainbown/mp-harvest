"""mitm 代理启停 + CA 安装/状态（设计稿 §7.1）。

对应旧模块：mitm_capture、platform.ca。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from mp_harvest.infra.platform import get_platform
from mp_harvest.server import state
from mp_harvest.server.ws import broadcast_event

router = APIRouter(tags=["mitm"])


def _mitm_status_payload(svc) -> dict:
    port = getattr(svc, "port", None)
    if port is None:
        try:
            from mp_harvest.infra.platform.ca_setup import PROXY_PORT

            port = PROXY_PORT
        except Exception:  # noqa: BLE001
            port = 8088
    return {"running": bool(svc.running), "port": int(port)}


@router.get("/api/mitm/status")
def mitm_status() -> dict:
    """当前抓包代理状态（前端进入凭证页时拉取，之后靠 mitm.status WS 事件）。"""
    try:
        svc = state.get_mitm()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"抓包组件不可用：{exc}") from exc
    return _mitm_status_payload(svc)


@router.post("/api/mitm/start")
def mitm_start() -> dict:
    try:
        svc = state.get_mitm()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"抓包组件不可用：{exc}") from exc
    ok, msg = svc.start()
    broadcast_event("mitm.status", _mitm_status_payload(svc))
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    return {"ok": True, "message": msg, **_mitm_status_payload(svc)}


@router.post("/api/mitm/stop")
def mitm_stop() -> dict:
    try:
        svc = state.get_mitm()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"抓包组件不可用：{exc}") from exc
    ok, msg = svc.stop()
    broadcast_event("mitm.status", _mitm_status_payload(svc))
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    return {"ok": True, "message": msg, **_mitm_status_payload(svc)}


@router.post("/api/ca/install")
def ca_install() -> dict:
    """安装并信任 CA；返回是否需要管理员（前端据此提示，§4 权限 UX）。"""
    result = get_platform().ca.install()
    return result.to_dict()


@router.get("/api/ca/status")
def ca_status() -> dict:
    ca = get_platform().ca
    return {
        "installed": bool(ca.status()),
        "cert_path": str(ca.cert_path()),
        "needs_admin": ca.needs_admin,
    }
