"""服务层进程内状态（单 worker 前提，设计稿 §3.1）。

- ``get_store()`` / ``get_sightings()`` / ``get_mitm()``：core / infra.mitm
  对象的惰性单例（core 未就绪时本模块仍可导入——全部函数内惰性 import）。
- 文章缓存：旧版文章列表只存内存（``self._history_articles``），重构后由
  服务层按 account_id 持有，拉历史/补录/AI 判定写回，GET /api/articles 读取。
"""

from __future__ import annotations

import threading
from typing import Any

from mp_harvest.infra.platform import paths

_lock = threading.RLock()
_store: Any = None
_sightings: Any = None
_mitm: Any = None
_articles: dict[str, list[dict[str, Any]]] = {}
_last_days: dict[str, int] = {}


def get_store():
    """core.store.AccountStore 单例（账号 + 凭证持久化）。"""
    global _store
    with _lock:
        if _store is None:
            from mp_harvest.core import store as store_mod

            _store = store_mod.AccountStore(paths.data_dir() / "accounts.json")
        return _store


def get_sightings():
    """core.sightings.SightingsStore 单例（目击/补录记录）。"""
    global _sightings
    with _lock:
        if _sightings is None:
            from mp_harvest.core import sightings as sightings_mod

            _sightings = sightings_mod.SightingsStore(paths.data_dir() / "sightings.json")
        return _sightings


def get_mitm():
    """infra.mitm.mitm_capture.MitmCaptureService 单例。"""
    global _mitm
    with _lock:
        if _mitm is None:
            from mp_harvest.infra.mitm import mitm_capture

            _mitm = mitm_capture.MitmCaptureService(paths.app_root())
        return _mitm


# ── 文章内存缓存 ──────────────────────────────────────────────────


def set_articles(account_id: str, articles: list[dict[str, Any]], *, days: int | None = None) -> None:
    with _lock:
        _articles[str(account_id)] = [dict(a) for a in articles]
        if days is not None:
            _last_days[str(account_id)] = int(days)


def get_last_days(account_id: str) -> int:
    with _lock:
        return _last_days.get(str(account_id), 7)


def get_articles(account_id: str) -> list[dict[str, Any]]:
    with _lock:
        return [dict(a) for a in _articles.get(str(account_id), [])]


def merge_article_verdicts(account_id: str, judged: list[dict[str, Any]]) -> None:
    """把 AI 判定字段（keep/category/...）按 identity/link 合并回缓存。"""
    key_of = lambda a: str(a.get("identity") or a.get("link") or "")  # noqa: E731
    with _lock:
        rows = _articles.get(str(account_id))
        if not rows:
            return
        verdicts = {
            key_of(j): {k: v for k, v in j.items() if not k.startswith("_")}
            for j in judged
            if key_of(j)
        }
        for row in rows:
            v = verdicts.get(key_of(row))
            if v:
                row.update(v)


def reset() -> None:
    """清掉全部单例与缓存（测试用）。"""
    global _store, _sightings, _mitm
    with _lock:
        _store = None
        _sightings = None
        _mitm = None
        _articles.clear()
        _last_days.clear()
