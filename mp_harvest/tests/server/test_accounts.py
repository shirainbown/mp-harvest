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
