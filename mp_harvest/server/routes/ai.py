"""AI 筛选 + 模型配置 + 筛选原则（设计稿 §7.1）。

对应旧模块：ai_filter。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from mp_harvest.infra.platform import paths
from mp_harvest.server import state
from mp_harvest.server.schemas import AiFilterIn, AiModelIn, ModelFetchIn, PrinciplesIn
from mp_harvest.server.tasks import Task, registry
from mp_harvest.server.ws import broadcast_event

router = APIRouter(tags=["ai"])


def _partial_verdict(row: dict) -> dict:
    """core 判定行 → 前端可合并的最小 Article 片断（只含 id/verdict/reason）。"""
    keep = row.get("keep")
    return {
        "id": str(row.get("identity") or row.get("link") or ""),
        "verdict": "keep" if keep is True else ("drop" if keep is False else None),
        "reason": str(row.get("reason") or ""),
    }


def _models_path():
    return paths.data_dir() / "ai_models.json"


def _principles_path():
    return paths.data_dir() / "ai_principles.txt"


def _cache_path():
    return paths.data_dir() / "ai_filter_cache.json"


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
    return {"ok": bool(ok), "message": str(message)}


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
    return {"ok": True}
