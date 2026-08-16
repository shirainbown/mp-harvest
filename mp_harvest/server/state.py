"""服务层进程内状态（单 worker 前提，设计稿 §3.1）。

- ``get_store()`` / ``get_sightings()`` / ``get_mitm()``：core / infra.mitm
  对象的惰性单例（core 未就绪时本模块仍可导入——全部函数内惰性 import）。
- 文章缓存：按 account_id 持有，拉历史/补录/AI 判定写回，GET /api/articles 读取；
  同时落盘到 ``data/articles_cache/<account_id>.json``（2026-08-09 新增），
  重启后 ``get_articles`` 懒加载恢复，关闭应用不再丢历史文章。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from mp_harvest.infra.platform import paths

_lock = threading.RLock()
_store: Any = None
_sightings: Any = None
_mitm: Any = None
_articles: dict[str, list[dict[str, Any]]] = {}
_last_days: dict[str, int] = {}

_ARTICLES_CACHE_DIR = "articles_cache"


def _account_cache_path(account_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(account_id)) or "account"
    return paths.data_dir() / _ARTICLES_CACHE_DIR / f"{safe}.json"


def _load_articles_from_disk(account_id: str) -> None:
    """从磁盘恢复某账号文章缓存（幂等；损坏/缺失静默跳过）。"""
    p = _account_cache_path(account_id)
    if not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    arts = data.get("articles")
    if isinstance(arts, list):
        _articles[account_id] = [dict(a) for a in arts]
        days = data.get("days")
        if isinstance(days, int):
            _last_days[account_id] = days


def _save_articles_to_disk(account_id: str) -> None:
    """把某账号文章缓存原子写盘（tmp + replace；失败不阻断主流程）。"""
    try:
        p = _account_cache_path(account_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "days": _last_days.get(account_id, 7),
            "articles": _articles.get(account_id, []),
        }
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001
        pass


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
        key = str(account_id)
        _articles[key] = [dict(a) for a in articles]
        if days is not None:
            _last_days[key] = int(days)
        _save_articles_to_disk(key)


def get_last_days(account_id: str) -> int:
    with _lock:
        key = str(account_id)
        if key not in _last_days and key not in _articles:
            _load_articles_from_disk(key)
        return _last_days.get(key, 7)


def get_articles(account_id: str) -> list[dict[str, Any]]:
    with _lock:
        key = str(account_id)
        if key not in _articles:
            _load_articles_from_disk(key)
        return [dict(a) for a in _articles.get(key, [])]


def merge_article_verdicts(account_id: str, judged: list[dict[str, Any]]) -> None:
    """把 AI 判定字段（keep/category/...）按 identity/link 合并回缓存。"""
    key_of = lambda a: str(a.get("identity") or a.get("link") or "")  # noqa: E731
    with _lock:
        key = str(account_id)
        rows = _articles.get(key)
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
        _save_articles_to_disk(key)


def merge_article_bodies(account_id: str, bodies: list[dict[str, Any]]) -> None:
    """把拉取到的正文（body_text/body_html）按 identity/link 合并回缓存，供内容筛选复用。"""
    key_of = lambda a: str(a.get("identity") or a.get("link") or "")  # noqa: E731
    with _lock:
        key = str(account_id)
        rows = _articles.get(key)
        if not rows:
            return
        updates = {
            key_of(b): {k: v for k, v in b.items() if k in ("body_text", "body_html") and v}
            for b in bodies
            if key_of(b)
        }
        for row in rows:
            up = updates.get(key_of(row))
            if up:
                row.update(up)
        _save_articles_to_disk(key)


def drop_articles(account_id: str) -> None:
    """删除账号时清空内存与磁盘文章缓存。"""
    with _lock:
        key = str(account_id)
        _articles.pop(key, None)
        _last_days.pop(key, None)
        try:
            p = _account_cache_path(key)
            if p.is_file():
                os.unlink(p)
        except Exception:  # noqa: BLE001
            pass


def reset() -> None:
    """清掉全部单例与缓存（测试用）。"""
    global _store, _sightings, _mitm
    with _lock:
        _store = None
        _sightings = None
        _mitm = None
        _articles.clear()
        _last_days.clear()
