"""AI 筛选 + 模型配置 + 筛选原则（设计稿 §7.1）。

对应旧模块：ai_filter。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from mp_harvest.infra.platform import paths
from mp_harvest.server import state
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
    return {
        "id": str(row.get("identity") or row.get("link") or ""),
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


@router.post("/api/ai/filter", status_code=202)
def ai_filter(body: AiFilterIn) -> dict:
    """AI 筛选 → task_id；判定结果合并回文章缓存（批次边界响应取消）。"""
    from mp_harvest.core import ai_filter as ai_mod

    account = state.get_store().get(body.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    articles = state.get_articles(body.account_id)
    if not articles:
        raise HTTPException(status_code=400, detail="没有可筛选的文章（请先拉取历史）")

    def work(task: Task) -> dict:
        models = ai_mod.load_models(_models_path())
        principles = ai_mod.load_principles(_principles_path())
        prompt = ai_mod.build_system_prompt(principles)

        def on_progress(done: int, total: int) -> None:
            task.check_cancelled()  # 每个模型批次一个边界
            pct = (done / total * 100.0) if total else 0.0
            task.update(percent=pct, message=f"AI 判定中 {done}/{total}")

        def on_batch(rows: list[dict], err: str | None) -> None:
            """每批完成即合并缓存 + WS 推送，前端实时刷新判定结果。"""
            state.merge_article_verdicts(body.account_id, rows)
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
        state.merge_article_verdicts(body.account_id, judged)
        return {
            "account_id": body.account_id,
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

    account = state.get_store().get(body.account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    articles = state.get_articles(body.account_id)
    if not articles:
        raise HTTPException(status_code=400, detail="没有可筛选的文章（请先拉取历史）")
    kept = [a for a in articles if a.get("title_keep") is True]
    if not kept:
        raise HTTPException(
            status_code=400,
            detail="没有通过标题筛选的文章（请先执行 AI 标题筛选）",
        )
    cred = account.get("credentials") or {}

    def work(task: Task) -> dict:
        models = ai_mod.load_models(_models_path())
        principles = ai_mod.load_content_principles(_content_principles_path())
        prompt = ai_mod.build_system_prompt(principles)

        # 1) 逐篇获取正文：已缓存 body_text 的复用，没有的现拉
        fetch_failed = 0
        fetch_errors: list[str] = []
        to_fetch = [
            a for a in kept if not str(a.get("body_text") or "").strip()
        ]
        total_fetch = len(to_fetch)
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
                row["content_keep"] = False
                row["content_reason"] = "无链接，无法获取正文，按丢弃处理"
                fetch_failed += 1
                fetch_errors.append(f"{art.get('title', '')}: 无链接")
                state.merge_article_verdicts(body.account_id, [row])
                broadcast_event(
                    "ai.batch",
                    {
                        "account_id": body.account_id,
                        "articles": [_partial_verdict(row)],
                        "stage": "content",
                    },
                )
                continue
            try:
                parsed = article_reader.fetch_and_parse_article(link, cred=cred)
            except Exception as exc:  # noqa: BLE001
                row["content_keep"] = False
                row["content_reason"] = f"正文获取失败，按丢弃处理：{exc}"
                fetch_failed += 1
                fetch_errors.append(f"{art.get('title', '')}: {exc}")
                state.merge_article_verdicts(body.account_id, [row])
                broadcast_event(
                    "ai.batch",
                    {
                        "account_id": body.account_id,
                        "articles": [_partial_verdict(row)],
                        "stage": "content",
                    },
                )
                continue
            body_text = str(parsed.get("body_text") or "").strip()
            if len(body_text) < 20:
                row["content_keep"] = False
                row["content_reason"] = "正文过短或无实质内容，按丢弃处理"
                fetch_failed += 1
                fetch_errors.append(f"{art.get('title', '')}: 正文过短")
                state.merge_article_verdicts(body.account_id, [row])
                broadcast_event(
                    "ai.batch",
                    {
                        "account_id": body.account_id,
                        "articles": [_partial_verdict(row)],
                        "stage": "content",
                    },
                )
                continue
            art["body_text"] = body_text
            if parsed.get("body_html"):
                art["body_html"] = str(parsed["body_html"])
        if to_fetch:
            state.merge_article_bodies(
                body.account_id,
                [a for a in to_fetch if str(a.get("body_text") or "").strip()],
            )

        # 2) 内容判定
        to_judge = [
            a for a in kept if str(a.get("body_text") or "").strip() and a.get("keep") is not False
        ]
        if not to_judge:
            task.update(percent=100.0, message="内容筛选完成")
            return {
                "account_id": body.account_id,
                "kept": 0,
                "dropped": len(kept),
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
            state.merge_article_verdicts(body.account_id, rows)
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
        state.merge_article_verdicts(body.account_id, judged)
        return {
            "account_id": body.account_id,
            "kept": len(result.get("kept") or []),
            "dropped": len(result.get("dropped") or []) + fetch_failed,
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

    cfg = ai_mod.ModelConfig.from_dict(body.model_dump())
    ok, message = ai_mod.test_connection(cfg)
    # error 与 message 同内容：前端测试结果直接读 error，保留 message 兼容旧客户端
    return {"ok": bool(ok), "message": str(message), "error": str(message)}


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

    ai_mod.save_principles(_principles_path(), body.text)
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

    ai_mod.save_content_principles(_content_principles_path(), body.text)
    _invalidate_cache(_content_cache_path())
    return {"ok": True}
