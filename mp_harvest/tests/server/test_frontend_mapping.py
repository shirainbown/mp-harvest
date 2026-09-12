"""前端对齐契约（API.md §3 第 5 条）：4 个端点返回裸数组/裸对象，

字段与 frontend/src/types.ts 的 Account / Article 逐字段一致。

Account: id / name / url / __biz? / expires_at(epoch 秒|null) / pending?
Article: id / account_id / title / url / date(可解析日期串) / fetched_at(抓取时间) / source(M|G|补)
         / verdict(keep|drop|null) / reason
         / title_verdict / title_reason / content_verdict / content_reason
"""

from __future__ import annotations

from datetime import datetime

from mp_harvest.tests.server.conftest import add_account, give_credential

ACCOUNT_KEYS = {"id", "name", "url", "expires_at", "pending", "__biz", "mitm_message"}
ARTICLE_KEYS = {
    "id", "account_id", "account_name", "title", "url", "date", "fetched_at", "source",
    "verdict", "reason",
    "title_verdict", "title_reason", "content_verdict", "content_reason",
    "exported",
}


def _fetch_history(client, auth, account_id):
    resp = client.post(
        "/api/history/fetch", params=auth, json={"account_id": account_id, "days": 7}
    )
    assert resp.status_code == 202, resp.text
    from mp_harvest.tests.server.conftest import wait_task

    wait_task(resp.json()["task_id"])


# ── accounts ──────────────────────────────────────────────────────


def test_accounts_list_bare_array_shape(client, auth):
    acc = add_account(client, auth)
    resp = client.get("/api/accounts", params=auth)
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list) and len(data) == 1  # 裸数组，无 {accounts} 信封
    row = data[0]
    assert set(row) <= ACCOUNT_KEYS
    assert row["id"] == acc["id"]
    assert row["name"] == "测试号"
    assert row["url"] == "https://mp.weixin.qq.com/s/abc"  # article_url → url
    assert row["expires_at"] is None
    assert row["pending"] is True


def test_add_account_bare_object_shape(client, auth):
    resp = client.post(
        "/api/accounts", params=auth, json={"name": "裸对象", "url": "https://mp.weixin.qq.com/s/x"}
    )
    assert resp.status_code == 201
    acc = resp.json()  # 账号对象本身，无 {account} 信封
    assert set(acc) <= ACCOUNT_KEYS
    assert acc["name"] == "裸对象"
    assert acc["url"] == "https://mp.weixin.qq.com/s/x"
    assert acc["expires_at"] is None
    assert acc["pending"] is True


def test_account_expires_at_iso_to_epoch_and_biz(client, auth):
    """core expires_at 是 ISO 字符串 → 前端 epoch 秒；__biz 取自 credentials。"""
    from mp_harvest.server import state

    acc = add_account(client, auth)
    give_credential(acc["id"])
    store = state.get_store()
    for row in store._rows:  # fake store 内存行
        if row["id"] == acc["id"]:
            row["expires_at"] = "2030-01-02T03:04:05"
            row["status"] = "active"
    resp = client.get("/api/accounts", params=auth)
    row = next(a for a in resp.json() if a["id"] == acc["id"])
    assert row["expires_at"] == int(datetime(2030, 1, 2, 3, 4, 5).timestamp())
    assert row["__biz"] == "fakebiz"
    assert row["pending"] is False


# ── articles ──────────────────────────────────────────────────────


def test_articles_bare_array_shape(client, auth):
    acc = add_account(client, auth)
    give_credential(acc["id"])
    _fetch_history(client, auth, acc["id"])

    resp = client.get("/api/articles", params={**auth, "account_id": acc["id"]})
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list) and len(data) == 2  # 裸数组，无 {articles} 信封
    for art in data:
        assert set(art) <= ARTICLE_KEYS
        assert art["id"]  # identity → id
        assert art["account_id"] == acc["id"]
        assert art["title"].startswith("文章")
        assert art["url"].startswith("https://mp.weixin.qq.com/s/")  # link → url
        # date 为可解析的 ISO 日期串
        assert datetime.fromisoformat(art["date"])
        assert art["source"] == "G"  # 拉历史来的（缺省 source）→ getmsg
        assert art["verdict"] is None  # 未判定
        assert art["reason"] == ""
        assert art["title_verdict"] is None
        assert art["title_reason"] == ""
        assert art["content_verdict"] is None
        assert art["content_reason"] == ""
        # 没导出过就是 False —— 默认成 True 会让每篇都挂个「已导出」的假标记
        assert art["exported"] is False


def test_articles_exported_flag_follows_the_file_on_disk(client, auth, tmp_path):
    """「已导出」按**文件是否还在**判定 —— 删掉导出的 HTML，标记必须跟着消失。

    用户报的原话：「已经删除了文章导出的材料，但是历史文章里面完全没有变化」。
    根因是那一页压根不显示导出状态；补上这个字段之后，判定必须落在磁盘上，
    否则标记会一直骗人（记录表里那行还在，本地其实什么都没有）。

    这里直接验路线：写一条导出记录 + 一个真实文件 → 标记亮；删文件 → 标记灭。
    """
    from mp_harvest.core.export_records import get_records
    from mp_harvest.server import state

    acc = add_account(client, auth)
    state.set_articles(acc["id"], [{
        "title": "有导出的文章", "link": "https://mp.weixin.qq.com/s/e1",
        "publish_ts": 1757000000, "publish_at": "2026-09-05 10:00",
        "identity": "art-exp-1", "body_text": "正文", "body_html": "<p>x</p>",
    }])
    html = tmp_path / "out" / "a.html"
    html.parent.mkdir(parents=True, exist_ok=True)
    html.write_text("<p>x</p>", encoding="utf-8")
    # 与路由同一个单例（隔离目录里的 harvest.db）
    get_records().record_export(article_id="art-exp-1", out_path=str(html), exported_at=1)

    def exported() -> bool:
        rows = client.get("/api/articles", params={**auth, "account_id": acc["id"]}).json()
        hit = [r for r in rows if r["title"] == "有导出的文章"]
        assert len(hit) == 1, rows
        return hit[0]["exported"]

    assert exported() is True
    html.unlink()
    assert exported() is False, "文件已删还显示「已导出」—— 正是用户报的那个现象"


def test_articles_exported_matches_identity_not_the_public_id(client, auth, tmp_path):
    """导出记录存的是 ``identity``，而前端看到的 ``id`` 是 ``{__biz}:{identity}``。

    拿后者去比会**一篇都对不上** —— 而且不报错，只是标记永远不亮。这里用一个
    带 ``__biz`` 的行钉住：必须按 identity 匹配。
    """
    from mp_harvest.core.export_records import get_records
    from mp_harvest.server import state

    acc = add_account(client, auth)
    state.set_articles(acc["id"], [{
        "title": "带 biz 的文章", "link": "https://mp.weixin.qq.com/s/e2",
        "publish_ts": 1757000000, "identity": "art-exp-2", "__biz": "MzA5demo",
        "body_text": "正文", "body_html": "<p>x</p>",
    }])
    html = tmp_path / "b.html"
    html.write_text("<p>x</p>", encoding="utf-8")
    get_records().record_export(article_id="art-exp-2", out_path=str(html), exported_at=1)

    rows = client.get("/api/articles", params={**auth, "account_id": acc["id"]}).json()
    hit = [r for r in rows if r["title"] == "带 biz 的文章"][0]
    assert hit["id"] == "MzA5demo:art-exp-2", "前提：公开 id 确实带了 __biz 前缀"
    assert hit["exported"] is True, "按公开 id 去比会永远不亮"


def test_articles_verdict_and_source_mapping(client, auth):
    """keep True/False → keep/drop；source getmsg/manual/mitm → G/补/M。"""
    from mp_harvest.server import state

    acc = add_account(client, auth)
    state.set_articles(
        acc["id"],
        [
            {"title": "A", "link": "https://x/1", "publish_ts": 1700000000,
             "identity": "i1", "source": "getmsg", "keep": True, "reason": "深度好文"},
            {"title": "B", "link": "https://x/2", "publish_ts": 1700000001,
             "identity": "i2", "source": "manual", "keep": False, "reason": "标题党"},
            {"title": "C", "link": "https://x/3", "publish_ts": 1700000002,
             "identity": "i3", "source": "mitm"},
        ],
    )
    resp = client.get("/api/articles", params={**auth, "account_id": acc["id"]})
    by_id = {a["id"]: a for a in resp.json()}
    assert by_id["i1"]["verdict"] == "keep" and by_id["i1"]["source"] == "G"
    assert by_id["i1"]["reason"] == "深度好文"
    assert by_id["i2"]["verdict"] == "drop" and by_id["i2"]["source"] == "补"
    assert by_id["i3"]["verdict"] is None and by_id["i3"]["source"] == "M"

    # view=keep/drop 过滤在映射前生效，响应仍为裸数组
    resp = client.get(
        "/api/articles", params={**auth, "account_id": acc["id"], "view": "keep"}
    )
    assert [a["id"] for a in resp.json()] == ["i1"]


def test_supplement_bare_article_shape(client, auth):
    acc = add_account(client, auth)
    resp = client.post(
        "/api/articles/supplement",
        params=auth,
        json={"account_id": acc["id"], "url": "https://mp.weixin.qq.com/s/new", "title": "补录文章"},
    )
    assert resp.status_code == 201
    art = resp.json()  # Article 对象本身，无 {sighting} 信封
    assert set(art) <= ARTICLE_KEYS
    assert art["id"]
    assert art["account_id"] == acc["id"]
    assert art["title"] == "补录文章"
    assert art["url"].endswith("/new")
    assert datetime.fromisoformat(art["date"])
    assert art["source"] == "补"
    assert art["verdict"] is None
    assert art["title_verdict"] is None
    assert art["title_reason"] == ""
    assert art["content_verdict"] is None
    assert art["content_reason"] == ""
    assert art["reason"] == ""
