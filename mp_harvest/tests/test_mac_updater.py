"""MacUpdater 升级脚本生成测试（2026-08-09 修复 .app 安装目录 + 管理员授权）。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import mp_harvest.infra.platform.paths as paths_mod  # noqa: E402
from mp_harvest.infra.platform.mac import (  # noqa: E402
    MacUpdater,
    _app_bundle_root,
    _build_apply_script,
    _mac_install_dir,
)
from mp_harvest.infra.platform.base import PlatformError  # noqa: E402


def _patch_frozen(monkey_value: bool):
    """临时把 ``sys.frozen`` 与 ``paths.is_frozen`` 设成指定值，返回恢复函数。"""
    old_frozen = getattr(sys, "frozen", None)
    old_is_frozen = paths_mod.is_frozen
    paths_mod.is_frozen = lambda: monkey_value
    if monkey_value:
        sys.frozen = True

    def restore():
        paths_mod.is_frozen = old_is_frozen
        if old_frozen is None:
            if hasattr(sys, "frozen"):
                delattr(sys, "frozen")
        else:
            sys.frozen = old_frozen

    return restore


def test_bundle_root_finds_app():
    exe = Path("/Applications/MP Harvest.app/Contents/MacOS/MP Harvest")
    assert _app_bundle_root(exe) == Path("/Applications/MP Harvest.app")


def test_bundle_root_none_for_plain_binary():
    assert _app_bundle_root(Path("/usr/local/bin/foo")) is None


def test_mac_install_dir_frozen_app_uses_bundle_parent():
    old_exe = sys.executable
    restore = _patch_frozen(True)
    try:
        sys.executable = "/Applications/MP Harvest.app/Contents/MacOS/MP Harvest"
        assert _mac_install_dir() == Path("/Applications")
    finally:
        sys.executable = old_exe
        restore()


def test_build_apply_script_contains_replace_and_admin_fallback():
    with tempfile.TemporaryDirectory() as td:
        pkg = Path(td) / "MP-Harvest.zip"
        pkg.touch()
        s = _build_apply_script(pkg=pkg, install_dir=Path("/Applications"), pid=12345)
        assert "while kill -0 12345" in s
        assert "ditto -xk" in s
        assert 'bash "' in s  # 替换由 core 子脚本执行
        assert 'osascript -e "do shell script' in s
        assert "with administrator privileges" in s
        assert 'open "/Applications/$APP"' in s
        assert 'rm -f' in s
        core = pkg.parent / "apply_core.sh"
        assert core.is_file()
        assert 'rm -rf "/Applications/$APP"' in core.read_text(encoding="utf-8")


def test_apply_dev_mode_raises():
    restore = _patch_frozen(False)
    try:
        with tempfile.TemporaryDirectory() as td:
            pkg = Path(td) / "x.zip"
            pkg.touch()
            try:
                MacUpdater().apply(pkg)
                assert False, "应抛出 PlatformError"
            except PlatformError as exc:
                assert "开发模式" in str(exc)
    finally:
        restore()
