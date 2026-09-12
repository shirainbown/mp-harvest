"""AI 筛选 + 模型配置 + 筛选原则（设计稿 §7.1）。

对应旧模块：ai_filter。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException

from mp_harvest.infra.platform import paths
from mp_harvest.server import mappers, state
from mp_harvest.server.schemas import (
    AiContentFilterIn,
    AiFilterIn,
    AiModelIn,
    ModelFetchIn,
    PrinciplesIn,
)
from mp_harvest.server.tasks import Task, registry
from mp_harvest.server.ws import broadcast_event

router = APIRouter(tags=["ai"])


def _verdict_of(keep: object) -> str | None:
    return "keep" if keep is True else ("drop" if keep is False else None)


def _partial_verdict(row: dict) -> dict:
    """core 判定行 → 前端可合并的 Article 判定片断（含两阶段字段）。"""
    title_keep = row.get("title_keep")
    content_keep = row.get("content_keep")
    final_keep = content_keep if content_keep is not None else title_keep
    if final_keep is None:
        final_keep = row.get("keep")
    reason = str(
        row.get("content_reason")
        or row.get("title_reason")
        or row.get("reason")
        or ""
    )
    from mp_harvest.server.mappers import article_public_id

    return {
        # 与 GET /api/articles 的 id 保持一致（含 __biz），否则前端按 id 合并不上
        "id": article_public_id(row.get("_source") or row),
        "verdict": _verdict_of(final_keep),
        "reason": reason,
        "title_verdict": _verdict_of(title_keep),
        "title_reason": str(row.get("title_reason") or ""),
        "content_verdict": _verdict_of(content_keep),
        "content_reason": str(row.get("content_reason") or ""),
    }


def _models_path():
    return paths.data_dir() / "ai_models.json"


def _principles_path():
    return paths.data_dir() / "ai_principles.txt"


def _cache_path():
    return paths.data_dir() / "ai_filter_cache.json"


def _content_principles_path():
    return paths.data_dir() / "ai_content_principles.txt"


def _content_cache_path():
    return paths.data_dir() / "ai_content_filter_cache.json"


def _invalidate_cache(path: str | Path) -> None:
    """删除 AI 判定缓存（原则变更后强制重新判定，避免旧 prompt 结果被复用）。"""
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
    except Exception:
        pass


def _no_articles_detail(ids: list[str] | None, account_id: str) -> str:
    """「一篇都没有」时到底为什么 —— 两种情况用户要做的事完全不同。

    勾选筛选的情形最容易被误解成「按钮没反应」：选中的文章可能刚被删掉、
    或者已经被同一阶段判过（前端列表里还在，但缓存里已不是待筛选状态）。
    """
    if ids:
        return "选中的文章已经不在待筛选范围里（可能已删除或刚被筛选过），刷新列表后再试"
    if account_id:
        return "这个公众号没有可筛选的文章（请先拉取历史）"
    return "没有可筛选的文章（请先拉取历史）"


def _articles_for(
    account_id: str,
    *,
    start_ts: int = 0,
    end_ts: int = 0,
    latest_fetch: bool = False,
    ids: list[str] | None = None,
) -> list[dict]:
    """取待筛选文章，每篇带 ``_account_id``；account_id 为空 = 全部公众号。

    start_ts/end_ts 按发布时间筛选；latest_fetch=True 只取各账号最近一次
    拉取的文章（2026-08-23）。

    ``ids`` 非空时只保留这些文章（前端 ``Article.id`` = ``{__biz}:{identity}``）
    —— 「只筛选中」用它。**按同一函数现算现比**，不去拆 id 里的冒号
    （identity 自己就带冒号，手工拆前缀迟早拆错，与 ``delete_articles`` 同）。
    """
    if account_id:
        if state.get_store().get(account_id) is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        rows = state.get_articles(account_id)
        latest_ts = state.get_last_fetch_ts(account_id) if latest_fetch else 0
        rows = state.time_filter(rows, start_ts=start_ts, end_ts=end_ts, latest_ts=latest_ts)
        out = [dict(a, _account_id=account_id) for a in rows]
    else:
        out = []
        for acct in state.get_store().list_accounts():
            aid = str(acct.get("id") or "")
            if not aid:
                continue
            acct_rows = state.get_articles(aid)
            latest_ts = state.get_last_fetch_ts(aid) if latest_fetch else 0
            acct_rows = state.time_filter(
                acct_rows, start_ts=start_ts, end_ts=end_ts, latest_ts=latest_ts
            )
            out.extend(dict(a, _account_id=aid) for a in acct_rows)

    wanted = {str(i).strip() for i in (ids or []) if str(i).strip()}
    if wanted:
        out = [a for a in out if mappers.article_public_id(a) in wanted]
    return out


def _merge_verdicts(account_id: str, rows: list[dict]) -> None:
    if account_id:
        state.merge_article_verdicts(account_id, rows)
    else:
        state.merge_article_verdicts_by_account(rows)


def _merge_bodies(account_id: str, rows: list[dict]) -> None:
    if account_id:
        state.merge_article_bodies(account_id, rows)
    else:
        state.merge_article_bodies_by_account(rows)


@router.post("/api/ai/filter", status_code=202)
def ai_filter(body: AiFilterIn) -> dict:
    """AI 筛选 → task_id；判定结果合并回文章缓存（批次边界响应取消）。"""
    from mp_harvest.core import ai_filter as ai_mod

    from mp_harvest.server.routes.history import parse_date_range

    start_ts, end_ts = parse_date_range(body.start_date, body.end_date)
    articles = _articles_for(
        body.account_id,
        start_ts=start_ts,
        end_ts=end_ts,
        latest_fetch=body.latest_fetch,
        ids=body.ids,
    )
    if not articles:
        raise HTTPException(status_code=400, detail=_no_articles_detail(body.ids, body.account_id))

    def work(task: Task) -> dict:
        models = ai_mod.load_models(_models_path())
        principles = ai_mod.load_principles(_principles_path())
        prompt = ai_mod.build_system_prompt(principles)
        task.update(percent=0.0, message=f"AI 判定中 0/{len(articles)}")

        def on_progress(done: int, total: int) -> None:
            task.check_cancelled()  # 每个模型批次一个边界
            pct = (done / total * 100.0) if total else 0.0
            task.update(percent=pct, message=f"AI 判定中 {done}/{total}")

        def on_batch(rows: list[dict], err: str | None) -> None:
            """每批完成即合并缓存 + WS 推送，前端实时刷新判定结果。"""
            _merge_verdicts(body.account_id, rows)
            broadcast_event(
                "ai.batch",
                {
                    "account_id": body.account_id,
                    "articles": [_partial_verdict(r) for r in rows],
                },
            )

        result = ai_mod.judge_articles(
            articles,
            models,
            prompt=prompt,
            cache_path=_cache_path(),
            batch_size=body.batch_size if body.batch_size is not None else 50,
            workers=body.workers if body.workers is not None else 4,
            prefix="title_",
            on_progress=on_progress,
            on_batch=on_batch,
        )
        task.check_cancelled()
        judged = list(result.get("kept") or []) + list(result.get("dropped") or [])
        _merge_verdicts(body.account_id, judged)
        return {
            "account_id": body.account_id,
            "ok": bool(result.get("ok")),
            "kept": len(result.get("kept") or []),
            "dropped": len(result.get("dropped") or []),
            "cached": result.get("cached", 0),
            "judged": result.get("judged", 0),
            "errors": result.get("errors") or [],
            "used_models": result.get("used_models") or [],
        }

    task = registry.create("ai.filter", work)
    return {"task_id": task.id, "type": task.type, "total": len(articles)}


@router.post("/api/ai/filter-content", status_code=202)
def ai_filter_content(body: AiContentFilterIn) -> dict:
    """内容筛选（第二阶段）→ task_id；仅对标题筛选 keep=True 的文章拉正文并判定。

    流程：逐篇拉取正文（失败/过短按 drop 兜底）→ 内容缓存命中直接复用 →
    AI 分批判定内容并合并回文章缓存。
    """
    from mp_harvest.core import ai_filter as ai_mod
    from mp_harvest.core import article_reader

    from mp_harvest.server.routes.history import parse_date_range

    start_ts, end_ts = parse_date_range(body.start_date, body.end_date)
    articles = _articles_for(
        body.account_id,
        start_ts=start_ts,
        end_ts=end_ts,
        latest_fetch=body.latest_fetch,
        ids=body.ids,
    )
    if not articles:
        raise HTTPException(status_code=400, detail=_no_articles_detail(body.ids, body.account_id))
    kept = [a for a in articles if a.get("title_keep") is True]
    if not kept:
        raise HTTPException(
            status_code=400,
            detail=(
                "选中的文章里没有通过标题筛选的 —— 内容筛选只处理标题阶段通过的文章"
                if body.ids
                else "没有通过标题筛选的文章（请先执行 AI 标题筛选）"
            ),
        )
    cred_by_account: dict[str, dict] = {}
    for a in kept:
        aid = str(a.get("_account_id") or "")
        if aid and aid not in cred_by_account:
            acct = state.get_store().get(aid) or {}
            cred_by_account[aid] = acct.get("credentials") or {}

    def work(task: Task) -> dict:
        models = ai_mod.load_models(_models_path())
        principles = ai_mod.load_content_principles(_content_principles_path())
        prompt = ai_mod.build_system_prompt(principles)
        task.update(percent=0.0, message="内容筛选准备中…")

        # 1) 逐篇获取正文：已缓存 body_text 的复用，没有的现拉
        fetch_failed = 0
        fetch_errors: list[str] = []
        to_fetch = [
            a for a in kept if not str(a.get("body_text") or "").strip()
        ]
        total_fetch = len(to_fetch)

        def _fetch_failed_keep_pending(art: dict, row: dict, why: str) -> None:
            """正文拿不到：只播报，**不写判定**（2026-09 修复）。

            原先写 ``content_keep=False`` 并合并回文章缓存，而内容筛选的候选又要求
            ``keep is not False`` —— 一次网络抖动就把文章永久钉成「丢弃」，
            下轮即使正文抓成功也不会再判它，理由还停在「正文获取失败」。
            现在改为保持待筛选，下次运行会重试。
            """
            nonlocal fetch_failed
            fetch_failed += 1
            row["content_reason"] = f"{why}（保留在「待内容筛选」，可重新运行）"
            fetch_errors.append(f"{art.get('title', '')}: {why}")
            broadcast_event(
                "ai.batch",
                {
                    "account_id": body.account_id,
                    "articles": [_partial_verdict(row)],
                    "stage": "content",
                },
            )

        # 正文落盘放到 finally（2026-09 断点拉取）：原先只在循环正常跑完后统一
        # merge，中途取消 / 出错就丢掉本轮**已经拉回来的每一篇正文**，下次全部
        # 重拉一遍 —— 既浪费又白挨一次限流风险。这里保证已拿到的先落盘。
        try:
            for i, art in enumerate(to_fetch, start=1):
                task.check_cancelled()
                task.update(
                    percent=((i - 1) / total_fetch * 45.0) if total_fetch else 0.0,
                    message=f"获取正文 {i}/{total_fetch}",
                )
                link = str(art.get("link") or "").strip()
                row = dict(art)
                row.pop("body_text", None)
                row.pop("body_html", None)
                if not link:
                    _fetch_failed_keep_pending(art, row, "无链接，无法获取正文")
                    continue
                cred = cred_by_account.get(str(art.get("_account_id") or ""), {})
                try:
                    parsed = article_reader.fetch_and_parse_article(link, cred=cred)
                except Exception as exc:  # noqa: BLE001
                    _fetch_failed_keep_pending(art, row, f"正文获取失败：{exc}")
                    continue
                if not parsed.get("content_found", True):
                    # 页面没有 #js_content：通常是环境校验页，拿它去判定毫无意义
                    _fetch_failed_keep_pending(art, row, "页面没有正文（可能触发了微信的环境校验）")
                    continue
                body_text = str(parsed.get("body_text") or "").strip()
                if len(body_text) < 20:
                    _fetch_failed_keep_pending(art, row, "正文过短或无实质内容")
                    continue
                art["body_text"] = body_text
                if parsed.get("body_html"):
                    art["body_html"] = str(parsed["body_html"])
        finally:
            if to_fetch:
                _merge_bodies(
                    body.account_id,
                    [a for a in to_fetch if str(a.get("body_text") or "").strip()],
                )

        # 2) 内容判定。
        # 不再排除 `keep is False`：内容筛完后 merge_article_verdicts 会把
        # keep 写成 content_keep，于是被内容筛掉的篇目 keep=False —— 排除它们
        # 就等于「改了原则重跑也永远翻不了案」（2026-09 修复）。
        # 只跳过已经判过的（content_keep 有值且缓存命中时本就由 judge_articles 处理）。
        to_judge = [a for a in kept if str(a.get("body_text") or "").strip()]
        if not to_judge:
            task.update(percent=100.0, message="内容筛选完成")
            return {
                "account_id": body.account_id,
                "ok": fetch_failed == 0,
                "kept": 0,
                "dropped": 0,
                "cached": 0,
                "judged": 0,
                "fetched": 0,
                "fetch_failed": fetch_failed,
                "errors": fetch_errors,
                "used_models": [],
            }

        def on_progress(done: int, total: int) -> None:
            task.check_cancelled()
            pct = 45.0 + (done / total * 55.0) if total else 100.0
            task.update(percent=pct, message=f"内容判定中 {done}/{total}")

        def on_batch(rows: list[dict], err: str | None) -> None:
            _merge_verdicts(body.account_id, rows)
            broadcast_event(
                "ai.batch",
                {
                    "account_id": body.account_id,
                    "articles": [_partial_verdict(r) for r in rows],
                    "stage": "content",
                },
            )

        result = ai_mod.judge_articles(
            to_judge,
            models,
            prompt=prompt,
            cache_path=_content_cache_path(),
            batch_size=body.batch_size if body.batch_size is not None else 30,
            workers=body.workers if body.workers is not None else 4,
            content_field="body_text",
            max_content_chars=6000,
            prefix="content_",
            on_progress=on_progress,
            on_batch=on_batch,
        )
        task.check_cancelled()
        judged = list(result.get("kept") or []) + list(result.get("dropped") or [])
        _merge_verdicts(body.account_id, judged)
        return {
            "account_id": body.account_id,
            "ok": bool(result.get("ok")),
            "kept": len(result.get("kept") or []),
            # 正文抓取失败的不再计入 dropped（它们没被判丢弃，仍在待筛选）
            "dropped": len(result.get("dropped") or []),
            "cached": result.get("cached", 0),
            "judged": result.get("judged", 0),
            "fetched": len(to_judge),
            "fetch_failed": fetch_failed,
            "errors": list(result.get("errors") or []) + fetch_errors,
            "used_models": result.get("used_models") or [],
        }

    task = registry.create("ai.filter_content", work)
    return {"task_id": task.id, "type": task.type, "total": len(kept)}


@router.get("/api/ai/models")
def get_models() -> dict:
    from mp_harvest.core import ai_filter as ai_mod

    models = ai_mod.load_models(_models_path())
    return {"models": [m.to_dict() if hasattr(m, "to_dict") else m for m in models]}


@router.put("/api/ai/models")
def put_models(body: list[AiModelIn]) -> dict:
    from mp_harvest.core import ai_filter as ai_mod

    models = [ai_mod.ModelConfig.from_dict(m.model_dump()) for m in body]
    ai_mod.save_models(_models_path(), models)
    return {"ok": True, "count": len(models)}


@router.post("/api/ai/models/test")
def test_model(body: AiModelIn) -> dict:
    from mp_harvest.core import ai_filter as ai_mod

    import time as _time

    cfg = ai_mod.ModelConfig.from_dict(body.model_dump())
    t0 = _time.perf_counter()
    ok, message = ai_mod.test_connection(cfg)
    # 前端会渲染「● 可用 · <latency_ms>ms」，路由此前不返回该字段 → 一直显示
    # 「● 可用 · ms」（2026-09 修复）
    latency_ms = int((_time.perf_counter() - t0) * 1000)
    # error 与 message 同内容：前端测试结果直接读 error，保留 message 兼容旧客户端
    return {
        "ok": bool(ok),
        "latency_ms": latency_ms,
        "message": str(message),
        "error": str(message),
    }


@router.post("/api/ai/models/fetch")
def fetch_models(body: ModelFetchIn) -> dict:
    """按 base_url + api_key 拉取 OpenAI 兼容 /models 列表，供前端下拉选择。"""
    from mp_harvest.core import ai_filter as ai_mod

    cfg = ai_mod.ModelConfig.from_dict(body.model_dump())
    ok, result = ai_mod.fetch_models(cfg)
    if not ok:
        return {"ok": False, "message": str(result), "models": []}
    return {"ok": True, "models": [str(m) for m in result], "message": ""}


@router.get("/api/ai/principles")
def get_principles() -> dict:
    """筛选原则：text 为当前生效文本，default 为内置默认原则（前端「恢复默认」）。"""
    from mp_harvest.core import ai_filter as ai_mod

    return {
        "text": ai_mod.load_principles(_principles_path()),
        "default": str(getattr(ai_mod, "DEFAULT_PRINCIPLES", "") or ""),
    }


@router.put("/api/ai/principles")
def put_principles(body: PrinciplesIn) -> dict:
    from mp_harvest.core import ai_filter as ai_mod

    # 只在**内容真的变了**才清判定缓存（2026-09）。
    # 判定结果以「当时用的原则」为前提，原则没变则判定依然有效；原先无条件清，
    # 于是用户点一下「保存」什么都没改，也会把攒了很久的判定结果全部丢掉。
    # 比较用生效值（空文本会被存成默认原则，否则会误判成「变了」）。
    old = ai_mod.load_principles(_principles_path())
    new = body.text or ai_mod.DEFAULT_PRINCIPLES
    ai_mod.save_principles(_principles_path(), body.text)
    if old != new:
        _invalidate_cache(_cache_path())
    return {"ok": True}


@router.get("/api/ai/content-principles")
def get_content_principles() -> dict:
    """内容筛选原则：text 为当前生效文本，default 为内置默认（前端「恢复默认」）。"""
    from mp_harvest.core import ai_filter as ai_mod

    return {
        "text": ai_mod.load_content_principles(_content_principles_path()),
        "default": str(getattr(ai_mod, "DEFAULT_CONTENT_PRINCIPLES", "") or ""),
    }


@router.put("/api/ai/content-principles")
def put_content_principles(body: PrinciplesIn) -> dict:
    from mp_harvest.core import ai_filter as ai_mod

    # 同 put_principles：内容没变就不清缓存
    old = ai_mod.load_content_principles(_content_principles_path())
    new = body.text or ai_mod.DEFAULT_CONTENT_PRINCIPLES
    ai_mod.save_content_principles(_content_principles_path(), body.text)
    if old != new:
        _invalidate_cache(_content_cache_path())
    return {"ok": True}
