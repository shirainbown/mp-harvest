"""按篇删掉历史文章（2026-09）。

用户要求：「有些文章我认为认定可以删掉」。语义是他选定的 —— **只从本地列表删掉**，
不动微信那边，下次「拉取历史」会重新抓到（另外两个候选是「删掉并永不收录」与
「两个动作分开」，都没选）。

所以这里钉三件事：
1. 删了就真的从列表里没了，而且**落了盘**（不是只改了内存）
2. id 用前端可见的 ``{__biz}:{identity}`` 也认得（现算现比，不解析字符串）
3. 不该误伤：只删点名的那些，别的账号/别的篇目不动
"""

from __future__ import annotations

from .conftest import add_account


def _seed(account_id: str, n: int = 3, *, tag: str = "d", biz: str = "") -> list[dict]:
    from mp_harvest.server import state

    rows = []
    for i in range(n):
        row = {
            "title": f"待删文章{tag}{i}",
            "link": f"https://mp.weixin.qq.com/s/{tag}{i}",
            "publish_ts": 1757000000 + i * 3600,
            "publish_at": "2026-09-05 10:00",
            "identity": f"mid:{tag}{i}",
            "body_text": "正文", "body_html": "<p>x</p>",
        }
        if biz:
            row["__biz"] = biz
        rows.append(row)
    state.set_articles(account_id, rows)
    return rows


def _ids(client, auth, account_id: str) -> list[str]:
    return [
        a["id"]
        for a in client.get("/api/articles", params={**auth, "account_id": account_id}).json()
    ]


def test_delete_removes_from_the_local_list(client, auth):
    acc = add_account(client, auth)
    _seed(acc["id"])
    before = _ids(client, auth, acc["id"])
    assert len(before) == 3

    r = client.post("/api/articles/delete", params=auth,
                      json={"account_id": acc["id"], "ids": [before[0]]})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "removed": 1}

    after = _ids(client, auth, acc["id"])
    assert len(after) == 2 and before[0] not in after
    assert sorted(after) == sorted(before[1:]), "只该删掉点名那一篇"


def test_delete_persists_to_disk(client, auth, isolated_data_dir):
    """删完要落盘 —— 只改内存的话，重启一次文章全回来了。

    直接读缓存文件断言（不能用 `state.drop_articles` 来「强制重读」：那个函数
    会把内存**和磁盘文件一起**删掉，于是读到空列表，测试看着像通过其实什么都没验）。
    """
    import json

    acc = add_account(client, auth)
    _seed(acc["id"])
    rows = client.get("/api/articles", params={**auth, "account_id": acc["id"]}).json()
    target, gone_title = rows[0]["id"], rows[0]["title"]
    client.post("/api/articles/delete", params=auth,
                json={"account_id": acc["id"], "ids": [target]})

    files = list((isolated_data_dir / "articles_cache").glob("*.json"))
    assert len(files) == 1, files
    # 缓存文件带信封：{"days":…, "last_fetch_ts":…, "articles":[…]}
    on_disk = json.loads(files[0].read_text(encoding="utf-8"))["articles"]
    assert len(on_disk) == 2, "删掉的文章又出现在磁盘上了"
    # 按标题比，不按 id —— id 是 {__biz}:{identity} 的拼接，这里再手工拼一遍
    # 等于把实现抄进测试，实现改了测试跟着错还看不出来
    assert gone_title not in {str(r.get("title")) for r in on_disk}


def test_delete_matches_the_public_id_with_biz(client, auth):
    """带 ``__biz`` 的文章用前端那个 id 也要删得掉。

    前端看到的是 ``{__biz}:{identity}``，而缓存行里存的是 ``identity`` ——
    按同一函数现算现比才能对上；手工拆前缀迟早拆错（identity 本身就带冒号）。
    """
    acc = add_account(client, auth)
    _seed(acc["id"], biz="MzA5demo")
    ids = _ids(client, auth, acc["id"])
    assert all(i.startswith("MzA5demo:") for i in ids), ids

    r = client.post("/api/articles/delete", params=auth,
                      json={"account_id": acc["id"], "ids": [ids[1]]})
    assert r.json()["removed"] == 1
    assert _ids(client, auth, acc["id"]) == [ids[0], ids[2]]


def test_delete_without_account_spans_all(client, auth):
    """不传 account_id = 「全部公众号」视图下删选中的那几篇。"""
    # 链接必须不同：add_account 用同一个默认 url 会被判成「账号已存在」409
    a1 = add_account(client, auth, name="号甲", url="https://mp.weixin.qq.com/s/acc-p")
    a2 = add_account(client, auth, name="号乙", url="https://mp.weixin.qq.com/s/acc-q")
    _seed(a1["id"], tag="p")
    _seed(a2["id"], tag="q")
    pick = [_ids(client, auth, a1["id"])[0], _ids(client, auth, a2["id"])[1]]

    r = client.post("/api/articles/delete", params=auth, json={"ids": pick})
    assert r.json() == {"ok": True, "removed": 2}
    assert len(_ids(client, auth, a1["id"])) == 2
    assert len(_ids(client, auth, a2["id"])) == 2


def test_delete_ignores_unknown_and_empty_ids(client, auth):
    """不认识的 id / 空列表 —— 不能报错，也不能误删。"""
    acc = add_account(client, auth)
    _seed(acc["id"])

    assert client.post("/api/articles/delete", params=auth,
                         json={"ids": []}).json() == {"ok": True, "removed": 0}
    assert client.post("/api/articles/delete", params=auth,
                         json={"ids": ["不存在", ""]}).json() == {"ok": True, "removed": 0}
    assert len(_ids(client, auth, acc["id"])) == 3, "什么都没点名却删掉了东西"


def test_delete_rejects_unknown_account(client, auth):
    r = client.post("/api/articles/delete", params=auth,
                      json={"account_id": "无此账号", "ids": ["x"]})
    assert r.status_code == 404
