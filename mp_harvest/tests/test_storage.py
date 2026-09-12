"""本地数据的清理与体检（2026-09）。

起因：用户问「把本地缓存删了会怎样」，一查发现 data/ 有 815MB ——
**806MB 是从来没清理过的历史更新包**，而界面上看不到任何占用信息。

这里钉住三件事：
1. 更新包按 mtime 只留最新，且**不碰非 zip 文件**（应用脚本就写在那个目录里）
2. 「可清理清单」只收可重建的，**账号/凭证/模型配置碰不到**
3. 设置/提示词被删掉时要出声（它们是静默回退默认值的，最坑）
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core import storage as st  # noqa: E402
from mp_harvest.infra.platform import base as platform_base  # noqa: E402


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """把数据目录指到临时位置（storage 与 base 都走 paths.data_dir）。"""
    import mp_harvest.infra.platform.base as b
    import mp_harvest.infra.platform.paths as p

    d = tmp_path / "data"
    d.mkdir(parents=True)
    monkeypatch.setattr(p, "data_dir", lambda *a, **k: d)
    monkeypatch.setattr(b.paths, "data_dir", lambda *a, **k: d)
    return d


# ── 更新包清理 ────────────────────────────────────────────────────


def _mkzip(d: Path, name: str, size: int = 1024, mtime: float = 0) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_bytes(b"x" * size)
    if mtime:
        os.utime(p, (mtime, mtime))
    return p


def test_prune_keeps_only_the_newest(data_dir):
    """实测用户目录里积了 16 个包共 806MB —— 只该留最新那个。"""
    now = time.time()
    for i, name in enumerate(["a.zip", "b.zip", "c.zip"]):
        _mkzip(data_dir / "update", name, mtime=now - (10 - i))
    assert platform_base.prune_update_packages() == 2
    left = [p.name for p in (data_dir / "update").glob("*.zip")]
    assert left == ["c.zip"], f"应当只留最新的，实际 {left}"


def test_prune_leaves_non_zip_alone(data_dir):
    """应用脚本就写在同目录（apply_update.sh）—— 清更新包不能把它删了。"""
    now = time.time()
    _mkzip(data_dir / "update", "old.zip", mtime=now - 100)
    _mkzip(data_dir / "update", "new.zip", mtime=now)
    script = data_dir / "update" / "apply_update.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")

    assert platform_base.prune_update_packages() == 1
    assert script.is_file(), "非 zip 的文件被误删了"
    assert [p.name for p in (data_dir / "update").glob("*.zip")] == ["new.zip"]


def test_prune_is_safe_on_missing_or_empty_dir(data_dir):
    """目录不存在 / 没有包 / 只有一个包 —— 都不能抛，也不能删。"""
    assert platform_base.prune_update_packages() == 0        # 目录都没有
    (data_dir / "update").mkdir()
    assert platform_base.prune_update_packages() == 0        # 空目录
    _mkzip(data_dir / "update", "only.zip")
    assert platform_base.prune_update_packages() == 0        # 只有一个
    assert (data_dir / "update" / "only.zip").is_file()


# ── 占用清单 ──────────────────────────────────────────────────────


def test_inventory_lists_sizes_and_safety(data_dir):
    _mkzip(data_dir / "update", "a.zip", size=2048)
    (data_dir / "weekly").mkdir()
    (data_dir / "weekly" / "cache.json").write_text("x" * 100, encoding="utf-8")
    (data_dir / "accounts.json").write_text("{}", encoding="utf-8")

    items = {i["key"]: i for i in st.inventory()}
    assert items["update"]["size"] == 2048 and items["update"]["safe"] is True
    assert items["weekly_cache"]["size"] == 100 and items["weekly_cache"]["safe"] is True
    # 账号是**不能清**的，但要在清单里露脸（让用户看见「不归清理管」）
    assert items["accounts"]["safe"] is False
    assert "重新抓包" in items["accounts"]["note"]
    # 按占用降序
    sizes = [i["size"] for i in st.inventory()]
    assert sizes == sorted(sizes, reverse=True)


def test_user_data_is_never_clearable(data_dir):
    """把账号/模型/提示词/文章缓存的键丢给 clear —— 必须**原样留着**。"""
    for name, body in [("accounts.json", '{"a":1}'), ("ai_models.json", '{"b":2}'),
                       ("settings.json", '{"c":3}')]:
        (data_dir / name).write_text(body, encoding="utf-8")
    (data_dir / "weekly").mkdir()
    (data_dir / "weekly" / "prompts.json").write_text('{"d":4}', encoding="utf-8")
    (data_dir / "articles_cache").mkdir()
    (data_dir / "articles_cache" / "x.json").write_text("[]", encoding="utf-8")

    res = st.clear(["accounts", "ai_models", "settings", "prompts", "articles_cache"])
    assert res["freed"] == 0 and res["removed"] == []
    assert len(res["errors"]) == 5, "越权清理必须被挡下并逐一说明"
    # 断言**拒绝的理由**而不只是「拒绝了」：否则随便一句「未知项」也能骗过测试，
    # 而那意味着将来把这些键加进删除分支就没人拦得住了
    assert all("不在可清理范围内" in e for e in res["errors"]), res["errors"]
    for name in ("accounts.json", "ai_models.json", "settings.json"):
        assert (data_dir / name).is_file(), f"{name} 被清掉了"
    assert (data_dir / "weekly" / "prompts.json").is_file()
    assert (data_dir / "articles_cache" / "x.json").is_file()


def test_clear_removes_only_what_was_asked(data_dir):
    _mkzip(data_dir / "update", "a.zip", size=500)
    _mkzip(data_dir / "update", "b.zip", size=500)
    (data_dir / "events.db").write_bytes(b"y" * 300)
    (data_dir / "accounts.json").write_text("{}", encoding="utf-8")

    res = st.clear(["update"])
    assert res["ok"] is True and res["freed"] == 1000 and res["removed"] == ["update"]
    assert list((data_dir / "update").iterdir()) == []
    assert (data_dir / "events.db").is_file(), "没点名的项被清了"
    assert (data_dir / "accounts.json").is_file()


def test_summary_totals_match_the_items(data_dir):
    _mkzip(data_dir / "update", "a.zip", size=700)
    (data_dir / "accounts.json").write_text("{}", encoding="utf-8")
    s = st.summary()
    assert s["total_bytes"] == sum(i["size"] for i in s["items"])
    assert s["clearable_bytes"] == sum(i["size"] for i in s["items"] if i["safe"])
    assert s["clearable_bytes"] < s["total_bytes"] or s["total_bytes"] == 700
