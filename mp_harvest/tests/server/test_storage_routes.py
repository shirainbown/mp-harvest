"""本地数据占用 / 清理端点 + 启动体检（2026-09）。"""

from __future__ import annotations

from .conftest import wait_task  # noqa: F401  （保持与其它 server 测试一致的导入风格）


def _rows(client, auth, **params) -> list[dict]:
    r = client.get("/api/logs", params={**auth, "kind": "storage", **params})
    assert r.status_code == 200, r.text
    return r.json()["events"]


# ── 端点 ──────────────────────────────────────────────────────────


def test_storage_endpoint_shape(client, auth, isolated_data_dir):
    """有材料才露脸：可清理的与不可清理的都要在。

    ⚠️ 必须先造出材料 —— 空项现在会被过滤掉（见下一条），全新安装的面板是空的。
    """
    (isolated_data_dir / "update").mkdir(parents=True, exist_ok=True)
    (isolated_data_dir / "update" / "MP-Harvest-mac-2.1.28.zip").write_bytes(b"x" * 4096)
    (isolated_data_dir / "events.db").write_bytes(b"x" * 128)
    (isolated_data_dir / "articles_cache").mkdir(parents=True, exist_ok=True)
    (isolated_data_dir / "articles_cache" / "a.json").write_text("{}", encoding="utf-8")

    b = client.get("/api/storage", params=auth).json()
    assert set(b) == {"items", "clearable_bytes", "total_bytes"}
    keys = {i["key"] for i in b["items"]}
    assert {"update", "events", "articles_cache"} <= keys


def test_storage_omits_items_whose_files_are_gone(client, auth, isolated_data_dir):
    """**材料删光了就不列出来**（2026-09 用户要求：「都已经删除了，那么就移除」）。

    这是用户当次报的场景：他在应用外面删掉了导出的 HTML，界面却毫无变化。
    判据是磁盘上的实际占用，所以删完再刷新，那一项自己就消失了 —— 而不是
    变成一个占着位置、显示 0 B 的空条目。
    """
    exports = isolated_data_dir / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    f = exports / "a.html"
    f.write_text("<p>x</p>", encoding="utf-8")

    keys = {i["key"] for i in client.get("/api/storage", params=auth).json()["items"]}
    assert "exports" in keys, "有导出文件时该项必须在"

    f.unlink()
    keys2 = {i["key"] for i in client.get("/api/storage", params=auth).json()["items"]}
    assert "exports" not in keys2, "导出文件删光后该项不该再占着列表"


def test_storage_clean_only_touches_the_named_safe_items(client, auth, isolated_data_dir):
    upd = isolated_data_dir / "update"
    upd.mkdir(parents=True, exist_ok=True)
    (upd / "MP-Harvest-mac-2.1.28.zip").write_bytes(b"x" * 4096)
    (isolated_data_dir / "accounts.json").write_text("{}", encoding="utf-8")

    r = client.post("/api/storage/clean", params=auth, json={"keys": ["update"]})
    body = r.json()
    assert body["ok"] is True and body["freed"] == 4096 and body["removed"] == ["update"]
    assert list(upd.iterdir()) == []
    assert (isolated_data_dir / "accounts.json").is_file(), "账号文件被误清"


def test_storage_clean_refuses_user_data(client, auth, isolated_data_dir):
    """越权清理必须被**挡下并说明**，不能静默忽略 —— 静默会让人以为清干净了。"""
    (isolated_data_dir / "accounts.json").write_text('{"keep":1}', encoding="utf-8")
    body = client.post("/api/storage/clean", params=auth,
                       json={"keys": ["accounts", "ai_models"]}).json()
    assert body["ok"] is False and body["removed"] == []
    assert len(body["errors"]) == 2
    assert all("不在可清理范围内" in e for e in body["errors"])
    assert (isolated_data_dir / "accounts.json").read_text(encoding="utf-8") == '{"keep":1}'


# ── 启动体检：静默回退要出声 ──────────────────────────────────────


def test_startup_warns_when_settings_file_vanished(client, auth, isolated_data_dir):
    """设置被删掉会**静默回退默认值** —— 用户只会觉得「我明明改过」。

    判据带上「目录里还有别的东西」：全新安装同样没有这个文件，那不是异常。
    """
    from mp_harvest.server.app import _check_local_data

    (isolated_data_dir / "accounts.json").write_text("{}", encoding="utf-8")
    _check_local_data()

    rows = [e for e in _rows(client, auth) if "设置文件缺失" in e["message"]]
    assert rows, "设置丢失没有留下任何痕迹"
    assert rows[0]["level"] == "warn"
    assert "重新设置" in rows[0]["message"], "要告诉用户后果与怎么办"


def test_startup_warns_when_prompts_vanished(client, auth, isolated_data_dir):
    """自定义提示词被删更坑：改没改过只有用户自己知道。

    用 `weekly/cache.json` 作证「这目录用过」—— 它每生成一期就会写。
    """
    from mp_harvest.server.app import _check_local_data

    (isolated_data_dir / "weekly").mkdir(parents=True, exist_ok=True)
    (isolated_data_dir / "weekly" / "cache.json").write_text("{}", encoding="utf-8")
    _check_local_data()

    rows = [e for e in _rows(client, auth) if "提示词缺失" in e["message"]]
    assert rows and rows[0]["level"] == "warn"
    assert "内置默认标准" in rows[0]["message"]


def test_startup_is_quiet_on_a_fresh_install(client, auth, isolated_data_dir):
    """全新安装没有这些文件是**正常的** —— 不能一上来就报警吓人。"""
    from mp_harvest.server.app import _check_local_data

    _check_local_data()          # 目录是空的，什么都没有
    assert [e for e in _rows(client, auth) if "缺失" in e["message"]] == []


def test_startup_prunes_update_packages_and_says_so(client, auth, isolated_data_dir):
    """积压的更新包在启动时清掉，并留一笔日志（否则用户只会觉得空间莫名少了）。"""
    import os
    import time

    from mp_harvest.server.app import _check_local_data

    upd = isolated_data_dir / "update"
    upd.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for i, name in enumerate(["v1.zip", "v2.zip", "v3.zip"]):
        p = upd / name
        p.write_bytes(b"x" * 100)
        os.utime(p, (now - (10 - i), now - (10 - i)))

    _check_local_data()
    assert [p.name for p in upd.glob("*.zip")] == ["v3.zip"]
    rows = [e for e in _rows(client, auth) if "更新包" in e["message"]]
    assert rows and "2" in rows[0]["message"]
