"""ai 路由契约：筛选任务 + 模型 CRUD/测试 + 原则。"""

from __future__ import annotations

from mp_harvest.tests.server.conftest import add_account, give_credential, wait_task


def _prepare_articles(client, auth):
    from mp_harvest.server import state

    acc = add_account(client, auth)
    give_credential(acc["id"])
    state.set_articles(
        acc["id"],
        [{"title": "A", "link": "https://x/1", "publish_ts": 2, "identity": "art-0"}],
    )
    return acc


def test_ai_filter_task_and_verdict_merge(client, auth):
    from mp_harvest.server import state

    acc = _prepare_articles(client, auth)
    resp = client.post("/api/ai/filter", params=auth, json={"account_id": acc["id"]})
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    assert task.result["kept"] == 1
    # 判定结果已合并回缓存 → view=keep 可见
    resp = client.get(
        "/api/articles", params={**auth, "account_id": acc["id"], "view": "keep"}
    )
    assert len(resp.json()) == 1  # 裸 Article[]


def test_ai_filter_no_articles_400(client, auth):
    acc = add_account(client, auth)
    give_credential(acc["id"])
    resp = client.post("/api/ai/filter", params=auth, json={"account_id": acc["id"]})
    assert resp.status_code == 400


def test_ai_filter_parallel_controls(client, auth):
    """batch_size / workers 透传（2026-08-09 新增并行判定控制）。"""
    from mp_harvest.server import state

    acc = _prepare_articles(client, auth)
    resp = client.post(
        "/api/ai/filter",
        params=auth,
        json={"account_id": acc["id"], "batch_size": 5, "workers": 2},
    )
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    assert task.result["kept"] == 1


def test_ai_filter_batch_realtime_broadcast(client, auth, monkeypatch):
    """每批完成即 WS 推 ai.batch（2026-08-09：前端实时刷新判定）。"""
    from mp_harvest.server.routes import ai as ai_routes

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ai_routes, "broadcast_event", lambda t, p=None: events.append((t, p))
    )
    acc = _prepare_articles(client, auth)
    resp = client.post(
        "/api/ai/filter",
        params=auth,
        json={"account_id": acc["id"], "batch_size": 1, "workers": 1},
    )
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    batch_events = [e for e in events if e[0] == "ai.batch"]
    assert batch_events
    payload = batch_events[0][1]
    assert payload["account_id"] == acc["id"]
    assert payload["articles"] and payload["articles"][0]["verdict"] == "keep"


def test_ai_filter_parallel_controls_validation_422(client, auth):
    acc = add_account(client, auth)
    give_credential(acc["id"])
    resp = client.post(
        "/api/ai/filter",
        params=auth,
        json={"account_id": acc["id"], "batch_size": 0, "workers": 0},
    )
    assert resp.status_code == 422


def test_ai_filter_unknown_account_404(client, auth):
    resp = client.post("/api/ai/filter", params=auth, json={"account_id": "nope"})
    assert resp.status_code == 404


def test_ai_filter_all_accounts(client, auth):
    """account_id 为空 = 全部公众号批量筛选（2026-08-16 新增）。"""
    from mp_harvest.server import state

    acc1 = _prepare_articles(client, auth)
    acc2 = _prepare_articles(client, auth)
    state.set_articles(
        acc1["id"],
        [{"title": "A", "link": "https://x/1", "publish_ts": 2, "identity": "art-0"}],
    )
    state.set_articles(
        acc2["id"],
        [{"title": "B", "link": "https://x/2", "publish_ts": 1, "identity": "art-1"}],
    )
    resp = client.post("/api/ai/filter", params=auth, json={"account_id": ""})
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    assert task.result["kept"] == 2

    resp = client.get("/api/articles", params={**auth, "view": "keep"})
    assert len(resp.json()) == 2


def test_models_get_put(client, auth):
    resp = client.get("/api/ai/models", params=auth)
    assert resp.status_code == 200
    assert resp.json()["models"][0]["name"] == "m1"

    body = [
        {"name": "gpt", "format": "openai", "base_url": "https://api.openai.com",
         "api_key": "sk-x", "model": "gpt-5", "enabled": True},
        {"name": "claude", "format": "anthropic", "enabled": False},
    ]
    resp = client.put("/api/ai/models", params=auth, json=body)
    assert resp.status_code == 200
    assert resp.json()["count"] == 2

    resp = client.get("/api/ai/models", params=auth)
    names = [m["name"] for m in resp.json()["models"]]
    assert names == ["gpt", "claude"]


def test_models_put_validation_422(client, auth):
    resp = client.put("/api/ai/models", params=auth, json=[{"name": 123}])
    assert resp.status_code == 422


def test_model_test_ok_and_fail(client, auth):
    resp = client.post(
        "/api/ai/models/test", params=auth, json={"name": "m", "api_key": "k"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["message"] == "连接成功"
    assert data["error"] == data["message"]

    resp = client.post("/api/ai/models/test", params=auth, json={"name": "m"})
    data = resp.json()
    assert data["ok"] is False
    assert data["error"] == "缺少 api_key"
    assert data["message"] == data["error"]


def test_models_fetch_list(client, auth):
    resp = client.post(
        "/api/ai/models/fetch",
        params=auth,
        json={"base_url": "https://api.deepseek.com", "api_key": "sk-x"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["models"] == ["deepseek-chat", "deepseek-reasoner"]


def test_models_fetch_bad_key(client, auth):
    resp = client.post(
        "/api/ai/models/fetch",
        params=auth,
        json={"base_url": "https://bad.example.com", "api_key": "sk-wrong"},
    )
    assert resp.status_code == 200  # 与 test 端点一致：业务失败不进 HTTP 错误
    data = resp.json()
    assert data["ok"] is False
    assert data["models"] == []
    assert "401" in data["message"]


def test_models_fetch_anthropic_unsupported(client, auth):
    resp = client.post(
        "/api/ai/models/fetch",
        params=auth,
        json={"base_url": "https://api.anthropic.com", "api_key": "sk-ant", "format": "anthropic"},
    )
    assert resp.json()["ok"] is False
    assert "Anthropic" in resp.json()["message"]


def test_principles_get_put(client, auth):
    resp = client.get("/api/ai/principles", params=auth)
    assert resp.status_code == 200
    data = resp.json()
    assert data["text"] == "默认原则"
    assert data["default"] == "内置默认原则"  # DEFAULT_PRINCIPLES（前端「恢复默认」）

    resp = client.put("/api/ai/principles", params=auth, json={"text": "只要技术文"})
    assert resp.status_code == 200
    resp = client.get("/api/ai/principles", params=auth)
    assert resp.json()["text"] == "只要技术文"


def test_content_principles_get_put(client, auth):
    resp = client.get("/api/ai/content-principles", params=auth)
    assert resp.status_code == 200
    data = resp.json()
    assert data["text"] == "默认内容原则"
    assert data["default"] == "内置默认内容原则"

    resp = client.put("/api/ai/content-principles", params=auth, json={"text": "正文必须有代码"})
    assert resp.status_code == 200
    resp = client.get("/api/ai/content-principles", params=auth)
    assert resp.json()["text"] == "正文必须有代码"


def test_ai_filter_content_task_and_merge(client, auth):
    """内容筛选：仅对标题筛选 keep=True 的文章拉正文并判定，结果合并回缓存。"""
    from mp_harvest.server import state

    acc = _prepare_articles(client, auth)
    state.set_articles(
        acc["id"],
        [
            {"title": "A", "link": "https://x/1", "publish_ts": 2, "identity": "art-0", "title_keep": True},
            {"title": "B", "link": "https://x/2", "publish_ts": 1, "identity": "art-1", "title_keep": False},
        ],
    )
    resp = client.post(
        "/api/ai/filter-content", params=auth, json={"account_id": acc["id"]}
    )
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    assert task.result["kept"] == 1
    assert task.result["dropped"] == 0

    # 内容判定结果已合并回缓存 → view=keep 只有 art-0
    resp = client.get(
        "/api/articles", params={**auth, "account_id": acc["id"], "view": "keep"}
    )
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["id"] == "art-0"


def test_ai_filter_content_requires_title_keep(client, auth):
    """没有 keep=True 的文章时，内容筛选接口返回 400（先做标题筛选）。"""
    from mp_harvest.server import state

    acc = _prepare_articles(client, auth)
    state.set_articles(
        acc["id"],
        [
            {"title": "B", "link": "https://x/2", "publish_ts": 1, "identity": "art-1", "title_keep": False},
        ],
    )
    resp = client.post(
        "/api/ai/filter-content", params=auth, json={"account_id": acc["id"]}
    )
    assert resp.status_code == 400
    assert "标题筛选" in resp.json()["detail"]


def test_ai_filter_content_unknown_account_404(client, auth):
    resp = client.post(
        "/api/ai/filter-content", params=auth, json={"account_id": "nope"}
    )
    assert resp.status_code == 404
