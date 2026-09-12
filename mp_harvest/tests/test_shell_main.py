"""shell/main.py 的平台分支契约（2026-09 Windows 版补齐）。

盯两类「看着能跑、实际串味」的故障：

1. ``webview.start()`` 在 Windows 上必须显式给 ``storage_path`` —— 否则 WebView2
   用户数据落在所有 pywebview 应用共享的 ``%APPDATA%\\pywebview``，与 WINDOWS.md
   承诺的 ``%APPDATA%\\MP Harvest\\data\\webview\\`` 不符，卸载也清不掉。
2. CA 未信任的提示文案不能写死「输入管理员密码」—— Windows 装 CA 无需管理员
   （前端 caStepText 已分平台，后端这句是漏网之鱼）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.infra.platform import paths  # noqa: E402
from mp_harvest.shell import main as shell_main  # noqa: E402


def test_start_kwargs_private_mode_off_everywhere():
    assert shell_main.webview_start_kwargs()["private_mode"] is False


def test_start_kwargs_storage_path_on_win32(monkeypatch, tmp_path):
    monkeypatch.setattr(shell_main.sys, "platform", "win32")
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")
    kwargs = shell_main.webview_start_kwargs()
    assert kwargs["storage_path"] == str(tmp_path / "data" / "webview")


def test_start_kwargs_no_storage_path_on_mac(monkeypatch):
    """macOS 不传 storage_path：Cocoa 忽略它，保持既有已验证行为不动。"""
    monkeypatch.setattr(shell_main.sys, "platform", "darwin")
    assert "storage_path" not in shell_main.webview_start_kwargs()


def test_ca_untrusted_hint_matches_platform(monkeypatch, tmp_path):
    """mitm_capture 的 CA 未信任提示按 needs_admin 分支，不写死管理员密码。"""
    import types

    import mp_harvest.infra.platform as platform_pkg
    from mp_harvest.infra.mitm import mitm_capture

    class _Ca:
        def __init__(self, needs_admin: bool) -> None:
            self.needs_admin = needs_admin

        def status(self) -> bool:
            return False

    for needs_admin, want, not_want in (
        (True, "输入管理员密码", "无需管理员"),
        (False, "无需管理员密码", "输入管理员密码"),
    ):
        fake = types.SimpleNamespace(ca=_Ca(needs_admin))
        # enable_system_proxy 内部惰性 import get_platform，桩模块属性即可
        monkeypatch.setattr(platform_pkg, "get_platform", lambda: fake)
        ok, msg = mitm_capture.MitmCaptureService(app_root=tmp_path).enable_system_proxy()
        assert not ok
        assert want in msg
        assert not_want not in msg
