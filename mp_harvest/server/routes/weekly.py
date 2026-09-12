"""周报生成（2026-09）：选题打分 → 深度解读 → 按模板渲染 → 归档原文。

数据源全部来自 MP 自己的库：公众号缓存（``server.state``）+「其他来源」外部目录
（``ExternalStore``）。核心逻辑在 ``core/weekly_report.py``，本模块只做
「取数据 / 校验 / 建任务 / 报进度」。

与既有链路的关系：**只读**微信文章缓存与外部来源库，不写它们；
AI 调用复用 ``ai_filter`` 的传输层与应用里已配置的模型（``ai_models.json``）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

from mp_harvest.core import external_sources as ext_mod
from mp_harvest.core import weekly_report as wr
from mp_harvest.infra.platform import paths
from mp_harvest.server import state
from mp_harvest.server.schemas import (
    WeeklyGenerateIn,
    WeeklyPromptIn,
    WeeklyRenderIn,
)
from mp_harvest.server.tasks import Task, registry

router = APIRouter(tags=["weekly"])


# ── 路径 ──────────────────────────────────────────────────────────


def _models_path() -> Path:
    return paths.data_dir() / "ai_models.json"


def _prompts_path() -> Path:
    return paths.data_dir() / "weekly" / "prompts.json"


def _cache_path() -> Path:
    return paths.data_dir() / "weekly" / "cache.json"


def _settings() -> dict[str, Any]:
    from mp_harvest.core import settings as settings_mod

    return settings_mod.load_settings()


def _ai_settings() -> tuple[int, int]:
    s = _settings()
    try:
        return int(s.get("ai.batch_size") or 50), int(s.get("ai.workers") or 4)
    except Exception:  # noqa: BLE001
        return 50, 4


def _resolve_out_dir(raw: str) -> Path:
    """输出目录：请求体 → 设置 weekly.dir → 数据目录。"""
    text = str(raw or "").strip()
    if text:
        return Path(text).expanduser()
    configured = str(_settings().get("weekly.dir") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return paths.data_dir() / "weekly" / "output"


def _resolve_template(raw: str) -> str:
    text = str(raw or "").strip()
    if text:
        return str(Path(text).expanduser())
    return str(_settings().get("weekly.template_path") or "").strip()


def _org() -> dict[str, str]:
    s = _settings()
    return {
        "name": str(s.get("weekly.org_name") or ""),
        "email": str(s.get("weekly.org_email") or ""),
        "archive_url": str(s.get("weekly.archive_url") or ""),
    }


# ── 候选收集 ──────────────────────────────────────────────────────


def _parse_range(from_date: str, to_date: str) -> tuple[int, int, str, str]:
    """YYYY-MM-DD → 本地时区闭区间 (start_ts, end_ts)。格式非法 400。"""
    from datetime import date, datetime

    def _ts(s: str, *, end: bool) -> int:
        try:
            d = date.fromisoformat(str(s).strip())
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"日期格式非法（需 YYYY-MM-DD）：{s}")
        t = datetime.max.time() if end else datetime.min.time()
        return int(datetime.combine(d, t).timestamp())

    start, end = _ts(from_date, end=False), _ts(to_date, end=True)
    if end < start:
        raise HTTPException(status_code=400, detail="结束日期早于起始日期")
    return start, end, from_date.strip(), to_date.strip()


def _gather(
    account_ids: list[str], source_ids: list[str], start_ts: int, end_ts: int
) -> list[dict[str, Any]]:
    """把公众号缓存与外部来源条目汇成候选。

    ``account_ids``/``source_ids`` 都为空 = 全部（公众号取所有已添加账号，
    外部来源取所有已启用目录）—— 与「全部公众号」的既有语义一致。
    """
    accounts = state.get_store().list_accounts()
    wanted = {str(a) for a in account_ids} if account_ids else None
    wechat_rows: list[tuple[dict[str, Any], str]] = []
    for acct in accounts:
        aid = str(acct.get("id") or "")
        if not aid or (wanted is not None and aid not in wanted):
            continue
        name = str(acct.get("name") or "")
        for row in state.get_articles(aid):
            wechat_rows.append((row, name))

    store = ext_mod.get_external_store()
    src_wanted = {str(s) for s in source_ids} if source_ids else None
    external_items: list[dict[str, Any]] = []
    for src in store.list_sources():
        sid = str(src.get("id") or "")
        if not sid or (src_wanted is not None and sid not in src_wanted):
            continue
        external_items.extend(store.list_items(sid))

    return wr.collect_candidates(
        wechat_rows=wechat_rows, external_items=external_items,
        start_ts=start_ts, end_ts=end_ts,
    )


# ── 预览 ──────────────────────────────────────────────────────────


@router.get("/api/weekly/preview")
def preview(
    from_date: str = "",
    to_date: str = "",
    account_ids: str = "",
    source_ids: str = "",
) -> dict:
    """候选统计 + 建议期号 + 当前模板信息（生成前先让用户看清会用到什么）。"""
    from datetime import date, timedelta

    if not to_date:
        to_date = date.today().isoformat()
    if not from_date:
        from_date = (date.today() - timedelta(days=6)).isoformat()
    start_ts, end_ts, fd, td = _parse_range(from_date, to_date)

    accs = [x for x in str(account_ids or "").split(",") if x.strip()]
    srcs = [x for x in str(source_ids or "").split(",") if x.strip()]
    cands = _gather(accs, srcs, start_ts, end_ts)

    out_dir = _resolve_out_dir("")
    tpl = _resolve_template("")
    tpl_path = Path(tpl) if tpl else wr.resolve_template_dir() / wr.BUILTIN_TEMPLATE_NAME
    s = _settings()
    return {
        "from_date": fd,
        "to_date": td,
        "suggested_issue": wr.suggest_issue_number(out_dir),
        "out_dir": str(out_dir),
        "selected_count": int(s.get("weekly.selected_count") or 15),
        "total": len(cands),
        "wechat": sum(1 for c in cands if c["kind"] == "公众号"),
        "arxiv": sum(1 for c in cands if c["kind"] == "arXiv"),
        "external_other": sum(1 for c in cands if c["kind"] not in ("公众号", "arXiv")),
        "template_path": str(tpl_path),
        "template_is_custom": bool(tpl),
        "template_exists": Path(tpl_path).is_file(),
    }


# ── 生成 ──────────────────────────────────────────────────────────


@router.post("/api/weekly/generate", status_code=202)
def generate(body: WeeklyGenerateIn) -> dict:
    """生成一期周报（异步任务）。"""
    from mp_harvest.core import ai_filter as ai_mod

    start_ts, end_ts, fd, td = _parse_range(body.from_date, body.to_date)
    candidates = _gather(body.account_ids, body.source_ids, start_ts, end_ts)
    if not candidates:
        raise HTTPException(
            status_code=400,
            detail="所选范围内没有候选文章（检查日期区间与来源勾选）",
        )

    models = ai_mod.load_models(_models_path())
    if not [m for m in models if getattr(m, "enabled", True)]:
        raise HTTPException(status_code=400, detail="没有启用的 AI 模型，请先到「AI 模型」页配置")

    out_dir = _resolve_out_dir(body.out_dir)
    template_path = _resolve_template(body.template_path)
    if template_path and not Path(template_path).is_file():
        raise HTTPException(status_code=400, detail=f"模板文件不存在：{template_path}")
    title = str(body.report_title or "").strip() or str(
        _settings().get("weekly.report_title") or "逻辑芯片行业洞察快报"
    )
    prompts = wr.load_prompts(_prompts_path())
    _, workers = _ai_settings()
    org = _org()

    def work(task: Task) -> dict:
        result = wr.generate_issue(
            candidates=candidates,
            models=models,
            prompts=prompts,
            cache=wr.WeeklyCache(_cache_path()),
            out_dir=out_dir,
            issue_num=body.issue_num,
            from_date=fd,
            to_date=td,
            selected_count=body.selected_count,
            org=org,
            report_title=title,
            template_path=template_path or None,
            download_images=body.download_images,
            workers=workers,
            on_stage=lambda msg: task.update(message=str(msg)),
            on_progress=lambda done, total: task.update(
                percent=(done / total * 90.0) if total else 0.0,
                message=f"{done}/{total}",
            ),
            check_cancelled=task.check_cancelled,
        )
        task.update(percent=100.0, message="完成")
        return result

    task = registry.create("weekly.generate", work)
    return {"task_id": task.id, "type": task.type, "total": len(candidates)}


@router.post("/api/weekly/render", status_code=202)
def rerender(body: WeeklyRenderIn) -> dict:
    """用某期归档里的 ``data/report.json`` 重渲染（**不调用 AI**）。

    改完模板想重出报告时用这个：数据是现成的，一分钱不花。
    """
    issue_dir = Path(body.issue_dir).expanduser()
    if not issue_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"期目录不存在：{issue_dir}")
    context = wr.load_saved_context(issue_dir)
    if context is None:
        raise HTTPException(
            status_code=400, detail=f"该期没有数据快照（data/report.json）：{issue_dir}"
        )
    template_path = _resolve_template(body.template_path)

    def work(task: Task) -> dict:
        task.update(percent=10.0, message="渲染中…")
        rendered = wr.render_report(context, template_path=template_path or None)
        if not rendered["ok"]:
            return {"ok": False, "error": rendered["error"], "issue_dir": str(issue_dir)}
        task.update(percent=80.0, message="写入…")
        report_title = str(
            (context.get("issue") or {}).get("title")
            or _settings().get("weekly.report_title")
            or "逻辑芯片行业洞察快报"
        )
        num = int((context.get("issue") or {}).get("num") or 0)
        out_path = issue_dir / f"{report_title}_第{num}期.html"
        out_path.write_text(rendered["html"], encoding="utf-8")
        task.update(percent=100.0, message="完成")
        return {
            "ok": True,
            "report_path": str(out_path),
            "issue_dir": str(issue_dir),
            "missing_vars": rendered.get("missing", []),
        }

    task = registry.create("weekly.render", work)
    return {"task_id": task.id, "type": task.type}


@router.get("/api/weekly/issues")
def list_issues() -> list[dict]:
    """往期列表（扫描输出目录里形如「第N期_日期」的目录）。"""
    import re

    out_dir = _resolve_out_dir("")
    rows: list[dict] = []
    try:
        if out_dir.is_dir():
            for p in sorted(out_dir.iterdir(), reverse=True):
                m = re.match(r"第(\d+)期[_-](\d{4}-\d{2}-\d{2})", p.name)
                if not (p.is_dir() and m):
                    continue
                reports = sorted(p.glob("*.html"))
                rows.append(
                    {
                        "issue_num": int(m.group(1)),
                        "date": m.group(2),
                        "dir": str(p),
                        "name": p.name,
                        "report": str(reports[0]) if reports else "",
                        "has_snapshot": (p / "data" / "report.json").is_file(),
                    }
                )
    except Exception:  # noqa: BLE001
        pass
    return rows


# ── 提示词 ────────────────────────────────────────────────────────


@router.get("/api/weekly/prompts")
def get_prompts() -> dict:
    """四段可编辑提示词（当前文本 + 内置默认，供「恢复默认」）。"""
    return {"prompts": wr.prompts_payload(_prompts_path())}


@router.put("/api/weekly/prompts")
def put_prompts(body: WeeklyPromptIn) -> dict:
    """保存某一段提示词。

    缓存键带提示词指纹，改完这段的旧缓存自动不再命中；这里顺手把**该阶段**
    那些再也用不到的旧条目清掉，免得缓存文件随改动次数无限膨胀。
    """
    if body.key not in wr.PROMPT_KEYS:
        raise HTTPException(status_code=400, detail=f"未知的提示词键：{body.key}")
    prompts = wr.load_prompts(_prompts_path())
    prompts[body.key] = body.text
    if not wr.save_prompts(_prompts_path(), prompts):
        raise HTTPException(status_code=500, detail="提示词保存失败")

    stage = {"scoring": "scores", "detail": "details", "brief": "briefs"}.get(body.key)
    pruned = 0
    if stage:
        pruned = wr.WeeklyCache(_cache_path()).prune_stage(
            stage, wr.prompt_fingerprint(body.key, prompts[body.key])
        )
    return {"ok": True, "pruned": pruned}


__all__ = ["router"]
