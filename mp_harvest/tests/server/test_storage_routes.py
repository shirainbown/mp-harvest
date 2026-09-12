"""本地数据占用 / 清理端点 + 启动体检（2026-09）。"""

from __future__ import annotations

from .conftest import add_account, wait_task  # noqa: F401  （与其它 server 测试同一导入风格）


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


def test_storage_clean_refuses_irreproducible_data(client, auth, isolated_data_dir):
    """**不可再生**的项越权清理必须被挡下并说明，不能静默忽略 —— 静默会让人以为清干净了。

    注意与「代价高」那档的区别（2026-09）：账号、文章缓存改成了可清（用户要求），
    但模型配置、设置、提示词、手工补录仍然挡住 —— 它们删了没有别处能重新得到。
    """
    (isolated_data_dir / "ai_models.json").write_text('{"keep":1}', encoding="utf-8")
    body = client.post("/api/storage/clean", params=auth,
                       json={"keys": ["ai_models", "settings", "prompts", "sightings"]}).json()
    assert body["ok"] is False and body["removed"] == []
    assert len(body["errors"]) == 4
    assert all("不在可清理范围内" in e for e in body["errors"])
    assert (isolated_data_dir / "ai_models.json").read_text(encoding="utf-8") == '{"keep":1}'


def test_storage_clean_resets_memory_so_it_does_not_grow_back(client, auth, isolated_data_dir):
    """清理文章缓存后，**内存里也必须没有了** —— 否则下一次保存会把它写回来。

    这是「代价高」那档能不能放开的**前提**：只删文件的话，进程里的副本还在，
    随便一次合并/拉取就把文件重新写出来，看着像没清干净（2026-09 实测：
    删光 29 个缓存文件后做一次普通操作，文件就回来了）。
    """
    from mp_harvest.server import state

    acc = add_account(client, auth)
    state.set_articles(acc["id"], [{
        "title": "会被清掉的文章", "link": "https://mp.weixin.qq.com/s/m1",
        "publish_ts": 1757000000, "identity": "art-mem-1",
        "body_text": "正文", "body_html": "<p>x</p>",
    }])
    cache_file = isolated_data_dir / "articles_cache"
    assert list(cache_file.glob("*.json")), "前提：确实落盘了"

    body = client.post("/api/storage/clean", params=auth,
                       json={"keys": ["articles_cache"]}).json()
    assert body["errors"] == [], body["errors"]
    assert list(cache_file.glob("*.json")) == [], "磁盘没清掉"

    # 关键：内存里也得没了。再做一次普通操作（写回正文），文件**不该**回来。
    state.merge_article_bodies(acc["id"], [
        {"identity": "art-mem-1", "body_text": "y" * 300},
    ])
    assert list(cache_file.glob("*.json")) == [], "内存里的旧数据把文件写回来了"
    assert state.get_articles(acc["id"]) == [], "内存里还留着已清掉的文章"


def test_storage_clean_refuses_ca_while_capturing(client, auth, isolated_data_dir, monkeypatch):
    """抓包运行中不许清 CA。

    删了文件，进程里那份证书还在用；而下次启动会生成一个**新的** CA ——
    系统钥匙串里信任的仍是旧的，抓包从此静默失败。这比不让清更糟。
    """
    from mp_harvest.server.routes import storage as storage_route

    ca = isolated_data_dir / "mitm_conf"
    ca.mkdir(parents=True, exist_ok=True)
    (ca / "mitmproxy-ca.pem").write_text("x", encoding="utf-8")
    monkeypatch.setattr(storage_route, "_mitm_running", lambda: True)

    r = client.post("/api/storage/clean", params=auth, json={"keys": ["mitm_conf"]})
    assert r.status_code == 409 and "抓包" in r.json()["detail"]
    assert (ca / "mitmproxy-ca.pem").is_file(), "运行中还是把 CA 清了"


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
