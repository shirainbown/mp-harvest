"""Windows 平台适配（设计稿 §4）。

- CA：``certutil -addstore -user Root``（用户存储，无需管理员）
- 代理：HKCU 注册表 ProxyEnable/ProxyServer + InternetSetOption 刷新（用户权限）
- 打开文件：``os.startfile``
- 更新：退出 → 替换目录 → 重启脚本

注意：``winreg`` / ``ctypes.windll`` / ``os.startfile`` 仅在 Windows 存在，
必须**函数内惰性 import**，保证本模块在 macOS 上可正常导入（契约测试用）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

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
            return InstallResult(
                ok=False,
                needs_admin=False,
                error="cert missing",
                message=f"CA 证书不存在：{cert}（请先启动一次抓包以生成证书）",
            )
        try:
            proc = subprocess.run(
                ["certutil", "-addstore", "-user", "Root", str(cert)],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001
            return InstallResult(ok=False, error=str(exc), message=f"certutil 调用失败：{exc}")
        if proc.returncode != 0:
            return InstallResult(
                ok=False,
                error=(proc.stderr or proc.stdout or "").strip(),
                message="证书安装失败（certutil 非零退出）",
            )
        return InstallResult(ok=True, message="CA 已安装到当前用户根证书存储")

    def status(self) -> bool:
        """Root 用户存储中是否已信任 mitmproxy CA。"""
        try:
            proc = subprocess.run(
                ["certutil", "-user", "-store", "Root"],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except Exception:
            return False
        if proc.returncode != 0:
            return False
        return "mitmproxy" in (proc.stdout or "").lower()


class WinProxyManager(ProxyManager):
    needs_admin = False

    def __init__(self) -> None:
        self._backup: dict[str, str] | None = None

    def _open_key(self):
        import winreg  # type: ignore  # 惰性 import：mac 上无此模块

        return winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _REG_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
        )

    @staticmethod
    def _notify() -> None:
        """InternetSetOption 刷新，让运行中的程序立即感知代理变更。"""
        import ctypes

        InternetSetOption = ctypes.windll.wininet.InternetSetOptionW
        InternetSetOption(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
        InternetSetOption(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH

    def enable(self, port: int) -> ProxyResult:
        if self._backup is not None:
            return ProxyResult(ok=True, message="已处于本应用代理模式（跳过重复设置）")
        try:
            import winreg  # type: ignore

            with self._open_key() as key:
                try:
                    prev_enable = winreg.QueryValueEx(key, "ProxyEnable")[0]
                except FileNotFoundError:
                    prev_enable = 0
                try:
                    prev_server = winreg.QueryValueEx(key, "ProxyServer")[0]
                except FileNotFoundError:
                    prev_server = ""
                try:
                    prev_override = winreg.QueryValueEx(key, "ProxyOverride")[0]
                except FileNotFoundError:
                    prev_override = ""
                self._backup = {
                    "ProxyEnable": str(prev_enable),
                    "ProxyServer": str(prev_server),
                    "ProxyOverride": str(prev_override),
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


class WinUpdater(GithubUpdater):
    asset_suffix = ".zip"

    def apply(self, package_path: str | Path) -> None:
        """生成「等待退出 → 解压替换 → 重启」bat 并启动，随后退出进程。"""
        import os
        import zipfile

        pkg = Path(package_path)
        if not pkg.exists():
            raise PlatformError(f"更新包不存在：{pkg}")
        if not paths.is_frozen():
            raise PlatformError("开发模式不支持应用内升级，请手动更新代码或重新安装")
        install_dir = Path(sys.executable).resolve().parent if paths.is_frozen() else paths.app_root()
        extract_dir = pkg.parent / "extracted"
        try:
            with zipfile.ZipFile(pkg) as zf:
                zf.extractall(extract_dir)
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"更新包解压失败：{exc}") from exc
        exe_name = Path(sys.executable).name if paths.is_frozen() else "MP Harvest.exe"
        script = paths.data_dir() / "update" / "apply_update.bat"
        script.write_text(
            "@echo off\r\n"
            f"taskkill /PID {os.getpid()} /F >nul 2>&1\r\n"
            "timeout /t 2 /nobreak >nul\r\n"
            f'xcopy /E /Y /I "{extract_dir}" "{install_dir}" >nul\r\n'
            f'start "" "{install_dir / exe_name}"\r\n'
            'del "%~f0"\r\n',
            encoding="utf-8",
        )
        subprocess.Popen(
            ["cmd", "/c", str(script)],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
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
