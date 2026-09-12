"""MP Harvest 入口（设计稿 §3.2 线程模型）。

主线程 pywebview GUI（macOS Cocoa 强制）→ uvicorn 后台线程（127.0.0.1 固定
端口 8765 起、占用则递增探测、单 worker）→ 生成带一次性 token 的 URL → 开窗；
窗口关闭时清理
（停 mitm、关代理、任务池 shutdown、uvicorn 退出）。

用法：
    python -m mp_harvest.shell.main                    # 生产：加载 frontend/dist
    python -m mp_harvest.shell.main --dev http://localhost:5173   # Vite dev server
    python -m mp_harvest.shell.main --no-window        # 仅起服务，浏览器调试
    python -m mp_harvest.shell.main --hidden-titlebar  # mac hidden title bar
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from typing import Any


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="mp_harvest")
    p.add_argument("--dev", metavar="URL", default="", help="加载 Vite dev server URL")
    p.add_argument("--no-window", action="store_true", help="只起服务不开窗（浏览器调试）")
    p.add_argument(
        "--hidden-titlebar",
        action="store_true",
        help="macOS 使用无边框（hidden title bar）窗口",
    )
    p.add_argument(
        "--self-check",
        action="store_true",
        help="自检后退出：验证模块/frontend/dist/模板/平台层都就位（CI 用）",
    )
    p.add_argument(
        "--out",
        metavar="PATH",
        default="",
        help="与 --self-check 搭配：把自检结果写成 JSON 文件",
    )
    return p.parse_args(argv)


def self_check(out_path: str = "") -> int:
    """启动自检：证明这份**产物**真的能用，而不只是「装上了」。

    为什么需要它：开发机是 macOS，Windows 二进制只能靠 CI 与用户真机验证。这个开关
    由 ``.github/workflows/build-windows.yml`` 在真实 Windows runner 上对**构建出来
    的 exe** 执行，是唯一能自动发现「spec 漏打 datas / hiddenimports 缺了 / 平台分派
    错了」的手段 —— 这几类故障的共同表现都是「窗口打开了，但里面什么都没有」。

    三条纪律：

    1. **不做任何有副作用的操作。** 不 enable/disable 系统代理、不装 CA 信任
       （``recover_stale`` 也别碰）—— 这是在 CI runner 上跑的，改注册表是越界的。
       只读状态、只在临时目录里生成一次 CA。
    2. **结果同时写文件与 stdout。** 冻结版是 ``console=False`` 的 GUI 程序，
       从资源管理器双击起来时根本没有 stdout；CI 里也必须靠退出码 + 文件双保险。
    3. **任何一项不过就返回非零**，不是「打印完就算」。
    """
    import json
    import sys
    import tempfile
    from pathlib import Path

    checks: list[dict[str, Any]] = []
    info: dict[str, Any] = {}

    def check(name: str, ok: bool, detail: str = "") -> bool:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})
        return bool(ok)

    # ── 模块：打包漏一个就是一个功能整块消失 ──
    for mod in (
        "webview",
        "mitmproxy",
        "mitmproxy_rs",
        "cryptography",
        "fastapi",
        "uvicorn",
        "jinja2",
        "pypdf",
        "certifi",
        "bs4",
        "lxml",
    ):
        try:
            __import__(mod)
            check(f"import {mod}", True)
        except Exception as exc:  # noqa: BLE001
            check(f"import {mod}", False, f"{type(exc).__name__}: {exc}")

    # tkinter 只是免责声明弹窗的兜底（主路径是 user32.MessageBoxW / 系统弹窗），
    # 缺了不算致命，但要看得见 —— 免得又出现「弹窗失败被当成用户拒绝」那种事。
    try:
        __import__("tkinter")
        info["tkinter"] = True
    except Exception:  # noqa: BLE001
        info["tkinter"] = False

    # WebView2 后端单独确认：pywebview 在 Windows 上靠 pythonnet 驱动 WinForms 宿主，
    # 拿不到就退回 IE11 —— Vue 3 的产物在 IE11 上解析不了，结果是一个**纯白窗口**，
    # 没有任何报错信息（2026-09 复查时发现）。
    if sys.platform == "win32":
        try:
            __import__("clr")
            __import__("webview.platforms.edgechromium")
            check("EdgeChromium 后端（pythonnet/clr）", True)
        except Exception as exc:  # noqa: BLE001
            check("EdgeChromium 后端（pythonnet/clr）", False, f"{type(exc).__name__}: {exc}")

    # ── 路径：四个根各不相同，报障时全靠它们 ──
    from mp_harvest.infra.platform import paths

    info["frozen"] = paths.is_frozen()
    info["executable"] = sys.executable
    info["meipass"] = getattr(sys, "_MEIPASS", None)
    info["package_root"] = str(paths.package_root())
    info["app_root"] = str(paths.app_root())
    info["data_dir"] = str(paths.data_dir())
    info["sys_platform"] = sys.platform

    exe_dir = Path(sys.executable).resolve().parent
    if paths.is_frozen() and sys.platform == "win32":
        # onedir 布局：exe 与 _internal 同层。WinUpdater 靠这一点决定覆盖到哪。
        check("onedir 布局（exe 同层的 _internal/）", (exe_dir / "_internal").is_dir(),
              str(exe_dir))
        check("package_root 不等于 exe 目录（说明 _MEIPASS 生效）",
              paths.package_root() != exe_dir, str(paths.package_root()))

    dist = paths.package_root() / "frontend" / "dist"
    check("frontend/dist/index.html", (dist / "index.html").is_file(), str(dist))

    # 模板用**运行时真正在用的那个解析函数**去查，而不是照着清单拼路径 ——
    # 拼对了但函数找不到，打包就是无效的。
    from mp_harvest.core import article_reader, weekly_report

    tdir = article_reader._resolve_template_dir()
    check("导出模板 article.html", (tdir / "article.html").is_file(), str(tdir))
    wdir = weekly_report.resolve_template_dir()
    check("周报内置模板 weekly.html",
          (wdir / weekly_report.BUILTIN_TEMPLATE_NAME).is_file(), str(wdir))

    # ── 数据目录可写（%APPDATA% 在某些受管环境里是只读或重定向的）──
    try:
        probe = paths.data_dir() / ".self_check_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        check("数据目录可写", True, str(paths.data_dir()))
    except Exception as exc:  # noqa: BLE001
        check("数据目录可写", False, f"{type(exc).__name__}: {exc}")

    # ── 平台层：只读，绝不 enable/disable ──
    try:
        from mp_harvest.infra.platform import get_platform

        p = get_platform()
        info["platform"] = p.info()
        check("平台层分派", True, str(info["platform"].get("os")))
        # ca.status() 只读证书存储；顺带验证 certutil 能跑、输出能解码
        info["ca_trusted"] = bool(p.ca.status())
    except Exception as exc:  # noqa: BLE001
        check("平台层分派", False, f"{type(exc).__name__}: {exc}")

    # ── CA 生成链路（cryptography + mitmproxy 的 OpenSSL 栈），只在临时目录里做 ──
    try:
        with tempfile.TemporaryDirectory() as td:
            from mitmproxy import certs

            certs.CertStore.from_store(Path(td), basename="selfcheck", key_size=2048)
        check("CA 生成（mitmproxy CertStore）", True)
    except Exception as exc:  # noqa: BLE001
        check("CA 生成（mitmproxy CertStore）", False, f"{type(exc).__name__}: {exc}")

    if sys.platform == "win32":
        info["webview2"] = _webview2_runtime_version()
        check("WebView2 运行时已安装", bool(info["webview2"]), str(info["webview2"]))

    # ── HTTP：把整条装配（中间件 / 静态资源 / 路由）在进程内跑一遍 ──
    # token 是每进程随机的，外部拿不到，只有在进程内自测才可行。
    try:
        from fastapi.testclient import TestClient

        from mp_harvest.server import get_token
        from mp_harvest.server.app import create_app

        token = get_token()
        with TestClient(create_app()) as client:
            check("GET /（SPA 外壳）", client.get("/").status_code == 200)
            check(
                "GET /api/platform（带 token）",
                client.get("/api/platform", params={"token": token}).status_code == 200,
            )
            check(
                "无 token 必须 401",
                client.get("/api/platform").status_code == 401,
            )
    except Exception as exc:  # noqa: BLE001
        check("HTTP 装配", False, f"{type(exc).__name__}: {exc}")

    # uvicorn.Config 的 __init__ 会跑 configure_logging → sys.stdout.isatty()。
    # GUI 程序（console=False）里 stdout 是 None，这就是 v2.3.0 Windows 真机
    # 「exe 起不来」的根因 —— 而上面的 HTTP 装配走的是 TestClient，抓不到它。
    # 这里真的构造一次 Config，把这个崩溃钉在 CI 上（依赖 main() 开头的 _ensure_stdio）。
    try:
        import uvicorn

        uvicorn.Config(create_app(), host="127.0.0.1", port=0, log_level="warning")
        check("uvicorn 配置（无控制台环境）", True)
    except Exception as exc:  # noqa: BLE001
        check("uvicorn 配置（无控制台环境）", False, f"{type(exc).__name__}: {exc}")

    failed = [c for c in checks if not c["ok"]]
    payload = {
        "ok": not failed,
        "version": _app_version(),
        "info": info,
        "checks": checks,
        "failed": [c["name"] for c in failed],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out_path:
        try:
            Path(out_path).write_text(text, encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    # 冻结版从资源管理器双击起来时 sys.stdout 可能是 None —— 别在这儿炸
    try:
        if sys.stdout is not None:
            sys.stdout.write(text + "\n")
            sys.stdout.flush()
    except Exception:  # noqa: BLE001
        pass
    return 0 if not failed else 1


def _app_version() -> str:
    try:
        from mp_harvest.infra.platform.base import APP_VERSION

        return APP_VERSION
    except Exception:  # noqa: BLE001
        return "unknown"


def _webview2_runtime_version() -> str:
    """已安装的 WebView2 Evergreen 运行时版本号；没装返回空串。

    键值来自微软文档的固定 GUID（{F3017226-…}）。装在 HKLM（系统级）或 HKCU（用户级）
    两处之一，都查一遍。
    """
    try:
        import winreg  # type: ignore
    except ImportError:
        return ""
    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, path in (
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{guid}"),
    ):
        try:
            with winreg.OpenKey(root, path, 0, winreg.KEY_QUERY_VALUE) as key:
                value = str(winreg.QueryValueEx(key, "pv")[0] or "").strip()
                if value:
                    return value
        except Exception:  # noqa: BLE001
            continue
    return ""


DEFAULT_PORT = 8765
PORT_PROBE_LIMIT = 100


def _ensure_stdio() -> None:
    """冻结版是 ``console=False`` 的 GUI 程序：没有控制台，``sys.stdout`` / ``sys.stderr``
    是 **None**。不补上的话 uvicorn 配日志时 ``sys.stdout.isatty()`` 直接
    AttributeError（v2.3.0 Windows 真机实测，进程起不来），``main()`` 里的 ``print()``
    同理。指到 ``os.devnull`` —— 诊断信息本就走 core/event_log 写文件，不靠控制台。

    mac 与 Windows 两份 spec 都是 ``console=False``，所以不分平台、一律兜底。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name) is None:
            setattr(
                sys, name, open(os.devnull, "w", encoding="utf-8", errors="replace")
            )


def webview_start_kwargs() -> dict[str, Any]:
    """``webview.start()`` 的参数（抽出是为了单测平台分支）。

    - ``private_mode=False``：pywebview 默认 private_mode=True，Cocoa 启动时会清空全部
      website data（cookie/localStorage 不保留）；置 False 使用持久化 datastore。
      已核验 pywebview 6.2.1 __init__.py / cocoa.py。
    - Windows 的 ``storage_path``：不传时 WebView2 的用户数据落在**所有 pywebview
      应用共享的** ``%APPDATA%\\pywebview``（winforms.init_storage 的默认值）——
      前端 localStorage 里的本地状态会跟别的 pywebview 应用串在一起，「卸载 =
      删 %APPDATA%\\MP Harvest\\」也清不掉它。显式指到应用数据目录下，与
      WINDOWS.md 承诺的 ``%APPDATA%\\MP Harvest\\data\\webview\\`` 一致。
      macOS 不传：Cocoa 忽略该参数，保持既有行为不动（2026-09 真机已验）。
    """
    kwargs: dict[str, Any] = {"private_mode": False}
    if sys.platform == "win32":
        from mp_harvest.infra.platform import paths

        kwargs["storage_path"] = str(paths.data_dir() / "webview")
    return kwargs


def find_free_port(start: int = DEFAULT_PORT, limit: int = PORT_PROBE_LIMIT) -> int:
    """从 start 起递增探测第一个可绑定的 127.0.0.1 TCP 端口。

    固定端口的意义：pywebview 下 localStorage/cookie 按 origin 隔离，端口每
    次随机会导致持久化数据（含一次性 token 之外的本地状态）完全失效（B4）。
    """
    import socket

    for port in range(start, start + limit):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"{start}~{start + limit - 1} 端口均被占用，无法启动服务")


def start_server() -> tuple[Any, threading.Thread, int]:
    """起 uvicorn（后台线程、127.0.0.1、固定端口、单 worker），返回 (server, thread, port)。"""
    import uvicorn

    from mp_harvest.server.app import create_app

    config = uvicorn.Config(
        create_app(),
        host="127.0.0.1",
        port=find_free_port(),
        workers=1,
        log_level="warning",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="mp_harvest-uvicorn", daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("uvicorn 启动超时")
        if not thread.is_alive():
            raise RuntimeError("uvicorn 线程提前退出")
        time.sleep(0.02)
    # 动态端口：从实际监听 socket 取
    for srv in server.servers or []:
        for sock in srv.sockets or []:
            if sock.family.name.endswith("INET"):
                return server, thread, int(sock.getsockname()[1])
    raise RuntimeError("无法确定 uvicorn 监听端口")


def ensure_frontend_dist() -> None:
    """生产模式缺 ``frontend/dist`` 时自动构建（任意目录直接运行，2026-08-09）。

    构建失败只提示不阻断：服务仍可启动，``/`` 会返回构建提示 JSON。
    """
    from mp_harvest.infra.platform import paths

    dist = paths.package_root() / "frontend" / "dist"
    if dist.is_dir() or paths.is_frozen():
        return
    print("[mp_harvest] 未找到 frontend/dist，尝试自动构建前端…")
    import shutil
    import subprocess

    npm = shutil.which("npm")
    node = shutil.which("node")
    if not npm or not node:
        print("[mp_harvest] 未检测到 Node.js/npm，无法自动构建。")
        print("[mp_harvest] 请手动执行：cd mp_harvest/frontend && npm install && npm run build")
        return
    fe = paths.package_root() / "frontend"
    if subprocess.run([npm, "install", "--no-audit", "--no-fund"], cwd=fe).returncode == 0:
        if subprocess.run([npm, "run", "build"], cwd=fe).returncode == 0 and dist.is_dir():
            print("[mp_harvest] 前端构建完成。")
            return
    print("[mp_harvest] 自动构建失败，请手动执行 npm install && npm run build 后重试")


def build_url(port: int, dev_url: str = "") -> str:
    from mp_harvest.server import get_token

    token = get_token()
    if dev_url:
        sep = "&" if "?" in dev_url else "?"
        return f"{dev_url}{sep}token={token}"
    return f"http://127.0.0.1:{port}/?token={token}"


def cleanup(server: Any) -> None:
    """退出清理：停 mitm → 关系统代理 → 任务池 shutdown → uvicorn 退出。"""
    from mp_harvest.server import state
    from mp_harvest.server.tasks import registry

    try:
        svc = state.get_mitm()
        if svc.running:
            svc.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        from mp_harvest.infra.platform import get_platform

        get_platform().proxy.disable()
    except Exception:  # noqa: BLE001
        pass
    registry.shutdown(wait=False)
    server.should_exit = True


def main(argv: list[str] | None = None) -> int:
    _ensure_stdio()
    args = parse_args(argv)

    # ⚠️ 自检必须排在免责声明门禁**之前**：未同意时 require_consent() 会
    # return 2 静默退出（下几行就是），CI 上的全新机器永远拿不到自检结果，
    # 看着就像「产物起不来」。见 self_check 的 docstring。
    if args.self_check:
        return self_check(args.out)

    try:
        from mp_harvest.core.consent import require_consent

        if not require_consent():
            # 未同意免责声明：静默退出，不启动服务/窗口
            return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[mp_harvest] 免责声明门禁异常，按未同意处理：{exc}", flush=True)
        return 2
    if not args.dev:
        ensure_frontend_dist()
    recover_stale_proxy()
    server, _thread, port = start_server()
    url = build_url(port, args.dev)
    print(f"[mp_harvest] 服务已启动：{url}", flush=True)

    if args.no_window:
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            cleanup(server)
        return 0

    import webview

    # B3：前端导出用 Blob + <a download>。pywebview 默认 ALLOW_DOWNLOADS=False，
    # Cocoa 下此类导航会按普通页面加载把窗口替换成纯文本；置 True 后走
    # WKNavigationActionPolicyDownload（注意：Cocoa 实现会弹系统 NSSavePanel
    # 保存对话框，由用户选路径）。已核验 pywebview 6.2.1 cocoa.py。
    webview.settings["ALLOW_DOWNLOADS"] = True

    window_kwargs: dict[str, Any] = {}
    if args.hidden_titlebar and sys.platform == "darwin":
        # pywebview 无原生 hidden-titlebar 参数，无边框窗口是最接近形态
        window_kwargs["frameless"] = True

    # 暴露给前端的原生能力（窗口对象的 js_api 桥）。
    # 设置页的「选择目录…」原先调用 window.pywebview.api.choose_directory，
    # 但从未注册过 js_api —— 那个按钮永远走 else 分支提示「不支持原生目录
    # 选择」（2026-09 修复）。
    class JsApi:
        def __init__(self) -> None:
            self._window: Any = None

        def bind(self, win: Any) -> None:
            self._window = win

        def choose_directory(self) -> str:
            """弹系统目录选择框；返回选中的绝对路径，取消/失败返回空串。"""
            if self._window is None:
                return ""
            try:
                picked = self._window.create_file_dialog(webview.FileDialog.FOLDER)
            except Exception:  # noqa: BLE001
                return ""
            if not picked:
                return ""
            if isinstance(picked, (list, tuple)):
                return str(picked[0]) if picked else ""
            return str(picked)

        def choose_file(self, kind: str = "html") -> str:
            """弹系统文件选择框；返回选中的绝对路径，取消/失败返回空串。

            ``kind`` 决定过滤的扩展名：``html``（周报模板）/ ``any``。
            """
            if self._window is None:
                return ""
            filters = (
                ["HTML 模板 (*.html;*.htm)", "所有文件 (*.*)"]
                if kind == "html"
                else ["所有文件 (*.*)"]
            )
            try:
                picked = self._window.create_file_dialog(
                    webview.FileDialog.OPEN, allow_multiple=False, file_types=filters
                )
            except Exception:  # noqa: BLE001
                return ""
            if not picked:
                return ""
            if isinstance(picked, (list, tuple)):
                return str(picked[0]) if picked else ""
            return str(picked)

        def open_external(self, url: str) -> bool:
            """在系统默认浏览器里打开链接；成功返回 True。

            必须由 shell 侧打开：pywebview/Cocoa 只把「真人点击 <a>」
            （WKNavigationTypeLinkActivated）交给系统浏览器，JS 的
            window.open(url,'_blank') 属于 Other，会被静默丢弃 ——
            表现为「点『打开』没反应」（2026-09 修复）。
            """
            target = str(url or "").strip()
            if not target:
                return False
            # 只放行 http(s) 是**故意**的：本地文件走 /api/shell/open
            # （见 server/routes/platform.py），那条路用平台的 shell_open，
            # 既能把目录交给 Finder，也会报错。别把 file:// 加进来 ——
            # 「删掉这个判断」正是 2026-09 那批「点了没反应」按钮的病根。
            if not target.lower().startswith(("http://", "https://")):
                return False
            try:
                import webbrowser

                return bool(webbrowser.open(target, 2, True))
            except Exception:  # noqa: BLE001
                return False

    js_api = JsApi()
    window = webview.create_window(
        "MP Harvest",
        url,
        width=1180,
        height=760,
        min_size=(960, 640),
        js_api=js_api,
        **window_kwargs,
    )
    js_api.bind(window)

    # 窗口关闭后，pywebview/Cocoa 事件循环的收尾可能很慢。这里在 closed
    # 事件触发时立即启动后台线程执行清理，并在清理完成后直接 os._exit(0)，
    # 确保进程能快速退出，不等 Cocoa runloop 慢慢停（2026-08-16 用户反馈）。
    cleanup_lock = threading.Lock()

    def _cleanup_and_exit() -> None:
        with cleanup_lock:
            cleanup(server)
        os._exit(0)

    window.events.closed += _cleanup_and_exit
    try:
        webview.start(**webview_start_kwargs())  # 阻塞至窗口关闭（macOS 必须在主线程）
    finally:
        _cleanup_and_exit()


def recover_stale_proxy() -> None:
    """启动自愈（2026-08-09）：异常退出把系统代理留在 127.0.0.1:8088 且端口已死
    时自动关闭，避免全机 HTTPS 走死端口导致断网（含本机助手）。

    **同时写事件日志，不能只 print**（2026-09）：冻结版是 ``console=False`` 的
    GUI 程序，Windows 上双击起来根本没有控制台，``print`` 出去的东西全丢了 ——
    而这条消息恰恰是用户排查「为什么刚才上不了网」的唯一线索。日志在窗口起来后
    可以在「日志」页看到。
    """
    try:
        from mp_harvest.infra.platform import get_platform

        result = get_platform().proxy.recover_stale()
        message = (result.message or "") if result else ""
        if not message:
            return
        if "已恢复" in message:
            _log_boot("warn", message)
            print(f"[mp_harvest] {message}", flush=True)
        elif result and not result.ok:
            # 自愈**失败**更要留痕：机器还在断网状态，用户得知道往哪看
            _log_boot("error", f"残留代理自愈失败：{result.error or message}")
    except Exception as exc:  # noqa: BLE001
        _log_boot("error", f"残留代理自愈异常：{exc}")


def _log_boot(level: str, message: str) -> None:
    """把启动阶段的消息写进事件日志（失败不影响启动）。"""
    try:
        from mp_harvest.core.event_log import log_event

        log_event(level, "app.startup", message)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    raise SystemExit(main())
