"""mitmproxy CA 准备（跨平台，平移自旧版 ca_setup.py 的 prepare_mitm_confdir）。

供 infra/mitm/mitm_capture 启动前调用：确保 confdir 内有带私钥的
``mitmproxy-ca.pem``（代理签名必需），并在数据目录放好公钥 ``.cer/.pem``
供 ``platform.ca.install()`` 信任。

信任安装本身在 ``mac.py`` / ``win.py`` 的 CaSetup 中实现（平台差异）。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from mp_harvest.infra.platform.paths import data_dir

# 公钥分发（可只含证书）；代理签名必须带私钥
P12_PUBLIC = "mitmproxy-ca-cert.p12"
P12_FULL = "mitmproxy-ca.p12"
PEM_CA = "mitmproxy-ca.pem"
CER_NAME = "mitmproxy-ca-cert.cer"

PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8088


def resource_roots(app_root: Path) -> list[Path]:
    roots = [Path(app_root)]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass))
    home_mitm = Path.home() / ".mitmproxy"
    if home_mitm.is_dir():
        roots.append(home_mitm)
    return roots


def _find(app_root: Path, name: str) -> Path | None:
    for root in resource_roots(app_root):
        p = root / name
        if p.is_file():
            return p
    return None


def ensure_beside(app_root: Path, name: str) -> Path | None:
    dest = Path(app_root) / name
    if dest.is_file():
        return dest
    src = _find(app_root, name)
    if src is None:
        return None
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest if dest.is_file() else None


def confdir() -> Path:
    d = data_dir() / "mitm_conf"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _export_cer_from_p12(p12_path: Path, cer_path: Path) -> bool:
    from cryptography.hazmat.primitives.serialization import Encoding, pkcs12

    data = p12_path.read_bytes()
    for pwd in (b"", b"mitmproxy", None):
        try:
            key, cert, extra = pkcs12.load_key_and_certificates(data, pwd)
        except Exception:
            continue
        chosen = cert
        if chosen is None and extra:
            chosen = extra[0]
        if chosen is None:
            continue
        cer_path.write_bytes(chosen.public_bytes(Encoding.DER))
        return True
    try:
        from cryptography.hazmat.primitives.serialization.pkcs12 import load_pkcs12

        for pwd in (b"", None):
            try:
                obj = load_pkcs12(data, pwd)
            except Exception:
                continue
            c = obj.cert.certificate if obj.cert else None
            if c is None and obj.additional_certs:
                c = obj.additional_certs[0].certificate
            if c is not None:
                cer_path.write_bytes(c.public_bytes(Encoding.DER))
                return True
    except Exception:
        pass
    return False


def prepare_mitm_confdir(app_root: Path) -> tuple[Path, str]:
    """确保 confdir 有 mitmproxy-ca.pem（证书+私钥），返回 (confdir, 消息)。

    优先 confdir 已有 → 捆绑/相邻 PEM → 完整 p12 提取；公钥 p12 无法签名则
    明确报错。同时把公钥 cert（``mitmproxy-ca-cert.cer``）放到数据目录，
    供 ``platform.ca.cert_path()`` 使用。
    """
    cdir = confdir()
    dest_pem = cdir / "mitmproxy-ca.pem"
    pub_dir = data_dir()

    # 1) 已准备好
    if dest_pem.is_file() and dest_pem.stat().st_size > 100:
        return cdir, f"已使用代理证书目录：{cdir}"

    # 2) 捆绑/相邻 PEM（含私钥）
    pem = _find(app_root, PEM_CA)
    if pem is not None:
        shutil.copy2(pem, dest_pem)
        try:
            from cryptography import x509
            from cryptography.hazmat.primitives.serialization import Encoding

            text = dest_pem.read_text(encoding="utf-8")
            begin = text.find("-----BEGIN CERTIFICATE-----")
            end = text.find("-----END CERTIFICATE-----")
            if begin >= 0 and end > begin:
                block = text[begin : end + len("-----END CERTIFICATE-----")]
                cert = x509.load_pem_x509_certificate(block.encode())
                (pub_dir / CER_NAME).write_bytes(cert.public_bytes(Encoding.DER))
        except Exception:
            pass
        return cdir, f"已从 {pem.name} 准备代理 CA → {cdir}"

    # 3) 完整 p12（含私钥）提取 PEM
    from cryptography.hazmat.primitives.serialization import (
        Encoding,
        NoEncryption,
        PrivateFormat,
        pkcs12,
    )

    for name in (P12_FULL, P12_PUBLIC):
        p12 = _find(app_root, name)
        if p12 is None:
            continue
        data = p12.read_bytes()
        for pwd in (b"", b"mitmproxy", None):
            try:
                key, cert, extra = pkcs12.load_key_and_certificates(data, pwd)
            except Exception:
                continue
            if key is None or cert is None:
                continue
            pem_bytes = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            pem_bytes += cert.public_bytes(Encoding.PEM)
            dest_pem.write_bytes(pem_bytes)
            (pub_dir / CER_NAME).write_bytes(cert.public_bytes(Encoding.DER))
            return cdir, f"已从 {p12.name} 提取代理 CA → {cdir}"

    # 4) 只有公钥 p12：导出 cer 供安装，但无法抓包——明确报错
    pub = _find(app_root, P12_PUBLIC)
    if pub is not None:
        _export_cer_from_p12(pub, pub_dir / CER_NAME)
        raise RuntimeError(
            f"{P12_PUBLIC} 只有公钥、没有私钥，无法启动抓包代理。\n"
            f"请把本机完整 CA 放入程序目录后重试：{PEM_CA}（推荐，来自 ~/.mitmproxy）或 {P12_FULL}"
        )

    # 5) 本机自动生成全新代理 CA（与 mitmdump 首次运行等价；开发模式无捆绑证书时兜底）
    try:
        from mitmproxy import certs
    except Exception as exc:  # noqa: BLE001
        raise FileNotFoundError(
            f"未找到代理 CA，且无法自动生成（mitmproxy 不可用：{exc}）。\n"
            f"请将 {PEM_CA} 或 {P12_FULL} 放到：{app_root}，或先运行一次 mitmdump 生成 ~/.mitmproxy"
        ) from exc
    try:
        certs.CertStore.from_store(cdir, basename="mitmproxy", key_size=2048)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"自动生成代理 CA 失败：{exc}") from exc
    if not dest_pem.is_file() or dest_pem.stat().st_size <= 100:
        raise FileNotFoundError(
            f"自动生成 CA 后仍缺少可用的 {PEM_CA}，"
            f"请手动将 {PEM_CA} 或 {P12_FULL} 放到：{app_root}"
        )
    # 导出公钥 .cer 供 platform.ca.install() 安装信任
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding

        text = dest_pem.read_text(encoding="utf-8")
        begin = text.find("-----BEGIN CERTIFICATE-----")
        end = text.find("-----END CERTIFICATE-----")
        if begin >= 0 and end > begin:
            block = text[begin : end + len("-----END CERTIFICATE-----")]
            cert = x509.load_pem_x509_certificate(block.encode())
            (pub_dir / CER_NAME).write_bytes(cert.public_bytes(Encoding.DER))
    except Exception:  # noqa: BLE001
        pass
    return cdir, f"已生成本机全新代理 CA → {cdir}（如需微信抓包请先安装信任）"


def public_cert_path() -> Path:
    """公钥证书路径（.cer 优先，其次 ~/.mitmproxy 默认证书）。"""
    cer = data_dir() / CER_NAME
    if cer.is_file():
        return cer
    home_pem = Path.home() / ".mitmproxy" / "mitmproxy-ca-cert.pem"
    if home_pem.is_file():
        return home_pem
    return cer  # 不存在也返回预期路径，由调用方判断 exists()
