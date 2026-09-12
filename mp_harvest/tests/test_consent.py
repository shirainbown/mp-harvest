"""免责声明门禁状态机测试（不弹窗、不碰真实数据目录）。"""

from __future__ import annotations

import tempfile
from pathlib import Path

from mp_harvest.core.consent import (
    is_blocked,
    load_consent,
    require_consent,
    save_consent,
)


def test_absent_agree_writes_agreed():
    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data"
        root = Path(td) / "app"
        assert require_consent(ask=lambda: True, data_dir=data, root=root) is True
        assert load_consent(data) == "agreed"
        assert is_blocked(data, root) is False


def test_absent_disagree_writes_blocked():
    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data"
        root = Path(td) / "app"
        assert require_consent(ask=lambda: False, data_dir=data, root=root) is False
        assert load_consent(data) == "blocked"
        assert is_blocked(data, root) is True


def test_blocked_never_asks_again():
    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data"
        root = Path(td) / "app"
        save_consent("blocked", data_dir=data, root=root)
        called = []
        assert require_consent(
            ask=lambda: called.append(1) or True, data_dir=data, root=root
        ) is False
        assert called == []  # 已阻止：不再弹窗、不再询问


def test_agreed_passes_without_asking():
    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data"
        root = Path(td) / "app"
        save_consent("agreed", data_dir=data, root=root)
        called = []
        assert require_consent(
            ask=lambda: called.append(1) or True, data_dir=data, root=root
        ) is True
        assert called == []


def test_failed_prompt_does_not_persist_blocked():
    """「没问成」不能记成「用户拒绝」（2026-09 Windows 适配）。

    冻结版是 console=False 的 GUI 程序。tkinter 一旦没被打进去（tcl/tk 的 DLL 或
    数据目录缺一个就够），弹窗会失败、``input()`` 也必然抛异常。此时若记成
    ``blocked``，就会写下 ``consent.json`` 与 ``.consent_blocked`` 两个**跨版本存活**
    的标记，应用从此静默退出、界面上一个字都不显示 —— 用户无从知道要删哪个文件。
    """
    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data"
        root = Path(td) / "app"
        assert require_consent(ask=lambda: None, data_dir=data, root=root) is False
        # 当次不放行
        assert load_consent(data) == ""
        assert is_blocked(data, root) is False
        # 下次启动还能再问一遍
        assert require_consent(ask=lambda: True, data_dir=data, root=root) is True


def test_bundle_block_marker_blocks():
    with tempfile.TemporaryDirectory() as td:
        data = Path(td) / "data"
        root = Path(td) / "app"
        root.mkdir(parents=True, exist_ok=True)
        (root / ".consent_blocked").write_text("blocked", encoding="utf-8")
        assert is_blocked(data, root) is True
        assert require_consent(ask=lambda: True, data_dir=data, root=root) is False
