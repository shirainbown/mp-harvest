"""历史拉取 + 文章列表 + 补录（设计稿 §7.1）。

对应旧模块：history_client、sightings。
"""

from __future__ import annotations

import time
from datetime import date, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from mp_harvest.server import mappers, state
from mp_harvest.server.schemas import (
    DeleteArticlesIn,
    HistoryFetchBatchIn,
    HistoryFetchIn,
    SupplementIn,
)
from mp_harvest.server.tasks import Task, TaskCancelled, registry
from mp_harvest.server.ws import broadcast_event

router = APIRouter(tags=["history"])


def _identity_of(row: dict[str, Any]) -> str:
    """导出记录里用的键。

    必须与 ``article_reader`` 写记录时**完全一致**（那里是 ``identity or link``）——
    导出记录存的是 identity，而前端可见的 ``id`` 是 ``{__biz}:{identity}``，
    拿后者去比会一篇都对不上（这是个不报错的静默失配）。
    """
    return str(row.get("identity") or row.get("link") or "")


def _exported_ids() -> set[str]:
    """本地**还留着**导出 HTML 的文章 id；记录库不可用就返回空集（一律显示未导出）。"""
    from mp_harvest.core.export_records import get_records

    try:
        return get_records().exported_article_ids()
    except Exception:  # noqa: BLE001
        return set()


def parse_date_range(start_date: str, end_date: str) -> tuple[int, int]:
    """YYYY-MM-DD → 本地时区闭区间 (start_ts, end_ts)；都为空返回 (0, 0)。

    只给 start 时 end 默认今天；格式非法或 start>end 报 400。
    ai/export 路由也复用本函数（2026-08-23）。
    """
    start_ts = end_ts = 0
    if (start_date or "").strip():
        try:
            d = date.fromisoformat(start_date.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"start_date 格式非法（需 YYYY-MM-DD）：{start_date}")
        start_ts = int(datetime.combine(d, datetime.min.time()).timestamp())
    if (end_date or "").strip():
        try:
            d = date.fromisoformat(end_date.strip())
        except ValueError:
            raise HTTPException(status_code=400, detail=f"end_date 格式非法（需 YYYY-MM-DD）：{end_date}")
        end_ts = int(datetime.combine(d, datetime.max.time()).timestamp())
    if start_ts and not end_ts:
        end_ts = int(datetime.combine(date.today(), datetime.max.time()).timestamp())
    if start_ts and end_ts and start_ts > end_ts:
        raise HTTPException(status_code=400, detail="start_date 不能晚于 end_date")
    return start_ts, end_ts


def apply_time_filter(
    tagged: list[tuple[str, str, dict[str, Any]]],
    *,
    start_ts: int = 0,
    end_ts: int = 0,
    latest_fetch: bool = False,
) -> list[tuple[str, str, dict[str, Any]]]:
    """对 (account_id, name, article) 列表做时间筛选；latest 按各账号自己的 last_fetch_ts。"""
    if not (start_ts or end_ts or latest_fetch):
        return tagged
    out: list[tuple[str, str, dict[str, Any]]] = []
    for aid, name, a in tagged:
        latest_ts = state.get_last_fetch_ts(aid) if latest_fetch else 0
        if state.time_filter([a], start_ts=start_ts, end_ts=end_ts, latest_ts=latest_ts):
            out.append((aid, name, a))
    return out


def _fetch_policy():
    """拉取节奏与容错（设置页可调；默认值见 ``SETTING_DEFAULTS``）。

    集中在这里换算，而不是让 core 自己去读设置 —— core 读不到注入的测试配置，
    而且「哪个键叫什么」属于 server 层的事（与 ``weekly._score_settings`` 同）。
    """
    from mp_harvest.core import history_client as hc
    from mp_harvest.core import settings as settings_mod

    s = settings_mod.load_settings()
    d = hc.DEFAULT_POLICY

    def _int(key: str, fallback: int, lo: int, hi: int) -> int:
        try:
            n = int(s.get(key, fallback))
        except (TypeError, ValueError):
            n = fallback
        return max(lo, min(hi, n))

    return hc.FetchPolicy(
        delay_min=float(_int("fetch.delay_min", int(d.delay_min), 0, 300)),
        delay_max=float(_int("fetch.delay_max", int(d.delay_max), 0, 300)),
        cooldown_every=_int("fetch.cooldown_pages", d.cooldown_every, 0, 1000),
        cooldown_seconds=float(_int("fetch.cooldown_seconds", int(d.cooldown_seconds), 0, 3600)),
        retries=_int("fetch.retries", d.retries, 0, 10),
        max_pages=_int("fetch.max_pages", d.max_pages, 1, 1000),
    )


def _get_account_or_404(account_id: str) -> dict[str, Any]:
    account = state.get_store().get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return account


def _fetch_one_account(
    account: dict[str, Any],
    *,
    days: int,
    start_ts: int = 0,
    end_ts: int = 0,
    task: Task,
    on_progress,
) -> dict[str, Any]:
    """拉取单个公众号历史并合并写缓存/自动改名；返回该账号结果。

    2026-08-23：合并式缓存（state.merge_articles）替代整体覆盖，历史文章与
    AI 判定结果不再丢失；start_ts>0 时按自定义日期范围拉取。
    """
    from mp_harvest.core import history_client
    from mp_harvest.core import store as store_mod

    account_id = str(account.get("id") or "")
    name = str(account.get("name") or "")
    cred = account.get("credentials") or {}
    biz = str(account.get("biz") or cred.get("__biz") or "")
    sightings = state.get_sightings().list_for_biz(biz)
    # 断点拉取（2026-09）：把已入库文章的 identity/link 交给翻页逻辑，
    # 某页窗口内全是老熟人就不再往更旧的页发请求 —— 这是省掉重复请求的大头。
    cached = state.get_articles(account_id)
    known_keys = {str(a.get("identity") or "") for a in cached}
    known_keys |= {str(a.get("link") or "") for a in cached}
    known_keys.discard("")
    # 窗口下界：自定义范围用 start_ts，按天数则换算（与 fetch_history_days 一致）
    win_start = int(start_ts) if start_ts else max(0, int(time.time()) - int(days) * 86400)
    win_end = int(end_ts) if start_ts else 0
    stamped = int(time.time())
    if start_ts:
        result = history_client.fetch_history_range(
            cred,
            start_ts=start_ts,
            end_ts=end_ts,
            policy=_fetch_policy(),
            on_progress=on_progress,
            sightings=sightings,
            known_keys=known_keys,
        )
    else:
        result = history_client.fetch_history_days(
            cred,
            days=days,
            policy=_fetch_policy(),
            on_progress=on_progress,
            sightings=sightings,
            known_keys=known_keys,
        )
    task.check_cancelled()
    articles = list(result.get("articles") or [])
    ok = bool(result.get("ok"))
    # 失败（凭证过期等）时不推进 last_fetch_ts，否则「最近拉取」会被筛空
    merge = state.merge_articles(
        account_id,
        articles,
        fetched_ts=stamped,
        days=days,
        advance_last_fetch=ok,
    )
    # 提前停止时，窗口内那些「没重新请求」的老文章也要补上本次标记，
    # 否则「最近拉取」会只剩最新几页（保持改造前的含义，纯本地不发请求）
    if ok and result.get("stopped_early"):
        state.touch_fetched_in_window(
            account_id, start_ts=win_start, end_ts=win_end, fetched_ts=stamped
        )
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
        "ok": ok,
        "count": len(articles),
        "added": merge["added"],
        "total": merge["total"],
        "pages": result.get("pages", 0),
        "warning": result.get("warning") or "",
        # 语义分开：truncated = 可能没拉完（要提醒）；notice = 好消息（合并了缺口）
        "truncated": bool(result.get("truncated")),
        "notice": str(result.get("notice") or ""),
        "error": result.get("error") or "",
        # 被微信限流（与「凭证过期」不是一回事，应对完全相反）。前端据此把提示
        # 做成**劝阻**语气 —— 用户最不该做的事就是马上再点一次。
        "rate_limited": bool(result.get("rate_limited")),
        # 断点拉取命中：没重复翻页（前端可据此提示「已是最新」而不是让用户困惑页数变少）
        "stopped_early": bool(result.get("stopped_early")),
    }


@router.post("/api/history/fetch", status_code=202)
def fetch_history(body: HistoryFetchIn) -> dict:
    """创建拉历史任务，立即返回 task_id（分页边界响应取消，§3.2）。

    start_date/end_date（YYYY-MM-DD）都提供时按自定义日期范围拉取，优先于 days。
    """
    account = _get_account_or_404(body.account_id)
    if not (account.get("credentials") or {}):
        raise HTTPException(status_code=409, detail="该账号尚无有效凭证，请先抓包")
    start_ts, end_ts = parse_date_range(body.start_date, body.end_date)

    def work(task: Task) -> dict:
        def on_progress(msg: str) -> None:
            # 旧版每翻一页回调一次 → 天然的分页边界，在此响应取消
            task.check_cancelled()
            task.update(message=str(msg))

        res = _fetch_one_account(
            account,
            days=body.days,
            start_ts=start_ts,
            end_ts=end_ts,
            task=task,
            on_progress=on_progress,
        )
        return {
            "account_id": res["account_id"],
            "ok": res["ok"],
            "count": res["count"],
            "added": res["added"],
            "total": res["total"],
            "pages": res["pages"],
            "warning": res["warning"],
            "truncated": res.get("truncated", False),
            "notice": res.get("notice", ""),
            "error": res["error"],
            # 必须透出：前端据此把提示做成**劝阻**语气。漏了这个字段，界面上就
            # 只剩一句普通红色报错，用户的下一个动作就是再点一次 —— 最坏的选择。
            "rate_limited": bool(res.get("rate_limited")),
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

    start_ts, end_ts = parse_date_range(body.start_date, body.end_date)

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

            results.append(
                _fetch_one_account(
                    acct,
                    days=body.days,
                    start_ts=start_ts,
                    end_ts=end_ts,
                    task=task,
                    on_progress=on_progress,
                )
            )
        task.check_cancelled()
        ok_n = sum(1 for r in results if r["ok"])
        return {
            "days": body.days,
            "total": len(results),
            "ok": ok_n,
            "failed": len(results) - ok_n,
            "added": sum(r.get("added", 0) for r in results),
            "results": results,
        }

    task = registry.create("history.fetch_batch", work)
    return {"task_id": task.id, "type": task.type, "total": len(accounts)}


@router.get("/api/articles")
def list_articles(
    account_id: str = "",
    view: str = "all",
    order: str = "desc",
    start_date: str = "",
    end_date: str = "",
    latest_fetch: bool = False,
) -> list[dict]:
    """文章列表（裸 Article[]，前端对齐）；account_id 空 = 全部公众号合并。

    view: all/keep/drop；order: desc/asc（按 publish_ts）。跨账号时每行带
    account_name 供前端按名称排序/显示（2026-08-09）。
    start_date/end_date（YYYY-MM-DD）按发布时间筛选；latest_fetch=true 只看
    最近一次拉取的文章（2026-08-23）。
    """
    if view not in ("all", "keep", "drop"):
        raise HTTPException(status_code=400, detail="view 必须是 all/keep/drop")
    if order not in ("desc", "asc"):
        raise HTTPException(status_code=400, detail="order 必须是 desc/asc")
    start_ts, end_ts = parse_date_range(start_date, end_date)
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
    tagged = apply_time_filter(
        tagged, start_ts=start_ts, end_ts=end_ts, latest_fetch=latest_fetch
    )
    tagged.sort(
        key=lambda t: int(t[2].get("publish_ts") or 0), reverse=(order == "desc")
    )
    # 「已导出」在**读的时候**判定（文件是否还在），不是查记录表里有没有行 ——
    # 用户删掉导出的 HTML 之后，列表就该跟着变（2026-09 用户报的）
    exported = _exported_ids()
    return [
        mappers.article_out(
            a,
            account_id=aid,
            account_name=name,
            exported=_identity_of(a) in exported,
        )
        for aid, name, a in tagged
    ]


@router.post("/api/articles/delete")
def delete_articles(body: DeleteArticlesIn) -> dict:
    """从**本地列表**删掉指定文章（2026-09，用户要求「有些文章我认为可以删掉」）。

    ⚠️ **只动本地缓存**：文章还在微信那边，下次「拉取历史」会重新抓到。
    这是用户选定的语义（另两个选项是「删掉并永不收录」和「两个动作分开」），
    界面上必须把这句话写出来 —— 否则用户删完以为再也不会出现，下次拉取时
    会当成 bug 报。

    ``ids`` 是前端可见的 ``Article.id``（``{__biz}:{identity}``）。**按同一函数
    现算现比**，不去解析字符串 —— identity 本身带冒号（``mid:1|idx:1|sn:x``），
    手工拆前缀迟早拆错。

    用 POST 而不是 DELETE：DELETE 带 body 在不少代理/客户端上会被静默丢掉，
    而这里必须带一串 id。项目里同类动作（``/api/storage/clean``）也是 POST。
    """
    wanted = {str(i) for i in body.ids if str(i).strip()}
    if not wanted:
        return {"ok": True, "removed": 0}

    store = state.get_store()
    if body.account_id:
        _get_account_or_404(body.account_id)
        account_ids = [body.account_id]
    else:
        account_ids = [str(a.get("id") or "") for a in store.list_accounts()]

    removed = 0
    for aid in account_ids:
        if not aid:
            continue
        rows = state.get_articles(aid)
        if not rows:
            continue
        keep = [r for r in rows if mappers.article_public_id(r) not in wanted]
        if len(keep) != len(rows):
            removed += len(rows) - len(keep)
            state.set_articles(aid, keep)   # 落盘由它负责
    return {"ok": True, "removed": removed}


@router.post("/api/articles/supplement", status_code=201)
def supplement_article(body: SupplementIn) -> dict:
    """补录链接（手工目击）；响应为前端 Article 对象本身（裸对象）。"""
    sightings = state.get_sightings()
    row = sightings.upsert({"link": body.url, "title": body.title, "source": "manual"})
    if row is None:
        raise HTTPException(status_code=400, detail="补录失败：链接与标题均为空")
    if body.account_id:
        _get_account_or_404(body.account_id)
        biz = str(row.get("__biz") or "")
        account = state.get_store().get(body.account_id) or {}
        acc_biz = str(account.get("biz") or (account.get("credentials") or {}).get("__biz") or "")
        if not acc_biz or not biz or acc_biz == biz:
            new_row = dict(row)
            # 带上 fetched_ts，补录文章才不会在「最近拉取」筛选
            # （latest_ts = 该账号 last_fetch_ts）下消失
            new_row["fetched_ts"] = int(time.time())
            # 原子追加：读-改-写收进 state 的锁内，避免与后台拉取并发时
            # 把刚拉到的文章覆盖掉（2026-09 修复）
            state.append_article(body.account_id, new_row)
    return mappers.article_out(
        row,
        account_id=body.account_id or "",
        exported=_identity_of(row) in _exported_ids(),
    )
