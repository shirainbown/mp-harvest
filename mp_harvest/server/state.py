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
_last_fetch_ts: dict[str, int] = {}

_ARTICLES_CACHE_DIR = "articles_cache"

# 合并时从新一轮拉取结果更新的抓取字段（判定/正文字段一律保留旧值，见 merge_articles）
_FETCH_FIELDS = (
    "title",
    "link",
    "digest",
    "cover",
    "author",
    "publish_ts",
    "publish_at",
    "mid",
    "idx",
    "sn",
    "source",
)


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
        last_fetch = data.get("last_fetch_ts")
        if isinstance(last_fetch, int):
            _last_fetch_ts[account_id] = last_fetch


def _save_articles_to_disk(account_id: str) -> None:
    """把某账号文章缓存原子写盘（tmp + replace；失败不阻断主流程）。"""
    try:
        p = _account_cache_path(account_id)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "days": _last_days.get(account_id, 7),
            "last_fetch_ts": _last_fetch_ts.get(account_id, 0),
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

            # 与 MITM addon（SCHINZA_SIGHTINGS）写同一文件，两个数据源才能交汇；
            # 统一走 core.sightings.default_sightings_path()（data/article_sightings.json）
            _sightings = sightings_mod.SightingsStore(sightings_mod.default_sightings_path())
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


def _article_key(a: dict[str, Any]) -> str:
    return str(a.get("identity") or a.get("link") or "")


def _account_biz(account_id: str) -> str:
    """账号的 __biz（凭证里取）；取不到返回空串。"""
    try:
        acct = get_store().get(str(account_id)) or {}
        return str(
            acct.get("biz") or (acct.get("credentials") or {}).get("__biz") or ""
        ).strip()
    except Exception:  # noqa: BLE001
        return ""


def merge_articles(
    account_id: str,
    new_articles: list[dict[str, Any]],
    *,
    fetched_ts: int,
    days: int | None = None,
    advance_last_fetch: bool = True,
) -> dict[str, int]:
    """把新一轮拉取结果合并进缓存（不覆盖历史），返回 {"added", "total"}。

    - 键 = identity or link；已存在的行只更新抓取字段（``_FETCH_FIELDS``），
      保留 AI 判定（keep/title_keep/content_keep/*reason）与正文（body_*）；
    - 本轮出现过的行都刷新 ``fetched_ts``（用于「最近一次拉取」筛选）；
    - 更新 ``last_fetch_ts`` 并落盘；合并后按 publish_ts desc 排序。
    """
    # 在拿 _lock 之前解析（避免与 store 的锁嵌套）
    biz = _account_biz(account_id)
    with _lock:
        key = str(account_id)
        if key not in _articles:
            _load_articles_from_disk(key)
        rows = _articles.get(key, [])
        by_key = {_article_key(r): r for r in rows if _article_key(r)}
        added = 0
        for a in new_articles or []:
            k = _article_key(a)
            if not k:
                continue
            existing = by_key.get(k)
            if existing is None:
                row = dict(a)
                # 补 __biz：identity 不含它，缺了会导致「全部公众号」聚合视图里
                # 跨账号同文 id 撞车（Vue key 冲突 / 勾选串号，2026-09 修复）
                if biz and not row.get("__biz"):
                    row["__biz"] = biz
                row["fetched_ts"] = int(fetched_ts)
                rows.append(row)
                by_key[k] = row
                added += 1
            else:
                for f in _FETCH_FIELDS:
                    if f in a:
                        existing[f] = a[f]
                if biz and not existing.get("__biz"):
                    existing["__biz"] = biz
                existing["fetched_ts"] = int(fetched_ts)
        rows.sort(key=lambda r: int(r.get("publish_ts") or 0), reverse=True)
        _articles[key] = rows
        if days is not None:
            _last_days[key] = int(days)
        # 只有成功的拉取才推进 last_fetch_ts（2026-09 修复）。
        # 原先无条件推进：一次失败拉取（凭证过期等）就会让所有旧行的
        # fetched_ts < last_fetch_ts，「最近拉取」筛选直接变成空列表，
        # 用户以为文章丢了（「全部」视图里还在）。
        if advance_last_fetch:
            _last_fetch_ts[key] = int(fetched_ts)
        _save_articles_to_disk(key)
        return {"added": added, "total": len(rows)}


def touch_fetched_in_window(
    account_id: str, *, start_ts: int, end_ts: int, fetched_ts: int
) -> int:
    """把窗口内**已缓存**的文章补打本次 ``fetched_ts``，返回改动条数。

    断点拉取的配套（2026-09）：翻页提前停止后，那些「已入库、本次没重新请求」的
    老文章不会被 ``merge_articles`` 碰到，``fetched_ts`` 就停在上一轮 ——
    「最近拉取」筛选会突然只剩最新几页。这里纯本地补标记（不发任何请求），
    让该筛选的含义与改造前保持一致。窗口判定与 ``fetch_history_range`` 一致。
    """
    with _lock:
        key = str(account_id)
        if key not in _articles:
            _load_articles_from_disk(key)
        rows = _articles.get(key, [])
        stamp = int(fetched_ts)
        touched = 0
        for r in rows:
            ts = int(r.get("publish_ts") or 0)
            if ts and ts < start_ts:
                continue
            if end_ts and ts and ts > end_ts:
                continue
            if int(r.get("fetched_ts") or 0) < stamp:
                r["fetched_ts"] = stamp
                touched += 1
        if touched:
            _save_articles_to_disk(key)
        return touched


def get_last_fetch_ts(account_id: str) -> int:
    """该账号最近一次拉取的时间戳（0 = 从未拉取/旧缓存）。"""
    with _lock:
        key = str(account_id)
        if key not in _last_fetch_ts and key not in _articles:
            _load_articles_from_disk(key)
        return _last_fetch_ts.get(key, 0)


def time_filter(
    articles: list[dict[str, Any]],
    *,
    start_ts: int = 0,
    end_ts: int = 0,
    latest_ts: int = 0,
) -> list[dict[str, Any]]:
    """按时间筛选文章（纯函数）。

    latest_ts > 0：只留 fetched_ts >= latest_ts 的行（最近一次拉取）；
    start_ts/end_ts：按 publish_ts 闭区间过滤（publish_ts 未知的行保留）。
    """
    out: list[dict[str, Any]] = []
    for a in articles or []:
        if latest_ts and int(a.get("fetched_ts") or 0) < latest_ts:
            continue
        ts = int(a.get("publish_ts") or 0)
        if ts:
            if start_ts and ts < start_ts:
                continue
            if end_ts and ts > end_ts:
                continue
        out.append(a)
    return out


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


_PLAIN_VERDICT_FIELDS = (
    "keep",
    "reason",
    "category",
    "relevance_score",
    "technical_depth",
    "confidence",
    "at",
    "model",
)
_VERDICT_FIELDS = frozenset(
    _PLAIN_VERDICT_FIELDS
    + tuple(f"{p}{f}" for p in ("title_", "content_") for f in _PLAIN_VERDICT_FIELDS)
)


def _verdict_only(row: dict[str, Any]) -> dict[str, Any]:
    """只取判定字段。

    原先合并的是**整行快照**（``{k: v for k, v in j.items() if not k.startswith("_")}``），
    而 AI 判定行里带着 title/link/publish_at 等抓取期快照 —— 判定跑的同时
    后台拉取刚刷新过这些字段的话，会被旧值回滚（2026-09 修复）。
    """
    return {k: v for k, v in row.items() if k in _VERDICT_FIELDS}


def append_article(account_id: str, row: dict[str, Any]) -> bool:
    """把单篇文章追加进缓存（在同一把锁内完成，返回是否新增）。

    补录走「读快照 → append → 整体写回」时，若期间有后台拉取写入了新文章，
    写回会把它丢掉（内存和磁盘一起丢）。这里把读-改-写收进锁内（2026-09 修复）。
    """
    key_of = lambda a: str(a.get("identity") or a.get("link") or "")  # noqa: E731
    biz = _account_biz(account_id)
    with _lock:
        key = str(account_id)
        if key not in _articles:
            _load_articles_from_disk(key)
        rows = _articles.setdefault(key, [])
        k = key_of(row)
        if k and any(key_of(r) == k for r in rows):
            return False
        new = dict(row)
        if biz and not new.get("__biz"):
            new["__biz"] = biz
        rows.append(new)
        rows.sort(key=lambda r: int(r.get("publish_ts") or 0), reverse=True)
        _save_articles_to_disk(key)
        return True


def merge_article_verdicts(account_id: str, judged: list[dict[str, Any]]) -> None:
    """把 AI 判定字段（title_*/content_*/keep 等）按 identity/link 合并回缓存。

    合并后自动计算最终 ``keep/reason``：内容筛选有结果时优先生效，否则用标题筛选
    结果；两阶段都没有则保持未判定。
    """
    key_of = lambda a: str(a.get("identity") or a.get("link") or "")  # noqa: E731
    with _lock:
        key = str(account_id)
        if key not in _articles:
            _load_articles_from_disk(key)
        rows = _articles.get(key)
        if not rows:
            return
        verdicts = {key_of(j): _verdict_only(j) for j in judged if key_of(j)}
        for row in rows:
            v = verdicts.get(key_of(row))
            if v:
                row.update(v)
            content_keep = row.get("content_keep")
            title_keep = row.get("title_keep")
            if content_keep is not None:
                row["keep"] = content_keep
                row["reason"] = str(row.get("content_reason") or "")
            elif title_keep is not None:
                row["keep"] = title_keep
                row["reason"] = str(row.get("title_reason") or "")
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


def _account_of(row: dict[str, Any]) -> str:
    return str(row.get("_account_id") or row.get("account_id") or "")


def merge_article_verdicts_by_account(judged: list[dict[str, Any]]) -> None:
    """把 AI 判定结果按 _account_id 分账号合并回缓存（支持全部公众号批量筛选）。"""
    by_account: dict[str, list[dict[str, Any]]] = {}
    for j in judged:
        aid = _account_of(j)
        if aid:
            by_account.setdefault(aid, []).append(j)
    for aid, rows in by_account.items():
        merge_article_verdicts(aid, rows)


def merge_article_bodies_by_account(bodies: list[dict[str, Any]]) -> None:
    """把正文拉取结果按 _account_id 分账号合并回缓存。"""
    by_account: dict[str, list[dict[str, Any]]] = {}
    for b in bodies:
        aid = _account_of(b)
        if aid:
            by_account.setdefault(aid, []).append(b)
    for aid, rows in by_account.items():
        merge_article_bodies(aid, rows)


def drop_articles(account_id: str) -> None:
    """删除账号时清空内存与磁盘文章缓存。"""
    with _lock:
        key = str(account_id)
        _articles.pop(key, None)
        _last_days.pop(key, None)
        _last_fetch_ts.pop(key, None)
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
        _last_fetch_ts.clear()


# ── 定向复位（「设置 → 存储占用」清理本地数据后调用）──────────────────
#
# 与 reset() 的区别：reset() 是测试用的**全清**，这里按项清。
#
# 为什么非清不可：删磁盘文件**不影响进程里的内存副本**，而各模块会在下一次保存
# 时把内存里的东西写回文件 —— 于是「清了又长回来」，而且只长回被碰到的那部分，
# 看起来像没清干净。实测：删光 29 个文章缓存文件后做一次普通操作，文件就回来了。
#
# 名字要与 core.storage._MEMORY_RESET 里登记的字符串对上（有测试钉住）。


def forget_articles() -> None:
    """丢掉全部文章内存缓存（磁盘文件由存储清理负责删）。"""
    with _lock:
        _articles.clear()
        _last_days.clear()
        _last_fetch_ts.clear()


def forget_store() -> None:
    """丢掉账号 store 单例。下次 get_store() 重新从磁盘读 —— 文件没了就是空的。"""
    global _store
    with _lock:
        _store = None


def forget_mitm() -> None:
    """丢掉抓包服务单例。CA 被删后要重新构造，不能继续用内存里那份旧证书。"""
    global _mitm
    with _lock:
        _mitm = None


def forget_external() -> None:
    """丢掉外部来源库单例（真身在 core，这里只是给路由一个统一入口）。"""
    from mp_harvest.core import external_sources as ext_mod

    ext_mod.reset_external_store()
