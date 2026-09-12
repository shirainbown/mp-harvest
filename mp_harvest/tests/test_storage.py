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


def test_inventory_has_three_tiers(data_dir):
    """三档：代价低 / 代价高但可清 / 不可再生（2026-09 从两档拆开）。"""
    _mkzip(data_dir / "update", "a.zip", size=2048)
    (data_dir / "weekly").mkdir()
    (data_dir / "weekly" / "cache.json").write_text("x" * 100, encoding="utf-8")
    (data_dir / "accounts.json").write_text("{}", encoding="utf-8")
    (data_dir / "weekly" / "prompts.json").write_text("{}", encoding="utf-8")

    items = {i["key"]: i for i in st.inventory()}
    # 代价低
    assert items["update"]["size"] == 2048
    assert items["update"]["safe"] is True and items["update"]["costly"] is False
    assert items["weekly_cache"]["safe"] is True and items["weekly_cache"]["costly"] is False
    # 代价高但**可清** —— 确实是用户自己的数据，该由他决定
    assert items["accounts"]["safe"] is True and items["accounts"]["costly"] is True
    assert "重新抓包" in items["accounts"]["note"]
    # 不可再生 —— 不给清，但要在清单里露脸（让用户看见「不归清理管」）
    assert items["prompts"]["safe"] is False
    # 按占用降序
    sizes = [i["size"] for i in st.inventory()]
    assert sizes == sorted(sizes, reverse=True)


def test_irreproducible_data_is_never_clearable(data_dir):
    """**不可再生**的四样丢给 clear —— 必须原样留着。

    它们与「代价高但可再生」的（账号、文章缓存）不是一回事：那些删了还能重新
    抓到/拉到，这四样删了就真没了 —— 模型 Key 和设置是你填的、提示词是你写的、
    补录链接是你手打的，没有别处能重新得到。
    """
    for name, body in [("ai_models.json", '{"b":2}'), ("settings.json", '{"c":3}'),
                       ("article_sightings.json", '{"e":5}')]:
        (data_dir / name).write_text(body, encoding="utf-8")
    (data_dir / "weekly").mkdir()
    (data_dir / "weekly" / "prompts.json").write_text('{"d":4}', encoding="utf-8")

    keys = ["ai_models", "settings", "prompts", "sightings"]
    # 先钉**清单的声明**：clear() 会拒绝，但如果清单把它们标成了可清，
    # 界面上就会给它们画勾选框 —— 那是「点了报错」而不是「不给你点」。
    # 只验 clear() 拒绝是不够的（第一版就漏在这里）。
    declared = {i["key"]: i for i in st.inventory(include_empty=True)}
    for k in keys:
        assert declared[k]["safe"] is False, f"{k} 被声明成了可清理"

    res = st.clear(keys)
    assert res["freed"] == 0 and res["removed"] == []
    assert len(res["errors"]) == len(keys), "越权清理必须被挡下并逐一说明"
    # 断言**拒绝的理由**而不只是「拒绝了」：否则随便一句「未知项」也能骗过测试，
    # 而那意味着将来把这些键加进删除分支就没人拦得住了
    assert all("不在可清理范围内" in e for e in res["errors"]), res["errors"]
    for name in ("ai_models.json", "settings.json", "article_sightings.json"):
        assert (data_dir / name).is_file(), f"{name} 被清掉了"
    assert (data_dir / "weekly" / "prompts.json").is_file()


def test_costly_data_is_clearable_but_flagged(data_dir):
    """「代价高」那档**真的能清掉**，同时带 costly 标记供前端写进确认框。

    用户原话：「为什么清理缓存的时候不能清理已经导出的内容，也不能清理已经添加的
    公众号，也不能清理已通过的历史文章的列表」—— 因为「代价高」被当成「不允许」了。
    """
    (data_dir / "accounts.json").write_text("{}", encoding="utf-8")
    (data_dir / "articles_cache").mkdir()
    (data_dir / "articles_cache" / "x.json").write_text("[]", encoding="utf-8")

    items = {i["key"]: i for i in st.inventory()}
    assert items["articles_cache"]["costly"] is True

    res = st.clear(["accounts", "articles_cache"])
    assert res["errors"] == [], res["errors"]
    assert sorted(res["removed"]) == ["accounts", "articles_cache"]
    assert not (data_dir / "accounts.json").exists()
    assert list((data_dir / "articles_cache").iterdir()) == []


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
    (data_dir / "settings.json").write_text("{}", encoding="utf-8")  # 不可再生
    s = st.summary()
    assert s["total_bytes"] == sum(i["size"] for i in s["items"])
    assert s["clearable_bytes"] == sum(i["size"] for i in s["items"] if i["safe"])
    # 「不可再生」那部分不计入可清总量 —— 否则界面上那个数字点不动，等于骗人
    assert s["clearable_bytes"] < s["total_bytes"]


# ── 清单与清理能力必须一致（补一次真事故）──────────────────────────


def _seed_all(data_dir: Path) -> None:
    """按 _CLEARABLE 给每一项造点东西 —— 空项不进清单，不造就看不出差异。"""
    for _key, (kind, rel) in st._CLEARABLE.items():
        p = data_dir / rel
        if kind == "dir":
            p.mkdir(parents=True, exist_ok=True)
            (p / "x.bin").write_bytes(b"x" * 16)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 16)


def test_clearable_keys_match_the_inventory(data_dir):
    """清单里**声明** safe=True 的，与 ``_CLEARABLE`` 里登记的必须是同一批。

    ⚠️ 必须用 ``include_empty=True`` 取**声明**，不能用默认（过滤掉空项的）清单：
    漏配那种 bug 恰恰会让那一项空着，而空项不进清单 —— 拿过滤后的清单去比，
    两边正好「一致」，bug 被掩盖。第一版就是这么骗过自己的：把 exports 从
    `_CLEARABLE` 里删掉，测试照样全绿。
    """
    declared = {i["key"] for i in st.inventory(include_empty=True) if i["safe"]}
    assert declared == set(st._CLEARABLE), (
        "清单说能清的与真能清的不是同一批 —— 差集里那个键在界面上会是个假按钮"
    )


def test_every_listed_clearable_item_really_clears(data_dir):
    """逐项真的清一遍，不许报错。

    这条是补一次**真事故**的：往清单里加了「导出的文章 HTML」（目录型），却忘了
    加清理分支，于是界面上出现一个勾了没反应的假按钮 —— 点了报「exports：未知项」，
    文件一个不删。而当时的测试只验了「列得出来」、没验「清得掉」，全绿放行，
    一直发到 v2.2.3 才被发现。
    """
    _seed_all(data_dir)
    for key in sorted(st._CLEARABLE):
        res = st.clear([key])
        assert res["errors"] == [], f"{key} 清不掉：{res['errors']}"
        assert key in res["removed"], f"{key} 列在清单里却没能清掉"
