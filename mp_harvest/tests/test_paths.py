"""infra/platform/paths 数据目录解析测试。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.infra.platform.paths import data_dir  # noqa: E402


def test_data_dir_dev_mode_points_inside_package():
    # 开发模式：mp_harvest/data/
    d = data_dir()
    assert d.name == "data"
    assert d.parent.name == "mp_harvest"
    assert (d.parent / "core").is_dir()


def test_data_dir_frozen_macos():
    # 手动模拟冻结 macOS：零依赖运行器无 monkeypatch，直接改 sys 属性
    import mp_harvest.infra.platform.paths as paths

    old_frozen = getattr(sys, "frozen", None)
    old_platform = sys.platform
    try:
        sys.frozen = True  # type: ignore[attr-defined]
        sys.platform = "darwin"
        d = paths.data_dir()
        # 设计稿 §3.4：~/Library/Application Support/MP Harvest/data
        assert str(d).endswith("Library/Application Support/MP Harvest/data")
    finally:
        sys.platform = old_platform
        if old_frozen is None:
            delattr(sys, "frozen")
        else:
            sys.frozen = old_frozen  # type: ignore[attr-defined]


def test_data_dir_frozen_windows():
    import shutil
    import tempfile
    import os

    import mp_harvest.infra.platform.paths as paths

    old_frozen = getattr(sys, "frozen", None)
    old_platform = sys.platform
    old_appdata = os.environ.get("APPDATA")
    tmp_base = tempfile.mkdtemp(prefix="mp_harvest-appdata-")
    try:
        sys.frozen = True  # type: ignore[attr-defined]
        sys.platform = "win32"
        # 用真实临时目录模拟 %APPDATA%（不要用字面 Windows 路径：
        # macOS 会把 "C:\Users\..." 当成相对目录建到 cwd 里，污染仓库）
        os.environ["APPDATA"] = tmp_base
        d = paths.data_dir()
        # 设计稿 §3.4：%APPDATA%\MP Harvest\data
        assert str(d).startswith(tmp_base)
        assert d.name == "data"
        assert d.parent.name == "MP Harvest"
    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)
        sys.platform = old_platform
        if old_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = old_appdata
        if old_frozen is None:
            delattr(sys, "frozen")
        else:
            sys.frozen = old_frozen  # type: ignore[attr-defined]
