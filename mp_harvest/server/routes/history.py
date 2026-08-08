"""历史拉取 + 文章列表 + 补录（设计稿 §7.1）。

对应旧模块：history_client、sightings。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from mp_harvest.server import mappers, state
from mp_harvest.server.schemas import HistoryFetchBatchIn, HistoryFetchIn, SupplementIn
from mp_harvest.server.tasks import Task, TaskCancelled, registry
from mp_harvest.server.ws import broadcast_event

router = APIRouter(tags=["history"])


def _get_account_or_404(account_id: str) -> dict[str, Any]:
    account = state.get_store().get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


def _fetch_one_account(
    account: dict[str, Any],
    *,
    days: int,
    task: Task,
    on_progress,
) -> dict[str, Any]:
    """拉取单个公众号历史并写缓存/自动改名；返回该账号结果。"""
    from mp_harvest.core import history_client
    from mp_harvest.core import store as store_mod

    account_id = str(account.get("id") or "")
    name = str(account.get("name") or "")
    cred = account.get("credentials") or {}
    biz = str(account.get("biz") or cred.get("__biz") or "")
    sightings = state.get_sightings().list_for_biz(biz)
    result = history_client.fetch_history_days(
        cred,
        days=days,
        on_progress=on_progress,
        sightings=sightings,
    )
    task.check_cancelled()
    articles = list(result.get("articles") or [])
    state.set_articles(account_id, articles, days=days)
    # 2026-08-09：默认「未命名公众号」时，用 getmsg 返回的官方昵称自动覆盖
    nickname = str(result.get("nickname") or "").strip()
    if nickname:
        store = state.get_store()
        account_row = store.get(account_id)
        if account_row and (account_row.get("name") or "").strip() == store_mod.DEFAULT_ACCOUNT_NAME:
            store.rename(account_id, nickname)
            broadcast_event("accounts.changed", {"account_id": account_id})
    return {
        "account_id": account_id,
        "name": name,
        "ok": bool(result.get("ok")),
        "count": len(articles),
        "pages": result.get("pages", 0),
        "warning": result.get("warning") or "",
        "error": result.get("error") or "",
    }


@router.post("/api/history/fetch", status_code=202)
def fetch_history(body: HistoryFetchIn) -> dict:
    """创建拉历史任务，立即返回 task_id（分页边界响应取消，§3.2）。"""
    account = _get_account_or_404(body.account_id)
    if not (account.get("credentials") or {}):
        raise HTTPException(status_code=409, detail="该账号尚无有效凭证，请先抓包")

    def work(task: Task) -> dict:
        def on_progress(msg: str) -> None:
            # 旧版每翻一页回调一次 → 天然的分页边界，在此响应取消
            task.check_cancelled()
            task.update(message=str(msg))

        res = _fetch_one_account(
            account,
            days=body.days,
            task=task,
            on_progress=on_progress,
        )
        return {
            "account_id": res["account_id"],
            "ok": res["ok"],
            "count": res["count"],
            "pages": res["pages"],
            "warning": res["warning"],
            "error": res["error"],
        }

    task = registry.create("history.fetch", work)
    return {"task_id": task.id, "type": task.type}


@router.post("/api/history/fetch-batch", status_code=202)
def fetch_history_batch(body: HistoryFetchBatchIn) -> dict:
    """批量拉取：勾选多个公众号 → 一个聚合任务逐个拉取（2026-08-09 新增）。"""
    store = state.get_store()
    accounts: list[dict[str, Any]] = []
    for account_id in body.account_ids:
        acct = store.get(account_id)
        if acct is None:
            raise HTTPException(status_code=404, detail=f"账号不存在：{account_id}")
        if not (acct.get("credentials") or {}):
            raise HTTPException(
                status_code=409,
                detail=f"账号尚无有效凭证，请先抓包：{acct.get('name') or account_id}",
            )
        accounts.append(acct)

    def work(task: Task) -> dict:
        total = len(accounts)
        results: list[dict[str, Any]] = []
        for i, acct in enumerate(accounts):
            def on_progress(msg: str, acct=acct, i=i) -> None:
                task.check_cancelled()
                task.update(
                    percent=i / total * 100,
                    message=f"正在拉取 {i + 1}/{total}：{acct.get('name') or acct.get('id')}（{msg}）",
                )

            results.append(_fetch_one_account(acct, days=body.days, task=task, on_progress=on_progress))
        task.check_cancelled()
        ok_n = sum(1 for r in results if r["ok"])
        return {
            "days": body.days,
            "total": len(results),
            "ok": ok_n,
            "failed": len(results) - ok_n,
            "results": results,
        }

    task = registry.create("history.fetch_batch", work)
    return {"task_id": task.id, "type": task.type, "total": len(accounts)}


@router.get("/api/articles")
def list_articles(
    account_id: str = "",
    view: str = "all",
    order: str = "desc",
) -> list[dict]:
    """文章列表（裸 Article[]，前端对齐）；account_id 空 = 全部公众号合并。

    view: all/keep/drop；order: desc/asc（按 publish_ts）。跨账号时每行带
    account_name 供前端按名称排序/显示（2026-08-09）。
    """
    if view not in ("all", "keep", "drop"):
        raise HTTPException(status_code=400, detail="view 必须是 all/keep/drop")
    if order not in ("desc", "asc"):
        raise HTTPException(status_code=400, detail="order 必须是 desc/asc")
    store = state.get_store()
    tagged: list[tuple[str, str, dict[str, Any]]] = []
    if account_id:
        _get_account_or_404(account_id)
        acct = store.get(account_id) or {}
        name = str(acct.get("name") or "")
        tagged = [(account_id, name, dict(a)) for a in state.get_articles(account_id)]
    else:
        for acct in store.list_accounts():
            aid = str(acct.get("id") or "")
            name = str(acct.get("name") or "")
            tagged.extend((aid, name, dict(a)) for a in state.get_articles(aid))
    articles = [a for _, _, a in tagged]
    if view == "keep":
        articles = [a for a in articles if a.get("keep") is True]
    elif view == "drop":
        articles = [a for a in articles if a.get("keep") is False]
    tagged = [t for t in tagged if t[2] in articles]
    tagged.sort(
        key=lambda t: int(t[2].get("publish_ts") or 0), reverse=(order == "desc")
    )
    return [
        mappers.article_out(a, account_id=aid, account_name=name)
        for aid, name, a in tagged
    ]


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
