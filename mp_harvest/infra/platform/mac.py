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


def _patch_trust_plist(
    data: dict,
    *,
    fingerprint: str,
    subject_der: bytes,
    serial_bytes: bytes,
) -> bool:
    """按证书 SHA-1 指纹把 trustList 条目设为显式 TrustRoot；缺失则新增。

    mkcert 同款方案：仅 ``add-trusted-cert`` 在部分 macOS 上不会建立信任设置。
    2026-08-09 修复：不能按 ``issuerName`` 匹配——开发/打包两把 CA 同名同
    issuer，会互相误改；trustList 的 key 就是证书 SHA-1 指纹。
    """
    import base64

    settings = [
        {
            "kSecTrustSettingsPolicy": base64.b64decode("KoZIhvdjZAED"),
            "kSecTrustSettingsPolicyName": "sslServer",
            "kSecTrustSettingsResult": 1,
        },
        {
            "kSecTrustSettingsPolicy": base64.b64decode("KoZIhvdjZAEC"),
            "kSecTrustSettingsPolicyName": "basicX509",
            "kSecTrustSettingsResult": 1,
        },
    ]
    trust_list = data.get("trustList")
    if not isinstance(trust_list, dict):
        return False
    entry = trust_list.get(fingerprint)
    if isinstance(entry, dict):
        entry["trustSettings"] = settings
    else:
        trust_list[fingerprint] = {
            "issuerName": subject_der,
            "serialNumber": serial_bytes,
            "trustSettings": settings,
        }
    return True


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
        """弹系统授权框安装 CA，并补写显式信任设置（2026-08-09 修复）。"""
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
        stderr = (proc.stderr or "").strip()
        if proc.returncode != 0 and ("User canceled" in stderr or "-128" in stderr):
            return InstallResult(
                ok=False, needs_admin=True, error="user canceled", message="用户取消了授权"
            )
        if proc.returncode != 0:
            # 回退：sudo 直接执行（终端场景）
            try:
                proc2 = _run(
                    ["sudo", "-n", "security", "add-trusted-cert", "-d", "-r",
                     "trustRoot", "-k", _SYSTEM_KEYCHAIN, str(cert)],
                    timeout=120,
                )
                if proc2.returncode != 0:
                    stderr = (proc2.stderr or stderr).strip()
            except Exception:
                pass
            if stderr:
                return InstallResult(
                    ok=False,
                    needs_admin=True,
                    error=stderr or "unknown",
                    message=f"证书安装失败：{stderr or 'security 命令非零退出'}",
                )
        # 关键：add-trusted-cert 在部分 macOS 上不建立显式信任设置 → 补 trust-settings-import
        err = self._import_trust(cert)
        if err:
            return InstallResult(
                ok=False,
                needs_admin=True,
                error=err,
                message=f"证书已加入钥匙串，但信任设置写入失败：{err}",
            )
        if self.status():
            return InstallResult(
                ok=True, needs_admin=True, message="CA 已安装并信任（系统钥匙串 + 显式信任设置）"
            )
        return InstallResult(
            ok=False,
            needs_admin=True,
            error="trust not effective",
            message="CA 已加入钥匙串，但系统信任校验未通过，请检查「系统设置 → 隐私与安全性」中的证书信任",
        )

    def _import_trust(self, cert: Path) -> str | None:
        """trust-settings-export → 补显式 TrustRoot → import（用户域，无需管理员）。"""
        import os
        import plistlib
        import tempfile

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes

        raw = cert.read_bytes()
        try:
            loaded = x509.load_pem_x509_certificate(raw)
        except Exception:  # noqa: BLE001
            try:
                loaded = x509.load_der_x509_certificate(raw)
            except Exception as exc:  # noqa: BLE001
                return f"解析 CA 证书失败：{exc}"
        subject_der = loaded.subject.public_bytes()
        fingerprint = loaded.fingerprint(hashes.SHA1()).hex().upper()
        serial = loaded.serial_number
        serial_bytes = serial.to_bytes((serial.bit_length() + 7) // 8, "big")
        fd, plist_path = tempfile.mkstemp(prefix="mp-harvest-trust-", suffix=".plist")
        os.close(fd)
        try:
            p = _run(["security", "trust-settings-export", "-d", plist_path], timeout=60)
            if p.returncode != 0:
                return f"trust-settings-export 失败：{(p.stderr or '').strip()}"
            with open(plist_path, "rb") as fh:
                data = plistlib.load(fh)
            if not _patch_trust_plist(
                data,
                fingerprint=fingerprint,
                subject_der=subject_der,
                serial_bytes=serial_bytes,
            ):
                return "无法写入信任条目（trustList 结构异常）"
            with open(plist_path, "wb") as fh:
                plistlib.dump(data, fh, fmt=plistlib.FMT_XML)
            p2 = _run(["security", "trust-settings-import", "-d", plist_path], timeout=60)
            if p2.returncode != 0:
                return f"trust-settings-import 失败：{(p2.stderr or '').strip()}"
            return None
        finally:
            try:
                os.unlink(plist_path)
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _applescript_quote(cmd: str) -> str:
        return '"' + cmd.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def status(self) -> bool:
        """CA 是否**真正**被系统信任：按证书 SHA-1 指纹查信任设置。

        2026-08-09 修复：不能按「信任列表里存在名为 mitmproxy 的条目」判断——
        多套数据目录各有一把 CA 时会被另一把误判；``security verify-cert`` 含
        CT/网络校验、结果不稳定，也不适用。trust-settings-export 的 trustList
        key 即证书 SHA-1，按指纹精确且确定。
        """
        try:
            cert = self.cert_path()
            if not cert.exists():
                return False
            from cryptography import x509
            from cryptography.hazmat.primitives import hashes

            raw = cert.read_bytes()
            try:
                loaded = x509.load_pem_x509_certificate(raw)
            except Exception:  # noqa: BLE001
                loaded = x509.load_der_x509_certificate(raw)
            fingerprint = loaded.fingerprint(hashes.SHA1()).hex().upper()
            import os
            import plistlib
            import tempfile

            for domain in ("-d", "-s"):
                fd, plist_path = tempfile.mkstemp(suffix=".plist")
                os.close(fd)
                try:
                    proc = _run(
                        ["security", "trust-settings-export", domain, plist_path],
                        timeout=30,
                    )
                    if proc.returncode != 0:
                        continue
                    with open(plist_path, "rb") as fh:
                        data = plistlib.load(fh)
                    entry = (data.get("trustList") or {}).get(fingerprint)
                    if isinstance(entry, dict) and entry.get("trustSettings"):
                        return True
                except Exception:  # noqa: BLE001
                    continue
                finally:
                    try:
                        os.unlink(plist_path)
                    except Exception:  # noqa: BLE001
                        pass
        except Exception:  # noqa: BLE001
            return False
        return False


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


def _app_bundle_root(exe: Path) -> Path | None:
    """从可执行文件向上找 ``.app`` 包根（如 ``/Applications/MP Harvest.app``）。"""
    p = Path(exe).resolve()
    for _ in range(8):
        p = p.parent
        if p.name.endswith(".app"):
            return p
    return None


def _mac_install_dir() -> Path:
    """冻结 ``.app`` 时返回其所在目录（如 ``/Applications``）；否则回退 exe 目录。"""
    bundle = _app_bundle_root(sys.executable)
    if bundle is not None:
        return bundle.parent
    return Path(sys.executable).resolve().parent


def _build_apply_script(*, pkg: Path, install_dir: Path, pid: int) -> str:
    """生成「等待退出 → 解压 → 替换 .app → 重新打开」脚本文本（可单测）。

    目标目录不可写（如 /Applications 为 root 属主）时，用 ``osascript``
    弹管理员授权框，以 root 完成替换（2026-08-09 修复）。
    """
    pkg = Path(pkg)
    install_dir = Path(install_dir)
    core = pkg.parent / "apply_core.sh"
    core.write_text(
        "#!/bin/bash\n"
        'APP="$1"\n'
        f'rm -rf "{install_dir}/$APP"\n'
        f'mv "$APP" "{install_dir}/"\n',
        encoding="utf-8",
    )
    core.chmod(0o755)
    return (
        "#!/bin/bash\n"
        f"while kill -0 {int(pid)} 2>/dev/null; do sleep 0.5; done\n"
        f'cd "{pkg.parent}" || exit 1\n'
        f'ditto -xk "{pkg.name}" . || exit 1\n'
        'APP="$(ls -d *.app 2>/dev/null | head -1)"\n'
        '[ -n "$APP" ] || exit 1\n'
        f'if [ -w "{install_dir}" ]; then\n'
        f'  bash "{core}" "$APP"\n'
        "else\n"
        f'  osascript -e "do shell script \\"bash \'{core}\' \'$APP\'\\" with administrator privileges"\n'
        "fi\n"
        f'open "{install_dir}/$APP"\n'
        f'rm -f "{core}" "$0"\n'
    )


class MacUpdater(GithubUpdater):
    asset_suffix = ".zip"

    def apply(self, package_path: str | Path) -> None:
        """生成「等待退出 → 替换 .app → 重新打开」shell 脚本并后台启动。"""
        import os

        if not paths.is_frozen():
            raise PlatformError("开发模式不支持应用内升级，请手动更新代码或重新安装")
        pkg = Path(package_path)
        if not pkg.exists():
            raise PlatformError(f"更新包不存在：{pkg}")
        install_dir = _mac_install_dir()
        script = paths.data_dir() / "update" / "apply_update.sh"
        script.write_text(
            _build_apply_script(pkg=pkg, install_dir=install_dir, pid=os.getpid()),
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
