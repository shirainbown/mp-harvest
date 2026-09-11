"""列表导出（同步）+ 正文 HTML 导出（任务，§6 正文唯一格式）。

对应旧模块：history_export、article_reader。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse

from mp_harvest.infra.platform import paths
from mp_harvest.server import state
from mp_harvest.server.schemas import ExportHtmlIn
from mp_harvest.server.tasks import Task, registry

router = APIRouter(tags=["export"])

_LIST_FORMATS = ("json", "csv", "tsv", "md", "links", "title+links")
# 前端格式 key → core render_export 的 fmt key / 文件扩展名（§7.1）
_FMT_TO_CORE = {
    "json": ("json", "json"),
    "csv": ("csv", "csv"),
    "tsv": ("tsv", "tsv"),
    "md": ("markdown", "md"),  # core render_export 的 fmt key 是 markdown
    "links": ("links", "txt"),
    "title+links": ("title_links", "txt"),
}


def _resolve_articles(
    account_id: str,
    *,
    view: str = "all",
    stage: str = "final",
    ids: list[str] | None = None,
    start_ts: int = 0,
    end_ts: int = 0,
    latest_fetch: bool = False,
) -> tuple[list[dict], str]:
    """取导出文章；account_id 为空 = 全部公众号，每篇带 _account_id/account。

    stage 控制 view 的过滤字段：final=keep / title=title_keep / content=content_keep。
    start_ts/end_ts 按发布时间筛选；latest_fetch=True 只取各账号最近一次拉取（2026-08-23）。
    """
    if account_id:
        account = state.get_store().get(account_id)
        if account is None:
            raise HTTPException(status_code=404, detail="账号不存在")
        latest_ts = state.get_last_fetch_ts(account_id) if latest_fetch else 0
        articles = state.time_filter(
            [dict(a) for a in state.get_articles(account_id)],
            start_ts=start_ts,
            end_ts=end_ts,
            latest_ts=latest_ts,
        )
        for art in articles:
            art["_account_id"] = account_id
            art["account"] = str(account.get("name") or "")
        account_name = str(account.get("name") or "")
    else:
        articles = []
        for acct in state.get_store().list_accounts():
            aid = str(acct.get("id") or "")
            name = str(acct.get("name") or "")
            latest_ts = state.get_last_fetch_ts(aid) if latest_fetch else 0
            for a in state.time_filter(
                state.get_articles(aid), start_ts=start_ts, end_ts=end_ts, latest_ts=latest_ts
            ):
                art = dict(a)
                art["_account_id"] = aid
                art["account"] = name
                articles.append(art)
        account_name = "全部公众号"

    if ids is not None:
        from mp_harvest.server.mappers import article_public_id

        wanted = set(ids)
        # 前端传的是 article_public_id（含 __biz 前缀）；只比 identity 的话
        # 聚合视图下会跨账号误选（2026-09 修复）
        articles = [a for a in articles if article_public_id(a) in wanted]

    if stage == "title":
        if view == "keep":
            articles = [a for a in articles if a.get("title_keep") is True]
        elif view == "drop":
            articles = [a for a in articles if a.get("title_keep") is False]
    elif stage == "content":
        # 内容阶段只导出标题通过的文章
        articles = [a for a in articles if a.get("title_keep") is True]
        if view == "keep":
            articles = [a for a in articles if a.get("content_keep") is True]
        elif view == "drop":
            articles = [a for a in articles if a.get("content_keep") is False]
        elif view == "pending":
            articles = [a for a in articles if a.get("content_keep") is None]
    else:
        if view == "keep":
            articles = [a for a in articles if a.get("keep") is True]
        elif view == "drop":
            articles = [a for a in articles if a.get("keep") is False]
    return articles, account_name


def _cred_by_account(articles: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for art in articles:
        aid = str(art.get("_account_id") or "")
        if aid and aid not in out:
            acct = state.get_store().get(aid) or {}
            out[aid] = acct.get("credentials") or {}
    return out


def _credential_active(account_id: str) -> bool:
    """该账号凭证是否未过期（B10）。优先用 store.is_active（真实 core）；
    fake store 无此方法时退化为「有凭证即可」（与 history.py 409 模式一致）。"""
    store = state.get_store()
    is_active = getattr(store, "is_active", None)
    if callable(is_active):
        try:
            return bool(is_active(account_id))
        except Exception:  # noqa: BLE001
            pass
    acct = store.get(account_id) or {}
    return bool(acct.get("credentials"))


@router.get("/api/articles/export-list")
def export_list(
    account_id: str,
    view: str = "all",
    format: str = "json",
    stage: str = "final",
    start_date: str = "",
    end_date: str = "",
    latest_fetch: bool = False,
):
    """列表导出：json/csv/tsv/md/links/title+links → 纯文本返回（前端复制/下载附件）。

    view: all/keep/drop(/pending，stage=content 时)；stage: final/title/content。
    start_date/end_date/latest_fetch：时间筛选，与文章列表一致（2026-08-23）。
    """
    from mp_harvest.core import history_export

    from mp_harvest.server.routes.history import parse_date_range

    fmt = (format or "json").lower()
    if fmt not in _LIST_FORMATS:
        raise HTTPException(
            status_code=400, detail=f"不支持的格式 {format!r}（可选：{', '.join(_LIST_FORMATS)}）"
        )
    if stage not in ("final", "title", "content"):
        raise HTTPException(status_code=400, detail="stage 必须是 final/title/content")
    allowed_views = ("all", "keep", "drop") if stage != "content" else ("all", "keep", "drop", "pending")
    if view not in allowed_views:
        raise HTTPException(status_code=400, detail=f"当前 stage 下 view 必须是 {'/'.join(allowed_views)}")
    start_ts, end_ts = parse_date_range(start_date, end_date)
    articles, account_name = _resolve_articles(
        account_id,
        view=view,
        stage=stage,
        start_ts=start_ts,
        end_ts=end_ts,
        latest_fetch=latest_fetch,
    )
    core_fmt, ext = _FMT_TO_CORE[fmt]
    days = state.get_last_days(account_id) if account_id else 7
    content = history_export.render_export(
        articles, fmt=core_fmt, account_name=account_name, days=days
    )
    filename = history_export.default_export_filename(
        account_name=account_name or "export", days=days, ext=ext
    )
    from urllib.parse import quote

    # 文件名可能含中文：ASCII 回退 + RFC 5987 UTF-8 编码（header 只能是 latin-1）
    fallback = "".join(c if ord(c) < 128 else "_" for c in filename)
    disposition = f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": disposition},
    )


@router.post("/api/articles/export-html", status_code=202)
def export_html(body: ExportHtmlIn) -> dict:
    """正文 HTML 导出 → task_id（设计稿 §6：正文导出只有 HTML）。

    ids 非空时导出指定文章；否则按 ``view``（all/keep/drop）过滤当前账号全部。
    ``out_dir`` 指定目标目录（支持 ``~`` 展开），并在其中生成 titles_filtered
    风格的 ``index.html`` 说明页（2026-08-09 新增）。
    """
    from mp_harvest.core import article_reader

    from mp_harvest.server.routes.history import parse_date_range

    account_id = body.account_id or ""
    view = (body.view or "all").lower()
    stage = (body.stage or "final").lower()
    if stage not in ("final", "title", "content"):
        raise HTTPException(status_code=400, detail="stage 必须是 final/title/content")
    allowed_views = ("all", "keep", "drop") if stage != "content" else ("all", "keep", "drop", "pending")
    if view not in allowed_views:
        raise HTTPException(status_code=400, detail=f"当前 stage 下 view 必须是 {'/'.join(allowed_views)}")
    start_ts, end_ts = parse_date_range(body.start_date, body.end_date)
    articles, account_name = _resolve_articles(
        account_id,
        view=view,
        stage=stage,
        ids=list(body.ids) if body.ids else None,
        start_ts=start_ts,
        end_ts=end_ts,
        latest_fetch=body.latest_fetch,
    )
    if not articles:
        raise HTTPException(status_code=400, detail="没有可导出的文章（请先拉取历史）")
    cred_by_account = _cred_by_account(articles)
    # B10：导出前校验凭证；过期账号的文章不拉取，直接进 errors（不整批 409，
    # 部分账号过期不应挡住其他账号）。
    cred_error_by_account: dict[str, str] = {}
    for aid in {str(a.get("_account_id") or "") for a in articles}:
        if not aid:
            continue
        if not _credential_active(aid):
            acct = state.get_store().get(aid) or {}
            cred_error_by_account[aid] = (
                f"凭证缺失或已过期：{acct.get('name') or aid}（请重新抓包后再导出）"
            )
    for art in articles:
        aid = str(art.get("_account_id") or "")
        err = cred_error_by_account.get(aid, "")
        if err:
            art["_cred_error"] = err
        art["_cred"] = cred_by_account.get(aid, {})
    from mp_harvest.core import settings as settings_mod

    app_settings = settings_mod.load_settings()
    # download_images：请求体显式指定优先；否则读后端设置（默认 False，B7）
    if body.download_images is not None:
        download_images = bool(body.download_images)
    else:
        download_images = bool(app_settings.get("export.download_images", False))
    if body.out_dir and str(body.out_dir).strip():
        out_dir = Path(str(body.out_dir).strip()).expanduser()
    else:
        # 请求体没带目录时用设置页配的「默认目录」（2026-09 修复）。
        # settings.py 的注释一直声称这里会读 export.default_dir，但实际没读，
        # 于是用户配了默认目录，点「导出 HTML」文件仍落在数据目录里。
        # 留空（清空设置）时才退回 data_dir()/exports/<账号>。
        default_dir = str(app_settings.get("export.default_dir") or "").strip()
        out_dir = (
            Path(default_dir).expanduser()
            if default_dir
            else paths.data_dir() / "exports" / (account_name or "articles")
        )

    def work(task: Task) -> dict:
        total = len(articles)
        done = 0

        def on_progress(msg: str) -> None:
            nonlocal done
            done += 1
            # 2026-08-09：导出进度此前只有 message 没有 percent，界面进度条不动
            task.update(percent=done / total * 100, message=str(msg))

        result = article_reader.batch_export_articles(
            articles,
            out_dir=out_dir,
            cred=None,
            account_name=account_name,
            download_images=download_images,
            on_progress=on_progress,
            check_cancelled=task.check_cancelled,
        )
        # B9：取消时 batch 已写出已完成部分的 index.html 并返回 partial 结果，
        # 不再抛 TaskCancelled，保证 partial 结果能带回前端。
        return result

    task = registry.create("articles.export_html", work)
    return {"task_id": task.id, "type": task.type, "total": len(articles)}
