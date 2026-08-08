"""MP Harvest 入口（设计稿 §3.2 线程模型）。

主线程 pywebview GUI（macOS Cocoa 强制）→ uvicorn 后台线程（127.0.0.1 动态
端口、单 worker）→ 生成带一次性 token 的 URL → 开窗；窗口关闭时清理
（停 mitm、关代理、任务池 shutdown、uvicorn 退出）。

用法：
    python -m mp_harvest.shell.main                    # 生产：加载 frontend/dist
    python -m mp_harvest.shell.main --dev http://localhost:5173   # Vite dev server
    python -m mp_harvest.shell.main --no-window        # 仅起服务，浏览器调试
    python -m mp_harvest.shell.main --hidden-titlebar  # mac hidden title bar
"""

from __future__ import annotations

import argparse
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
    return p.parse_args(argv)


def start_server() -> tuple[Any, threading.Thread, int]:
    """起 uvicorn（后台线程、127.0.0.1、动态端口 0、单 worker），返回 (server, thread, port)。"""
    import uvicorn

    from mp_harvest.server.app import create_app

    config = uvicorn.Config(
        create_app(),
        host="127.0.0.1",
        port=0,
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
    args = parse_args(argv)
    if not args.dev:
        ensure_frontend_dist()
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

    window_kwargs: dict[str, Any] = {}
    if args.hidden_titlebar and sys.platform == "darwin":
        # pywebview 无原生 hidden-titlebar 参数，无边框窗口是最接近形态
        window_kwargs["frameless"] = True

    webview.create_window(
        "MP Harvest",
        url,
        width=1180,
        height=760,
        min_size=(960, 640),
        **window_kwargs,
    )
    try:
        webview.start()  # 阻塞至窗口关闭（macOS 必须在主线程）
    finally:
        cleanup(server)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
