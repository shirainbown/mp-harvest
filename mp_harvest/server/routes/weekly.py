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


def _score_settings() -> tuple[int, int]:
    """打分的「每批几篇 / 并发请求数」。

    用周报**自己的**键（``weekly.score_batch_size`` / ``weekly.workers``），
    不借 ``ai.batch_size`` —— 那个默认 50 是给逐篇判定调的，打分一篇的输出是它的
    两倍多，套过来会整批超输出上限。见 settings.py 的键表注释。
    """
    s = _settings()
    try:
        batch = int(s.get("weekly.score_batch_size") or wr.SCORING_BATCH_DEFAULT)
        workers = int(s.get("weekly.workers") or 4)
    except Exception:  # noqa: BLE001
        return wr.SCORING_BATCH_DEFAULT, 4
    return max(1, min(wr.SCORING_BATCH_MAX, batch)), max(1, min(16, workers))


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


def _external_verdicts() -> dict[str, dict[str, Any]]:
    """外部条目的**最终**判定与理由：``{<ext:item_key>: {"keep": bool, "reason": str}}``。

    外部条目不把判定存在自己的库里（避免两处真相），而是复用 ai_filter 的两阶段
    缓存，读取时合并 —— 这里按与 ``state.merge_article_verdicts`` **同一条优先级**
    取：内容筛选优先，没有则标题筛选；都没判过就不进这个字典。

    理由跟着它自己那一阶段走：内容筛选定的用 ``content_reason``，标题筛选定的用
    ``title_reason`` —— 不能一律取某一个，否则会给出**另一阶段**的理由。
    """
    from mp_harvest.core import ai_filter as ai_mod

    data = paths.data_dir()
    out: dict[str, dict[str, Any]] = {}
    title: dict = {}
    content: dict = {}
    # 前缀必须与**该文件字段的前缀**一致：标题缓存里存的是 ``title_keep``，传 ""
    # 就一条都映射不出来 —— 于是下面 ``t.get("title_keep")`` 恒为 None、标题阶段的
    # 判定永远合并不进来（2026-09 修复，与 _migrate_flat_entries 的两代格式问题同时发现）
    for path, prefix, bucket in (
        (data / "ai_filter_cache.json", "title_", title),
        (data / "ai_content_filter_cache.json", "content_", content),
    ):
        try:
            bucket.update(ai_mod.load_verdicts(path, prefix=prefix) or {})
        except Exception:  # noqa: BLE001
            continue
    for key in set(title) | set(content):
        c = content.get(key) or {}
        t = title.get(key) or {}
        ck = c.get("content_keep")
        tk = t.get("title_keep")
        keep = ck if ck is not None else tk
        if keep is not None:
            out[key] = {
                "keep": bool(keep),
                "reason": str((c.get("content_reason") if ck is not None
                               else t.get("title_reason")) or ""),
            }
    return out


def _gather(
    account_ids: list[str],
    source_ids: list[str],
    start_ts: int,
    end_ts: int,
    *,
    only_kept: bool = False,
) -> list[dict[str, Any]]:
    """把公众号缓存与外部来源条目汇成候选。

    ``account_ids``/``source_ids`` 都为空 = 全部（公众号取所有已添加账号，
    外部来源取所有已启用目录）—— 与「全部公众号」的既有语义一致。

    ``only_kept``：跳过被 AI 筛选判定为「过滤掉」的文章（未判定照收）。
    """
    accounts = state.get_store().list_accounts()
    wanted = {str(a) for a in account_ids} if account_ids else None
    # 带上账号 id：前端要按账号显示「本区间 N 篇」（文章缓存行里只有账号名）
    wechat_rows: list[tuple[dict[str, Any], str, str]] = []
    for acct in accounts:
        aid = str(acct.get("id") or "")
        if not aid or (wanted is not None and aid not in wanted):
            continue
        name = str(acct.get("name") or "")
        for row in state.get_articles(aid):
            wechat_rows.append((row, name, aid))

    store = ext_mod.get_external_store()
    src_wanted = {str(s) for s in source_ids} if source_ids else None
    external_items: list[dict[str, Any]] = []
    verdicts = _external_verdicts()
    for src in store.list_sources():
        sid = str(src.get("id") or "")
        if not sid or (src_wanted is not None and sid not in src_wanted):
            continue
        for it in store.list_items(sid):
            # 判定不进外部库（避免两处真相），读的时候合并进来
            k = f"ext:{it.get('item_key') or ''}"
            v = verdicts.get(k)
            external_items.append({**it, "keep": v["keep"], "reason": v["reason"]} if v else it)

    return wr.collect_candidates(
        wechat_rows=wechat_rows, external_items=external_items,
        start_ts=start_ts, end_ts=end_ts, only_kept=only_kept,
    )


# ── 预览 ──────────────────────────────────────────────────────────


@router.get("/api/weekly/preview")
def preview(
    from_date: str = "",
    to_date: str = "",
    account_ids: str = "",
    source_ids: str = "",
    only_kept: bool = True,
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
    cands = _gather(accs, srcs, start_ts, end_ts, only_kept=only_kept)

    # 按账号 / 按来源的**本区间**候选数。必须在**去重之后**数 —— 同一篇论文可能
    # 同时登记在两个目录下，各自先数再加会大于总数，前端看到的就对不上了。
    account_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    for c in cands:
        sid = str(c.get("source_id") or "")
        if not sid:
            continue
        bucket = account_counts if c["kind"] == "公众号" else source_counts
        bucket[sid] = bucket.get(sid, 0) + 1

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
        # 逐行的区间内候选数（前端每行显示「N 篇」，与 total 是同一套口径）
        "only_kept": bool(only_kept),
        "account_counts": account_counts,
        "source_counts": source_counts,
        "template_path": str(tpl_path),
        "template_is_custom": bool(tpl),
        "template_exists": Path(tpl_path).is_file(),
    }


@router.get("/api/weekly/candidates")
def candidates(
    from_date: str = "",
    to_date: str = "",
    account_ids: str = "",
    source_ids: str = "",
    only_kept: bool = True,
) -> dict:
    """候选**逐篇明细**（2026-09）：生成前就能看清每一篇「为什么在池子里、怎么被判的」。

    与 ``/api/weekly/preview`` **同源同口径** —— 同一个 ``_gather``、同一个
    ``only_kept``，只是把 preview 只用来计数的逐篇信息还回来。所以抽屉里看到
    「共 N 篇」与面板上的数字必然对得上。

    **单独一个接口**而不是并进 preview：preview 在改日期、勾来源时会被反复调用，
    挂上整份明细会让每次交互都多传几十 KB。

    两块「判定」分开给，因为它们来自不同阶段、回答不同问题：
    - ``verdict``/``verdict_reason`` —— **AI 筛选**（要不要进候选池），读文章缓存；
    - ``score``/``reason`` —— **周报打分**（排多少名、算不算半导体），读打分缓存。

    ⚠️ 打分理由**只可能来自缓存**：生成前本来就不存在这次的分数。指纹对不上的
    （用户改过提示词）一律按未打分给，由生成时补 —— 拿旧指纹的分数冒充会更糟。
    """
    from datetime import date, timedelta

    if not to_date:
        to_date = date.today().isoformat()
    if not from_date:
        from_date = (date.today() - timedelta(days=6)).isoformat()
    start_ts, end_ts, fd, td = _parse_range(from_date, to_date)

    accs = [x for x in str(account_ids or "").split(",") if x.strip()]
    srcs = [x for x in str(source_ids or "").split(",") if x.strip()]
    cands = _gather(accs, srcs, start_ts, end_ts, only_kept=only_kept)

    prompts = wr.load_prompts(_prompts_path())
    try:
        scores = wr.cached_scores(
            cands, prompts=prompts, cache=wr.WeeklyCache(_cache_path())
        )
    except Exception:  # noqa: BLE001
        # 缓存读不出来只是「看不到已有分数」，不该让整页打不开
        scores = {}

    items: list[dict[str, Any]] = []
    for c in cands:
        s = scores.get(c["key"]) or {}
        items.append(
            {
                "key": c["key"],
                "title": c["title"],
                "title_cn": str(s.get("title_cn") or c.get("title_cn") or ""),
                "source": str(c.get("source") or ""),
                "source_id": str(c.get("source_id") or ""),
                "kind": str(c.get("kind") or ""),
                "date": str(c.get("date") or ""),
                "publish_ts": int(c.get("publish_ts") or 0),
                "url": str(c.get("url") or ""),
                "verdict": c.get("verdict"),
                "verdict_reason": str(c.get("verdict_reason") or ""),
                "scored": bool(s),
                "score": s.get("score"),
                "semiconductor": s.get("semiconductor"),
                "domain": str(s.get("domain") or ""),
                "business_tags": list(s.get("business_tags") or []),
                "reason": str(s.get("reason") or ""),
            }
        )

    return {
        "from_date": fd,
        "to_date": td,
        "only_kept": bool(only_kept),
        "total": len(items),
        "scored": sum(1 for i in items if i["scored"]),
        "items": items,
    }


# ── 生成 ──────────────────────────────────────────────────────────


@router.post("/api/weekly/generate", status_code=202)
def generate(body: WeeklyGenerateIn) -> dict:
    """生成一期周报（异步任务）。"""
    from mp_harvest.core import ai_filter as ai_mod

    start_ts, end_ts, fd, td = _parse_range(body.from_date, body.to_date)
    candidates = _gather(body.account_ids, body.source_ids, start_ts, end_ts,
                         only_kept=body.only_kept)
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
    batch_size, workers = _score_settings()
    org = _org()

    # 补正文要用账号凭证（与 AI 内容筛选同一套取法）
    cred_by_account: dict[str, dict] = {}
    for acct in state.get_store().list_accounts():
        aid = str(acct.get("id") or "")
        if aid:
            cred_by_account[aid] = acct.get("credentials") or {}

    def _cred_for(aid: str) -> dict:
        return cred_by_account.get(str(aid or ""), {})

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
            batch_size=batch_size,
            fetch_bodies=body.fetch_bodies,
            cred_for=_cred_for,
            save_bodies=state.merge_article_bodies_by_account,
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
