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
