"""history / articles / supplement 路由契约 + 拉历史任务。"""

from __future__ import annotations

import time

from mp_harvest.tests.server.conftest import add_account, give_credential, wait_task


def _fetch(client, auth, account_id, days=7):
    resp = client.post(
        "/api/history/fetch", params=auth, json={"account_id": account_id, "days": days}
    )
    assert resp.status_code == 202, resp.text
    return resp.json()["task_id"]


def test_history_fetch_task_done(client, auth):
    acc = add_account(client, auth)
    give_credential(acc["id"])
    task_id = _fetch(client, auth, acc["id"])
    task = wait_task(task_id)
    assert task.status == "done"
    assert task.result["count"] == 2
    assert task.result["ok"] is True


def test_history_fetch_unknown_account_404(client, auth):
    resp = client.post("/api/history/fetch", params=auth, json={"account_id": "nope", "days": 7})
    assert resp.status_code == 404


def test_history_fetch_no_credential_409(client, auth):
    acc = add_account(client, auth)
    resp = client.post(
        "/api/history/fetch", params=auth, json={"account_id": acc["id"], "days": 7}
    )
    assert resp.status_code == 409


def test_history_fetch_cancel(client, auth, fake_core):
    """取消语义：cancel 后业务在分页边界（on_progress）抛 TaskCancelled。"""
    acc = add_account(client, auth)
    give_credential(acc["id"])
    # 慢速拉取：每页 sleep，给 cancel 留窗口
    def slow_fetch(cred, *, days=7, on_progress=None, sightings=None, **kw):
        for i in range(100):
            if on_progress:
                on_progress(f"第 {i} 页")
            time.sleep(0.05)
        return {"ok": True, "articles": [], "pages": 100}

    fake_core.history_client.fetch_history_days = slow_fetch
    task_id = _fetch(client, auth, acc["id"])
    time.sleep(0.1)  # 确保已进入第一页
    resp = client.post(f"/api/tasks/{task_id}/cancel", params=auth)
    assert resp.status_code == 200
    task = wait_task(task_id)
    assert task.status == "cancelled"
    assert task.error


def test_articles_view_and_order(client, auth):
    from mp_harvest.server import state

    acc = add_account(client, auth)
    give_credential(acc["id"])
    task_id = _fetch(client, auth, acc["id"])
    wait_task(task_id)

    resp = client.get("/api/articles", params={**auth, "account_id": acc["id"]})
    assert resp.status_code == 200
    data = resp.json()  # 裸 Article[]
    assert isinstance(data, list)
    assert len(data) == 2
    dates = [a["date"] for a in data]
    assert dates == sorted(dates, reverse=True)  # 默认 desc（按 publish_ts 排序后映射）

    resp = client.get(
        "/api/articles", params={**auth, "account_id": acc["id"], "order": "asc"}
    )
    dates = [a["date"] for a in resp.json()]
    assert dates == sorted(dates)

    # view 过滤：标记一篇 keep=True 一篇 keep=False
    arts = state.get_articles(acc["id"])
    arts[0]["keep"] = True
    arts[1]["keep"] = False
    state.set_articles(acc["id"], arts)
    resp = client.get(
        "/api/articles", params={**auth, "account_id": acc["id"], "view": "keep"}
    )
    assert len(resp.json()) == 1
    resp = client.get(
        "/api/articles", params={**auth, "account_id": acc["id"], "view": "drop"}
    )
    assert len(resp.json()) == 1


def test_articles_bad_view_400(client, auth):
    acc = add_account(client, auth)
    resp = client.get(
        "/api/articles", params={**auth, "account_id": acc["id"], "view": "bogus"}
    )
    assert resp.status_code == 400


def test_articles_unknown_account_404(client, auth):
    resp = client.get("/api/articles", params={**auth, "account_id": "nope"})
    assert resp.status_code == 404


def test_supplement_ok(client, auth):
    acc = add_account(client, auth)
    resp = client.post(
        "/api/articles/supplement",
        params=auth,
        json={"account_id": acc["id"], "url": "https://mp.weixin.qq.com/s/new", "title": "补录"},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["url"].endswith("/new")  # 裸 Article 对象
    # 补录文章进入该账号缓存
    resp = client.get("/api/articles", params={**auth, "account_id": acc["id"]})
    assert len(resp.json()) == 1


def test_supplement_empty_422(client, auth):
    resp = client.post("/api/articles/supplement", params=auth, json={"url": ""})
    assert resp.status_code == 422
