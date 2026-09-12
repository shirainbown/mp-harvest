"""几个前端行为用例共用的 node / esbuild 定位（2026-09 Windows 适配）。

**为什么不能只判 ``node_modules/.bin/esbuild`` 存不存在。** npm 在 Windows 上装的
是 ``esbuild.cmd``（外加一个给 Git Bash 用的无扩展名 shell 脚本）。那个无扩展名的
文件在 Windows 上**存在但不可执行** —— 于是「没有 esbuild 就跳过」的 skipif 不生效，
用例真跑起来，``subprocess.run(["…/.bin/esbuild", …])`` 以 ``WinError 193``
（%1 不是有效的 Win32 应用程序）炸掉。

所以：路径按平台挑，且要能被 ``shutil.which`` 在 Windows 上解析（``.cmd`` 靠
PATHEXT 解析得到）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"


def esbuild_bin() -> str | None:
    """可直接执行的 esbuild 路径；本机没有（没装前端依赖）时返回 None。"""
    if shutil.which("node") is None:
        return None
    root = FRONTEND / "node_modules" / ".bin"
    for name in ("esbuild.cmd", "esbuild.exe", "esbuild"):
        cand = root / name
        if cand.exists():
            return str(cand)
    found = shutil.which("esbuild")
    return found or None


def node_available() -> bool:
    return shutil.which("node") is not None
