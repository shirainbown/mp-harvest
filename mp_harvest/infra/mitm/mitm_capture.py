"""Start/stop local mitmproxy capture in-process (thread) + system proxy."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from pathlib import Path
from typing import Any

from mp_harvest.infra.platform.paths import data_dir

try:
    from mp_harvest.infra.platform.ca_setup import PROXY_HOST, PROXY_PORT
except ImportError:  # platform 层（Epic B）未就绪时的占位常量
    PROXY_HOST = "127.0.0.1"
    PROXY_PORT = 8088

# 只中间人拦截微信公众平台域名，其他 HTTPS 流量（AI 模型、GitHub、浏览器等）
# 由 mitmproxy 透传，不再重签证书。否则本应用自身 AI 请求 / 用户浏览器都会被
# 自签 CA 拦截导致 SSL 校验失败（2026-08-16 真机定位）。
ALLOW_HOSTS = [r"mp\.weixin\.qq\.com"]


def prepare_mitm_confdir(app_root: Path) -> tuple[Path, str]:
    """接口占位：CA 准备逻辑由 infra/platform 适配层实现（Epic B，设计稿 §4）。"""
    try:
        from mp_harvest.infra.platform import ca_setup
    except ImportError as exc:
        raise RuntimeError(
            "CA 适配层（infra/platform/ca_setup）尚未就绪，完成 Epic B 后才能启动抓包"
        ) from exc
    return ca_setup.prepare_mitm_confdir(app_root)

class MitmCaptureService:
    """Run DumpMaster inside a daemon thread — avoids frozen-exe SSL DLL relaunch bugs."""

    def __init__(self, app_root: Path | None = None) -> None:
        self.app_root = app_root or data_dir()
        self.inbox = data_dir() / "capture_inbox.json"
        self._lock = threading.RLock()
        self._last_inbox_mtime: float = 0.0
        self._thread: threading.Thread | None = None
        self._master: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = threading.Event()
        self._start_error: str | None = None
        self._running = False
        self._capture_addon: Any = None

    @property
    def running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def clear_inbox(self) -> None:
        try:
            if self.inbox.exists():
                self.inbox.unlink()
        except Exception:
            pass
        self._last_inbox_mtime = 0.0

    def reset_capture_state(self) -> None:
        """Clear inbox + in-memory merge so renew waits for fresh WeChat traffic."""
        self.clear_inbox()
        addon = self._capture_addon
        if addon is not None and hasattr(addon, "reset_merge_state"):
            try:
                addon.reset_merge_state()
            except Exception:
                pass

    def reset_inbox_cursor(self) -> None:
        """Allow re-reading the current inbox file (e.g. after binding a pending account)."""
        self._last_inbox_mtime = 0.0

    def ack_inbox(self) -> None:
        """Mark current inbox as consumed after credentials were applied."""
        try:
            if self.inbox.is_file():
                self._last_inbox_mtime = self.inbox.stat().st_mtime
        except Exception:
            pass

    def read_new_credentials(self, *, consume: bool = True) -> dict[str, str] | None:
        if not self.inbox.is_file():
            return None
        try:
            mtime = self.inbox.stat().st_mtime
        except Exception:
            return None
        if mtime <= self._last_inbox_mtime:
            return None
        try:
            data = json.loads(self.inbox.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(data, dict):
            return None
        if not (data.get("__biz") and data.get("uin") and data.get("key")):
            return None
        if consume:
            self._last_inbox_mtime = mtime
        return {
            k: str(data.get(k) or "")
            for k in ("__biz", "uin", "key", "pass_ticket", "appmsg_token")
            if data.get(k)
        }

    def _thread_main(self, confdir: Path) -> None:
        os.environ["SCHINZA_CAPTURE_INBOX"] = str(self.inbox)
        os.environ["SCHINZA_SIGHTINGS"] = str(data_dir() / "article_sightings.json")
        try:
            from mitmproxy.options import Options
            from mitmproxy.tools.dump import DumpMaster

            from mp_harvest.infra.mitm.mitm_addon import CredentialCapture

            opts = Options(
                listen_host=PROXY_HOST,
                listen_port=PROXY_PORT,
                confdir=str(confdir),
                allow_hosts=list(ALLOW_HOSTS),
            )
            # block_global may be set after construct on some versions
            try:
                opts.update(block_global=False)
            except Exception:
                pass

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop

            async def _run() -> None:
                master = DumpMaster(
                    opts,
                    loop=loop,
                    with_termlog=False,
                    with_dumper=False,
                )
                addon = CredentialCapture()
                self._capture_addon = addon
                master.addons.add(addon)
                self._master = master
                self._running = True
                self._started.set()
                await master.run()

            loop.run_until_complete(_run())
        except SystemExit as exc:  # mitmproxy ErrorCheck 启动失败直接 sys.exit(1)
            self._start_error = f"mitmproxy 启动失败（exit {exc.code}），请检查 8088 端口是否被占用"
            self._started.set()
        except Exception as exc:  # noqa: BLE001
            self._start_error = str(exc)
            self._started.set()
        finally:
            self._running = False
            self._master = None
            try:
                if self._loop and self._loop.is_running():
                    self._loop.stop()
            except Exception:
                pass
            self._loop = None

    def start(self, *, set_system_proxy: bool = True) -> tuple[bool, str]:
        with self._lock:
            if self.running:
                return True, f"抓包代理已在运行 {PROXY_HOST}:{PROXY_PORT}"

            try:
                confdir, prep_msg = prepare_mitm_confdir(self.app_root)
            except Exception as exc:  # noqa: BLE001
                return False, f"准备 CA 失败：{exc}"

            self.inbox.parent.mkdir(parents=True, exist_ok=True)
            self.clear_inbox()
            self._start_error = None
            self._started.clear()

            self._thread = threading.Thread(
                target=self._thread_main,
                args=(confdir,),
                name="mp_harvest-mitm",
                daemon=True,
            )
            self._thread.start()

            # 等到代理端口真正可连（bind 完成）或线程失败/退出
            # （mitmproxy 的 _started 事件在 bind 之前触发，不能作为就绪信号）
            deadline = time.time() + 8.0
            up = False
            while time.time() < deadline:
                if self._start_error:
                    err = self._start_error
                    self._start_error = None
                    return False, f"启动抓包失败：{err}"
                if not self._thread.is_alive():
                    return False, "抓包线程已退出，请确认 mitmproxy-ca.pem 可用且 8088 空闲"
                try:
                    with socket.create_connection((PROXY_HOST, PROXY_PORT), timeout=0.3):
                        up = True
                        break
                except OSError:
                    time.sleep(0.15)
            if not up:
                return False, "启动抓包超时，请重试或检查端口 8088 是否被占用"
            if not self.running:
                return False, "抓包线程状态异常，请重试"

            proxy_msg = ""
            if set_system_proxy:
                ok, proxy_msg = self.enable_system_proxy()
                if not ok:
                    proxy_msg = f"代理已启动，但系统代理设置失败：{proxy_msg}"

            if not proxy_msg:
                proxy_msg = (
                    "已开启系统代理（停止抓包后自动恢复原设置）。"
                    if set_system_proxy
                    else "未设置系统代理（开发/调试模式，仅监听 8088）。"
                )
            return True, (
                f"{prep_msg}\n抓包代理已启动 {PROXY_HOST}:{PROXY_PORT}。"
                + f"\n{proxy_msg}"
                + "\n请用微信桌面打开公众号文章（内置浏览器）。"
            )

    def stop(self, *, restore_proxy: bool = True) -> tuple[bool, str]:
        with self._lock:
            msg_parts: list[str] = []
            master = self._master
            loop = self._loop
            if master is not None:
                # mitmproxy 的 proxyserver 没有 done 钩子：shutdown() 只让 run() 返回，
                # 监听 socket 不会关闭，进程内二次启动会 EADDRINUSE。
                # 先在其事件循环里显式停掉所有 server，再 shutdown。
                if loop is not None and loop.is_running():

                    async def _teardown() -> None:
                        try:
                            ps = master.addons.get("proxyserver")
                            if ps is not None:
                                await ps.servers.update([])
                        except Exception:
                            pass
                        master.shutdown()

                    try:
                        asyncio.run_coroutine_threadsafe(_teardown(), loop).result(timeout=5.0)
                    except Exception:
                        pass
                else:
                    try:
                        master.shutdown()
                    except Exception:
                        pass
                self._master = None
            if self._thread is not None:
                self._thread.join(timeout=3.0)
                self._thread = None
                msg_parts.append("已停止抓包代理")
            self._running = False
            if restore_proxy:
                ok, m = self.restore_system_proxy()
                msg_parts.append(m if ok else f"恢复系统代理失败：{m}")
            return True, "；".join(msg_parts) if msg_parts else "代理未在运行"

    def enable_system_proxy(self) -> tuple[bool, str]:
        """委托平台代理层（win/mac 各自备份原设置后写入 127.0.0.1:8088）。

        前置守卫（2026-08-09）：CA 未被系统信任时**拒绝切换代理**——否则整机 HTTPS
        会被中间人拦截且握手失败，表现为全机断网/本机助手断连。
        """
        try:
            from mp_harvest.infra.platform import get_platform

            platform = get_platform()
            if not platform.ca.status():
                return False, (
                    "CA 证书未被系统信任：开启抓包会劫持全机 HTTPS 导致断网。"
                    "请先在「凭证管理」点「安装 CA 证书」并输入管理员密码完成信任，再启动抓包"
                )
            result = platform.proxy.enable(PROXY_PORT)
        except Exception as exc:  # noqa: BLE001
            return False, f"设置系统代理异常：{exc}"
        return result.ok, result.message or result.error or ""

    def restore_system_proxy(self) -> tuple[bool, str]:
        """委托平台代理层恢复备份的原设置；未由本应用开启时安全 no-op。"""
        try:
            from mp_harvest.infra.platform import get_platform

            result = get_platform().proxy.disable()
        except Exception as exc:  # noqa: BLE001
            return False, f"恢复系统代理异常：{exc}"
        return result.ok, result.message or result.error or ""
