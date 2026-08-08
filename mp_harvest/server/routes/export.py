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


@router.get("/api/articles/export-list")
def export_list(account_id: str, view: str = "all", format: str = "json"):
    """列表导出：json/csv/tsv/md/links/title+links → 纯文本返回（前端复制/下载附件）。

    view: all/keep/drop —— 始终只导出当前视图（§5.5）。
    """
    from mp_harvest.core import history_export

    fmt = (format or "json").lower()
    if fmt not in _LIST_FORMATS:
        raise HTTPException(
            status_code=400, detail=f"不支持的格式 {format!r}（可选：{', '.join(_LIST_FORMATS)}）"
        )
    if view not in ("all", "keep", "drop"):
        raise HTTPException(status_code=400, detail="view 必须是 all/keep/drop")
    account = state.get_store().get(account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    articles = state.get_articles(account_id)
    if view == "keep":
        articles = [a for a in articles if a.get("keep") is True]
    elif view == "drop":
        articles = [a for a in articles if a.get("keep") is False]
    core_fmt, ext = _FMT_TO_CORE[fmt]
    days = state.get_last_days(account_id)
    content = history_export.render_export(
        articles, fmt=core_fmt, account_name=str(account.get("name") or ""), days=days
    )
    filename = history_export.default_export_filename(
        account_name=str(account.get("name") or "export"), days=days, ext=ext
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

    account_id = body.account_id or ""
    account = state.get_store().get(account_id) if account_id else None
    if account_id and account is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    articles = state.get_articles(account_id) if account_id else []
    if body.ids:
        wanted = set(body.ids)
        articles = [
            a for a in articles if str(a.get("identity") or "") in wanted
        ]
    else:
        view = (body.view or "all").lower()
        if view not in ("all", "keep", "drop"):
            raise HTTPException(status_code=400, detail="view 必须是 all/keep/drop")
        if view == "keep":
            articles = [a for a in articles if a.get("keep") is True]
        elif view == "drop":
            articles = [a for a in articles if a.get("keep") is False]
    if not articles:
        raise HTTPException(status_code=400, detail="没有可导出的文章（请先拉取历史）")
    cred = (account or {}).get("credentials") or {}
    if body.out_dir and str(body.out_dir).strip():
        out_dir = Path(str(body.out_dir).strip()).expanduser()
    else:
        out_dir = paths.data_dir() / "exports" / (account.get("name") if account else "articles")

    def work(task: Task) -> dict:
        total = len(articles)
        done = 0

        def on_progress(msg: str) -> None:
            nonlocal done
            done += 1
            task.check_cancelled()  # 每篇文章一个批次边界
            # 2026-08-09：导出进度此前只有 message 没有 percent，界面进度条不动
            task.update(percent=done / total * 100, message=str(msg))

        result = article_reader.batch_export_articles(
            articles,
            out_dir=out_dir,
            cred=cred or None,
            account_name=str((account or {}).get("name") or ""),
            on_progress=on_progress,
        )
        task.check_cancelled()
        return result

    task = registry.create("articles.export_html", work)
    return {"task_id": task.id, "type": task.type, "total": len(articles)}
