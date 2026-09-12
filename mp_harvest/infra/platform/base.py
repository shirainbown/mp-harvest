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

# 更新包目录里保留几个。**一个就够**（要装的就是最新的那个），
# 多留几个纯粹是占地方。
UPDATE_KEEP = 1


def update_dir() -> Path:
    return paths.data_dir() / "update"


def prune_update_packages(keep: int = UPDATE_KEEP) -> int:
    """只保留最新的 ``keep`` 个更新包，其余删掉。返回删掉的个数。

    **原先从来不删**（2026-09 用户报「815MB 数据目录」时发现）：每次点
    「立即更新」都会在 ``data/update/`` 留一个 ~50MB 的 zip，下载后、应用后、
    下次启动都没有任何清理 —— 实测一个用户的目录里积了 16 个包共 806MB，
    而其余全部数据加起来不到 9MB。

    按 mtime 排序（下载时间），保留最新的。非 ``.zip`` 的文件一律不碰
    （应用脚本就写在这个目录里）。任何异常都不抛 —— 这是启动路径上的清理。
    """
    d = update_dir()
    try:
        zips = [p for p in d.glob("*.zip") if p.is_file()]
        if len(zips) <= max(0, int(keep)):
            return 0
        zips.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        removed = 0
        for p in zips[max(0, int(keep)):]:
            try:
                p.unlink()
                removed += 1
            except OSError:
                continue
        return removed
    except Exception:  # noqa: BLE001
        return 0


def dir_size(path: Path) -> tuple[int, int]:
    """目录占用 ``(字节, 文件数)``；不存在或读不到时返回 ``(0, 0)``。"""
    total = 0
    count = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
                    count += 1
            except OSError:
                continue
    except Exception:  # noqa: BLE001
        return 0, 0
    return total, count

APP_VERSION = "2.2.4"

_SSL_CONTEXT = None


def _make_ssl_context():
    """返回使用 certifi CA bundle 的 SSLContext（PyInstaller 冻结版默认
    urllib 找不到系统根证书，必须显式走 certifi，否则检查更新/AI 请求会
    报 CERTIFICATE_VERIFY_FAILED）。"""
    global _SSL_CONTEXT
    if _SSL_CONTEXT is None:
        import ssl

        import certifi

        _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
    return _SSL_CONTEXT

# GitHub Releases 更新源：默认你的仓库 shirainbown/mp-harvest，可用环境变量覆盖。
# 用法：MP_HARVEST_GITHUB_REPO=用户名/仓库名
GITHUB_REPO = os.environ.get("MP_HARVEST_GITHUB_REPO", "shirainbown/mp-harvest").strip()
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASE_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
# API 被限流时的兜底：atom feed 不消耗 API 配额、不需要认证
RELEASES_ATOM = f"https://github.com/{GITHUB_REPO}/releases.atom"


def _html_notes_to_markdown(fragment: str) -> str:
    """把 atom feed 里的 HTML 正文转成 markdown。

    为什么必须转：前端的检查更新弹窗用 markdown-it 且 ``html: false``（防 XSS），
    直接把 HTML 塞进 ``notes`` 会被**转义成字面文本** —— 用户看到的就是一堆
    ``<h2>``（2026-09 实测）。GitHub API 路径返回的 ``body`` 本来就是 markdown，
    这里对齐成同一种格式，前端契约保持单一。
    """
    try:
        from bs4 import BeautifulSoup, Tag
    except Exception:  # noqa: BLE001
        return fragment

    soup = BeautifulSoup(fragment or "", "html.parser")

    def render(el: Any) -> list[str]:
        if not isinstance(el, Tag):
            text = str(el).strip()
            return [text] if text else []
        name = (el.name or "").lower()
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            title = el.get_text(" ", strip=True)
            return [f"{'#' * int(name[1])} {title}"] if title else []
        if name in ("ul", "ol"):
            out: list[str] = []
            for i, li in enumerate(el.find_all("li", recursive=False), start=1):
                item = li.get_text(" ", strip=True)
                if item:
                    out.append(f"{i}. {item}" if name == "ol" else f"- {item}")
            return out
        if name == "hr":
            return ["---"]
        if name == "pre":
            return ["```", el.get_text(), "```"]
        if name == "blockquote":
            text = el.get_text(" ", strip=True)
            return [f"> {text}"] if text else []
        if name == "p":
            text = el.get_text(" ", strip=True)
            return [text] if text else []
        # 其它容器（div/section/…）递归到子节点
        out = []
        for child in el.children:
            out.extend(render(child))
        return out

    import re as _re

    blocks: list[str] = []
    for top in soup.children:
        blocks.extend(render(top))
    if not blocks:
        return soup.get_text("\n", strip=True)

    # 空行分段，让 markdown-it 正确识别标题/段落；但**连续的列表项之间不能有空行**，
    # 否则每个条目会被当成独立的列表（渲染成一堆松散段落）
    merged: list[str] = []
    item_re = _re.compile(r"^([-*]|\d+\.)\s")
    for block in (x.strip() for x in blocks):
        if not block:
            continue
        if merged and item_re.match(block) and item_re.match(merged[-1]):
            merged[-1] = f"{merged[-1]}\n{block}"
        else:
            merged.append(block)
    return "\n\n".join(merged)


def _system_proxy() -> str:
    """当前系统代理（返回 ``http://host:port``，取不到返回空串）。

    供「网络设置 → 更新与下载代理」的三种模式使用（见 routes/update.py
    的 ``_settings_proxy``）：``direct`` 不走代理、``system`` 走这里探测到的
    系统代理、``custom`` 走用户填写的地址。未配置时按 ``system`` 处理 ——
    国内用户开着 Clash 时这是唯一开箱即用的选项。

    注意一个坑：``urllib.request.ProxyHandler()`` 空参会调 ``getproxies()``，
    而 macOS 上它依赖 ``_scproxy`` 这个 C 扩展；PyInstaller 冻结后一旦漏打包，
    ``getproxies()`` 会**静默返回空**（spec 已显式打包 ``_scproxy``）。所以本函数
    在 ``getproxies()`` 之外还补了一条不依赖 C 扩展的 ``scutil --proxy`` 读取。
    """
    import urllib.request

    try:
        proxies = urllib.request.getproxies()
        for key in ("https", "http"):
            val = str(proxies.get(key) or "").strip()
            if val:
                return val if "://" in val else f"http://{val}"
    except Exception:  # noqa: BLE001
        pass

    if sys.platform == "darwin":
        import re
        import subprocess

        vals: dict[str, str] = {}
        try:
            out = subprocess.run(
                ["scutil", "--proxy"], capture_output=True, text=True, timeout=10
            ).stdout or ""
            for m in re.finditer(r"(\w+)\s*:\s*(\S+)", out):
                vals[m.group(1)] = m.group(2)
        except Exception:  # noqa: BLE001
            return ""
        for scheme in ("HTTPS", "HTTP"):
            if vals.get(f"{scheme}Enable") == "1":
                host = vals.get(f"{scheme}Proxy", "")
                port = vals.get(f"{scheme}Port", "")
                if host and port:
                    return f"http://{host}:{port}"
    return ""


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
    # atom 兜底时按命名约定拼下载地址用；留空 = 不猜地址（只报版本 + 发布页）
    asset_prefix: str = ""
    check_timeout: int = 12
    download_timeout: int = 600

    def _opener(self, proxy: str | None):
        import urllib.request

        # 只认调用方显式传入的代理（由「网络设置」的 mode 决定：直连 = 真的直连）。
        # 不要在这里自动回退到系统代理 —— UI 上「直连（不使用代理）」就是这个意思。
        effective = str(proxy).strip() if proxy and str(proxy).strip() else ""
        if effective:
            handler = urllib.request.ProxyHandler({"http": effective, "https": effective})
        else:
            # 显式空 dict：不要再让 ProxyHandler 去读一次环境（拿不到就是没代理）
            handler = urllib.request.ProxyHandler({})
        return urllib.request.build_opener(
            handler,
            urllib.request.HTTPSHandler(context=_make_ssl_context()),
        )

    def _check_via_atom(self, proxy: str | None) -> UpdateCheckResult | None:
        """API 不可用时的兜底：读 ``releases.atom``（不消耗配额、不需要认证）。

        返回 ``None`` 表示兜底也失败，调用方按原错误处理（2026-09 修复）。
        """
        import html as html_mod
        import re
        import urllib.request

        try:
            req = urllib.request.Request(
                RELEASES_ATOM,
                headers={
                    "User-Agent": "MP Harvest-update-check",
                    "Accept": "application/atom+xml",
                },
            )
            with self._opener(proxy).open(req, timeout=self.check_timeout) as resp:  # noqa: S310
                xml = resp.read().decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return None

        entry_m = re.search(r"<entry>(.*?)</entry>", xml, re.S)
        if not entry_m:
            return None
        entry = entry_m.group(1)
        tag_m = re.search(r"tag:github\.com,\d+:Repository/\d+/([^<]+)</id>", entry)
        if not tag_m:
            return None
        tag = tag_m.group(1).strip()
        if not tag:
            return None

        notes = ""
        content_m = re.search(r'<content type="html">(.*?)</content>', entry, re.S)
        if content_m:
            # atom 给的是 HTML；统一转成 markdown，避免前端把它当纯文本显示出一堆标签
            notes = _html_notes_to_markdown(html_mod.unescape(content_m.group(1))).strip()

        zip_url = ""
        if self.asset_prefix:
            # 按仓库约定拼：CI 产物名为 MP-Harvest-mac-<ver>.zip
            zip_url = (
                f"https://github.com/{GITHUB_REPO}/releases/download/"
                f"{tag}/{self.asset_prefix}{tag.lstrip('v')}{self.asset_suffix}"
            )
        return UpdateCheckResult(
            ok=True,
            available=_parse_version(tag) > _parse_version(APP_VERSION),
            version=tag,
            release_url=f"https://github.com/{GITHUB_REPO}/releases/tag/{tag}",
            zip_url=zip_url,
            notes=notes,
        )

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
            # 未认证的 GitHub API 每小时只有 60 次（走共享代理出口更易耗尽），
            # 这是限流而非网络故障；改走不限流的 atom feed 兜底（2026-09 修复）。
            fallback = self._check_via_atom(proxy)
            if fallback is not None:
                return fallback
            import urllib.error

            # 报错尽可能详细（2026-09）：限流的剩余配额 / 重置时间就在响应头里，
            # 之前只给了一句「请稍后重试」，用户和我都无从判断该等多久。
            if isinstance(exc, urllib.error.HTTPError) and exc.code == 403:
                parts = ["GitHub API 限流：未认证请求每小时 60 次"]
                headers = getattr(exc, "headers", None)
                remaining = headers.get("X-RateLimit-Remaining") if headers else None
                limit = headers.get("X-RateLimit-Limit") if headers else None
                reset = headers.get("X-RateLimit-Reset") if headers else None
                if remaining is not None and limit:
                    parts.append(f"当前配额 {remaining}/{limit}")
                if reset:
                    try:
                        import datetime as _dt

                        when = _dt.datetime.fromtimestamp(int(reset)).strftime("%H:%M")
                        parts.append(f"将于 {when} 重置")
                    except Exception:  # noqa: BLE001
                        pass
                message = (
                    "；".join(parts)
                    + "\n可用「网络设置 → 自定义 HTTP 代理」走代理出口（换 IP 即换配额），或稍后重试"
                    + f"\n（已尝试 releases.atom 兜底，同样失败：{exc}）"
                )
            else:
                message = (
                    f"无法访问 GitHub：{exc}"
                    "\n请在「网络设置」里选择「跟随系统代理」或「自定义 HTTP 代理」"
                )
            return UpdateCheckResult(ok=False, message=message, error=str(exc))
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
        should_cancel: Callable[[], bool] | None = None,
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
                        if should_cancel and should_cancel():
                            return DownloadResult(
                                ok=False,
                                error="cancelled",
                                message="下载已取消",
                            )
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        fh.write(chunk)
                        done += len(chunk)
                        if on_progress and total:
                            on_progress(done, total)
        except Exception as exc:  # noqa: BLE001
            return DownloadResult(ok=False, error=str(exc), message=f"下载失败：{exc}")
        # 新包到手，把旧的清掉（否则每更新一次留 50MB，只增不减）
        prune_update_packages()
        return DownloadResult(ok=True, path=str(out), message=f"已下载到 {out}")
