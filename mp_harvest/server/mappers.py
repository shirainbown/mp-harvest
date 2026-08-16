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

# core 行 ``source`` → 前端 ArticleSource（M=MITM目击, G=getmsg, 补=补录）
_SOURCE_MAP = {
    "getmsg": "G",
    "manual": "补",
    "mitm": "M",
    "mitm_getmsg": "M",
    "sighting": "M",
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


def article_source(source: Any) -> str:
    """core ``source`` → 'M' | 'G' | '补'；未知目击类归 M，缺省（拉历史来的）归 G。"""
    s = str(source or "").strip()
    if s in _SOURCE_MAP:
        return _SOURCE_MAP[s]
    return "M" if s else "G"


def article_out(row: dict[str, Any], *, account_id: str = "", account_name: str = "") -> dict[str, Any]:
    """core 文章行（history_client / sightings 合并行）→ 前端 ``Article``（types.ts）。

    字段映射：``identity→id``、``link→url``、``publish_ts→date``（本地 ISO 字符串，
    ``Date.parse`` 可解析）、``source→M/G/补``、``keep True/False/None→keep/drop/null``、
    ``reason``（缺省空串）。
    """
    keep = row.get("keep")
    title_keep = row.get("title_keep")
    content_keep = row.get("content_keep")
    verdict = "keep" if keep is True else ("drop" if keep is False else None)
    title_verdict = "keep" if title_keep is True else ("drop" if title_keep is False else None)
    content_verdict = "keep" if content_keep is True else ("drop" if content_keep is False else None)
    ts = int(row.get("publish_ts") or 0)
    if ts:
        date = datetime.fromtimestamp(ts).isoformat(timespec="seconds")
    else:
        # 无发布时间（如手动补录）：退到 seen_at（ISO），再退 publish_at，保可解析
        date = str(row.get("seen_at") or row.get("publish_at") or "")
    return {
        "id": str(row.get("identity") or row.get("link") or ""),
        "account_id": str(account_id or ""),
        "account_name": str(account_name or ""),
        "title": str(row.get("title") or ""),
        "url": str(row.get("link") or ""),
        "date": date,
        "source": article_source(row.get("source")),
        "verdict": verdict,
        "reason": str(row.get("reason") or ""),
        "title_verdict": title_verdict,
        "title_reason": str(row.get("title_reason") or ""),
        "content_verdict": content_verdict,
        "content_reason": str(row.get("content_reason") or ""),
    }


__all__ = ["account_out", "article_out", "article_source"]
