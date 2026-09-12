"""更新包选择：**必须按平台挑**（2026-09，Windows 版上线时发现）。

同一个 GitHub Release 从此同时挂着 ``MP-Harvest-mac-<ver>.zip`` 与
``MP-Harvest-win-<ver>.zip``。只按 ``.zip`` 后缀挑的话会拿到对面平台的包，
而两个方向的失败**都是静默的**：

- mac 拿到 Windows 包：``apply`` 里 ``ls -d *.app`` 找不到东西 → 脚本 ``exit 1``，
  应用已经关了却不再起来，屏幕上什么都没有；
- Windows 拿到 mac 包：``xcopy`` 把 ``MP Harvest.app/...`` 抄进安装目录，最后
  ``start`` 起来的还是原来那个 exe → 「更新成功但版本没变」。

这组用例纯字符串 + 临时目录，在 macOS 上跑。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.infra.platform.base import pick_zip_url  # noqa: E402
from mp_harvest.infra.platform.mac import MacUpdater  # noqa: E402
from mp_harvest.infra.platform.win import WinUpdater  # noqa: E402
from mp_harvest.server.routes import update as update_route  # noqa: E402

MAC = "MP-Harvest-mac-9.9.9.zip"
WIN = "MP-Harvest-win-9.9.9.zip"


def _assets(*names: str) -> list[dict[str, str]]:
    return [
        {"name": n, "browser_download_url": f"https://example.invalid/{n}"} for n in names
    ]


# ── pick_zip_url ─────────────────────────────────────────────────


def test_win_user_never_gets_the_mac_zip():
    """mac 包排在前面也不行 —— 这正是原先的行为（取第一个）。"""
    got = pick_zip_url(_assets(MAC, WIN), ".zip", WinUpdater.asset_prefix)
    assert got.endswith(WIN)


def test_mac_user_never_gets_the_win_zip():
    """反过来同理：Windows 包排在前面时，原先 mac 用户会下到它。"""
    got = pick_zip_url(_assets(WIN, MAC), ".zip", MacUpdater.asset_prefix)
    assert got.endswith(MAC)


def test_missing_platform_asset_returns_empty_not_the_other_one():
    """对面平台的包**一个都不能给** —— 宁可报「没找到」，也不能装错东西。"""
    assert pick_zip_url(_assets(MAC), ".zip", WinUpdater.asset_prefix) == ""
    assert pick_zip_url(_assets(WIN), ".zip", MacUpdater.asset_prefix) == ""


def test_prefix_filtering_is_case_insensitive():
    got = pick_zip_url(_assets("mp-harvest-win-9.9.9.ZIP"), ".zip", WinUpdater.asset_prefix)
    assert got.endswith("mp-harvest-win-9.9.9.ZIP")


def test_empty_prefix_keeps_old_behaviour():
    """没有平台前缀的调用方（自定义更新源）退回「第一个 .zip」。"""
    assert pick_zip_url(_assets(MAC, WIN)) .endswith(MAC)


def test_platform_prefixes_are_distinct():
    """两个前缀必须互不包含，否则过滤等于没做。"""
    assert not MacUpdater.asset_prefix.startswith(WinUpdater.asset_prefix)
    assert not WinUpdater.asset_prefix.startswith(MacUpdater.asset_prefix)
    assert MacUpdater.asset_prefix != WinUpdater.asset_prefix


# ── _find_downloaded_package：手动下载过对面平台的包时也不能挑错 ──


class _FakeUpdater:
    def __init__(self, prefix: str) -> None:
        self.asset_prefix = prefix


@pytest.fixture()
def fake_update_dir(monkeypatch, tmp_path):
    d = tmp_path / "update"
    d.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(update_route.paths, "data_dir", lambda: tmp_path)
    return d


def _touch(path: Path, mtime: float) -> None:
    path.write_bytes(b"")
    import os

    os.utime(path, (mtime, mtime))


def test_find_downloaded_package_ignores_other_platform(
    monkeypatch, fake_update_dir
):
    """对面的包更新（mtime 更大）也要跳过 —— 按 mtime 取最新会挑中它。"""
    _touch(fake_update_dir / MAC, 1000.0)
    _touch(fake_update_dir / WIN, 2000.0)
    monkeypatch.setattr(update_route, "get_platform", lambda: type(
        "P", (), {"updater": _FakeUpdater(WinUpdater.asset_prefix)}
    )())
    got = update_route._find_downloaded_package()
    assert got is not None and got.name == WIN


def test_find_downloaded_package_picks_newest_of_our_own(monkeypatch, fake_update_dir):
    _touch(fake_update_dir / WIN, 1000.0)
    old = fake_update_dir / "MP-Harvest-win-9.9.8.zip"
    _touch(old, 500.0)
    monkeypatch.setattr(update_route, "get_platform", lambda: type(
        "P", (), {"updater": _FakeUpdater(WinUpdater.asset_prefix)}
    )())
    got = update_route._find_downloaded_package()
    assert got is not None and got.name == WIN


def test_find_downloaded_package_none_when_only_other_platform(
    monkeypatch, fake_update_dir
):
    _touch(fake_update_dir / MAC, 1000.0)
    monkeypatch.setattr(update_route, "get_platform", lambda: type(
        "P", (), {"updater": _FakeUpdater(WinUpdater.asset_prefix)}
    )())
    assert update_route._find_downloaded_package() is None
