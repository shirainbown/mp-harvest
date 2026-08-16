"""平台抽象层（设计稿 §4）。

业务/UI 只调这里的统一接口：

- ``platform.ca.install() -> InstallResult``
- ``platform.ca.status() -> bool``
- ``platform.ca.cert_path() -> Path``
- ``platform.proxy.enable(port) -> ProxyResult``
- ``platform.proxy.disable() -> ProxyResult``
- ``platform.paths.data_dir() -> Path``
- ``platform.shell_open(path)``
- ``platform.updater.check() / .download() / .apply()``
- ``platform.info() -> dict``

失败一律返回结构化结果对象或抛 :class:`PlatformError`，绝不静默。
"""

from __future__ import annotations

import os
import sys
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from mp_harvest.infra.platform import paths

APP_VERSION = "2.1.0"

# GitHub Releases 更新源：默认你的仓库 shirainbown/mp-harvest，可用环境变量覆盖。
# 用法：MP_HARVEST_GITHUB_REPO=用户名/仓库名
GITHUB_REPO = os.environ.get("MP_HARVEST_GITHUB_REPO", "shirainbown/mp-harvest").strip()
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


class PlatformError(RuntimeError):
    """平台操作失败（带语义）。"""


@dataclass
class InstallResult:
    """CA 安装结果。"""

    ok: bool
    message: str = ""
    needs_admin: bool = False
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProxyResult:
    """系统代理设置结果。"""

    ok: bool
    message: str = ""
    error: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class UpdateCheckResult:
    """更新检查结果。"""

    ok: bool
    available: bool = False
    version: str = ""
    current_version: str = APP_VERSION
    release_url: str = RELEASE_PAGE
    zip_url: str = ""
    notes: str = ""
    message: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DownloadResult:
    """更新包下载结果。"""

    ok: bool
    path: str = ""
    message: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CaSetup(ABC):
    """抓包 CA 证书安装/状态查询。"""

    needs_admin: bool = False

    @abstractmethod
    def install(self) -> InstallResult: ...

    @abstractmethod
    def status(self) -> bool: ...

    @abstractmethod
    def cert_path(self) -> Path: ...


class ProxyManager(ABC):
    """系统代理开关。"""

    needs_admin: bool = False

    @abstractmethod
    def enable(self, port: int) -> ProxyResult: ...

    @abstractmethod
    def disable(self) -> ProxyResult: ...

    def recover_stale(self, host: str = "127.0.0.1", port: int = 8088) -> ProxyResult:
        """启动自愈（2026-08-09）：异常退出残留的系统代理由平台层实现，默认无操作。"""
        return ProxyResult(ok=True, message="无需恢复")


class Updater(ABC):
    """在线更新（流程双平台同构，设计稿 §4）。"""

    @abstractmethod
    def check(self, proxy: str | None = None) -> UpdateCheckResult: ...

    @abstractmethod
    def download(
        self,
        zip_url: str,
        *,
        proxy: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> DownloadResult: ...

    @abstractmethod
    def apply(self, package_path: str | Path) -> None:
        """退出当前进程 → 替换安装 → 重启。此调用不返回。"""


class Platform(ABC):
    """平台能力聚合。"""

    os_name: str = "unknown"

    def __init__(self) -> None:
        self.ca: CaSetup = self._make_ca()
        self.proxy: ProxyManager = self._make_proxy()
        self.updater: Updater = self._make_updater()
        self.paths = paths

    @abstractmethod
    def _make_ca(self) -> CaSetup: ...

    @abstractmethod
    def _make_proxy(self) -> ProxyManager: ...

    @abstractmethod
    def _make_updater(self) -> Updater: ...

    @abstractmethod
    def shell_open(self, path: str | Path) -> None:
        """用系统默认方式打开文件/目录，失败抛 :class:`PlatformError`。"""

    def info(self) -> dict[str, Any]:
        """前端能力矩阵（设计稿 §4 权限 UX）。"""
        return {
            "os": self.os_name,
            "ca_needs_admin": self.ca.needs_admin,
            "proxy_needs_admin": self.proxy.needs_admin,
            "data_dir": str(paths.data_dir()),
            "engine": _webview_engine(),
            "version": APP_VERSION,
        }


def _webview_engine() -> str:
    """pywebview 当前 GUI 引擎名（best-effort，不引入硬依赖）。"""
    try:
        import webview  # type: ignore

        guilib = getattr(webview, "guilib", None)
        name = getattr(guilib, "__name__", "") if guilib else ""
        if name:
            return name.rsplit(".", 1)[-1]
        return "pywebview"
    except Exception:
        return "unknown"


_platform: Platform | None = None


def get_platform() -> Platform:
    """按 ``sys.platform`` 分派（进程内单例）。"""
    global _platform
    if _platform is not None:
        return _platform
    if sys.platform == "win32":
        from mp_harvest.infra.platform.win import WinPlatform

        _platform = WinPlatform()
    elif sys.platform == "darwin":
        from mp_harvest.infra.platform.mac import MacPlatform

        _platform = MacPlatform()
    else:
        raise PlatformError(f"不支持的平台：{sys.platform}（仅支持 win32 / darwin）")
    return _platform


def reset_platform() -> None:
    """清掉单例（测试用）。"""
    global _platform
    _platform = None

# ── GitHub Releases 更新公共逻辑（win/mac Updater 复用，平移自旧版 updater.py） ──


def _parse_version(tag: str) -> tuple[int, ...]:
    import re

    m = re.search(r"(\d+(?:\.\d+)+)", tag or "")
    if not m:
        return (0,)
    try:
        return tuple(int(x) for x in m.group(1).split("."))
    except ValueError:
        return (0,)


def pick_zip_url(assets: list[dict[str, Any]], suffix: str = ".zip") -> str:
    """从 release assets 挑第一个匹配后缀的下载地址（纯函数）。"""
    for asset in assets or []:
        name = str(asset.get("name") or "")
        url = str(asset.get("browser_download_url") or "")
        if url and name.lower().endswith(suffix):
            return url
    return ""


class GithubUpdater(Updater):
    """基于 GitHub Releases 的更新器基类；子类只实现 :meth:`apply` 与 asset 后缀。"""

    asset_suffix: str = ".zip"
    check_timeout: int = 12
    download_timeout: int = 600

    def _opener(self, proxy: str | None):
        import urllib.request

        if proxy and str(proxy).strip():
            handler = urllib.request.ProxyHandler(
                {"http": proxy.strip(), "https": proxy.strip()}
            )
            return urllib.request.build_opener(handler)
        return urllib.request.build_opener(urllib.request.ProxyHandler())

    def check(self, proxy: str | None = None) -> UpdateCheckResult:
        import json
        import urllib.request

        if not GITHUB_REPO:
            return UpdateCheckResult(
                ok=False,
                message="未配置更新源：请设置环境变量 MP_HARVEST_GITHUB_REPO=用户名/仓库名",
                error="no repo configured",
            )
        req = urllib.request.Request(
            RELEASES_API,
            headers={
                "User-Agent": "MP Harvest-update-check",
                "Accept": "application/vnd.github+json",
            },
        )
        try:
            with self._opener(proxy).open(req, timeout=self.check_timeout) as resp:  # noqa: S310
                payload = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001
            return UpdateCheckResult(
                ok=False,
                message="无法访问 GitHub（请检查网络 / 代理设置）",
                error=str(exc),
            )
        tag = str(payload.get("tag_name") or "")
        if not tag:
            return UpdateCheckResult(ok=False, message="release 响应缺少 tag_name", error="no tag")
        available = _parse_version(tag) > _parse_version(APP_VERSION)
        return UpdateCheckResult(
            ok=True,
            available=available,
            version=tag,
            release_url=str(payload.get("html_url") or RELEASE_PAGE),
            zip_url=pick_zip_url(payload.get("assets") or [], self.asset_suffix),
            notes=str(payload.get("body") or "").strip(),
            message=(f"发现新版本 {tag}" if available else f"已是最新版（{APP_VERSION}）"),
        )

    def download(
        self,
        zip_url: str,
        *,
        proxy: str | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> DownloadResult:
        import urllib.request

        if not zip_url or not str(zip_url).strip():
            return DownloadResult(ok=False, error="empty zip_url", message="缺少下载地址")
        dest = paths.data_dir() / "update"
        dest.mkdir(parents=True, exist_ok=True)
        name = str(zip_url).rstrip("/").split("/")[-1] or f"MP Harvest{self.asset_suffix}"
        out = dest / name
        req = urllib.request.Request(zip_url, headers={"User-Agent": "MP Harvest-update"})
        try:
            with self._opener(proxy).open(req, timeout=self.download_timeout) as resp:  # noqa: S310
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                with open(out, "wb") as fh:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if on_progress and total:
                            on_progress(done, total)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(ok=False, error=str(exc), message=f"下载失败：{exc}")
        return DownloadResult(ok=True, path=str(out), message=f"已下载到 {out}")
