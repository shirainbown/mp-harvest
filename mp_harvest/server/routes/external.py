"""「其他来源」目录：登记 / 扫描 / 浏览 / AI 筛选 / 写回（2026-09）。

外部来源是**用户手动登记的一个目录**，其下按 ``YYYY-MM-DD/`` 分日期子目录存放
非公众号的文章（典型是 arXiv 论文流水线产出的 ``papers_data.json``）。
扫描建库后可在应用里浏览/搜索/筛选，也能按同样的格式写回目录。

与微信文章路径的关系：**互不干扰**。``GET /api/articles``、``/api/ai/filter``、
``state.py`` 的文章缓存一律不碰；这里读的是另一套存储（``data/external.db``），
判定则复用 ``core/ai_filter.judge_articles`` 与同一批缓存文件 —— 缓存键带 ``ext:``
前缀，与 ``mid:…``/``s:…`` 等微信身份结构上不可能撞车。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from mp_harvest.core import external_sources as ext
from mp_harvest.infra.platform import paths
from mp_harvest.server.mappers import article_out
from mp_harvest.server.schemas import (
    ExternalExportIn,
    ExternalFilterIn,
    ExternalSourceIn,
    ExternalSourcePatchIn,
)
from mp_harvest.server.tasks import Task, registry

router = APIRouter(tags=["external"])


# ── 路径 ──────────────────────────────────────────────────────────


def _models_path() -> Path:
    return paths.data_dir() / "ai_models.json"


def _principles_path() -> Path:
    return paths.data_dir() / "ai_principles.txt"


def _content_principles_path() -> Path:
    return paths.data_dir() / "ai_content_principles.txt"


def _cache_path() -> Path:
    return paths.data_dir() / "ai_filter_cache.json"


def _content_cache_path() -> Path:
    return paths.data_dir() / "ai_content_filter_cache.json"


# ── 行映射 ────────────────────────────────────────────────────────


def _external_identity(item_key: str) -> str:
    """外部条目的 AI 缓存键。

    带 ``ext:`` 前缀是**必须的**：判定缓存以 identity 为键，微信那边是
    ``mid:…|idx:…|sn:…`` / ``s:…`` / 原始 URL。不隔离的话，某个外部条目的
    URL 万一与某篇文章相同就会互相覆盖判定结果。
    """
    return f"ext:{item_key}"


def _body_text(item: dict[str, Any]) -> str:
    """内容筛选用的正文：优先读本地正文文件，退回中文摘要/英文摘要。

    外部条目的一个天然优势是**不用联网抓正文** —— 正文要么已经在目录里，
    要么摘要本身就是可判定的内容（arXiv 摘要信息量足够）。
    """
    body_path = str(item.get("body_path") or "")
    if body_path:
        try:
            from mp_harvest.core.article_reader import _html_to_text

            text = _html_to_text(Path(body_path).read_text(encoding="utf-8", errors="ignore"))
            if text.strip():
                return text.strip()
        except Exception:  # noqa: BLE001
            pass
    return str(item.get("summary_cn") or item.get("abstract") or "").strip()


def _core_row(item: dict[str, Any], *, verdicts: dict[str, dict], content: dict[str, dict]) -> dict:
    """外部条目 → core 行形状，好让 ``article_out`` / ``judge_articles`` 直接复用。

    ``__biz`` 放 ``source_id``：``article_public_id`` 会算出
    ``<source_id>:ext:<item_key>``。同一篇论文登记在两个目录下时,没有这个前缀
    就会算出同一个 id —— 「全部来源」聚合视图里 ``:key`` 冲突、勾选串号、
    按 ids 写回落到错误的目录（与 2026-09 微信侧 ``__biz`` 那次修复同源）。
    """
    key = _external_identity(str(item.get("item_key") or ""))
    row: dict[str, Any] = {
        "identity": key,
        "__biz": str(item.get("source_id") or ""),
        "title": str(item.get("title") or ""),
        "link": str(item.get("url") or ""),
        "publish_ts": int(item.get("publish_ts") or 0),
        "account": str(item.get("source_name") or ""),
        "source": "external",
        # 内容筛选的输入（judge_articles 的 content_field="body_text"）
        "body_text": _body_text(item),
    }
    # 判定结果从缓存合并进来（外部条目不另存判定，避免两处真相）。
    # 标题缓存是裸字段名，内容缓存读出来就带 content_ 前缀，各自 update 即可。
    row.update(verdicts.get(key) or {})
    row.update(content.get(key) or {})
    return row


def _load_verdicts() -> tuple[dict[str, dict], dict[str, dict]]:
    """读两阶段判定缓存；只读无副作用（见 ``ai_filter.load_verdicts`` 的注释）。"""
    from mp_harvest.core import ai_filter as ai_mod

    try:
        title = ai_mod.load_verdicts(_cache_path(), prefix="")
    except Exception:  # noqa: BLE001
        title = {}
    try:
        content = ai_mod.load_verdicts(_content_cache_path(), prefix="content_")
    except Exception:  # noqa: BLE001
        content = {}
    return title, content


def _item_out(item: dict[str, Any], *, verdicts: dict, content: dict) -> dict[str, Any]:
    """外部条目 → 前端行（与 ``article_out`` 同形 + 外部来源特有字段）。"""
    row = _core_row(item, verdicts=verdicts, content=content)
    out = article_out(
        row,
        account_id=str(item.get("source_id") or ""),
        account_name=str(item.get("source_name") or ""),
    )
    out.update(
        {
            "arxiv_id": str(item.get("arxiv_id") or ""),
            "domain": str(item.get("domain") or ""),
            "primary_category": str(item.get("primary_category") or ""),
            "authors": item.get("authors") or [],
            "categories": item.get("categories") or [],
            "dir_date": str(item.get("dir_date") or ""),
            "pdf_path": str(item.get("pdf_path") or ""),
            "body_path": str(item.get("body_path") or ""),
            "item_key": str(item.get("item_key") or ""),
        }
    )
    return out


# ── 来源目录 CRUD ─────────────────────────────────────────────────


@router.get("/api/external/sources")
def list_sources() -> list[dict]:
    """所有登记的目录（含条目数）。"""
    return ext.get_external_store().list_sources()


@router.post("/api/external/sources", status_code=201)
def add_source(body: ExternalSourceIn) -> dict:
    """登记一个目录；路径不存在 → 400，重复登记 → 返回已有记录（幂等）。"""
    store = ext.get_external_store()
    path = ext.normalize_path(body.path)
    if not path or not Path(path).is_dir():
        raise HTTPException(status_code=400, detail=f"目录不存在或不可读：{body.path}")
    row = store.add_source(path, body.name)
    if row is None:
        raise HTTPException(status_code=400, detail=f"登记失败：{body.path}")
    return row


@router.patch("/api/external/sources/{source_id}")
def patch_source(source_id: str, body: ExternalSourcePatchIn) -> dict:
    """改名 / 启停。"""
    store = ext.get_external_store()
    if store.get_source(source_id) is None:
        raise HTTPException(status_code=404, detail="来源目录未登记")
    row = store.update_source(source_id, name=body.name, enabled=body.enabled)
    if row is None:
        raise HTTPException(status_code=400, detail="更新失败")
    return row


@router.delete("/api/external/sources/{source_id}")
def remove_source(source_id: str) -> dict:
    """**只解除登记**，磁盘上的目录与文件一概不动。"""
    store = ext.get_external_store()
    if store.get_source(source_id) is None:
        raise HTTPException(status_code=404, detail="来源目录未登记")
    return {"ok": store.remove_source(source_id), "id": source_id}


@router.post("/api/external/sources/{source_id}/scan", status_code=202)
def scan_source(source_id: str) -> dict:
    """扫描该目录下的日期子目录 → 建库（异步任务）。"""
    store = ext.get_external_store()
    if store.get_source(source_id) is None:
        raise HTTPException(status_code=404, detail="来源目录未登记")

    def work(task: Task) -> dict:
        task.update(percent=0.0, message="扫描中…")

        def on_progress(msg: str) -> None:
            # 每个日期目录一条；具体百分比由 core 的 seen 总数推不出，用消息表达
            task.update(message=str(msg))

        return ext.scan_source(
            store, source_id, on_progress=on_progress, check_cancelled=task.check_cancelled
        )

    task = registry.create("external.scan", work)
    return {"task_id": task.id, "type": task.type, "source_id": source_id}


# ── 条目列表 ──────────────────────────────────────────────────────


@router.get("/api/external/items")
def list_items(
    source_id: str = "",
    q: str = "",
    order: str = "desc",
    start_date: str = "",
    end_date: str = "",
    verdict: str = "all",
) -> list[dict]:
    """列出外部条目（裸数组，与 ``GET /api/articles`` 同形）。

    ``source_id`` 为空 = 所有已启用目录。判定结果从 AI 缓存合并进来。
    """
    from mp_harvest.server.routes.history import parse_date_range

    start_ts, end_ts = parse_date_range(start_date, end_date)
    store = ext.get_external_store()
    rows = store.list_items(
        source_id, q=q, start_ts=start_ts, end_ts=end_ts, order=order
    )
    verdicts, content = _load_verdicts()
    out = [_item_out(r, verdicts=verdicts, content=content) for r in rows]
    if verdict in ("keep", "drop"):
        want = "keep" if verdict == "keep" else "drop"
        out = [r for r in out if r["verdict"] == want]
    return out


def _items_by_ids(source_id: str, ids: list[str] | None) -> list[dict[str, Any]]:
    """按前端 id（``<source_id>:ext:<item_key>``）取条目；ids 为空 = 该来源全部。

    **必须按 public id 匹配而不是裸 item_key**：聚合视图下 id 带 source 前缀，
    只比对 item_key 会把别的来源的同名条目也捞进来（与 ``export.py`` 那次修复同源）。
    """
    store = ext.get_external_store()
    rows = store.list_items(source_id)
    if not ids:
        return rows
    wanted = {str(i) for i in ids}
    picked = []
    for r in rows:
        public_id = f"{r.get('source_id')}:{_external_identity(str(r.get('item_key') or ''))}"
        if public_id in wanted or str(r.get("item_key") or "") in wanted:
            picked.append(r)
    return picked


# ── AI 筛选 ───────────────────────────────────────────────────────


@router.post("/api/external/filter", status_code=202)
def filter_items(body: ExternalFilterIn) -> dict:
    """外部条目的 AI 筛选（标题 / 内容两阶段）→ task_id。

    复用 ``judge_articles`` 与**同一批缓存文件**：缓存键是 ``ext:<item_key>``，
    所以同一篇论文在不同登记目录下只判一次，用户不会重复付费。
    """
    from mp_harvest.core import ai_filter as ai_mod

    store = ext.get_external_store()
    if body.source_id and store.get_source(body.source_id) is None:
        raise HTTPException(status_code=404, detail="来源目录未登记")
    articles = _items_by_ids(body.source_id, body.ids)
    if not articles:
        raise HTTPException(status_code=400, detail="没有可筛选的条目（请先扫描来源目录）")

    stage = "content" if str(body.stage).lower() == "content" else "title"
    verdicts, content_cache = _load_verdicts()
    rows = [_core_row(r, verdicts=verdicts, content=content_cache) for r in articles]
    if stage == "content":
        # 没有标题判定为 keep 的不做内容筛选（与微信侧同一语义）
        rows = [r for r in rows if r.get("title_keep") is True]
        if not rows:
            raise HTTPException(
                status_code=400,
                detail="没有通过标题筛选的条目（请先执行 AI 标题筛选）",
            )

    def work(task: Task) -> dict:
        models = ai_mod.load_models(_models_path())
        principles = (
            ai_mod.load_content_principles(_content_principles_path())
            if stage == "content"
            else ai_mod.load_principles(_principles_path())
        )
        prompt = ai_mod.build_system_prompt(principles)
        task.update(percent=0.0, message="AI 筛选准备中…")
        settings = _ai_settings()
        result = ai_mod.judge_articles(
            rows,
            models,
            prompt=prompt,
            cache_path=_content_cache_path() if stage == "content" else _cache_path(),
            prefix="content_" if stage == "content" else "title_",
            content_field="body_text" if stage == "content" else None,
            batch_size=body.batch_size or settings[0],
            workers=body.workers or settings[1],
            on_progress=lambda done, total: task.update(
                percent=(done / total * 100) if total else 0.0,
                message=f"AI 判定 {done}/{total}",
            ),
        )
        # 判定已由 judge_articles 落进缓存文件，这里不需要再 merge 回去 ——
        # 列表端点是读缓存渲染的（外部条目不另存一份判定，避免两处真相）
        return result

    task = registry.create("external.filter", work)
    return {"task_id": task.id, "type": task.type, "total": len(rows)}


def _ai_settings() -> tuple[int, int]:
    """每批篇数 / 并发批数：读设置，读不到用 core 默认。"""
    try:
        from mp_harvest.core import settings as settings_mod

        s = settings_mod.load_settings()
        return int(s.get("ai.batch_size") or 50), int(s.get("ai.workers") or 4)
    except Exception:  # noqa: BLE001
        return 50, 4


# ── 写回目录 ──────────────────────────────────────────────────────


@router.post("/api/external/export", status_code=202)
def export_items(body: ExternalExportIn) -> dict:
    """把条目按 arXiv 格式写回目录（``papers_data.json`` + 逐篇正文）→ task_id。

    合并语义：目标目录里已有的 ``papers_data.json`` 会被读出来按 key 合并，
    **不属于本次导出的条目原样保留**。
    """
    store = ext.get_external_store()
    if body.source_id and store.get_source(body.source_id) is None:
        raise HTTPException(status_code=404, detail="来源目录未登记")
    # date_dir 来自请求体，直接拼路径会穿越出目标目录 —— 必须先验形态
    if body.date_dir and not ext.DATE_DIR_RE.match(str(body.date_dir).strip()):
        raise HTTPException(status_code=400, detail=f"date_dir 格式非法（需 YYYY-MM-DD）：{body.date_dir}")

    items = _items_by_ids(body.source_id, body.ids)
    if not items:
        raise HTTPException(status_code=400, detail="没有可导出的条目")

    out_dir = _resolve_out_dir(body.out_dir, body.source_id, store)

    def work(task: Task) -> dict:
        total = len(items)
        # 指定 date_dir 时覆盖每条自身的日期目录
        if body.date_dir:
            for it in items:
                it["dir_date"] = str(body.date_dir).strip()
        done = 0

        def on_progress(msg: str) -> None:
            nonlocal done
            done += 1
            task.update(percent=done / total * 100, message=str(msg))

        return ext.write_external_export(
            items,
            out_dir,
            on_progress=on_progress,
            check_cancelled=task.check_cancelled,
        )

    task = registry.create("external.export", work)
    return {"task_id": task.id, "type": task.type, "total": len(items)}


def _resolve_out_dir(raw: str, source_id: str, store: ext.ExternalStore) -> Path:
    """导出目标目录：请求体 → 设置里的默认目录/其他来源/<名> → 数据目录。"""
    if str(raw or "").strip():
        return Path(str(raw).strip()).expanduser()
    try:
        from mp_harvest.core import settings as settings_mod

        default_dir = str(settings_mod.load_settings().get("export.default_dir") or "").strip()
    except Exception:  # noqa: BLE001
        default_dir = ""
    src = store.get_source(source_id) or {}
    name = str(src.get("name") or "其他来源")
    if default_dir:
        return Path(default_dir).expanduser() / "其他来源" / name
    return paths.data_dir() / "exports" / "其他来源" / name


__all__ = ["router"]
