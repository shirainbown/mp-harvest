"""两个平台的 PyInstaller spec 共用的事实清单（2026-09，做 Windows 版时抽出）。

**为什么单独一个模块。** 这个仓库栽过一次：v2.0.4 / v2.0.5 的 mac spec 漏打了
``core/templates``，结果**导出功能全部失败**（``MP_Harvest.spec`` 里那段注释就是
那次事故的现场记录）。两份 spec 各写一遍 ``datas`` 必然重演，所以清单只留一份，
mac 与 Windows 的 spec 都 import 它。

⚠️ **``datas`` 里的目标路径字符串是契约，不要"顺手整理"。** 这些前缀是被运行时代码
按字面量探测的，改一个字符就是「功能看着实现了、但完全没生效」这种最难查的故障：

- ``frontend/dist`` —— ``infra/platform/paths.package_root()`` 逐个候选目录探测它；
- ``mp_harvest/core/templates`` —— ``core/article_reader._resolve_template_dir()``
  与 ``core/weekly_report._template_dir()`` 都探测 ``<root>/mp_harvest/core/templates``；
- ``frontend/public`` —— ``server/app.py`` 挂图标。

``tests/test_build_specs.py`` 会把这份清单与两份 spec 对着钉一遍。
"""

from __future__ import annotations

import re
from pathlib import Path

# 重型 / 动态导入包：整包收集（含数据与子模块）。
# pypdf 那条的注释见 mac spec：静态分析本来也能追到函数体内的导入，整包收集只是稳妥。
COLLECT_ALL_PKGS: tuple[str, ...] = (
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
    "certifi",
    "pypdf",
)

# 两个平台都要的隐式依赖（模块级导入分析看不到的那些）
COMMON_HIDDENIMPORTS: tuple[str, ...] = (
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
)

# macOS 专属：系统代理读取走 ``urllib.request.getproxies``，它依赖这个 C 扩展。
# 冻结后一旦漏打包，getproxies() 会**静默返回空** → 直连 GitHub →
# 「检查更新：无法连接 github」（2026-09 实测定位）。**不能带进 Windows 包。**
MAC_HIDDENIMPORTS: tuple[str, ...] = ("_scproxy",)

# Windows 专属：
# - tkinter/_tkinter：免责声明弹窗的兜底（主路径已经是 user32.MessageBoxW，
#   见 core/consent._ask_windows；带上是为了 --no-window 之类场景）；
# - clr/pythonnet：pywebview 的 EdgeChromium 后端靠它驱动 WinForms 宿主，
#   漏了会退回 IE11 → Vue 3 的包解析不了 → **一个纯白窗口，没有任何报错**。
WIN_HIDDENIMPORTS: tuple[str, ...] = (
    "tkinter",
    "_tkinter",
    "clr",
    "pythonnet",
    "webview.platforms.edgechromium",
)


def package_datas(pkg_root: Path) -> list[tuple[str, str]]:
    """``(源路径, 包内目标路径)``；**目标路径是契约**，见模块 docstring。"""
    frontend = pkg_root / "frontend"
    return [
        (str(frontend / "dist"), "frontend/dist"),
        (str(frontend / "public" / "icon.png"), "frontend/public"),
        (str(pkg_root / "core" / "templates"), "mp_harvest/core/templates"),
    ]


def version_from_source(pkg_root: Path) -> str:
    """从 ``infra/platform/base.py`` 的 ``APP_VERSION`` 取版本号。

    单一来源：三处版本号（package.json / config.ts / base.py）本就靠人工同步，
    这里至少不再让两个 spec 各解析一遍正则、还可能解析出不同结果。
    """
    base_py = (pkg_root / "infra" / "platform" / "base.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION = "([^"]+)"', base_py)
    return match.group(1) if match else "0.0.0"


def windows_version_file(pkg_root: Path, tmp_dir: Path) -> str:
    """生成 PyInstaller 的 Windows 版本资源文件，返回其路径。

    等价于 mac 侧 Info.plist 里的 ``CFBundleShortVersionString``：没有它，
    exe 属性页里的「文件版本」是空的，用户报障时说不清装的是哪一版。
    版本号取 ``APP_VERSION``，与另外两处保持一致。
    """
    version = version_from_source(pkg_root)
    parts = (version.split(".") + ["0", "0", "0"])[:4]
    # (major, minor, patch, build) 都要是整数
    nums = []
    for p in parts:
        try:
            nums.append(int(p))
        except ValueError:
            nums.append(0)
    quad = ", ".join(str(n) for n in nums)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    path = tmp_dir / "win_version_info.txt"
    path.write_text(
        "VSVersionInfo(\n"
        "  ffi=FixedFileInfo(\n"
        f"    filevers=({quad}),\n"
        f"    prodvers=({quad}),\n"
        "    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)\n"
        "  ),\n"
        "  kids=[\n"
        "    StringFileInfo([\n"
        "      StringTable('080404B0', [\n"
        "        StringStruct('CompanyName', 'MP Harvest'),\n"
        "        StringStruct('FileDescription', 'MP Harvest'),\n"
        "        StringStruct('FileVersion', '" + version + "'),\n"
        "        StringStruct('InternalName', 'MP Harvest'),\n"
        "        StringStruct('OriginalFilename', 'MP Harvest.exe'),\n"
        "        StringStruct('ProductName', 'MP Harvest'),\n"
        "        StringStruct('ProductVersion', '" + version + "'),\n"
        "      ])\n"
        "    ]),\n"
        "    VarFileInfo([VarStruct('Translation', [2052, 1200])])\n"
        "  ]\n"
        ")\n",
        encoding="utf-8",
    )
    return str(path)
