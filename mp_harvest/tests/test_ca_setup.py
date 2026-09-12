"""公共 CA 证书（``.cer``）与代理 PEM 的同步（2026-09）。

数据目录里有**两份**东西：

- ``mitm_conf/mitmproxy-ca.pem`` —— 带私钥，代理签名用；
- ``mitmproxy-ca-cert.cer`` —— 只有公钥，``platform.ca.install()`` 装信任用。

原先 ``prepare_mitm_confdir`` 只在「从捆绑 PEM / p12 生成」的分支里导出 ``.cer``，
走「confdir 里已经有 PEM」那条早退分支时**不导出**。于是只要用户清理过数据目录、
或者上次写 ``.cer`` 时静默失败，就会卡在「PEM 在、.cer 不在」：抓包跑得好好的，
点「安装 CA 证书」却永远报「CA 证书不存在」—— 死循环。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.infra.platform import ca_setup  # noqa: E402


def _make_ca_pem(path: Path, common_name: str = "mitmproxy") -> None:
    """造一把自签 CA 的 PEM（证书 + 私钥），返回 None。"""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        + cert.public_bytes(serialization.Encoding.PEM)
    )


@pytest.fixture()
def data_dir(monkeypatch, tmp_path):
    d = tmp_path / "data"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ca_setup, "data_dir", lambda: d)
    return d


def test_sync_public_cert_exports_der(data_dir, tmp_path):
    pem = tmp_path / "mitmproxy-ca.pem"
    _make_ca_pem(pem)
    assert ca_setup.sync_public_cert(pem) is True
    cer = data_dir / ca_setup.CER_NAME
    assert cer.is_file()
    # 必须是 DER（不是 PEM 文本），certutil / 钥匙串都按 DER 吃
    assert cer.read_bytes()[:1] == b"\x30"


def test_sync_public_cert_replaces_stale_cer(data_dir, tmp_path):
    """换了一把 CA 之后，.cer 必须跟着换 —— 否则会把**旧 CA** 装进信任。"""
    cer = data_dir / ca_setup.CER_NAME
    _make_ca_pem(tmp_path / "a.pem", "first")
    ca_setup.sync_public_cert(tmp_path / "a.pem")
    first = cer.read_bytes()
    _make_ca_pem(tmp_path / "b.pem", "second")
    ca_setup.sync_public_cert(tmp_path / "b.pem")
    assert cer.read_bytes() != first


def test_sync_public_cert_removes_cer_when_export_fails(data_dir, tmp_path):
    """导不出来就把旧的删掉：装上一把不是代理在用的 CA 比装不上更糟。

    那种情况界面会显示「已信任」，而拦截时握手全失败 —— 完全看不出原因。
    """
    pem = tmp_path / "mitmproxy-ca.pem"
    _make_ca_pem(pem)
    ca_setup.sync_public_cert(pem)
    cer = data_dir / ca_setup.CER_NAME
    assert cer.is_file()
    # 把 PEM 换成垃圾内容，导出必然失败
    pem.write_text("not a certificate", encoding="utf-8")
    assert ca_setup.sync_public_cert(pem) is False
    assert not cer.exists()


def test_prepare_confdir_backfills_cer_from_existing_pem(data_dir, tmp_path):
    """走「confdir 里已有 PEM」的早退分支时，也要把缺的 .cer 补上。"""
    cdir = ca_setup.confdir()
    pem = cdir / ca_setup.PEM_CA
    _make_ca_pem(pem)
    assert not (data_dir / ca_setup.CER_NAME).exists()

    cdir2, msg = ca_setup.prepare_mitm_confdir(tmp_path)
    assert cdir2 == cdir
    assert "已使用代理证书目录" in msg
    assert (data_dir / ca_setup.CER_NAME).is_file(), "早退分支漏了导出公钥证书"
