"""Windows 平台适配（设计稿 §4）。

- CA：``certutil -addstore -user Root``（用户存储，无需管理员）
- 代理：HKCU 注册表 ProxyEnable/ProxyServer + InternetSetOption 刷新（用户权限）
- 打开文件：``os.startfile``
- 更新：退出 → 替换目录 → 重启脚本

注意：``winreg`` / ``ctypes.windll`` / ``os.startfile`` 仅在 Windows 存在，
必须**函数内惰性 import**，保证本模块在 macOS 上可正常导入（契约测试用）。

2026-09 Windows 版补齐（本模块此前只被 mac 上的契约测试导入过，从没在真机跑）：
``recover_stale`` 自愈、CA 缺失时自动生成、更新包 asset 命名、子进程不闪控制台、
更新脚本的编码与目录层数 —— 逐条见各自函数的注释。
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from mp_harvest.infra.platform.base import (
    CaSetup,
    GithubUpdater,
    InstallResult,
    Platform,
    PlatformError,
    ProxyManager,
    ProxyResult,
    Updater,
)
from mp_harvest.infra.platform import paths

_PROXY_HOST = "127.0.0.1"
_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"

# 本应用抓包代理的默认端口。**故意不 import ca_setup.PROXY_PORT**：ca_setup 一被
# 导入就会去建数据目录，而本模块要在 macOS 上也干干净净地可导入（契约测试）。
_OWN_PROXY_PORT = 8088


def _no_window() -> int:
    """子进程「不弹控制台」的 creationflags（非 Windows 上为 0）。

    冻结版是 ``console=False`` 的 GUI 程序，而它调起来的 ``certutil`` 是控制台程序：
    不带这个标志，每次「安装 CA 证书」「刷新 CA 状态」都会在屏幕上闪一个黑框。
    """
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    """跑一条外部命令（捕获输出、不弹窗、不超时挂死）。

    与 mac.py 的 ``_run`` 同形，差别只在 creationflags —— 调用点看不出区别，
    单测也可以照 mac 那套来写。
    """
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_no_window(),
    )


def parse_proxy_server(raw: str) -> str:
    """把注册表 ``ProxyServer`` 的值解析成 ``host:port``；解析不出返回空串。

    纯函数，便于在 mac 上单测。Windows 上这个值有两种写法：

    - 单代理：``127.0.0.1:7897``
    - 按协议分列：``http=127.0.0.1:7897;https=127.0.0.1:7898``

    第二种如果不解析、直接拼成 ``http://http=127.0.0.1:7897;https=...``，那就是个
    非法 URL，表现是「明明开着 Clash，检查更新还是连不上」。优先 **https**
    （检查更新与下载走 https），其次 http，最后兜第一个非空段。
    """
    text = str(raw or "").strip()
    if not text:
        return ""
    if "=" not in text:
        # 单代理写法：``host`` / ``host:port``。带 ``;`` 却又没有 ``=`` 的值是坏的
        # （按协议分列才会用 ``;``）—— 那种残渣拼成 URL 只会让连接以看不懂的方式
        # 失败，不如当「没有代理」，让上层走「未检测到系统代理」的提示。
        return "" if ";" in text else text
    pairs: dict[str, str] = {}
    order: list[str] = []
    for chunk in text.split(";"):
        key, _, val = chunk.partition("=")
        key = key.strip().lower()
        val = val.strip()
        if key and val:
            pairs[key] = val
            order.append(key)
    for scheme in ("https", "http", "socks", "socks5"):
        if pairs.get(scheme):
            return pairs[scheme]
    return pairs[order[0]] if order else ""


def _proxy_is_ours(server: str, host: str = _PROXY_HOST, port: int = _OWN_PROXY_PORT) -> bool:
    """``server`` 是否就是本应用的抓包代理。

    按 :func:`parse_proxy_server` 的优先级取值再比（与 :func:`read_system_proxy`
    同一条口径），所以按协议分列、且 https 指向别处的值**不算我们的** —— 那种写法
    本来也不是本应用写的（我们只写单一的 ``host:port``），判成「不是」才安全：
    宁可不去动一份不是自己留下的配置。
    """
    text = parse_proxy_server(server).strip().lower()
    if not text:
        return False
    return text in (f"{host}:{port}", f"{host}:{port}/")


def read_system_proxy() -> str:
    """当前系统代理（``http://host:port``）；没有、读不到、或**就是我们自己**时返回空串。

    供 ``base._system_proxy()`` 的 win32 分支调用，也供「设置」页显示。

    为什么要把「就是本应用自己的抓包代理」判成没有：那时「跟随系统代理」等于让
    检查更新/下载绕回本机 8088 —— 能不能通完全取决于抓包代理是否还活着，下载到
    一半用户把抓包停了就断。这不是一个可用的出口，不如报「没检测到」，
    界面上那句「请改用自定义 HTTP 代理」正好把人引到正确的做法上。

    ``AutoConfigURL``（PAC）无法求值 —— 脚本要跑起来才知道代理是谁，这里不猜，
    当没有静态代理处理。
    """
    try:
        import winreg  # type: ignore  # 惰性 import：mac 上无此模块
    except ImportError:
        return ""
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_QUERY_VALUE
        ) as key:
            try:
                enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            except FileNotFoundError:
                enabled = 0
            try:
                server = str(winreg.QueryValueEx(key, "ProxyServer")[0] or "")
            except FileNotFoundError:
                server = ""
    except Exception:  # noqa: BLE001
        return ""
    if not int(enabled or 0):
        return ""
    if _proxy_is_ours(server):
        return ""
    hostport = parse_proxy_server(server).strip()
    if not hostport:
        return ""
    return hostport if "://" in hostport else f"http://{hostport}"


def default_cert_path() -> Path:
    """公钥 CA 路径：数据目录 .cer 优先，其次 ``%USERPROFILE%\\.mitmproxy\\``。"""
    from mp_harvest.infra.platform.ca_setup import public_cert_path

    return public_cert_path()


class WinCaSetup(CaSetup):
    needs_admin = False

    def __init__(self, cert: Path | None = None) -> None:
        self._cert = cert or default_cert_path()

    def cert_path(self) -> Path:
        return self._cert

    def install(self) -> InstallResult:
        cert = self.cert_path()
        if not cert.exists():
            # 证书缺失时现场生成，与 mac 同形（2026-09 Windows 版补齐）。
            # 原先这里直接报「请先启动一次抓包以生成证书」—— 但界面上「安装 CA 证书」
            # 是三步引导的**第一步**，新用户按指引点下去只会拿到这句话，卡死。
            try:
                from mp_harvest.infra.platform import ca_setup

                ca_setup.prepare_mitm_confdir(paths.app_root())
            except Exception as exc:  # noqa: BLE001
                return InstallResult(
                    ok=False,
                    needs_admin=False,
                    error="cert missing",
                    message=f"CA 证书不存在：{cert}（自动生成失败：{exc}）",
                )
            if not cert.exists():
                return InstallResult(
                    ok=False,
                    needs_admin=False,
                    error="cert missing",
                    message=f"CA 证书不存在：{cert}（自动生成后仍未找到，请先启动一次抓包）",
                )
        try:
            proc = _run(["certutil", "-addstore", "-user", "Root", str(cert)], timeout=60)
        except Exception as exc:  # noqa: BLE001
            return InstallResult(ok=False, error=str(exc), message=f"certutil 调用失败：{exc}")
        if proc.returncode != 0:
            return InstallResult(
                ok=False,
                error=(proc.stderr or proc.stdout or "").strip(),
                message="证书安装失败（certutil 非零退出）",
            )
        # 诚实返回：非零退出之外还要真的校验一遍，避免"命令成功但证书没进存储"
        if not self.status():
            return InstallResult(
                ok=False,
                error="trust not effective",
                message="证书命令已执行，但存储中未找到该 CA，请重试或手动导入",
            )
        return InstallResult(ok=True, message="CA 已安装到当前用户根证书存储")

    def status(self) -> bool:
        """Root 用户存储中是否已信任**本应用的这把** CA（按 SHA-1 指纹精确校验）。

        2026-09 修复：原先只判断 ``"mitmproxy" in stdout``，任何一把同名 CA
        （另一套数据目录、残留的旧 CA）都会误判为已信任 —— 与 mac 侧
        「孤儿信任条目」同类，都会导致显示已信任但拦截必然失败。
        """
        want = self._thumbprint()
        if not want:
            return False
        try:
            proc = _run(["certutil", "-user", "-store", "Root"], timeout=60)
        except Exception:  # noqa: BLE001
            return False
        if proc.returncode != 0:
            return False
        # certutil 输出形如 "Cert Hash(sha1): 4ae3e76e c743be13 ..."（带空格）
        compact = "".join((proc.stdout or "").split()).lower()
        return want in compact

    def _thumbprint(self) -> str:
        """证书 DER 的 SHA-1（certutil 的 thumbprint），取不到返回空串。"""
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.serialization import Encoding

            cert = self.cert_path()
            if not cert.exists():
                return ""
            raw = cert.read_bytes()
            try:
                loaded = x509.load_pem_x509_certificate(raw)
            except Exception:  # noqa: BLE001
                loaded = x509.load_der_x509_certificate(raw)
            der = loaded.public_bytes(Encoding.DER)
            import hashlib

            return hashlib.sha1(der).hexdigest()
        except Exception:  # noqa: BLE001
            return ""


class WinProxyManager(ProxyManager):
    needs_admin = False

    def __init__(self) -> None:
        self._backup: dict[str, str] | None = None
        self._lock = threading.Lock()

    def _open_key(self):
        import winreg  # type: ignore  # 惰性 import：mac 上无此模块

        return winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
        )

    @staticmethod
    def _read_state() -> dict[str, Any]:
        """当前 HKCU 代理三项；读不到给安全默认（不抛）。

        与 :meth:`enable` 里的备份逻辑读的是同一批值 —— 抽出来是为了让
        :meth:`recover_stale` 能在**没有内存备份**的情况下（崩溃重启后就是这样）
        判断「现在注册表里到底是不是我们自己留下的代理」。
        """
        out: dict[str, Any] = {"enable": 0, "server": "", "override": ""}
        try:
            import winreg  # type: ignore

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_QUERY_VALUE
            ) as key:
                for field, name in (
                    ("enable", "ProxyEnable"),
                    ("server", "ProxyServer"),
                    ("override", "ProxyOverride"),
                ):
                    try:
                        out[field] = winreg.QueryValueEx(key, name)[0]
                    except FileNotFoundError:
                        pass
        except Exception:  # noqa: BLE001
            return {"enable": 0, "server": "", "override": ""}
        return out

    @staticmethod
    def _notify() -> None:
        """InternetSetOption 刷新，让运行中的程序立即感知代理变更。"""
        import ctypes

        InternetSetOption = ctypes.windll.wininet.InternetSetOptionW
        InternetSetOption(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        InternetSetOption(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH

    def enable(self, port: int) -> ProxyResult:
        with self._lock:
            if self._backup is not None:
                return ProxyResult(ok=True, message="已处于本应用代理模式（跳过重复设置）")
            state = self._read_state()
            try:
                import winreg  # type: ignore

                with self._open_key() as key:
                    # 备份**先于**写入：万一写到一半失败（ProxyEnable 已置 1、
                    # ProxyServer 还没换），没有备份的话 disable() 就还原不回去，
                    # 机器会停在一个「开着代理但指向旧地址」的状态上。
                    self._backup = {
                        "ProxyEnable": str(state.get("enable") or 0),
                        "ProxyServer": str(state.get("server") or ""),
                        "ProxyOverride": str(state.get("override") or ""),
                    }
                    winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                    winreg.SetValueEx(
                        key, "ProxyServer", 0, winreg.REG_SZ, f"{_PROXY_HOST}:{int(port)}"
                    )
                    winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, "<local>")
                self._notify()
                return ProxyResult(
                    ok=True, message=f"系统代理已开启 {_PROXY_HOST}:{int(port)}（已备份原设置）"
                )
            except Exception as exc:  # noqa: BLE001
                return ProxyResult(ok=False, error=str(exc), message=f"开启系统代理失败：{exc}")

    def disable(self) -> ProxyResult:
        """恢复备份的原代理设置；无备份（非本应用开启）时安全 no-op。"""
        with self._lock:
            if self._backup is None:
                return ProxyResult(ok=True, message="无需恢复（未由本应用开启代理）")
            try:
                import winreg  # type: ignore

                with self._open_key() as key:
                    winreg.SetValueEx(
                        key,
                        "ProxyEnable",
                        0,
                        winreg.REG_DWORD,
                        int(str(self._backup.get("ProxyEnable") or 0)),
                    )
                    winreg.SetValueEx(
                        key,
                        "ProxyServer",
                        0,
                        winreg.REG_SZ,
                        str(self._backup.get("ProxyServer") or ""),
                    )
                    winreg.SetValueEx(
                        key,
                        "ProxyOverride",
                        0,
                        winreg.REG_SZ,
                        str(self._backup.get("ProxyOverride") or ""),
                    )
                self._backup = None
                self._notify()
                return ProxyResult(ok=True, message="已恢复原先系统代理设置")
            except Exception as exc:  # noqa: BLE001
                return ProxyResult(ok=False, error=str(exc), message=f"恢复系统代理失败：{exc}")

    def recover_stale(self, host: str = _PROXY_HOST, port: int = _OWN_PROXY_PORT) -> ProxyResult:
        """启动自愈：系统代理指向 host:port 但端口已死（上次异常退出残留）时关掉它。

        **Windows 上这条比 macOS 更要紧。** 这里的系统代理是 HKCU 里的一个全局开关，
        一旦进程崩在「代理已开」的状态，注册表就留在 127.0.0.1:8088 而端口没人监听
        —— 整机所有走 WinINet 的程序（浏览器、微信、各类客户端）**全部连不上网**，
        而且没有任何东西会去关它。mac 侧靠 networksetup 逐服务恢复（``mac.py`` 同名前缀
        的方法），Windows 侧此前直接继承 ``base`` 的空操作，等于没有自愈。

        判据必须来自**注册表**而不是内存里的 ``_backup``：崩溃重启后那个备份早就没了。
        """
        import socket

        up = False
        try:
            with socket.create_connection((host, port), timeout=0.5):
                up = True
        except OSError:
            up = False
        if up:
            return ProxyResult(ok=True, message="代理端口正常监听，无需恢复")

        state = self._read_state()
        if not int(state.get("enable") or 0):
            return ProxyResult(ok=True, message="未发现残留代理")
        if not _proxy_is_ours(str(state.get("server") or ""), host, port):
            # 指向别处的代理（用户自己的 Clash 等）**绝不能动** —— 那是人家的正常配置
            return ProxyResult(ok=True, message="未发现残留代理")
        try:
            import winreg  # type: ignore

            with self._open_key() as key:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
            self._notify()
        except Exception as exc:  # noqa: BLE001
            return ProxyResult(ok=False, error=str(exc), message=f"恢复残留系统代理失败：{exc}")
        return ProxyResult(
            ok=True,
            message=f"已恢复异常退出残留的系统代理（{host}:{port} 未监听）",
        )


def _bat_encoding() -> str:
    """写 ``.bat`` 该用的编码：**OEM 代码页**，既不是 UTF-8 也不是 ANSI。

    ``cmd.exe`` 是按控制台 OEM 代码页逐行解析批处理文件的。原先这里用
    ``encoding="utf-8"`` 写（2026-09 补齐时发现），只要里面插了非 ASCII 的路径就完蛋
    —— 而中文用户的 ``%USERPROFILE%``（也就是 ``%APPDATA%`` 和安装目录）里几乎必然
    有中文。表现是 ``xcopy`` 路径乱码 → **更新静默失败，可应用已经被 taskkill 掉了**。

    注意不能用 ``locale.getpreferredencoding()`` 顶上：那给的是 ANSI 代码页
    （西欧 1252），而 OEM 是 437 —— 两者并不相同。非 Windows 上（单测）没有
    ``GetOEMCP``，退回 locale 偏好编码即可，那里本来也不会真去执行这个文件。
    """
    try:
        import ctypes

        cp = int(ctypes.windll.kernel32.GetOEMCP())
        if cp:
            return f"cp{cp}"
    except Exception:  # noqa: BLE001
        pass
    import locale

    return locale.getpreferredencoding(False) or "utf-8"


def _payload_root(extract_dir: Path) -> Path:
    """解压目录里「真正该覆盖到安装目录上去」的那一层。

    便携 zip 的顶层是 ``MP Harvest/``（人手动解压出来是个整齐的文件夹，与 mac 的
    ``--keepParent`` 对称），而更新要的是把它**里面的东西**覆盖上去。直接
    ``xcopy`` 整个解压目录会得到 ``安装目录/MP Harvest/...`` 这个套娃 ——
    更新"成功"了，重启还是老版本（mac 侧靠脚本里 ``ls -d *.app`` 剥掉这一层）。

    只认「恰好一个子目录」：平铺的 zip（里面直接就是 exe 和 ``_internal/``）会同时
    有多个条目，那就用它本身。两种布局都能用。
    """
    try:
        entries = [p for p in extract_dir.iterdir() if not p.name.startswith("__MACOSX")]
    except OSError:
        return extract_dir
    dirs = [p for p in entries if p.is_dir()]
    files = [p for p in entries if p.is_file()]
    if len(dirs) == 1 and not files:
        return dirs[0]
    return extract_dir


def _build_apply_script(
    *, payload: Path, install_dir: Path, exe_name: str, pid: int, log_path: Path
) -> str:
    """生成「等应用退出 → 覆盖安装目录 → 重启」的批处理文本（可单测）。

    三处都是真机上踩过的坑：

    1. **等**应用真的退出，而不是 ``timeout /t 2``。原先那行在这里必然失败：
       ``timeout`` 要求有控制台输入，而冻结版是 ``console=False`` 的 GUI 程序，
       它拉起来的 cmd 没有控制台 → ``timeout`` 立刻报「不支持输入重定向」退出
       （被 ``>nul`` 藏住了），于是**根本没等**，xcopy 去覆盖一个还开着的 exe，
       报「拒绝访问」，更新失败。改用 ``ping -n`` 做延时（它不需要控制台）。
       轮询用 ``tasklist + find ".exe"``：``/FI "PID eq N"`` 已经限定了只有这一行，
       ``/NH`` 去掉表头，所以「输出里有没有 .exe」就是「进程还在不在」。
    2. **留日志**。批处理是在应用退出之后跑的，出了什么事没人看得见 —— 用户只会发现
       「点了更新，重启还是老版本」。把输出写到数据目录里，是唯一能事后排查的手段。
    3. 覆盖失败**不要**再启动应用（会启动出一个半旧半新的安装），留给用户手动处理。
    """
    exe_path = install_dir / exe_name
    return (
        "@echo off\r\n"
        f'del "{log_path}" 2>nul\r\n'
        "setlocal\r\n"
        "set /a __n=0\r\n"
        ":waitloop\r\n"
        f'tasklist /FI "PID eq {int(pid)}" /NH 2>nul | find /I ".exe" >nul\r\n'
        "if errorlevel 1 goto ready\r\n"
        "set /a __n+=1\r\n"
        "if %__n% GEQ 60 goto kill\r\n"
        "ping -n 2 127.0.0.1 >nul\r\n"
        "goto waitloop\r\n"
        ":kill\r\n"
        f'taskkill /PID {int(pid)} /F >>"{log_path}" 2>&1\r\n'
        "ping -n 2 127.0.0.1 >nul\r\n"
        ":ready\r\n"
        f'xcopy /E /Y /I "{payload}" "{install_dir}" >>"{log_path}" 2>&1\r\n'
        "if errorlevel 1 goto failed\r\n"
        f'>>"{log_path}" echo [OK] 覆盖完成，重启应用\r\n'
        f'start "" "{exe_path}"\r\n'
        "goto done\r\n"
        ":failed\r\n"
        f'>>"{log_path}" echo [FAIL] xcopy 非零退出，请手动解压覆盖：{install_dir}\r\n'
        ":done\r\n"
        "endlocal\r\n"
        'del "%~f0"\r\n'
    )


class WinUpdater(GithubUpdater):
    asset_suffix = ".zip"
    # CI 产物命名（.github/workflows/build-windows.yml）：MP-Harvest-win-<ver>.zip
    # 只在「GitHub API 被限流 → atom 兜底」时用来拼下载地址。**缺了它兜底拿不到
    # 下载地址**（base.py 的 _check_via_atom 靠它拼 URL），表现为限流时更新直接失效。
    asset_prefix = "MP-Harvest-win-"

    def apply(self, package_path: str | Path) -> None:
        """生成「等待退出 → 解压替换 → 重启」bat 并启动，随后退出进程。

        所有前置校验都放在 ``os._exit(0)`` **之前**：一旦退了进程，任何失败都只能
        留在日志里，用户看到的就是「点了更新，应用关了，然后再也没起来」。
        """
        import os
        import shutil
        import zipfile

        pkg = Path(package_path)
        if not pkg.exists():
            raise PlatformError(f"更新包不存在：{pkg}")
        if not paths.is_frozen():
            raise PlatformError("开发模式不支持应用内升级，请手动更新代码或重新安装")
        install_dir = Path(sys.executable).resolve().parent
        exe_name = Path(sys.executable).name

        # 安装目录可写性预检：便携版被解压到 C:\Program Files 或某个同步盘里时，
        # xcopy 会「拒绝访问」——而那是应用已经退出之后才发生的，用户无从知道。
        try:
            probe = install_dir / ".mp_harvest_write_probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except OSError as exc:
            raise PlatformError(
                f"安装目录不可写（{install_dir}）：{exc}。"
                "请把它解压到用户目录下再更新，或手动解压覆盖"
            ) from exc

        extract_dir = pkg.parent / "extracted"
        # 先清空：extractall 是**叠加**的，上一版残留的文件会与新版本混在一起，
        # 下一次 xcopy 就把两个版本搅成一份谁也认不出的安装。
        shutil.rmtree(extract_dir, ignore_errors=True)
        try:
            with zipfile.ZipFile(pkg) as zf:
                zf.extractall(extract_dir)
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"更新包解压失败：{exc}") from exc
        payload = _payload_root(extract_dir)
        # 解压出来的东西**必须**长得像一份安装：exe 与 _internal 都在。不校验的话，
        # 拿错平台的包（或压缩层级不对）也照样会把应用杀掉再装回去一堆没用的东西。
        missing = [n for n in (exe_name, "_internal") if not (payload / n).exists()]
        if missing:
            raise PlatformError(
                f"更新包内容不对，缺少 {'、'.join(missing)}：{payload}。"
                "可能下载到了其它平台的安装包，请重新检查更新"
            )

        update_dir = paths.data_dir() / "update"
        update_dir.mkdir(parents=True, exist_ok=True)
        script = update_dir / "apply_update.bat"
        script.write_text(
            _build_apply_script(
                payload=payload,
                install_dir=install_dir,
                exe_name=exe_name,
                pid=os.getpid(),
                log_path=update_dir / "apply_update.log",
            ),
            encoding=_bat_encoding(),
            # newline="" 必须显式给：默认的 newline=None 会把文本模式下的 "\n"
            # 翻译成 os.linesep，而我们脚本里**已经**写死了 "\r\n" ——
            # 在 Windows 上就会变成 "\r\r\n"（2026-09 补齐时发现）。cmd 对这种
            # 多余的回车通常能忍，但没必要赌。
            newline="",
        )
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            creationflags=_no_window(),
        )
        os._exit(0)


class WinPlatform(Platform):
    os_name = "win"

    def _make_ca(self) -> CaSetup:
        return WinCaSetup()

    def _make_proxy(self) -> ProxyManager:
        return WinProxyManager()

    def _make_updater(self) -> Updater:
        return WinUpdater()

    def shell_open(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            raise PlatformError(f"路径不存在：{p}")
        try:
            os_startfile = getattr(__import__("os"), "startfile")  # 惰性获取：mac 无此属性
            os_startfile(str(p))  # type: ignore[attr-defined]
        except AttributeError as exc:
            raise PlatformError("os.startfile 仅在 Windows 可用") from exc
        except OSError as exc:
            raise PlatformError(f"打开失败：{p}: {exc}") from exc

    def shell_reveal(self, path: str | Path) -> None:
        """``explorer /select,``：打开资源管理器并选中该文件。"""
        p = Path(path).expanduser()
        if not p.exists():
            raise PlatformError(f"路径不存在：{p}")
        try:
            # ⚠️ explorer 即使成功也常常返回非 0，**不能**按返回码判失败
            # （这是它几十年的老毛病）。只处理「起不来」的情况。
            subprocess.Popen(["explorer", f"/select,{p}"])  # noqa: S603,S607
        except OSError as exc:
            raise PlatformError(f"定位失败：{p}: {exc}") from exc
