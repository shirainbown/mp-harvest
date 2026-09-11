"""应用设置（含网络代理）+ 代理连通测试（设计稿 §7.1）。

对应旧模块：settings。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from mp_harvest.server.schemas import TestProxyIn

router = APIRouter(tags=["settings"])

# 设置页契约（扁平 KV，点号分隔命名空间）。缺省值由后端补全；
# 导出路由读取 "export.default_dir" / "export.download_images" 作为缺省。
SETTING_DEFAULTS: dict[str, Any] = {
    "export.default_dir": str(Path.home() / "Downloads" / "mp-harvest-export"),
    "export.download_images": False,
    "ai.batch_size": 50,
    "ai.workers": 4,
    "ai.continue_content_filter": True,
}

# 已知设置项的声明类型（PUT 校验用；bool 判定须先于 int，因 bool 是 int 子类）
_SETTING_TYPES: dict[str, type] = {
    "export.default_dir": str,
    "export.download_images": bool,
    "ai.batch_size": int,
    "ai.workers": int,
    "ai.continue_content_filter": bool,
}


def _validate_settings(body: dict[str, Any]) -> None:
    for key, value in body.items():
        if not isinstance(key, str) or not key.strip():
            raise HTTPException(status_code=400, detail="设置项 key 必须是非空字符串")
        expected = _SETTING_TYPES.get(key)
        if expected is bool:
            ok = isinstance(value, bool)
        elif expected is int:
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif expected is str:
            ok = isinstance(value, str)
        else:  # 未知设置项：仅接受标量，避免把对象/数组写进 settings.json
            ok = isinstance(value, (str, int, float, bool))
        if not ok:
            typename = {bool: "bool", int: "int", str: "str"}.get(expected, "标量(bool/int/str)")
            raise HTTPException(status_code=400, detail=f"设置项 {key} 的值类型错误，应为 {typename}")


def _normalize(value: Any) -> Any:
    """导出目录统一存展开 ~ 后的绝对路径字符串。

    空值保持为空（2026-09 修复）：``os.path.abspath("")`` 会返回**进程 CWD**，
    于是用户在设置页清空目录再失焦，就会把应用启动目录悄悄存成「默认导出目录」，
    之后导出对话框里预填的就是它。
    """
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return ""
    return os.path.abspath(os.path.expanduser(text))


@router.get("/api/settings")
def get_settings() -> dict:
    from mp_harvest.core import settings as settings_mod

    # 附带后端探测到的系统代理：mode=system 时如果这里是空的，
    # 说明系统没配代理（界面会给提示），便于排查「跟随了但连不上」
    from mp_harvest.infra.platform.base import _system_proxy

    try:
        detected = _system_proxy()
    except Exception:  # noqa: BLE001
        detected = ""
    return {
        "settings": {**SETTING_DEFAULTS, **settings_mod.load_settings()},
        "system_proxy": detected,
    }


@router.put("/api/settings")
def put_settings(body: dict[str, Any]) -> dict:
    from mp_harvest.core import settings as settings_mod

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="settings 必须是 JSON 对象")
    _validate_settings(body)
    merged = {**settings_mod.load_settings(), **body}
    merged["export.default_dir"] = _normalize(merged.get("export.default_dir"))
    settings_mod.save_settings(None, merged)
    return {"ok": True, "settings": {**SETTING_DEFAULTS, **merged}}


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
    import time as _time

    t0 = _time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=5):
            pass
    except OSError as exc:
        return {"ok": False, "message": f"代理不可达 {host}:{port}：{exc}"}
    # 返回实测延迟：前端一直显示「连接成功 · 延迟 ?ms」（路由没给这个字段，
    # 2026-09 修复）
    latency_ms = int((_time.perf_counter() - t0) * 1000)
    return {"ok": True, "latency_ms": latency_ms, "message": f"代理可达 {host}:{port}"}
