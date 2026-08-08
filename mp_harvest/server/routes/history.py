"""历史拉取 + 文章列表 + 补录（设计稿 §7.1）。

对应旧模块：history_client、sightings。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from mp_harvest.server import mappers, state
from mp_harvest.server.schemas import HistoryFetchIn, SupplementIn
from mp_harvest.server.tasks import Task, TaskCancelled, registry

router = APIRouter(tags=["history"])


def _get_account_or_404(account_id: str) -> dict[str, Any]:
    account = state.get_store().get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


@router.post("/api/history/fetch", status_code=202)
def fetch_history(body: HistoryFetchIn) -> dict:
    """创建拉历史任务，立即返回 task_id（分页边界响应取消，§3.2）。"""
    from mp_harvest.core import history_client

    account = _get_account_or_404(body.account_id)
    cred = account.get("credentials") or {}
    if not cred:
        raise HTTPException(status_code=409, detail="该账号尚无有效凭证，请先抓包")
    biz = str(account.get("biz") or cred.get("__biz") or "")

    def work(task: Task) -> dict:
        def on_progress(msg: str) -> None:
            # 旧版每翻一页回调一次 → 天然的分页边界，在此响应取消
            task.check_cancelled()
            task.update(message=str(msg))

        sightings = state.get_sightings().list_for_biz(biz)
        result = history_client.fetch_history_days(
            cred,
            days=body.days,
            on_progress=on_progress,
            sightings=sightings,
        )
        task.check_cancelled()
        articles = list(result.get("articles") or [])
        state.set_articles(body.account_id, articles, days=body.days)
        return {
            "account_id": body.account_id,
            "ok": bool(result.get("ok")),
            "count": len(articles),
            "pages": result.get("pages", 0),
            "warning": result.get("warning") or "",
            "error": result.get("error") or "",
        }

    task = registry.create("history.fetch", work)
    return {"task_id": task.id, "type": task.type}


@router.get("/api/articles")
def list_articles(
    account_id: str,
    view: str = "all",
    order: str = "desc",
) -> list[dict]:
    """文章列表（裸 Article[]，前端对齐）；view: all/keep/drop；order: desc/asc（按 publish_ts）。"""
    if view not in ("all", "keep", "drop"):
        raise HTTPException(status_code=400, detail="view 必须是 all/keep/drop")
    if order not in ("desc", "asc"):
        raise HTTPException(status_code=400, detail="order 必须是 desc/asc")
    _get_account_or_404(account_id)
    articles = state.get_articles(account_id)
    if view == "keep":
        articles = [a for a in articles if a.get("keep") is True]
    elif view == "drop":
        articles = [a for a in articles if a.get("keep") is False]
    articles.sort(
        key=lambda a: int(a.get("publish_ts") or 0), reverse=(order == "desc")
    )
    return [mappers.article_out(a, account_id=account_id) for a in articles]


@router.post("/api/articles/supplement", status_code=201)
def supplement_article(body: SupplementIn) -> dict:
    """补录链接（手工目击）；响应为前端 Article 对象本身（裸对象）。"""
    sightings = state.get_sightings()
    row = sightings.upsert({"link": body.url, "title": body.title, "source": "manual"})
    if row is None:
        raise HTTPException(status_code=400, detail="补录失败：链接与标题均为空")
    if body.account_id:
        _get_account_or_404(body.account_id)
        cached = state.get_articles(body.account_id)
        if all(str(a.get("identity")) != str(row.get("identity")) for a in cached):
            biz = str(row.get("__biz") or "")
            account = state.get_store().get(body.account_id) or {}
            acc_biz = str(account.get("biz") or (account.get("credentials") or {}).get("__biz") or "")
            if not acc_biz or not biz or acc_biz == biz:
                cached.append(dict(row))
                state.set_articles(body.account_id, cached)
    return mappers.article_out(row, account_id=body.account_id or "")
