"""macOS 平台适配（设计稿 §4）。

- CA：``security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain``
  需管理员；优先用 ``osascript ... with administrator privileges`` 弹系统授权框。
- 代理：``networksetup`` 遍历**所有**网络服务（跳过 ``*`` 禁用项）
  setwebproxy / setsecurewebproxy，关闭用 setwebproxystate off。
- 打开文件：``open`` 命令。
- 更新：.app 整体替换脚本（退出后执行）。

失败一律返回结构化结果，绝不静默。
"""

from __future__ import annotations

import shlex
import subprocess
import sys
import threading
from pathlib import Path

from mp_harvest.infra.platform import paths
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

_PROXY_HOST = "127.0.0.1"
_SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"


def default_cert_path() -> Path:
    """公钥 CA 路径：数据目录 .cer 优先，其次 ``~/.mitmproxy/`` 默认证书。"""
    from mp_harvest.infra.platform.ca_setup import public_cert_path

    return public_cert_path()


def _run(cmd: list[str], timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


class MacCaSetup(CaSetup):
    needs_admin = True

    def __init__(self, cert: Path | None = None) -> None:
        self._cert = cert or default_cert_path()

    def cert_path(self) -> Path:
        return self._cert

    def _security_cmd(self) -> str:
        return (
            f"security add-trusted-cert -d -r trustRoot -k {_SYSTEM_KEYCHAIN} "
            f"{shlex.quote(str(self.cert_path()))}"
        )

    def install(self) -> InstallResult:
        """弹系统授权框安装 CA；用户取消或失败都返回结构化错误。"""
        cert = self.cert_path()
        if not cert.exists():
            return InstallResult(
                ok=False,
                needs_admin=True,
                error="cert missing",
                message=f"CA 证书不存在：{cert}（请先启动一次抓包以生成证书）",
            )
        # 优先 osascript 弹原生授权对话框（UX 比 sudo 读密码好）
        script = f'do shell script {self._applescript_quote(self._security_cmd())} with administrator privileges'
        try:
            proc = _run(["osascript", "-e", script], timeout=300)
        except Exception as exc:  # noqa: BLE001
            return InstallResult(
                ok=False, needs_admin=True, error=str(exc), message=f"调起系统授权失败：{exc}"
            )
        if proc.returncode == 0:
            return InstallResult(ok=True, needs_admin=True, message="CA 已安装并信任（系统钥匙串）")
        stderr = (proc.stderr or "").strip()
        if "User canceled" in stderr or "-128" in stderr:
            return InstallResult(
                ok=False, needs_admin=True, error="user canceled", message="用户取消了授权"
            )
        # 回退：sudo 直接执行（终端场景）
        try:
            proc2 = _run(["sudo", "-n", "security", "add-trusted-cert", "-d", "-r",
                          "trustRoot", "-k", _SYSTEM_KEYCHAIN, str(cert)], timeout=120)
            if proc2.returncode == 0:
                return InstallResult(
                    ok=True, needs_admin=True, message="CA 已安装并信任（sudo -n 回退）"
                )
            stderr = (proc2.stderr or stderr).strip()
        except Exception:
            pass
        return InstallResult(
            ok=False,
            needs_admin=True,
            error=stderr or "unknown",
            message=f"证书安装失败：{stderr or 'security 命令非零退出'}",
        )

    @staticmethod
    def _applescript_quote(cmd: str) -> str:
        return '"' + cmd.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def status(self) -> bool:
        """系统钥匙串中是否已存在 mitmproxy CA（信任设置已由 add-trusted-cert 建立）。"""
        try:
            proc = _run(
                ["security", "find-certificate", "-c", "mitmproxy", _SYSTEM_KEYCHAIN],
                timeout=30,
            )
        except Exception:
            return False
        return proc.returncode == 0


class MacProxyManager(ProxyManager):
    needs_admin = True

    def __init__(self) -> None:
        self._backup: dict[str, dict[str, dict[str, str]]] | None = None
        self._lock = threading.Lock()

    @staticmethod
    def list_services() -> list[str]:
        """所有启用的网络服务名（跳过带 ``*`` 的禁用项与表头）。"""
        proc = _run(["networksetup", "-listallnetworkservices"], timeout=30)
        if proc.returncode != 0:
            raise PlatformError(
                f"networksetup -listallnetworkservices 失败：{(proc.stderr or '').strip()}"
            )
        services: list[str] = []
        for line in (proc.stdout or "").splitlines():
            name = line.strip()
            if not name or name.startswith("*"):
                continue
            if "denotes that a network service is disabled" in name:
                continue  # 表头说明行
            services.append(name)
        return services

    @staticmethod
    def _read_state(svc: str, kind: str) -> dict[str, str]:
        """读取某网络服务当前代理状态：{enabled, server, port}（kind=webproxy/securewebproxy）。"""
        out = {"enabled": "No", "server": "", "port": ""}
        try:
            proc = _run(["networksetup", f"-get{kind}", svc], timeout=15)
        except Exception:  # noqa: BLE001
            return out
        for line in (proc.stdout or "").splitlines():
            key, _, val = line.partition(":")
            k = key.strip().lower()
            if k == "enabled":
                out["enabled"] = val.strip()
            elif k == "server":
                out["server"] = val.strip()
            elif k == "port":
                out["port"] = val.strip()
        return out

    def _apply(self, args_for_service: list[list[str]]) -> ProxyResult:
        try:
            services = self.list_services()
        except PlatformError as exc:
            return ProxyResult(ok=False, error=str(exc), message=str(exc))
        if not services:
            return ProxyResult(ok=False, error="no services", message="未找到可用网络服务")
        failures: dict[str, str] = {}
        for svc in services:
            for tail in args_for_service:
                proc = _run(["networksetup", *tail[:1], svc, *tail[1:]], timeout=30)
                if proc.returncode != 0:
                    failures[svc] = (proc.stderr or proc.stdout or "").strip() or "非零退出"
        if failures:
            return ProxyResult(
                ok=False,
                error="; ".join(f"{k}: {v}" for k, v in failures.items()),
                message=f"部分网络服务设置失败（{len(failures)}/{len(services)}）",
                details={"services": services, "failures": failures},
            )
        return ProxyResult(
            ok=True,
            message=f"已应用到 {len(services)} 个网络服务",
            details={"services": services},
        )

    def enable(self, port: int) -> ProxyResult:
        """备份各网络服务当前代理设置，再统一指向 127.0.0.1:port（设计稿 §4）。"""
        port = int(port)
        with self._lock:
            if self._backup is not None:
                # 已处于本应用代理模式：不覆盖备份，避免恢复时还原成 8088 自身
                return ProxyResult(ok=True, message="已处于本应用代理模式（跳过重复设置）")
            try:
                services = self.list_services()
            except PlatformError as exc:
                return ProxyResult(ok=False, error=str(exc), message=str(exc))
            if not services:
                return ProxyResult(ok=False, error="no services", message="未找到可用网络服务")
            backup: dict[str, dict[str, dict[str, str]]] = {}
            failures: dict[str, str] = {}
            for svc in services:
                backup[svc] = {
                    "webproxy": self._read_state(svc, "webproxy"),
                    "securewebproxy": self._read_state(svc, "securewebproxy"),
                }
                for kind in ("webproxy", "securewebproxy"):
                    proc = _run(
                        ["networksetup", f"-set{kind}", svc, _PROXY_HOST, str(port)],
                        timeout=30,
                    )
                    if proc.returncode != 0:
                        failures[svc] = (proc.stderr or proc.stdout or "").strip() or "非零退出"
            self._backup = backup
            if failures:
                return ProxyResult(
                    ok=False,
                    error="; ".join(f"{k}: {v}" for k, v in failures.items()),
                    message=f"部分网络服务设置失败（{len(failures)}/{len(services)}）",
                    details={"services": services, "failures": failures},
                )
            return ProxyResult(
                ok=True,
                message=f"已开启系统代理并备份原设置（{len(services)} 个网络服务）",
                details={"services": services},
            )

    def disable(self) -> ProxyResult:
        """恢复备份的原代理设置；无备份（非本应用开启）时安全 no-op。"""
        with self._lock:
            if not self._backup:
                return ProxyResult(ok=True, message="无需恢复（未由本应用开启代理）")
            services = list(self._backup)
            failures: dict[str, str] = {}
            for svc, state in self._backup.items():
                for kind in ("webproxy", "securewebproxy"):
                    prev = state.get(kind) or {}
                    try:
                        if str(prev.get("enabled") or "").lower() == "yes":
                            server = str(prev.get("server") or "").strip()
                            port = str(prev.get("port") or "").strip()
                            cmds: list[list[str]] = []
                            if server and port:
                                cmds.append(["networksetup", f"-set{kind}", svc, server, port])
                            cmds.append(["networksetup", f"-set{kind}state", svc, "on"])
                        else:
                            cmds = [["networksetup", f"-set{kind}state", svc, "off"]]
                        for args in cmds:
                            proc = _run(args, timeout=30)
                            if proc.returncode != 0:
                                failures[svc] = (proc.stderr or proc.stdout or "").strip() or "非零退出"
                                break
                    except Exception as exc:  # noqa: BLE001
                        failures[svc] = str(exc)
            self._backup = None
            if failures:
                return ProxyResult(
                    ok=False,
                    error="; ".join(f"{k}: {v}" for k, v in failures.items()),
                    message=f"部分网络服务恢复失败（{len(failures)}/{len(services)}）",
                    details={"services": services, "failures": failures},
                )
            return ProxyResult(
                ok=True, message=f"已恢复原系统代理设置（{len(services)} 个网络服务）"
            )


class MacUpdater(GithubUpdater):
    asset_suffix = ".zip"

    def apply(self, package_path: str | Path) -> None:
        """生成「等待退出 → 替换 .app → 重新打开」shell 脚本并后台启动。"""
        import os

        pkg = Path(package_path)
        if not pkg.exists():
            raise PlatformError(f"更新包不存在：{pkg}")
        install_dir = Path(sys.executable).resolve().parent if paths.is_frozen() else paths.app_root()
        script = paths.data_dir() / "update" / "apply_update.sh"
        script.write_text(
            "#!/bin/bash\n"
            f"while kill -0 {os.getpid()} 2>/dev/null; do sleep 0.5; done\n"
            f'cd "{pkg.parent}" && ditto -xk "{pkg.name}" . || exit 1\n'
            # 解压出的 .app 整体覆盖安装目录（公证 zip 解压即带签名）
            f'for app in *.app; do rm -rf "{install_dir}/$app" && mv "$app" "{install_dir}/"; done\n'
            f'open "{install_dir}"\n'
            'rm -f "$0"\n',
            encoding="utf-8",
        )
        script.chmod(0o755)
        subprocess.Popen(["/bin/bash", str(script)], start_new_session=True)
        os._exit(0)


class MacPlatform(Platform):
    os_name = "mac"

    def _make_ca(self) -> CaSetup:
        return MacCaSetup()

    def _make_proxy(self) -> ProxyManager:
        return MacProxyManager()

    def _make_updater(self) -> Updater:
        return MacUpdater()

    def shell_open(self, path: str | Path) -> None:
        p = Path(path)
        if not p.exists():
            raise PlatformError(f"路径不存在：{p}")
        try:
            proc = _run(["open", str(p)], timeout=30)
        except Exception as exc:  # noqa: BLE001
            raise PlatformError(f"open 调用失败：{p}: {exc}") from exc
        if proc.returncode != 0:
            raise PlatformError(f"open 失败：{p}: {(proc.stderr or '').strip()}")
