"""数据目录与应用根目录解析（设计稿 §3.4 / §4）。

- 开发模式：包内 ``mp_harvest/data/``（gitignored）；
- 冻结（PyInstaller）模式：按平台规范放置
  - Windows: ``%APPDATA%\\MP Harvest\\data``
  - macOS:   ``~/Library/Application Support/MP Harvest/data``
  - 其他:    ``~/.local/share/MP Harvest/data``
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "MP Harvest"


def is_frozen() -> bool:
    """是否为 PyInstaller 冻结产物。"""
    return bool(getattr(sys, "frozen", False))


def package_root() -> Path:
    """``mp_harvest`` 包目录（开发模式下的项目根）。

    冻结（PyInstaller 6 onedir .app）时资源可能平铺在 ``Contents/Resources`` 下，
    也可能放在 ``_internal/`` 子目录：依次探测，保证 ``frontend/dist`` 能被找到。
    """
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            root = Path(meipass)
            for cand in (root, root / "_internal"):
                if (cand / "frontend" / "dist").is_dir():
                    return cand
            return root
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def app_root() -> Path:
    """应用根目录：冻结时为可执行文件所在目录，否则为包目录。"""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return package_root()


def platform_data_root() -> Path:
    """冻结模式下按平台规范的用户数据根（不含 ``data`` 子目录）。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return root / APP_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    return Path.home() / ".local" / "share" / APP_NAME


def data_dir() -> Path:
    """统一数据目录（自动创建）。"""
    if is_frozen():
        root = platform_data_root() / "data"
    else:
        root = package_root() / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root
