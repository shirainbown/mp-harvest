"""ai 路由契约：筛选任务 + 模型 CRUD/测试 + 原则。"""

from __future__ import annotations

from mp_harvest.tests.server.conftest import add_account, give_credential, wait_task


def _prepare_articles(client, auth, url="https://mp.weixin.qq.com/s/abc"):
    from mp_harvest.server import state

    acc = add_account(client, auth, url=url)
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
    # B18 起相同 article_url 返回 409，第二个账号需用不同链接
    acc2 = _prepare_articles(client, auth, url="https://mp.weixin.qq.com/s/abc2")
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


def test_content_filter_cancel_keeps_fetched_bodies(client, auth, fake_core):
    """正文抓到一半被取消 → 已拿到的必须落盘（2026-09 断点拉取）。

    原先正文只在循环**正常跑完**后统一 merge 回缓存，中途取消（或抛错）就丢掉
    本轮已经拉回来的每一篇 —— 下次再跑全部重拉，既浪费又白挨一次限流风险。
    """
    from mp_harvest.server import state
    from mp_harvest.server.tasks import registry

    acc = _prepare_articles(client, auth)
    state.set_articles(
        acc["id"],
        [
            {"title": "A", "link": "https://x/1", "publish_ts": 3,
             "identity": "a1", "title_keep": True},
            {"title": "B", "link": "https://x/2", "publish_ts": 2,
             "identity": "a2", "title_keep": True},
            {"title": "C", "link": "https://x/3", "publish_ts": 1,
             "identity": "a3", "title_keep": True},
        ],
    )

    calls = {"n": 0}
    body = "这是用于内容筛选的正文，包含足够的技术细节与实现方法，长度超过二十个字。"

    def cancel_after_second(url, *, cred=None, timeout=25.0):
        calls["n"] += 1
        if calls["n"] == 2:
            # 第二篇抓完就置取消标志 → 第三篇循环开头的 check_cancelled 抛异常
            for t in registry.list():
                if t.type == "ai.filter_content" and t.status == "running":
                    registry.cancel(t.id)
        return {"title": "t", "link": url, "body_text": body,
                "body_html": f"<p>{body}</p>", "content_found": True}

    fake_core.article_reader.fetch_and_parse_article = cancel_after_second

    resp = client.post("/api/ai/filter-content", params=auth, json={"account_id": acc["id"]})
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "cancelled", task.status

    rows = {r["identity"]: r for r in state.get_articles(acc["id"])}
    assert str(rows["a1"].get("body_text") or "").strip(), "取消前抓到的正文必须已落盘"
    assert str(rows["a2"].get("body_text") or "").strip(), "第二篇同样要保住"


def test_content_filter_skips_already_cached_bodies(client, auth, fake_core):
    """已有正文的文章不再抓取（一次不联网）。"""
    from mp_harvest.server import state

    acc = _prepare_articles(client, auth)
    state.set_articles(
        acc["id"],
        [
            {"title": "A", "link": "https://x/1", "publish_ts": 2, "identity": "a1",
             "title_keep": True, "body_text": "早已缓存的正文，包含足够的技术细节与实现方法，超过二十个字。"},
            {"title": "B", "link": "https://x/2", "publish_ts": 1, "identity": "a2",
             "title_keep": True},
        ],
    )

    fetched: list[str] = []
    body = "这是用于内容筛选的正文，包含足够的技术细节与实现方法，长度超过二十个字。"

    def counting(url, *, cred=None, timeout=25.0):
        fetched.append(url)
        return {"title": "t", "link": url, "body_text": body,
                "body_html": f"<p>{body}</p>", "content_found": True}

    fake_core.article_reader.fetch_and_parse_article = counting

    resp = client.post("/api/ai/filter-content", params=auth, json={"account_id": acc["id"]})
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done", task.error
    assert fetched == ["https://x/2"], f"只该抓没有正文的那一篇，实际 {fetched}"


def test_put_principles_only_wipes_cache_when_changed(client, auth, isolated_data_dir):
    """原则**没变**时不能清 AI 判定缓存（2026-09）。

    判定结果以「当时用的原则」为前提，内容没变则判定依然有效。原先无条件
    `_invalidate_cache` —— 用户点一下「保存」什么都没改，也会把攒了很久的
    判定结果全部丢掉，下次筛选得重新花钱判定一遍。
    """
    from mp_harvest.server.routes.ai import _cache_path

    cache = _cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)

    def wipe_and_check(new_text: str) -> bool:
        cache.write_text('{"__version__":2,"entries":{"k":{"keep":true}}}', encoding="utf-8")
        r = client.put("/api/ai/principles", params=auth, json={"text": new_text})
        assert r.status_code == 200, r.text
        return not cache.exists()

    # 先写入一个自定义原则
    assert wipe_and_check("自定义原则 A") is True   # 内容变了 → 清缓存
    # 原样再存一次 → 不该清
    assert wipe_and_check("自定义原则 A") is False
    # 真的改了 → 该清
    assert wipe_and_check("自定义原则 B") is True


def test_put_content_principles_only_wipes_cache_when_changed(client, auth, isolated_data_dir):
    from mp_harvest.server.routes.ai import _content_cache_path

    cache = _content_cache_path()
    cache.parent.mkdir(parents=True, exist_ok=True)

    cache.write_text('{"__version__":2,"entries":{}}', encoding="utf-8")
    client.put("/api/ai/content-principles", params=auth, json={"text": "内容原则 A"})
    assert not cache.exists()          # 改了 → 清

    cache.write_text('{"__version__":2,"entries":{}}', encoding="utf-8")
    client.put("/api/ai/content-principles", params=auth, json={"text": "内容原则 A"})
    assert cache.exists()              # 没改 → 保留


# ── 只筛选中（2026-09）────────────────────────────────────────────
#
# 场景：一次内容筛选里有几篇正文没抓到（微信的环境校验、网络抖动），
# 用户只想重跑那几篇。整体重跑虽然缓存命中不花 AI 的钱，但仍会**重新联网**
# 去拉那些没正文的，而且失败原因不一定还是同一个。
#
# ids 用的是前端可见的 ``Article.id``（``{__biz}:{identity}``）——所以测试
# 一律**从 /api/articles 取 id**，不自己拼字符串：拼法一旦和 mappers 不一致，
# 测试会跟着一起错，等于没测。


def _mk(tag: str, **over) -> dict:
    # 默认带 __biz：**真实的缓存行一定有**（merge_articles 会写上），而
    # ``article_public_id`` 带上它之后 id 才不等于 identity。不带的话
    # 「按 id 比对」和「按 identity 比对」两种写法结果一样，用例就是空转的
    # ——变异测试正是这么抓出来的。
    row = {
        "title": tag,
        "link": f"https://x/{tag}",
        "publish_ts": 2,
        "identity": f"art-{tag}",
        "__biz": "bizTest",
    }
    row.update(over)
    return row


def _verdicts(client, auth, account_id) -> dict[str, object]:
    rows = client.get("/api/articles", params={**auth, "account_id": account_id}).json()
    return {r["id"]: r["verdict"] for r in rows}


def test_ai_filter_only_selected_ids(client, auth):
    """只判勾选的那一篇，其余原样不动。"""
    from mp_harvest.server import state

    acc = _prepare_articles(client, auth)
    state.set_articles(acc["id"], [_mk("A"), _mk("B"), _mk("C")])
    listing = client.get("/api/articles", params={**auth, "account_id": acc["id"]}).json()
    assert len(listing) == 3
    picked = [listing[0]["id"]]

    resp = client.post(
        "/api/ai/filter", params=auth, json={"account_id": acc["id"], "ids": picked}
    )
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.result["judged"] == 1, "只勾了一篇，判定数必须是 1"
    assert resp.json()["total"] == 1

    verdicts = _verdicts(client, auth, acc["id"])
    assert verdicts[picked[0]] == "keep"
    others = [v for k, v in verdicts.items() if k != picked[0]]
    assert others == [None, None], f"没勾的不该被判定：{verdicts}"


def test_ai_filter_empty_ids_keeps_whole_scope_behaviour(client, auth):
    """ids 为空 = 老行为（整个范围），保证旧调用方不受影响。"""
    from mp_harvest.server import state

    acc = _prepare_articles(client, auth)
    state.set_articles(acc["id"], [_mk("A"), _mk("B")])
    for payload in ({"account_id": acc["id"]}, {"account_id": acc["id"], "ids": []}):
        resp = client.post("/api/ai/filter", params=auth, json=payload)
        assert resp.status_code == 202, resp.text
        assert resp.json()["total"] == 2, payload


def test_ai_filter_ids_from_another_account_are_not_matched(client, auth):
    """勾选的 id 属于另一个账号时不能被误判（同一 identity 可能存在于多个账号）。

    这是「只筛选中」最容易出的错：如果按 identity 比对而不是按现算的 id，
    另一个账号下同 identity 的文章会连坐被判。
    """
    from mp_harvest.server import state

    a = _prepare_articles(client, auth)
    b = _prepare_articles(client, auth, url="https://mp.weixin.qq.com/s/def")
    # 行上必须带 __biz：真实缓存里 merge_articles 会写上（id 靠它区分账号）。
    # 不写的话两个账号的文章会算出**同一个 id**，这个用例就变成了自欺欺人 ——
    # 它要验的正是「id 能区分账号」。
    state.set_articles(a["id"], [_mk("A", __biz="bizA")])
    state.set_articles(b["id"], [_mk("A", __biz="bizB")])
    ids_a = [r["id"] for r in client.get("/api/articles", params={**auth, "account_id": a["id"]}).json()]
    ids_b = [r["id"] for r in client.get("/api/articles", params={**auth, "account_id": b["id"]}).json()]
    assert ids_a != ids_b, f"前提不成立：两个账号的 id 竟然一样（{ids_a}）"

    # 拿 B 的 id 去筛 A：一篇都匹配不上 → 400，且 A 的文章一篇都没被判
    resp = client.post(
        "/api/ai/filter", params=auth, json={"account_id": a["id"], "ids": ids_b}
    )
    assert resp.status_code == 400, resp.text
    assert "选中" in resp.json()["detail"], resp.json()["detail"]
    assert _verdicts(client, auth, a["id"]) == {ids_a[0]: None}


def test_ai_filter_ids_not_found_message_is_specific(client, auth):
    """勾选的都失效了 → 提示要说明是「选中的不在范围内」，不是「请先拉取历史」。

    两种情况用户要做的事完全不同：前者刷新列表重勾，后者去拉历史。
    """
    acc = _prepare_articles(client, auth)
    resp = client.post(
        "/api/ai/filter",
        params=auth,
        json={"account_id": acc["id"], "ids": ["不存在:id"]},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "选中" in detail and "拉取历史" not in detail, detail


def test_content_filter_only_selected_ids(client, auth):
    """内容筛选同样支持只筛选中，且只处理勾选中通过标题阶段的那几篇。"""
    from mp_harvest.server import state

    acc = _prepare_articles(client, auth)
    state.set_articles(
        acc["id"],
        [
            _mk("A", title_keep=True, body_text="正文" * 20),
            _mk("B", title_keep=True, body_text="正文" * 20),
            _mk("C", title_keep=False, body_text="正文" * 20),
        ],
    )
    listing = client.get("/api/articles", params={**auth, "account_id": acc["id"]}).json()
    picked = [listing[0]["id"]]

    resp = client.post(
        "/api/ai/filter-content",
        params=auth,
        json={"account_id": acc["id"], "ids": picked},
    )
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.result["judged"] == 1, f"只勾了一篇（且它有正文）→ 判定数应为 1：{task.result}"

    verdicts = {
        r["id"]: r["content_verdict"]
        for r in client.get("/api/articles", params={**auth, "account_id": acc["id"]}).json()
    }
    assert verdicts[picked[0]] == "keep"
    assert [v for k, v in verdicts.items() if k != picked[0]] == [None, None]


def test_content_filter_ids_without_title_keep_says_why(client, auth):
    """勾选的都没过标题阶段 → 说清楚原因（内容筛选只处理标题通过的）。"""
    from mp_harvest.server import state

    acc = _prepare_articles(client, auth)
    state.set_articles(acc["id"], [_mk("A", title_keep=False), _mk("B")])
    listing = client.get("/api/articles", params={**auth, "account_id": acc["id"]}).json()

    resp = client.post(
        "/api/ai/filter-content",
        params=auth,
        json={"account_id": acc["id"], "ids": [r["id"] for r in listing]},
    )
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert "标题" in detail and "选中" in detail, detail


# ── 正文反复拿不到 → 记原因、够次数就停（2026-09）──────────────────
#
# 用户问「拿不到正文为什么内容筛选时没被剔除」。答案是**故意不剔除**：剔除
# （写 content_keep=False）是不可逆的，一次网络抖动就把文章永久钉成「AI 判定不
# 相关」，而且跟真判定长得一样。但一直不管也不行 —— 这类文章（转发/分享型消息，
# 正文根本不在那一页）会每期被重抓、每期报一次失败。折中是：记下原因 + 够
# BODY_GIVE_UP_AFTER 次就停止自动重试，**手动勾选仍可强制重跑**。


def _failing_fetch(monkeypatch, why="正文过短或无实质内容"):
    """让 article_reader.fetch_and_parse_article 每次都拿不到正文。"""
    from mp_harvest.core import article_reader as ar

    monkeypatch.setattr(
        ar,
        "fetch_and_parse_article",
        lambda url, **kw: {"content_found": True, "body_text": "", "body_html": "", "title": "t"},
    )
    return why


def _run_content(client, auth, account_id, ids=None):
    payload = {"account_id": account_id}
    if ids:
        payload["ids"] = ids
    r = client.post("/api/ai/filter-content", params=auth, json=payload)
    assert r.status_code == 202, r.text
    return wait_task(r.json()["task_id"])


def test_body_failure_is_recorded_with_reason(client, auth, monkeypatch):
    """拿不到正文：计数 + 原因落盘（原先只广播，重启就没了）。"""
    from mp_harvest.server import state

    _failing_fetch(monkeypatch)
    acc = _prepare_articles(client, auth)
    state.set_articles(acc["id"], [_mk("A", title_keep=True)])
    assert _run_content(client, auth, acc["id"]).status == "done"

    row = state.get_articles(acc["id"])[0]
    assert row["body_fail_count"] == 1
    assert "正文过短" in row["body_error"]
    assert not row.get("body_give_up"), "第一次失败不该放弃"
    # ⚠️ 绝不能顺手写判定 —— 那正是被修掉的「网络抖动 = 永久丢弃」
    assert row.get("content_keep") is None
    assert row.get("keep") is None


def test_body_failure_gives_up_after_three_tries(client, auth, monkeypatch):
    """连续 BODY_GIVE_UP_AFTER 次之后标记放弃，并且**不再自动重试**。"""
    from mp_harvest.server import state

    _failing_fetch(monkeypatch)
    acc = _prepare_articles(client, auth)
    state.set_articles(acc["id"], [_mk("A", title_keep=True)])
    for _ in range(state.BODY_GIVE_UP_AFTER):
        _run_content(client, auth, acc["id"])

    row = state.get_articles(acc["id"])[0]
    assert row["body_fail_count"] == state.BODY_GIVE_UP_AFTER
    assert row["body_give_up"] is True

    # 再跑一次：不该再抓（次数不再增长）
    before = state.get_articles(acc["id"])[0]["body_fail_count"]
    task = _run_content(client, auth, acc["id"])
    assert task.result["fetch_failed"] == 0, "已放弃的不该再被重试"
    assert state.get_articles(acc["id"])[0]["body_fail_count"] == before


def test_manual_selection_forces_retry_of_given_up(client, auth, monkeypatch):
    """勾选重跑 = 明确要求再试一次，放弃标记不该挡住它（唯一的重试入口）。"""
    from mp_harvest.server import state

    _failing_fetch(monkeypatch)
    acc = _prepare_articles(client, auth)
    state.set_articles(
        acc["id"], [_mk("A", title_keep=True, body_give_up=True, body_fail_count=9)]
    )
    rows = client.get("/api/articles", params={**auth, "account_id": acc["id"]}).json()
    task = _run_content(client, auth, acc["id"], ids=[rows[0]["id"]])
    assert task.result["fetch_failed"] == 1, "手动指定就该重试"
    assert state.get_articles(acc["id"])[0]["body_fail_count"] == 10


def test_body_state_is_exposed_to_the_list(client, auth, monkeypatch):
    """列表要能看到「为什么它还在待筛选」—— 这是用户要求的可见性。"""
    from mp_harvest.server import state

    _failing_fetch(monkeypatch)
    acc = _prepare_articles(client, auth)
    state.set_articles(acc["id"], [_mk("A", title_keep=True)])
    _run_content(client, auth, acc["id"])

    row = client.get("/api/articles", params={**auth, "account_id": acc["id"]}).json()[0]
    assert row["body_fail_count"] == 1
    assert "正文过短" in row["body_error"]
    assert row["body_give_up"] is False


def test_body_failure_broadcast_does_not_fake_a_verdict(client, auth, monkeypatch):
    """失败时的**实时推送**也不能带上判定 —— 前端会当场把这篇显示成「过滤」。

    那条推送载荷只是 ``dict(art)`` 的副本、不会写回缓存，所以它错得「不持久」，
    刷新一下又变回待判。这种「显示一会儿红、刷新又好了」最难被当成 bug 报上来，
    可它确实是错的：正文没拿到 ≠ 判定为不相关。
    """
    from mp_harvest.server import state
    from mp_harvest.server.routes import ai as ai_routes

    _failing_fetch(monkeypatch)
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        ai_routes, "broadcast_event", lambda t, p=None: events.append((t, p))
    )
    acc = _prepare_articles(client, auth)
    state.set_articles(acc["id"], [_mk("A", title_keep=True)])
    assert _run_content(client, auth, acc["id"]).status == "done"

    pushes = [p for t, p in events if t == "ai.batch"]
    assert pushes, "失败也该推一条（前端要即时看到「正文没拿到」）"
    art = pushes[-1]["articles"][0]
    assert art["content_verdict"] is None, f"失败被推成了判定：{art}"
    assert art["verdict"] != "drop", f"失败被推成了「过滤」：{art}"
    assert "正文" in str(art["content_reason"]), "要推原因，不能空着"
