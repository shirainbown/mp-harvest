# -*- mode: python ; coding: utf-8 -*-
"""MP Harvest macOS 打包（PyInstaller spec，2026-08-09）。

构建：
    cd <项目根>
    .venv/bin/python -m PyInstaller --noconfirm --clean \\
        mp_harvest/packaging/MP_Harvest.spec \\
        --distpath /tmp/mp_build/dist --workpath /tmp/mp_build/work

产物：<distpath>/MP Harvest.app（未签名；分发需 zip，Gatekeeper 提示时右键打开）。
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

ROOT = Path(SPEC).resolve().parents[1]  # mp_harvest/ 包目录
PKG_ROOT = ROOT.parent                  # 仓库根（mp_harvest 的父目录）
FRONTEND = ROOT / "frontend"
ICON = str(ROOT / "packaging" / "mp_harvest.icns")

datas = [
    (str(FRONTEND / "dist"), "frontend/dist"),
    (str(FRONTEND / "public" / "icon.png"), "frontend/public"),
]
binaries = []
hiddenimports = collect_submodules("mp_harvest")

# 重型/动态导入包：整包收集（含数据与子模块）
for pkg in (
    "mitmproxy",
    "pywebview",
    "uvicorn",
    "fastapi",
    "starlette",
    "websockets",
    "wsproto",
    "h11",
    "h2",
    "jinja2",
    "multipart",
):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:  # noqa: BLE001
        pass

hiddenimports += [
    "brotli",
    "certifi",
    "sortedcontainers",
    "msgpack",
    "cryptography",
    "lxml",
    "bs4",
    "requests",
    "pyasn1",
    "cffi",
    "kaitaistruct",
    "ruamel.yaml",
    "zstandard",
    "mitmproxy_rs",
]

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
        "CFBundleShortVersionString": "2.0.2",
        "CFBundleVersion": "2.0.2",
        "NSHighResolutionCapable": True,
    },
)
