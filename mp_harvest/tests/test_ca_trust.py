"""CA 信任检查 + 抓包代理守卫测试（2026-08-09：防止未信任 CA 时劫持全机 HTTPS）。"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.infra.platform.mac import _patch_trust_plist  # noqa: E402


def test_patch_trust_plist_sets_explicit_settings():
    subject = b"0(1\x120\x10\x06\x03U\x04\x03\x0c\tmitmproxy"
    data = {
        "trustVersion": 1,
        "trustList": {
            "AAA": {"issuerName": subject, "serialNumber": b"x"},
            "BBB": {"issuerName": b"other", "serialNumber": b"y"},
        },
    }
    assert _patch_trust_plist(data, subject) is True
    settings = data["trustList"]["AAA"].get("trustSettings")
    assert settings is not None and len(settings) == 2
    assert settings[0]["kSecTrustSettingsPolicyName"] == "sslServer"
    assert settings[0]["kSecTrustSettingsResult"] == 1
    assert "trustSettings" not in data["trustList"]["BBB"]


def test_patch_trust_plist_no_match_returns_false():
    data = {"trustVersion": 1, "trustList": {"AAA": {"issuerName": b"other"}}}
    assert _patch_trust_plist(data, b"target") is False


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
