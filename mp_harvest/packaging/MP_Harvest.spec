# -*- mode: python ; coding: utf-8 -*-
"""MP Harvest macOS 打包（PyInstaller spec，2026-08-09）。

构建：
    cd <项目根>
    .venv/bin/python -m PyInstaller --noconfirm --clean \\
        mp_harvest/packaging/MP_Harvest.spec \\
        --distpath /tmp/mp_build/dist --workpath /tmp/mp_build/work

产物：<distpath>/MP Harvest.app（未签名；分发需 zip，Gatekeeper 提示时右键打开）。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# 与 Windows 版共用的清单（datas / 整包收集 / 隐式依赖），只写一份 —— 见该模块的
# docstring：v2.0.4/v2.0.5 就是因为 mac spec 漏打 core/templates 导致导出全挂。
sys.path.insert(0, str(Path(SPEC).resolve().parent))
from build_common import (  # noqa: E402
    COLLECT_ALL_PKGS,
    COMMON_HIDDENIMPORTS,
    MAC_HIDDENIMPORTS,
    package_datas,
    version_from_source,
)

ROOT = Path(SPEC).resolve().parents[1]  # mp_harvest/ 包目录
PKG_ROOT = ROOT.parent                  # 仓库根（mp_harvest 的父目录）
FRONTEND = ROOT / "frontend"
ICON = str(ROOT / "packaging" / "mp_harvest.icns")

# Info.plist 版本号与 base.py 的 APP_VERSION 保持一致，避免 Finder 显示旧版本
BUNDLE_VERSION = version_from_source(ROOT)

datas = package_datas(ROOT)
binaries = []
hiddenimports = collect_submodules("mp_harvest")

# 重型/动态导入包：整包收集（含数据与子模块）
for pkg in COLLECT_ALL_PKGS:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:  # noqa: BLE001
        pass

hiddenimports += list(MAC_HIDDENIMPORTS) + list(COMMON_HIDDENIMPORTS)

a = Analysis(
    [str(ROOT / "__main__.py")],
    pathex=[str(PKG_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MP Harvest",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="MP Harvest",
)

app = BUNDLE(
    coll,
    name="MP Harvest.app",
    icon=ICON,
    bundle_identifier="com.shirainbown.mp-harvest",
    info_plist={
        "CFBundleName": "MP Harvest",
        "CFBundleDisplayName": "MP Harvest",
        "CFBundleShortVersionString": BUNDLE_VERSION,
        "CFBundleVersion": BUNDLE_VERSION,
        "NSHighResolutionCapable": True,
    },
)
