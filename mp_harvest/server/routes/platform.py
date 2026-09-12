"""平台能力：能力矩阵 + 用系统默认程序打开本地文件/目录。

为什么「打开本地路径」要走后端（2026-09 修复）
--------------------------------------------
前端原先用 ``openExternal('file://' + path)``，但 shell 侧的
``open_external`` 只放行 ``http(s)://``（见 ``shell/main.py``），
``file://`` 被挡下返回 False —— 而 ``desktop.ts`` 又把 bridge 调用
当成成功的（没有 await 那个 Promise），于是按钮**点得动、却什么都不发生**，
连个报错都没有。周报的「打开报告 / 打开目录 / 上次输出」和「其他来源」的
「打开本地正文 / 打开 PDF」五个入口全中。

正确做法是交给平台层已有的 ``shell_open``（macOS ``open`` / Windows
``os.startfile``）—— 它既能把目录交给 Finder（``webbrowser.open('file://目录')``
只会让浏览器列个目录），也会在失败时抛 ``PlatformError`` 让前端能提示。
``/api/ca/open`` 早就是这么做的，这里把它推广成通用入口。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from mp_harvest.infra.platform import get_platform
from mp_harvest.infra.platform.base import PlatformError

router = APIRouter(tags=["platform"])


@router.get("/api/platform")
def platform_info() -> dict:
    return get_platform().info()


class ShellOpenBody(BaseModel):
    path: str


@router.post("/api/shell/open")
def shell_open(body: ShellOpenBody) -> dict:
    """用系统默认方式打开一个本地文件或目录。

    路径由前端传入（来自服务端自己列出的周报输出目录 / 外部来源条目），
    端点本身在 token 鉴权之后，仅监听 127.0.0.1。这里只做「存在性」校验，
    为的是给出人话错误而不是 ``PlatformError`` 的原始文本。
    """
    raw = (body.path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="路径为空")
    # 前端传的可能是 file:// 形式（历史调用方），统一去掉前缀再落盘校验
    if raw.lower().startswith("file://"):
        raw = raw[7:]
    p = Path(raw).expanduser()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在：{p}")
    try:
        get_platform().shell_open(p)
    except PlatformError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "path": str(p), "is_dir": p.is_dir()}


@router.post("/api/shell/reveal")
def shell_reveal(body: ShellOpenBody) -> dict:
    """在文件管理器里**定位**到该文件/目录（不打开它本身）。

    与 ``/api/shell/open`` 分开而不是加一个 mode 参数：调用方要的东西不一样
    （「打开看看」vs「它在哪」），混成一个端点后前端每次都得想一遍传什么。
    校验与错误文案与 open 一致。
    """
    raw = (body.path or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="路径为空")
    if raw.lower().startswith("file://"):
        raw = raw[7:]
    p = Path(raw).expanduser()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在：{p}")
    try:
        get_platform().shell_reveal(p)
    except PlatformError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return {"ok": True, "path": str(p), "is_dir": p.is_dir()}


__all__ = ["router"]
