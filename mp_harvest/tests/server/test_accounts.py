"""accounts 路由契约：CRUD + 两段式导入 + 凭证。"""

from __future__ import annotations

from mp_harvest.tests.server.conftest import add_account, give_credential


def test_add_and_list_accounts(client, auth):
    acc = add_account(client, auth)
    assert acc["name"] == "测试号"
    assert acc["pending"] is True  # status=awaiting → pending（前端 Account 形状）

    resp = client.get("/api/accounts", params=auth)
    assert resp.status_code == 200
    accounts = resp.json()  # 裸账号数组
    assert isinstance(accounts, list)
    assert any(a["id"] == acc["id"] for a in accounts)


def test_add_account_without_name_defaults_unnamed(client, auth):
    """名称可留空：后端默认「未命名公众号」（2026-08-09 用户反馈）。"""
    resp = client.post(
        "/api/accounts", params=auth, json={"url": "https://mp.weixin.qq.com/s/abc"}
    )
    assert resp.status_code == 201, resp.text
    acc = resp.json()
    assert acc["name"] == "未命名公众号"
    assert acc["pending"] is True


def test_add_account_validation_error(client, auth):
    # url 仍必填；name 已放开（空名称允许）
    resp = client.post("/api/accounts", params=auth, json={"name": "", "url": ""})
    assert resp.status_code == 422


def test_delete_account(client, auth):
    acc = add_account(client, auth)
    resp = client.delete(f"/api/accounts/{acc['id']}", params=auth)
    assert resp.status_code == 200
    resp = client.get("/api/accounts", params=auth)
    assert all(a["id"] != acc["id"] for a in resp.json())


def test_delete_missing_404(client, auth):
    resp = client.delete("/api/accounts/nope", params=auth)
    assert resp.status_code == 404


def test_get_credential_ok(client, auth):
    acc = add_account(client, auth)
    give_credential(acc["id"])
    resp = client.get(f"/api/accounts/{acc['id']}/credential", params=auth)
    assert resp.status_code == 200
    data = resp.json()
    assert data["credentials"]["__biz"] == "fakebiz"
    assert '"__biz"' in data["json"]


def test_get_credential_no_credential_409(client, auth):
    acc = add_account(client, auth)
    resp = client.get(f"/api/accounts/{acc['id']}/credential", params=auth)
    assert resp.status_code == 409


def test_get_credential_missing_account_404(client, auth):
    resp = client.get("/api/accounts/nope/credential", params=auth)
    assert resp.status_code == 404


def test_import_preview(client, auth):
    """preview：{text} → {items:[{name,url,dup}]}（§7.1 两段式，与前端约定形状）。"""
    add_account(client, auth, name="已有号", url="https://mp.weixin.qq.com/s/dup")
    text = (
        "新号一 https://mp.weixin.qq.com/s/1\n"
        "新号二 https://mp.weixin.qq.com/s/2\n"
        "新号一 https://mp.weixin.qq.com/s/3\n"  # 批内同名
        "已有号 https://mp.weixin.qq.com/s/4\n"  # 与已有同名
        "https://mp.weixin.qq.com/s/dup\n"      # 与已有同 URL
        "这行没有链接\n"                          # 错误行（不进 items）
    )
    resp = client.post("/api/accounts/import", params=auth, json={"text": text})
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    fresh = [i for i in items if not i["dup"]]
    dups = [i for i in items if i["dup"]]
    assert len(fresh) == 2   # 新号一/新号二
    assert len(dups) == 3    # 批内同名 + 已有同名 + 已有同 URL


def test_import_preview_empty_text_400(client, auth):
    resp = client.post("/api/accounts/import", params=auth, json={"text": "  "})
    assert resp.status_code == 400


def test_import_confirm(client, auth):
    """confirm：{stage:'confirm', items} → {imported, skipped}；dup/无链接跳过。"""
    items = [
        {"name": "号A", "url": "https://mp.weixin.qq.com/s/a", "dup": False},
        {"name": "号B", "url": "https://mp.weixin.qq.com/s/b", "dup": False},
        {"name": "重复号", "url": "https://mp.weixin.qq.com/s/c", "dup": True},
        {"name": "无效", "url": "", "dup": False},
    ]
    resp = client.post(
        "/api/accounts/import", params=auth, json={"stage": "confirm", "items": items}
    )
    assert resp.status_code == 200
    assert resp.json() == {"imported": 2, "skipped": 2}
    resp = client.get("/api/accounts", params=auth)
    assert len(resp.json()) == 2


def test_import_confirm_missing_items_400(client, auth):
    resp = client.post("/api/accounts/import", params=auth, json={"stage": "confirm"})
    assert resp.status_code == 400


# ── renew（续约）──────────────────────────────────────────────────


def test_renew_ok(client, auth, fake_core):
    """续约：重置为等待抓包（§5.4）——清抓包状态 + set_awaiting + mitm 确保运行。"""
    from mp_harvest.server import state

    acc = add_account(client, auth)
    svc = state.get_mitm()
    svc.stop()
    resp = client.post(f"/api/accounts/{acc['id']}/renew", params=auth)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["ok"] is True
    assert data["status"] == "awaiting"
    assert svc.reset_called is True          # 已清 inbox/合并，强制等新流量
    assert svc.running is True               # mitm 未运行时自动拉起
    assert state.get_store().get(acc["id"])["status"] == "awaiting"


def test_renew_missing_account_404(client, auth):
    resp = client.post("/api/accounts/nope/renew", params=auth)
    assert resp.status_code == 404


def test_renew_no_biz_no_url_400(client, auth):
    from mp_harvest.server import state

    acc = add_account(client, auth)
    store = state.get_store()
    for row in store._rows:
        if row["id"] == acc["id"]:
            row["article_url"] = ""
            row["credentials"] = {}
            row["biz"] = ""
    resp = client.post(f"/api/accounts/{acc['id']}/renew", params=auth)
    assert resp.status_code == 400
    assert "无法续约" in resp.json()["detail"]


# ── 重复公众号的发现与合并（2026-09）──────────────────────────────
#
# 用户实机数据里出现 4 对「同一个 __biz 两行」（批量导入按名称去重、不按公众号，
# 而短链里没有 __biz 可判）→ 同一篇文章在两个账号下各存一份：列表里成对出现、
# 筛选跑两遍、正文拉两遍、日志打两遍。341 行里 55 行是重复的。


def _acc_with_biz(client, auth, name, biz, articles=()) -> dict:
    """建一个带 biz 的账号（正常流程里 biz 由抓包写入，测试直接给）。

    URL 必须逐行不同：添加接口按文章链接去重，同名同链接的第二行会 409。
    """
    from mp_harvest.server import state

    acc = add_account(client, auth, name=name, url=f"https://mp.weixin.qq.com/s/{name}")
    store = state.get_store()
    for row in store._rows:
        if row["id"] == acc["id"]:
            row["biz"] = biz
            row["credentials"] = {"__biz": biz, "key": "k", "uin": "u"}
    if articles:
        state.set_articles(acc["id"], [dict(a) for a in articles])
    return acc


def _art(tag, **over) -> dict:
    row = {"title": tag, "link": f"https://x/{tag}", "publish_ts": 2, "identity": f"art-{tag}"}
    row.update(over)
    return row


def test_duplicates_lists_same_biz_groups(client, auth):
    """同 __biz 两行 → 报成一组；不同 biz 的账号不进列表。"""
    _acc_with_biz(client, auth, "甲号", "bizA", [_art("1"), _art("2")])
    _acc_with_biz(client, auth, "甲号重复", "bizA", [_art("1")])
    _acc_with_biz(client, auth, "乙号", "bizB", [_art("9")])

    resp = client.get("/api/accounts/duplicates", params=auth)
    assert resp.status_code == 200, resp.text
    groups = resp.json()["groups"]
    assert len(groups) == 1, f"只该有一组重复：{groups}"
    assert groups[0]["biz"] == "bizA"
    names = [a["name"] for a in groups[0]["accounts"]]
    assert names == ["甲号", "甲号重复"], "文章多的应排在前面（前端拿它当默认选择）"
    assert groups[0]["accounts"][0]["article_count"] == 2


def test_duplicates_ignores_accounts_without_biz(client, auth):
    """取不到 biz 的账号不能被当成「彼此重复」—— 那会把两个不相干的号并到一起。"""
    add_account(client, auth, name="无biz一号", url="https://mp.weixin.qq.com/s/nb1")
    add_account(client, auth, name="无biz二号", url="https://mp.weixin.qq.com/s/nb2")
    resp = client.get("/api/accounts/duplicates", params=auth)
    assert resp.json()["groups"] == []


def test_merge_duplicates_unions_articles_and_drops_rows(client, auth):
    """合并：文章取并集、判定结果以保留行为准、多余的行被删掉。"""
    from mp_harvest.server import state

    keep = _acc_with_biz(client, auth, "保留行", "bizA", [_art("1", keep=True, reason="已判")])
    drop = _acc_with_biz(client, auth, "被并掉", "bizA", [
        _art("1", keep=False, reason="旧判定"),   # 同一篇（identity 相同）→ 以保留行为准
        _art("2"),                                # 只在这行有 → 搬过去
    ])

    resp = client.post(
        "/api/accounts/merge-duplicates",
        params=auth,
        json={"keep_id": keep["id"], "drop_ids": [drop["id"]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added_articles"] == 1 and body["total"] == 2

    rows = state.get_articles(keep["id"])
    by_key = {r["identity"]: r for r in rows}
    assert set(by_key) == {"art-1", "art-2"}
    assert by_key["art-1"]["reason"] == "已判", "保留行的判定不能被覆盖掉"

    # 被合并的行连同文章缓存一起消失
    assert client.get("/api/accounts", params=auth).json()
    assert state.get_store().get(drop["id"]) is None
    assert state.get_articles(drop["id"]) == []


def test_merge_refuses_different_biz(client, auth):
    """__biz 不同 = 不是同一个公众号，合并会把两个号的文章混在一起（不可逆）。"""
    from mp_harvest.server import state

    keep = _acc_with_biz(client, auth, "甲", "bizA", [_art("1")])
    other = _acc_with_biz(client, auth, "乙", "bizB", [_art("2")])
    resp = client.post(
        "/api/accounts/merge-duplicates",
        params=auth,
        json={"keep_id": keep["id"], "drop_ids": [other["id"]]},
    )
    assert resp.status_code == 400, resp.text
    assert "__biz" in resp.json()["detail"]
    # 两边都原样留着
    assert state.get_store().get(other["id"]) is not None
    assert len(state.get_articles(keep["id"])) == 1


def test_merge_refuses_missing_biz(client, auth):
    """两边都取不到 biz 时也必须拒绝 —— 空 == 空 不是「同一个号」。"""
    from mp_harvest.server import state

    keep = add_account(client, auth, name="无biz甲", url="https://mp.weixin.qq.com/s/nb3")
    other = add_account(client, auth, name="无biz乙", url="https://mp.weixin.qq.com/s/nb4")
    state.set_articles(keep["id"], [_art("1")])
    resp = client.post(
        "/api/accounts/merge-duplicates",
        params=auth,
        json={"keep_id": keep["id"], "drop_ids": [other["id"]]},
    )
    assert resp.status_code == 400, resp.text


def test_merge_requires_at_least_one_drop(client, auth):
    acc = _acc_with_biz(client, auth, "甲", "bizA", [_art("1")])
    resp = client.post(
        "/api/accounts/merge-duplicates", params=auth, json={"keep_id": acc["id"], "drop_ids": []}
    )
    assert resp.status_code == 400


def test_merge_unknown_keep_404(client, auth):
    resp = client.post(
        "/api/accounts/merge-duplicates", params=auth, json={"keep_id": "nope", "drop_ids": ["x"]}
    )
    assert resp.status_code == 404
