"""文章缓存持久化：拉取/补录/AI 判定落盘，重启后恢复（2026-08-09 修复）。"""

from __future__ import annotations

from mp_harvest.tests.server.conftest import add_account, wait_task


def _seed(client, auth, acc_id: str) -> None:
    from mp_harvest.server import state

    state.set_articles(
        acc_id,
        [
            {"title": "A", "link": "https://x/1", "publish_ts": 2, "identity": "art-0", "keep": True},
            {"title": "B", "link": "https://x/2", "publish_ts": 1, "identity": "art-1"},
        ],
        days=30,
    )


def _clear_memory_articles() -> None:
    """模拟重启后内存文章缓存丢失（账号仍由 accounts.json 持久化）。"""
    from mp_harvest.server import state

    state._articles.clear()
    state._last_days.clear()


def test_articles_survive_restart(client, auth, isolated_data_dir):
    """内存缓存清空后，get_articles / API 从磁盘恢复历史文章。"""
    acc = add_account(client, auth)
    _seed(client, auth, acc["id"])

    _clear_memory_articles()
    from mp_harvest.server import state

    rows = state.get_articles(acc["id"])
    assert len(rows) == 2
    assert state.get_last_days(acc["id"]) == 30

    resp = client.get("/api/articles", params={**auth, "account_id": acc["id"]})
    assert resp.status_code == 200
    assert len(resp.json()) == 2
    assert (isolated_data_dir / "articles_cache" / f"{acc['id']}.json").is_file()


def test_export_html_by_ids_after_restart(client, auth):
    """选中文章导出：重启后仍可从磁盘缓存导出，不报「没有拉取历史文章」。"""
    acc = add_account(client, auth)
    _seed(client, auth, acc["id"])

    _clear_memory_articles()
    resp = client.post(
        "/api/articles/export-html",
        params=auth,
        json={"account_id": acc["id"], "ids": ["art-0"]},
    )
    assert resp.status_code == 202, resp.text
    task = wait_task(resp.json()["task_id"])
    assert task.status == "done"
    assert task.result["ok"] == 1


def test_delete_account_removes_cache(client, auth, isolated_data_dir):
    acc = add_account(client, auth)
    _seed(client, auth, acc["id"])
    from mp_harvest.server import state

    assert state.get_articles(acc["id"])
    resp = client.delete(f"/api/accounts/{acc['id']}", params=auth)
    assert resp.status_code == 200
    assert state.get_articles(acc["id"]) == []
    cache = isolated_data_dir / "articles_cache"
    assert not list(cache.glob(f"{acc['id']}.json")) if cache.exists() else True
