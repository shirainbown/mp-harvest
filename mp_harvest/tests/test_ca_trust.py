"""CA 信任检查 + 抓包代理守卫测试（2026-08-09：防止未信任 CA 时劫持全机 HTTPS）。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.infra.platform.mac import _patch_trust_plist  # noqa: E402


def test_patch_trust_plist_updates_by_fingerprint():
    fp = "695B4025F6A349F34D65BADA54A0F15AD3FD9A96"
    subject = b"subject-der"
    data = {
        "trustVersion": 1,
        "trustList": {
            fp: {"issuerName": subject, "serialNumber": b"ser"},
            # 同名 issuer 的另一把 CA（开发版）：不应被误改
            "EC7D0039295A25C58527EA546D5971B8BD609AA5": {
                "issuerName": subject,
                "serialNumber": b"other",
            },
        },
    }
    assert (
        _patch_trust_plist(data, fingerprint=fp, subject_der=subject, serial_bytes=b"ser")
        is True
    )
    settings = data["trustList"][fp].get("trustSettings")
    assert settings is not None and len(settings) == 2
    assert settings[0]["kSecTrustSettingsPolicyName"] == "sslServer"
    assert settings[0]["kSecTrustSettingsResult"] == 1
    assert "trustSettings" not in data["trustList"]["EC7D0039295A25C58527EA546D5971B8BD609AA5"]


def test_patch_trust_plist_inserts_missing_entry():
    fp = "NEWFINGERPRINT"
    subject = b"subject-der"
    data = {"trustVersion": 1, "trustList": {}}
    assert (
        _patch_trust_plist(data, fingerprint=fp, subject_der=subject, serial_bytes=b"ser")
        is True
    )
    entry = data["trustList"][fp]
    assert entry["issuerName"] == subject
    assert entry["serialNumber"] == b"ser"
    assert entry.get("trustSettings")
    # 新增条目必须带 modDate，否则 trust-settings-import 报 corrupted（2026-08-09）
    assert entry.get("modDate") is not None


def test_patch_trust_plist_bad_structure_returns_false():
    assert (
        _patch_trust_plist(
            {"trustVersion": 1, "trustList": "nope"},
            fingerprint="A",
            subject_der=b"x",
            serial_bytes=b"y",
        )
        is False
    )


def test_enable_system_proxy_blocks_untrusted_ca():
    import mp_harvest.infra.platform as plat_mod
    from mp_harvest.infra.mitm import mitm_capture

    class _FakeCA:
        def __init__(self, trusted: bool) -> None:
            self._trusted = trusted

        def status(self) -> bool:
            return self._trusted

    class _FakeProxy:
        def __init__(self) -> None:
            self.called = False

        def enable(self, port: int):
            self.called = True
            return SimpleNamespace(ok=True, message=f"enabled {port}")

    class _FakePlatform:
        def __init__(self, trusted: bool) -> None:
            self.ca = _FakeCA(trusted)
            self.proxy = _FakeProxy()

    original = plat_mod.get_platform
    try:
        plat_mod.get_platform = lambda: _FakePlatform(False)
        svc = mitm_capture.MitmCaptureService()
        ok, msg = svc.enable_system_proxy()
        assert ok is False
        assert "信任" in msg

        fake = _FakePlatform(True)
        plat_mod.get_platform = lambda: fake
        ok2, msg2 = svc.enable_system_proxy()
        assert ok2 is True
        assert fake.proxy.called is True
    finally:
        plat_mod.get_platform = original


def _self_signed_ca(path: Path) -> str:
    """生成一把自签 CA 写到 path（DER），返回 SHA-1 指纹（大写十六进制）。"""
    import datetime

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "MP Harvest Local CA")])
    now = datetime.datetime.now(datetime.UTC)
    crt = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(crt.public_bytes(serialization.Encoding.DER))
    return crt.fingerprint(hashes.SHA1()).hex().upper()


def test_status_false_when_cert_missing_from_keychain(tmp_path, monkeypatch):
    """信任设置里有条目、但证书本体不在钥匙串 → 必须报「未信任」。

    2026-09 用户实测的故障：``add-trusted-cert`` 写钥匙串失败、而用户态的
    ``trust-settings-import`` 成功，于是 trust-settings 里有指纹条目、证书却
    不在钥匙串。旧 ``status()`` 只查 plist，报 True →
      · 界面隐藏「安装 CA 证书」按钮（用户无法自救）；
      · 安全守卫误判放行，切了系统代理但 mitmproxy 无法完成握手 →
        因为拦截范围被限制成只有 mp.weixin.qq.com，症状就是「微信文章打不开」
        而机器上其他一切正常。
    """
    import plistlib
    from pathlib import Path as _P

    from mp_harvest.infra.platform import mac

    cert = tmp_path / "mitmproxy-ca-cert.cer"
    fp = _self_signed_ca(cert)

    class _Proc:
        def __init__(self, rc: int = 0, out: str = "", err: str = "") -> None:
            self.returncode, self.stdout, self.stderr = rc, out, err

    def _run_factory(chain_fingerprint: str | None):
        def _run(cmd, timeout=120):  # noqa: ANN001
            if cmd[1:2] == ["trust-settings-export"]:
                _P(cmd[-1]).write_bytes(
                    plistlib.dumps(
                        {"trustVersion": 1,
                         "trustList": {fp: {"trustSettings": [{"kSecTrustSettingsResult": 1}]}}}
                    )
                )
                return _Proc(0)
            if cmd[1:2] == ["find-certificate"]:
                out = f"SHA-1 hash: {chain_fingerprint}\n" if chain_fingerprint else "no certs\n"
                return _Proc(0, out)
            return _Proc(1)
        return _run

    ca = mac.MacCaSetup(cert)

    # 登录钥匙串不存在时（非 macOS）跳过前半段，避免误报
    monkeypatch.setattr(mac, "_run", _run_factory("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"))
    assert ca.status() is False, "证书不在钥匙串里时必须报未信任"

    if _P(mac._SYSTEM_KEYCHAIN).exists() or _P(mac._LOGIN_KEYCHAIN).exists():
        monkeypatch.setattr(mac, "_run", _run_factory(fp))
        assert ca.status() is True, "证书在钥匙串且信任设置有条目时应报已信任"
