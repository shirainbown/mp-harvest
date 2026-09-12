"""core 行格式 → 前端 API 形状的适配映射（Epic D 联调对齐，API.md §3 第 5 条）。

边界约定：不改 ``mp_harvest/core``、不改 ``mp_harvest/frontend/src`` —— 在 server 层把
core 持久化行（``article_url`` / ``publish_ts`` / ``keep`` / ``identity`` …）映射为
前端 ``types.ts`` 期望的字段（``url`` / ``date`` / ``verdict`` / ``id`` …），且
``GET/POST /api/accounts``、``GET /api/articles``、``POST /api/articles/supplement``
四个端点响应为裸数组/裸对象（不带信封），与前端 store 的解析方式逐字段对齐。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

# core 行 ``source`` → 前端 ArticleSource（M=MITM目击, G=getmsg, 补=补录, 外=其他来源）
_SOURCE_MAP = {
    "getmsg": "G",
    "manual": "补",
    "mitm": "M",
    "mitm_getmsg": "M",
    "sighting": "M",
    "external": "外",
}


def _epoch_seconds(value: Any) -> int | None:
    """core ``expires_at`` 是 ISO 字符串；前端要 epoch 秒（``* 1000`` 与 ``Date.now()`` 比较）。"""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value)).timestamp())
    except Exception:
        return None


def account_out(row: dict[str, Any]) -> dict[str, Any]:
    """core 账号行（``store.AccountStore``）→ 前端 ``Account``（types.ts）。

    字段映射：``article_url→url``、``biz|credentials.__biz→__biz``、
    ISO ``expires_at`` → epoch 秒、``status=='awaiting'→pending``。
    """
    cred = row.get("credentials") or {}
    biz = str(row.get("biz") or cred.get("__biz") or "").strip()
    out: dict[str, Any] = {
        "id": str(row.get("id") or ""),
        "name": str(row.get("name") or ""),
        "url": str(row.get("article_url") or ""),
        "expires_at": _epoch_seconds(row.get("expires_at")),
        "pending": str(row.get("status") or "") == "awaiting",
    }
    if biz:
        out["__biz"] = biz
    return out


def article_public_id(row: dict[str, Any]) -> str:
    """前端可见的文章稳定 id = ``__biz`` + ``identity``。

    ``identity`` 不含 ``__biz``（它只服务于**单账号**内部的去重），于是同一篇
    文章出现在两个公众号下时会算出同一个 id：「全部公众号」聚合视图里
    ``:key`` 冲突（虚拟列表错行）、勾选串号（``selected`` 是 id 的 Set）、
    按 ids 导出跨账号误选（2026-09 修复）。

    只要行里有 ``__biz`` 就加上前缀；没有则退回原来的 identity，保持兼容。
    """
    ident = str(row.get("identity") or row.get("link") or "")
    biz = str(row.get("__biz") or (row.get("credentials") or {}).get("__biz") or "").strip()
    return f"{biz}:{ident}" if biz and ident else ident


def article_source(source: Any) -> str:
    """core ``source`` → 'M' | 'G' | '补'；未知目击类归 M，缺省（拉历史来的）归 G。"""
    s = str(source or "").strip()
    if s in _SOURCE_MAP:
        return _SOURCE_MAP[s]
    return "M" if s else "G"


def article_out(
    row: dict[str, Any],
    *,
    account_id: str = "",
    account_name: str = "",
    exported: bool,          # **必填、无默认值**：见下
) -> dict[str, Any]:
    """core 文章行（history_client / sightings 合并行）→ 前端 ``Article``（types.ts）。

    字段映射：``identity→id``、``link→url``、``publish_ts→date``（本地 ISO 字符串，
    ``Date.parse`` 可解析）、``source→M/G/补``、``keep True/False/None→keep/drop/null``、
    ``reason``（缺省空串）。

    ``exported``：本地是否**还留着**导出的 HTML。由调用方查好传进来（映射函数
    不该自己去碰导出记录库）。

    **刻意不给默认值**：写 `= False` 看着无害，但变异测试证明它躲得过所有用例
    —— 真的把默认值翻成 True，历史那条路每行都显式传参、照常绿，只有**没传的**
    调用点（外部来源）会静默挂上假的「已导出」。必填就把这个位置堵死了，
    改的人必须对每个调用点表态。
    """
    keep = row.get("keep")
    title_keep = row.get("title_keep")
    content_keep = row.get("content_keep")
    verdict = "keep" if keep is True else ("drop" if keep is False else None)
    title_verdict = "keep" if title_keep is True else ("drop" if title_keep is False else None)
    content_verdict = "keep" if content_keep is True else ("drop" if content_keep is False else None)
    ts = int(row.get("publish_ts") or 0)
    seen_at = str(row.get("seen_at") or "")
    if ts:
        date = datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    else:
        # 无发布时间（抓包目击 / 手动补录）：退到 seen_at（ISO），再退 publish_at，
        # 保可解析 —— 前端排序和「按时间」筛选都指着它。
        date = seen_at or str(row.get("publish_at") or "")
    fetched_ts = int(row.get("fetched_ts") or 0)
    fetched_at = (
        datetime.fromtimestamp(fetched_ts).isoformat(timespec="seconds") if fetched_ts else ""
    )
    return {
        "id": article_public_id(row),
        "account_id": str(account_id or ""),
        "account_name": str(account_name or ""),
        "title": str(row.get("title") or ""),
        "url": str(row.get("link") or ""),
        "date": date,
        # ``date`` 是**能拿来排序的最早已知时间**，不等于发布时间：抓包目击
        # （source=M）只有「看到这篇文章的时刻」，微信在链接里不给发布时间。
        # 不把这件事说出来，界面就会把目击时刻当发布时间显示 —— 一篇几个月前的
        # 文章挂着今天的日期，用户只会觉得数据坏了（2026-09 用户报的）。
        "has_publish_time": bool(ts),
        "seen_at": seen_at,
        "fetched_at": fetched_at,
        "source": article_source(row.get("source")),
        "verdict": verdict,
        "reason": str(row.get("reason") or ""),
        "title_verdict": title_verdict,
        "title_reason": str(row.get("title_reason") or ""),
        "content_verdict": content_verdict,
        "content_reason": str(row.get("content_reason") or ""),
        "exported": bool(exported),
    }


__all__ = ["account_out", "article_out", "article_source"]
