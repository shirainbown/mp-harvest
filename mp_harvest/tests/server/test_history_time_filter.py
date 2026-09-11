"""自定义日期范围拉取 + 缓存合并 + 时间筛选契约测试（2026-08-23）。

覆盖：
- POST /api/history/fetch 带 start_date/end_date → 走 fetch_history_range
- 合并式缓存：重复拉取不丢历史、保留 AI 判定、added/total 正确
- GET /api/articles 的 start_date/end_date/latest_fetch 筛选
- AI 筛选 / 列表导出同样受时间筛选约束
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta

from mp_harvest.tests.server.conftest import add_account, give_credential, wait_task


def _day(d: date) -> str:
    return d.isoformat()


def test_fetch_with_custom_date_range(client, auth, fake_core):
    acc = add_account(client, auth)
    give_credential(acc["id"])
    start = _day(date.today() - timedelta(days=10))
    end = _day(date.today() - timedelta(days=3))
    resp = client.post(
        "/api/history/fetch",
        params=auth,
        json={"account_id": acc["id"], "start_date": start, "end_date": end},
    )
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    # fake 记录了 start_ts/end_ts：本地时区当天 00:00 / 23:59:59
    call = fake_core.history_client.last_range_call
    assert call["start_ts"] == int(datetime.combine(date.fromisoformat(start), datetime.min.time()).timestamp())
    assert call["end_ts"] == int(datetime.combine(date.fromisoformat(end), datetime.max.time()).timestamp())
    # 任务结果带 added/total
    assert task.result["added"] == 1
    assert task.result["total"] == 1


def test_fetch_start_date_only_defaults_end_today(client, auth, fake_core):
    acc = add_account(client, auth)
    give_credential(acc["id"])
    start = _day(date.today() - timedelta(days=5))
    resp = client.post(
        "/api/history/fetch",
        params=auth,
        json={"account_id": acc["id"], "start_date": start},
    )
    assert resp.status_code == 202, resp.text
    wait_task(resp.json()["task_id"])
    call = fake_core.history_client.last_range_call
    today_end = int(datetime.combine(date.today(), datetime.max.time()).timestamp())
    assert abs(call["end_ts"] - today_end) < 2


def test_fetch_invalid_date_range_400(client, auth):
    acc = add_account(client, auth)
    give_credential(acc["id"])
    resp = client.post(
        "/api/history/fetch",
        params=auth,
        json={"account_id": acc["id"], "start_date": "2026-08-10", "end_date": "2026-08-01"},
    )
    assert resp.status_code == 400
    resp = client.post(
        "/api/history/fetch",
        params=auth,
        json={"account_id": acc["id"], "start_date": "08-01"},
    )
    assert resp.status_code == 400


def test_merge_preserves_history_and_verdicts(client, auth):
    """重复拉取：历史不丢、AI 判定保留、added 只计新文章。"""
    from mp_harvest.server import state

    acc = add_account(client, auth)
    give_credential(acc["id"])
    # 预置两篇历史：一篇不在本次范围（i-old），一篇与本次拉取重叠（art-0，已判定）
    state.set_articles(
        acc["id"],
        [
            {"title": "旧文", "link": "https://mp.weixin.qq.com/s/old", "publish_ts": 1600000000,
             "identity": "i-old", "keep": True, "title_keep": True, "title_reason": "好文"},
            {"title": "旧标题", "link": "https://mp.weixin.qq.com/s/x0", "publish_ts": 1699999999,
             "identity": "art-0", "keep": True},
        ],
    )
    resp = client.post("/api/history/fetch", params=auth, json={"account_id": acc["id"], "days": 7})
    assert resp.status_code == 202
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    # fake 返回 art-0/art-1（pages_before_return=2）：art-0 已存在 → added=1，total=3
    assert task.result["added"] == 1
    assert task.result["total"] == 3
    rows = {r["identity"]: r for r in state.get_articles(acc["id"])}
    assert set(rows) == {"i-old", "art-0", "art-1"}  # 历史未丢
    assert rows["art-0"]["keep"] is True  # 判定保留
    assert rows["art-0"]["title"] == "文章0"  # 抓取字段更新为新拉取的值
    assert rows["i-old"]["title_keep"] is True
    # 本次拉取见到的文章刷新 fetched_ts，未见到的历史文章没有
    assert rows["art-0"]["fetched_ts"] > 0
    assert rows["art-1"]["fetched_ts"] > 0
    assert not rows["i-old"].get("fetched_ts")
    # latest_fetch 只见本次拉取的文章
    resp = client.get("/api/articles", params={**auth, "account_id": acc["id"], "latest_fetch": "true"})
    ids = {a["id"] for a in resp.json()}
    # 对外 id 现在带 __biz 前缀（跨账号同一篇文章不再撞车），故按后缀断言
    assert {i.split(":", 1)[-1] for i in ids} == {"art-0", "art-1"}
    assert all(":" in i for i in ids), f"id 应带 __biz 前缀：{ids}"


def test_merge_articles_unit(isolated_data_dir):
    """state.merge_articles：新增/更新/保留判定/last_fetch_ts 落盘恢复。"""
    from mp_harvest.server import state

    state.reset()
    acc = "acc1"
    state.set_articles(
        acc,
        [{"title": "A", "link": "https://x/1", "publish_ts": 100, "identity": "i1",
          "keep": False, "reason": "旧判定", "body_text": "正文"}],
    )
    r1 = state.merge_articles(
        acc,
        [
            {"title": "A-new", "link": "https://x/1", "publish_ts": 100, "identity": "i1"},
            {"title": "B", "link": "https://x/2", "publish_ts": 200, "identity": "i2"},
        ],
        fetched_ts=1111,
    )
    assert r1 == {"added": 1, "total": 2}
    rows = {r["identity"]: r for r in state.get_articles(acc)}
    assert rows["i1"]["title"] == "A-new"  # 抓取字段更新
    assert rows["i1"]["keep"] is False  # 判定保留
    assert rows["i1"]["reason"] == "旧判定"
    assert rows["i1"]["body_text"] == "正文"  # 正文保留
    assert rows["i1"]["fetched_ts"] == 1111
    assert rows["i2"]["fetched_ts"] == 1111
    assert state.get_last_fetch_ts(acc) == 1111
    # 落盘恢复
    state.reset()
    assert state.get_last_fetch_ts(acc) == 1111
    rows = {r["identity"]: r for r in state.get_articles(acc)}
    assert rows["i1"]["keep"] is False and rows["i2"]["title"] == "B"
    state.reset()


def test_articles_time_filter(client, auth):
    """GET /api/articles：start_date/end_date 按发布时间、latest_fetch 按抓取时间筛选。"""
    from mp_harvest.server import state

    acc = add_account(client, auth)
    now = int(time.time())
    state.merge_articles(
        acc["id"],
        [
            {"title": "最新", "link": "https://x/1", "publish_ts": now, "identity": "i1"},
            {"title": "十天前", "link": "https://x/2", "publish_ts": now - 10 * 86400, "identity": "i2"},
        ],
        fetched_ts=now - 86400,  # 上次拉取
    )
    state.merge_articles(
        acc["id"],
        [{"title": "最新", "link": "https://x/1", "publish_ts": now, "identity": "i1"}],
        fetched_ts=now,  # 本次拉取只见到 i1
    )
    # latest_fetch：只剩 i1
    resp = client.get("/api/articles", params={**auth, "account_id": acc["id"], "latest_fetch": "true"})
    assert [a["title"] for a in resp.json()] == ["最新"]
    # 发布时间范围：近 7 天只剩 i1
    start = _day(date.today() - timedelta(days=7))
    resp = client.get(
        "/api/articles", params={**auth, "account_id": acc["id"], "start_date": start}
    )
    assert [a["title"] for a in resp.json()] == ["最新"]
    # 宽范围：两篇都在
    start = _day(date.today() - timedelta(days=30))
    resp = client.get(
        "/api/articles", params={**auth, "account_id": acc["id"], "start_date": start}
    )
    assert len(resp.json()) == 2
    # fetched_at 字段透出
    assert resp.json()[0]["fetched_at"]


def test_ai_filter_respects_latest_fetch(client, auth):
    """AI 标题筛选带 latest_fetch 时只判定最近一次拉取的文章。"""
    from mp_harvest.server import state

    acc = add_account(client, auth)
    now = int(time.time())
    state.merge_articles(
        acc["id"],
        [
            {"title": "新", "link": "https://x/1", "publish_ts": now, "identity": "i1"},
            {"title": "旧", "link": "https://x/2", "publish_ts": now - 86400, "identity": "i2"},
        ],
        fetched_ts=now - 3600,
    )
    state.merge_articles(
        acc["id"],
        [{"title": "新", "link": "https://x/1", "publish_ts": now, "identity": "i1"}],
        fetched_ts=now,
    )
    resp = client.post(
        "/api/ai/filter",
        params=auth,
        json={"account_id": acc["id"], "latest_fetch": True},
    )
    assert resp.status_code == 202, resp.text
    assert resp.json()["total"] == 1  # 只筛选 1 篇
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    rows = {r["identity"]: r for r in state.get_articles(acc["id"])}
    assert rows["i1"].get("title_keep") is True
    assert rows["i2"].get("title_keep") is None  # 旧文章未被判定


def test_export_list_respects_time_filter(client, auth):
    from mp_harvest.server import state

    acc = add_account(client, auth)
    now = int(time.time())
    state.merge_articles(
        acc["id"],
        [
            {"title": "新", "link": "https://x/1", "publish_ts": now, "identity": "i1"},
            {"title": "旧", "link": "https://x/2", "publish_ts": now - 86400, "identity": "i2"},
        ],
        fetched_ts=now - 3600,
    )
    state.merge_articles(
        acc["id"],
        [{"title": "新", "link": "https://x/1", "publish_ts": now, "identity": "i1"}],
        fetched_ts=now,
    )
    resp = client.get(
        "/api/articles/export-list",
        params={**auth, "account_id": acc["id"], "format": "json", "latest_fetch": "true"},
    )
    assert resp.status_code == 200
    assert "N=1" in resp.text  # fake render_export 输出文章数
