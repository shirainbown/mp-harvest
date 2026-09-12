# -*- mode: python ; coding: utf-8 -*-
"""MP Harvest Windows 打包（PyInstaller spec，2026-09）。

构建（**在仓库根执行**，与 mac 一致）：
    python -m PyInstaller --noconfirm --clean \\
        mp_harvest/packaging/MP_Harvest_win.spec \\
        --distpath dist --workpath build/work

产物：``<distpath>/MP Harvest/``（onedir：``MP Harvest.exe`` + ``_internal/``）。
分发时把**整个目录**打成 ``MP-Harvest-win-<版本>.zip``，用户解压后双击 exe 即用，
无需管理员、无需安装。CI 见 ``.github/workflows/build-windows.yml``。

为什么是 onedir 而不是 onefile：一是启动快（onefile 每次运行都要解压到临时目录），
二是应用内更新（``infra/platform/win.WinUpdater``）假定「exe 与 ``_internal`` 在同一层、
可以整目录覆盖」，onefile 下这个模型不成立。mac 侧的 .app 也是同理。
"""

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules

# datas / 整包收集 / 隐式依赖与 mac 共用一份，见 build_common 的 docstring
sys.path.insert(0, str(Path(SPEC).resolve().parent))
from build_common import (  # noqa: E402
    COLLECT_ALL_PKGS,
    COMMON_HIDDENIMPORTS,
    WIN_HIDDENIMPORTS,
    package_datas,
    version_from_source,
    windows_version_file,
)

ROOT = Path(SPEC).resolve().parents[1]  # mp_harvest/ 包目录
PKG_ROOT = ROOT.parent                  # 仓库根
ICON = str(ROOT / "packaging" / "mp_harvest.ico")
BUNDLE_VERSION = version_from_source(ROOT)

datas = package_datas(ROOT)
binaries = []
hiddenimports = collect_submodules("mp_harvest")

for pkg in COLLECT_ALL_PKGS:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception:  # noqa: BLE001
        pass

hiddenimports += list(WIN_HIDDENIMPORTS) + list(COMMON_HIDDENIMPORTS)

# Windows 版本资源：exe 属性页里的「文件版本」。没有它用户报障时说不清装的是哪一版。
# 需要 pefile —— 它在 Windows 上是 PyInstaller 的依赖，会随 pip install pyinstaller 装好。
VERSION_FILE = windows_version_file(ROOT, PKG_ROOT / "build" / "win_version")

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
    # 无控制台窗口（GUI 程序）。注意：这会让 print/异常都无处可去 ——
    # 所以诊断信息走 core/event_log 写文件，启动自检走 --self-check 的 JSON 输出。
    console=False,
    icon=ICON,
    version=VERSION_FILE,
    # **不要**设成 True：webview 初始化失败（比如缺 WebView2 运行时）时，
    # 那唯一一个「弹出来告诉你哪里错了」的窗口就靠它。设成 True 会只剩一个白窗口。
    disable_windowed_traceback=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    # PyInstaller 6 的 onedir 布局：exe 在外层，其余全在 _internal/。
    # paths.package_root() 的 _MEIPASS → _internal 探测就是照着它写的，别改。
    contents_directory="_internal",
    name="MP Harvest",
)
