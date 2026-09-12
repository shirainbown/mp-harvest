"""「其他来源」目录：扫描 → 建库 → 写回（SQLite，data/external.db）。

MP 原先只管微信公众号文章。本模块管另一类内容：用户手动登记一个目录，
其下按 ``YYYY-MM-DD/`` 分日期子目录存放外部来源的文章（典型是 arXiv 论文
流水线产出的 ``papers_data.json``）。扫进库后可在应用里浏览/搜索/AI 筛选/导出，
也能按同样的格式写回目录。

设计要点：
- **独立 DB 文件**（``data/external.db``）而不复用 ``harvest.db``：外部索引是
  「目录的可重建缓存」，导出记录不是，生命周期不同，分开放便于单独删除重建。
- **连接级容错**：任何 SQLite 故障都不阻断主流程（仿 ``core/export_records.py``）。
- **AI 判定不存这里** —— 复用 ``data/ai_filter_cache.json`` 那一套（键是
  ``article_key()`` 的 identity），天然持久、跨数据源共享，同一篇论文只判一次。
- ``core/`` 不 import ``server/``：进度与取消用注入的 ``on_progress`` /
  ``check_cancelled``（与 ``batch_export_articles`` 同一约定）。

``papers_data.json`` 有**两种变体**，都必须能读（实测同一目录里就并存）：
- 变体 A 扁平 list：``[{title, abstract, url, arxiv_id, …}]``
- 变体 B 按领域分组：``{"domains": {"领域名": [{…, title_cn, summary_cn, pdf_local_path}]}}``
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    path            TEXT NOT NULL UNIQUE,
    enabled         INTEGER NOT NULL DEFAULT 1,
    added_at        INTEGER NOT NULL DEFAULT 0,
    last_scan_at    INTEGER NOT NULL DEFAULT 0,
    last_scan_seen  INTEGER NOT NULL DEFAULT 0,
    last_scan_new   INTEGER NOT NULL DEFAULT 0,
    last_scan_error TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS items (
    source_id        TEXT NOT NULL,
    item_key         TEXT NOT NULL,
    dir_date         TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL DEFAULT '',
    title_cn         TEXT NOT NULL DEFAULT '',
    abstract         TEXT NOT NULL DEFAULT '',
    summary_cn       TEXT NOT NULL DEFAULT '',
    url              TEXT NOT NULL DEFAULT '',
    arxiv_id         TEXT NOT NULL DEFAULT '',
    authors          TEXT NOT NULL DEFAULT '[]',
    categories       TEXT NOT NULL DEFAULT '[]',
    primary_category TEXT NOT NULL DEFAULT '',
    domain           TEXT NOT NULL DEFAULT '',
    publish_ts       INTEGER NOT NULL DEFAULT 0,
    body_path        TEXT NOT NULL DEFAULT '',
    pdf_path         TEXT NOT NULL DEFAULT '',
    origin_file      TEXT NOT NULL DEFAULT '',
    seen_at          INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source_id, item_key)
);

CREATE INDEX IF NOT EXISTS idx_ext_items_date ON items(source_id, publish_ts DESC);
"""

# 日期子目录名（YYYY-MM-DD），只认这一种形态
DATE_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# URL 里常见的跟踪参数，参与 item_key 计算前先剔除（否则同一篇会算出不同键）
_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "ref_src", "spm", "from", "share_token", "fbclid", "gclid",
}

# 文件名里不能出现的字符（与 article_reader.safe_export_filename 同一套）
_UNSAFE_FILENAME_RE = re.compile(r'[\\/:*?"<>|\s]+')

# arXiv 版本后缀（v1 / v2 …）：参与 item_key 前剥掉，见 item_key_for
_ARXIV_VERSION_RE = re.compile(r"v\d+$", re.IGNORECASE)


# ── 解析 ──────────────────────────────────────────────────────────


def normalize_url(url: str) -> str:
    """去掉 fragment 与跟踪参数、去尾斜杠，得到稳定的 URL 形式。"""
    raw = str(url or "").strip()
    if not raw:
        return ""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    try:
        parts = urlsplit(raw)
    except Exception:  # noqa: BLE001
        return raw
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in _TRACKING_PARAMS]
    path = parts.path.rstrip("/") or parts.path
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(query), ""))


def item_key_for(rec: dict[str, Any]) -> str:
    """条目的全局稳定键：``arxiv_id`` > 规范化 ``url`` > ``domain|title`` 摘要。

    全局而非按目录，是为了让同一篇论文在不同日期目录、甚至不同登记目录下
    都算作同一条 —— 既避免重复入库，也让 AI 判定只做一次
    （判定缓存以 identity 为键）。
    """
    arxiv_id = str(rec.get("arxiv_id") or "").strip()
    if arxiv_id:
        # 剥掉版本号：同一篇论文改版（v1→v2）在 arXiv 上算**同一篇**，而流水线
        # 会把它当新记录再次报告 —— 不剥就变成两行、两次 AI 判定、导出两份。
        # 完整带版本的 ID 仍原样存在 items.arxiv_id 与 items.url 里，信息不丢。
        return f"arxiv:{_ARXIV_VERSION_RE.sub('', arxiv_id).lower()}"
    url = normalize_url(str(rec.get("url") or ""))
    if url:
        return f"url:{url}"
    seed = f"{rec.get('domain') or ''}|{rec.get('title') or ''}".encode("utf-8", "ignore")
    return f"title:{hashlib.sha1(seed).hexdigest()[:16]}"


def _as_str_list(value: Any) -> list[str]:
    """``authors`` / ``categories`` 容错：可能是 list，也可能是逗号串或 None。"""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,;，、]", value) if p.strip()]
    return []


def _publish_ts(date_str: Any, fallback_file_date: str = "") -> int:
    """把条目里的 ``date``（YYYY-MM-DD）转成 epoch 秒；取不到就用日期目录名。"""
    for candidate in (date_str, fallback_file_date):
        raw = str(candidate or "").strip()
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})", raw)
        if not m:
            continue
        try:
            return int(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).timestamp())
        except Exception:  # noqa: BLE001
            continue
    return 0


def normalize_record(raw: dict[str, Any], *, fallback_domain: str = "",
                     fallback_date: str = "") -> dict[str, Any] | None:
    """单条原始 JSON → 内部记录；既无标题也无 URL 的条目丢弃（非文章）。"""
    if not isinstance(raw, dict):
        return None
    rec = {
        "title": str(raw.get("title") or "").strip(),
        "title_cn": str(raw.get("title_cn") or "").strip(),
        "abstract": str(raw.get("abstract") or "").strip(),
        "summary_cn": str(raw.get("summary_cn") or "").strip(),
        "url": normalize_url(str(raw.get("url") or raw.get("link") or "")),
        "arxiv_id": str(raw.get("arxiv_id") or "").strip(),
        # 变体 B 的领域来自分组 key，条目自身往往没有这个字段
        "domain": str(raw.get("domain") or fallback_domain or "").strip(),
        "primary_category": str(raw.get("primary_category") or "").strip(),
        "authors": _as_str_list(raw.get("authors")),
        "categories": _as_str_list(raw.get("categories")),
        "pdf_local_path": str(raw.get("pdf_local_path") or "").strip(),
    }
    if not rec["title"] and not rec["url"]:
        return None
    rec["publish_ts"] = _publish_ts(raw.get("date"), fallback_date)
    rec["item_key"] = item_key_for(rec)
    return rec


def parse_papers_data(path: str | Path, *, fallback_date: str = "") -> list[dict[str, Any]]:
    """读一个 ``papers_data.json``，兼容三种顶层形态。

    - 扁平 list（变体 A）
    - ``{"domains": {"领域": [...]}}``（变体 B）—— 把分组 key 回填成每条 ``domain``
    - ``{"papers": [...]}``（防御性：其它工具可能这么写）

    文件不存在 / 解析失败 / 形态不认识，一律返回空列表（调用方按「这天的目录没内容」处理）。
    """
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return []

    out: list[dict[str, Any]] = []
    if isinstance(data, list):
        for raw in data:
            rec = normalize_record(raw, fallback_date=fallback_date)
            if rec:
                out.append(rec)
        return out

    if isinstance(data, dict):
        domains = data.get("domains")
        if isinstance(domains, dict):
            for domain, rows in domains.items():
                if not isinstance(rows, list):
                    continue
                for raw in rows:
                    rec = normalize_record(raw, fallback_domain=str(domain),
                                            fallback_date=fallback_date)
                    if rec:
                        out.append(rec)
            return out
        papers = data.get("papers")
        if isinstance(papers, list):
            for raw in papers:
                rec = normalize_record(raw, fallback_date=fallback_date)
                if rec:
                    out.append(rec)
    return out


def safe_id(item_key: str) -> str:
    """``item_key`` → 可用作文件名的短标识（arXiv 条目就是 ``2608.26575v1``）。

    净化后为空则退回键的摘要。
    """
    payload = item_key.split(":", 1)[1] if ":" in item_key else item_key
    cleaned = _UNSAFE_FILENAME_RE.sub("_", payload).strip("_")
    if not cleaned:
        return hashlib.sha1(item_key.encode("utf-8")).hexdigest()[:16]
    return cleaned[:80]


def body_filename(rec: dict[str, Any]) -> str:
    """条目的正文文件名。

    **优先用完整的 arxiv_id**（含版本号），这样正文与同目录的 ``{arxiv_id}.pdf``
    一一对齐，``ls`` 一眼能看出谁是谁。不能直接用 ``safe_id(item_key)``：
    ``item_key`` 为了去重剥掉了版本号，会算出 ``2608.25061.html`` 而对面的
    PDF 叫 ``2608.25061v2.pdf`` —— 名对不上号。

    没有 arxiv_id 的条目（非 arXiv 来源）退回 ``safe_id(item_key)``。
    """
    arxiv_id = str(rec.get("arxiv_id") or "").strip()
    if arxiv_id:
        stem = _UNSAFE_FILENAME_RE.sub("_", arxiv_id).strip("_")
        if stem:
            return f"{stem[:80]}.html"
    return f"{safe_id(str(rec.get('item_key') or ''))}.html"


# ── 正文 ──────────────────────────────────────────────────────────


def read_external_body(item: dict[str, Any]) -> str:
    """条目的正文：本地正文文件 → ``summary_cn`` → ``abstract``。

    外部条目的一个天然优势是**不用联网抓正文** —— 正文要么已经在目录里
    （写回时产出的自包含 HTML，内容是摘要），要么摘要本身就是可判定的内容
    （arXiv 摘要信息量足够）。AI 内容筛选与周报都用它。

    2026-09 从两处**逐字相同**的实现（``server/routes/external.py`` 与
    ``core/weekly_report.py``）合并到这里 —— 外部来源的正文规则只该有一份。
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


# ── 存储 ──────────────────────────────────────────────────────────


def _default_db_path() -> Path:
    from mp_harvest.infra.platform import paths

    return paths.data_dir() / "external.db"


def normalize_path(path: str | Path) -> str:
    """统一成 realpath 绝对路径。

    macOS 上 ``/tmp`` 与 ``/private/tmp`` 是同一个地方，用户手输的路径还可能带
    ``~`` 或 ``..``；不归一化会把同一个目录登记成两行，聚合视图里条目全翻倍。
    """
    import os

    raw = str(path or "").strip()
    if not raw:
        return ""
    try:
        return os.path.realpath(os.path.expanduser(raw))
    except Exception:  # noqa: BLE001
        return raw


class ExternalStore:
    """登记目录 + 已索引条目（单连接 + 锁；所有方法异常容错）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ── 连接管理 ──────────────────────────────────────────────────

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:  # noqa: BLE001
                    pass
                self._conn = None

    # ── 目录登记 ──────────────────────────────────────────────────

    def list_sources(self) -> list[dict[str, Any]]:
        """所有登记的目录（含条目数），按加入时间升序。"""
        try:
            with self._lock:
                rows = self._connect().execute(
                    "SELECT s.*, (SELECT COUNT(*) FROM items i WHERE i.source_id = s.id)"
                    " AS item_count FROM sources s ORDER BY s.added_at ASC"
                ).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_source(self, source_id: str) -> dict[str, Any] | None:
        """单个来源（**形状与 list_sources 一致，含 item_count**）。

        前端拿到来源对象的地方不止一处（登记 / 改名 / 列表），形状不一致的话
        改名后条目数会凭空消失 —— 统一在这里补上。
        """
        try:
            with self._lock:
                row = self._connect().execute(
                    "SELECT s.*, (SELECT COUNT(*) FROM items i WHERE i.source_id = s.id)"
                    " AS item_count FROM sources s WHERE s.id=?",
                    (str(source_id),),
                ).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def find_source_by_path(self, path: str | Path) -> dict[str, Any] | None:
        try:
            with self._lock:
                row = self._connect().execute(
                    "SELECT * FROM sources WHERE path=?", (normalize_path(path),)
                ).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def add_source(self, path: str | Path, name: str = "") -> dict[str, Any] | None:
        """登记一个目录；路径已存在则返回已有记录（幂等，不重复登记）。

        路径不存在时返回 ``None`` —— 调用方据此报错，避免登记一个扫不出东西的目录。
        """
        resolved = Path(normalize_path(path))
        if not resolved.is_dir():
            return None
        existing = self.find_source_by_path(resolved)
        if existing:
            return existing
        import uuid

        sid = str(uuid.uuid4())
        try:
            with self._lock:
                conn = self._connect()
                conn.execute(
                    "INSERT INTO sources (id, name, path, enabled, added_at) VALUES (?,?,?,1,?)",
                    (sid, str(name or resolved.name), str(resolved), int(time.time())),
                )
                conn.commit()
        except Exception:  # noqa: BLE001
            return None
        return self.get_source(sid)

    def update_source(self, source_id: str, *, name: str | None = None,
                      enabled: bool | None = None) -> dict[str, Any] | None:
        sets: list[str] = []
        args: list[Any] = []
        if name is not None:
            sets.append("name=?")
            args.append(str(name))
        if enabled is not None:
            sets.append("enabled=?")
            args.append(1 if enabled else 0)
        if not sets:
            return self.get_source(source_id)
        args.append(str(source_id))
        try:
            with self._lock:
                conn = self._connect()
                conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id=?", args)
                conn.commit()
        except Exception:  # noqa: BLE001
            return None
        return self.get_source(source_id)

    def remove_source(self, source_id: str) -> bool:
        """移除登记目录**及其索引条目**；磁盘上的目录与文件一概不动。"""
        try:
            with self._lock:
                conn = self._connect()
                conn.execute("DELETE FROM items WHERE source_id=?", (str(source_id),))
                cur = conn.execute("DELETE FROM sources WHERE id=?", (str(source_id),))
                conn.commit()
                return cur.rowcount > 0
        except Exception:  # noqa: BLE001
            return False

    def mark_scanned(self, source_id: str, *, seen: int, new: int, error: str = "") -> None:
        try:
            with self._lock:
                conn = self._connect()
                conn.execute(
                    "UPDATE sources SET last_scan_at=?, last_scan_seen=?, last_scan_new=?,"
                    " last_scan_error=? WHERE id=?",
                    (int(time.time()), int(seen), int(new), str(error), str(source_id)),
                )
                conn.commit()
        except Exception:  # noqa: BLE001
            pass

    # ── 条目 ──────────────────────────────────────────────────────

    _ITEM_COLS = (
        "source_id", "item_key", "dir_date", "title", "title_cn", "abstract", "summary_cn",
        "url", "arxiv_id", "authors", "categories", "primary_category", "domain",
        "publish_ts", "body_path", "pdf_path", "origin_file", "seen_at",
    )

    def upsert_item(self, source_id: str, rec: dict[str, Any], *,
                    origin_file: str = "", dir_date: str = "",
                    body_path: str = "", pdf_path: str = "") -> bool:
        """按 ``(source_id, item_key)`` 插入或更新一条；成功返回 True。

        ``dir_date`` 取自所在日期子目录。同一篇出现在多个日期目录时，
        由调用方（``scan_source``）保证最终留下的是**最新**那个。
        """
        try:
            with self._lock:
                conn = self._connect()
                conn.execute(
                    "INSERT OR REPLACE INTO items ("
                    + ", ".join(self._ITEM_COLS)
                    + ") VALUES (" + ", ".join("?" * len(self._ITEM_COLS)) + ")",
                    (
                        str(source_id),
                        str(rec.get("item_key") or ""),
                        str(dir_date or ""),
                        str(rec.get("title") or ""),
                        str(rec.get("title_cn") or ""),
                        str(rec.get("abstract") or ""),
                        str(rec.get("summary_cn") or ""),
                        str(rec.get("url") or ""),
                        str(rec.get("arxiv_id") or ""),
                        json.dumps(rec.get("authors") or [], ensure_ascii=False),
                        json.dumps(rec.get("categories") or [], ensure_ascii=False),
                        str(rec.get("primary_category") or ""),
                        str(rec.get("domain") or ""),
                        int(rec.get("publish_ts") or 0),
                        str(body_path or ""),
                        str(pdf_path or ""),
                        str(origin_file or ""),
                        int(time.time()),
                    ),
                )
                conn.commit()
            return True
        except Exception:  # noqa: BLE001
            return False

    def list_items(self, source_id: str = "", *, q: str = "", start_ts: int = 0,
                   end_ts: int = 0, order: str = "desc") -> list[dict[str, Any]]:
        """列出条目；``source_id`` 为空 = 所有已启用目录。

        ``q`` 在标题/中文标题/摘要/作者/领域里做子串匹配（LIKE 通配符已转义）。
        """
        sql = "SELECT i.*, s.name AS source_name, s.path AS source_path FROM items i" \
              " LEFT JOIN sources s ON s.id = i.source_id"
        cond: list[str] = []
        args: list[Any] = []
        if source_id:
            cond.append("i.source_id=?")
            args.append(str(source_id))
        else:
            cond.append("COALESCE(s.enabled, 1)=1")
        if q:
            needle = "%" + str(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            cond.append(
                "(i.title LIKE ? ESCAPE '\\' OR i.title_cn LIKE ? ESCAPE '\\'"
                " OR i.abstract LIKE ? ESCAPE '\\' OR i.authors LIKE ? ESCAPE '\\'"
                " OR i.domain LIKE ? ESCAPE '\\')"
            )
            args.extend([needle] * 5)
        if start_ts:
            cond.append("i.publish_ts>=?")
            args.append(int(start_ts))
        if end_ts:
            cond.append("i.publish_ts<=?")
            args.append(int(end_ts))
        sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY i.publish_ts " + ("ASC" if str(order).lower() == "asc" else "DESC")
        try:
            with self._lock:
                rows = self._connect().execute(sql, args).fetchall()
            return [self._row_to_item(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def get_items(self, source_id: str, keys: Iterable[str]) -> list[dict[str, Any]]:
        """按 item_key 精确取一批（导出/AI 筛选按选中项时用）。"""
        wanted = [str(k) for k in keys]
        if not wanted:
            return []
        try:
            with self._lock:
                conn = self._connect()
                out: list[dict[str, Any]] = []
                for chunk_start in range(0, len(wanted), 500):
                    chunk = wanted[chunk_start:chunk_start + 500]
                    rows = conn.execute(
                        "SELECT i.*, s.name AS source_name, s.path AS source_path FROM items i"
                        " LEFT JOIN sources s ON s.id = i.source_id WHERE i.source_id=?"
                        " AND i.item_key IN (" + ", ".join("?" * len(chunk)) + ")",
                        [str(source_id), *chunk],
                    ).fetchall()
                    out.extend(self._row_to_item(r) for r in rows)
            return out
        except Exception:  # noqa: BLE001
            return []

    def remove_missing_files(self, source_id: str) -> int:
        """清掉 ``origin_file`` 已不存在的条目（目录里被删掉后重扫要跟着消失）。"""
        removed = 0
        try:
            with self._lock:
                conn = self._connect()
                rows = conn.execute(
                    "SELECT item_key, origin_file FROM items WHERE source_id=?",
                    (str(source_id),),
                ).fetchall()
                for r in rows:
                    origin = str(r["origin_file"] or "")
                    if origin and not Path(origin).is_file():
                        conn.execute(
                            "DELETE FROM items WHERE source_id=? AND item_key=?",
                            (str(source_id), str(r["item_key"])),
                        )
                        removed += 1
                conn.commit()
        except Exception:  # noqa: BLE001
            return 0
        return removed

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> dict[str, Any]:
        """DB 行 → 内部条目 dict（JSON 列解回 list）。"""
        d = dict(row)
        for col in ("authors", "categories"):
            try:
                parsed = json.loads(d.get(col) or "[]")
                d[col] = parsed if isinstance(parsed, list) else []
            except Exception:  # noqa: BLE001
                d[col] = []
        return d


# ── 扫描 ──────────────────────────────────────────────────────────


def _date_dirs(root: Path) -> list[str]:
    """``root`` 下所有形如 ``YYYY-MM-DD`` 的子目录名，升序。"""
    try:
        names = [p.name for p in root.iterdir() if p.is_dir() and DATE_DIR_RE.match(p.name)]
    except Exception:  # noqa: BLE001
        return []
    return sorted(names)


def _find_pdf(source_root: Path, date_dir: Path, rec: dict[str, Any]) -> str:
    """定位本地 PDF。

    ``pdf_local_path`` 的**相对基准两种都存在**，必须都试（实测）：
    - 相对日期目录：``"2609.10057v1.pdf"``（服务器新格式）
    - 相对来源根目录：``"2026-08-10/2608.07078v1.pdf"``（本地 2026-08-10 就是这样）

    只按日期目录拼会算成 ``2026-08-10/2026-08-10/xxx.pdf`` 而漏掉，
    最后靠 ``{arxiv_id}.pdf`` 兜底才碰巧命中 —— 一旦 PDF 名与 arxiv_id 不同就彻底找不到。
    """
    rel = str(rec.get("pdf_local_path") or "").strip()
    if rel:
        for candidate in (Path(rel), source_root / rel, date_dir / rel):
            try:
                if candidate.is_file():
                    return str(candidate)
            except Exception:  # noqa: BLE001
                continue
    arxiv_id = str(rec.get("arxiv_id") or "").strip()
    if arxiv_id:
        candidate = date_dir / f"{arxiv_id}.pdf"
        if candidate.is_file():
            return str(candidate)
    return ""


def scan_source(
    store: ExternalStore,
    source_id: str,
    *,
    on_progress: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """扫描一个登记目录下的日期子目录，把 ``papers_data.json`` 索引进库。

    幂等：同一篇重复扫到只更新不重复；跨日期目录重复时**保留最新的 dir_date**。
    扫完顺带清掉文件已不存在的条目。返回 ``{ok, seen, new, removed, error}``。
    """
    src = store.get_source(source_id)
    if not src:
        return {"ok": False, "seen": 0, "new": 0, "removed": 0, "error": "目录未登记"}
    root = Path(str(src.get("path") or ""))
    if not root.is_dir():
        msg = f"目录不存在：{root}"
        store.mark_scanned(source_id, seen=0, new=0, error=msg)
        return {"ok": False, "seen": 0, "new": 0, "removed": 0, "error": msg}

    before = {it["item_key"] for it in store.list_items(source_id)}
    # 实际入过库的键（用于把 seen 报成「唯一条目数」，而不是「重复出现次数」）
    indexed: set[str] = set()

    try:
        for dir_name in _date_dirs(root):
            if check_cancelled:
                check_cancelled()
            date_dir = root / dir_name
            data_file = date_dir / "papers_data.json"
            if not data_file.is_file():
                continue
            records = parse_papers_data(data_file, fallback_date=dir_name)
            if on_progress:
                on_progress(f"{dir_name}：{len(records)} 条")
            for rec in records:
                if check_cancelled:
                    check_cancelled()
                key = str(rec.get("item_key") or "")
                if not key:
                    continue
                # 跨日期目录重复（arXiv 流水线会跨天重复报告同一篇）：靠
                # 「升序遍历 + 主键 INSERT OR REPLACE」让**最新**的日期目录最终胜出。
                # 顺序是这里的前提 —— _date_dirs 必须返回升序，改它就会静默改变语义。
                indexed.add(key)
                body_file = date_dir / body_filename(rec)
                store.upsert_item(
                    source_id,
                    rec,
                    origin_file=str(data_file),
                    dir_date=dir_name,
                    body_path=str(body_file) if body_file.is_file() else "",
                    pdf_path=_find_pdf(root, date_dir, rec),
                )
    except Exception as exc:  # noqa: BLE001
        # check_cancelled 抛出的取消异常也走这里：已入库的部分保留，如实上报
        msg = str(exc) or exc.__class__.__name__
        seen = len(indexed)
        removed = store.remove_missing_files(source_id)
        store.mark_scanned(source_id, seen=seen, new=0, error=msg)
        return {"ok": False, "seen": seen, "new": 0, "removed": removed, "error": msg}

    seen = len(indexed)
    removed = store.remove_missing_files(source_id)
    after = {it["item_key"] for it in store.list_items(source_id)}
    new = len(after - before)
    store.mark_scanned(source_id, seen=seen, new=new)
    return {"ok": True, "seen": seen, "new": new, "removed": removed, "error": ""}


# ── 写回 ──────────────────────────────────────────────────────────


def _export_dir_date(item: dict[str, Any]) -> str:
    """条目该落在哪个日期子目录：``dir_date`` → ``publish_ts`` → 今天。"""
    raw = str(item.get("dir_date") or "").strip()
    if DATE_DIR_RE.match(raw):
        return raw
    ts = int(item.get("publish_ts") or 0)
    if ts:
        try:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            pass
    return datetime.now().strftime("%Y-%m-%d")


def _item_to_papers_json(item: dict[str, Any]) -> dict[str, Any]:
    """内部条目 → ``papers_data.json`` 的一条（**变体 A 的字段集**）。

    与用户给的参考格式一致；解析器两种变体都读，所以往返无损。
    ``pdf_local_path`` 只在确实有本地 PDF 时写，且写相对日期目录的路径。
    """
    out: dict[str, Any] = {
        "title": str(item.get("title") or ""),
        "abstract": str(item.get("abstract") or ""),
        "url": str(item.get("url") or ""),
        "arxiv_id": str(item.get("arxiv_id") or ""),
        "authors": item.get("authors") or [],
        "categories": item.get("categories") or [],
        "primary_category": str(item.get("primary_category") or ""),
        "domain": str(item.get("domain") or ""),
    }
    ts = int(item.get("publish_ts") or 0)
    if ts:
        try:
            out["date"] = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            out["date"] = ""
    else:
        out["date"] = ""
    # 中文标题/摘要与 PDF 路径是变体 B 的字段，有则保留（不丢信息）
    if str(item.get("title_cn") or ""):
        out["title_cn"] = str(item["title_cn"])
    if str(item.get("summary_cn") or ""):
        out["summary_cn"] = str(item["summary_cn"])
    if str(item.get("pdf_path") or ""):
        out["pdf_local_path"] = Path(str(item["pdf_path"])).name
    return out


def _render_body_html(item: dict[str, Any]) -> str:
    """条目的正文文件内容：自包含 HTML（标题 + 元信息 + 摘要/正文）."""
    import html as html_mod

    def esc(v: Any) -> str:
        return html_mod.escape(str(v or ""), quote=True)

    title = str(item.get("title") or "").strip() or "(无标题)"
    title_cn = str(item.get("title_cn") or "").strip()
    url = str(item.get("url") or "").strip()
    authors = "、".join(str(a) for a in (item.get("authors") or []))
    domain = str(item.get("domain") or "").strip()
    body = str(item.get("summary_cn") or item.get("abstract") or "").strip()
    pub = ""
    ts = int(item.get("publish_ts") or 0)
    if ts:
        try:
            pub = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:  # noqa: BLE001
            pub = ""

    meta = " · ".join(p for p in (pub, authors, domain) if p)
    link = f'<p class="src"><a href="{esc(url)}" target="_blank">{esc(url)}</a></p>' if url else ""
    cn = f"<h2>{esc(title_cn)}</h2>" if title_cn else ""
    return (
        "<!DOCTYPE html><html lang=\"zh-CN\"><head><meta charset=\"UTF-8\">"
        f"<title>{esc(title)}</title>"
        "<style>body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',sans-serif;"
        "max-width:860px;margin:40px auto;padding:0 20px;line-height:1.75;color:#222}"
        "h1{font-size:24px;line-height:1.4;margin:0 0 8px}"
        "h2{font-size:17px;color:#444;margin:20px 0 8px}"
        ".meta{color:#888;font-size:13px;margin:0 0 4px}"
        ".src{font-size:13px;margin:0 0 20px;word-break:break-all}"
        "a{color:#1a56db;text-decoration:none}"
        ".abs{white-space:pre-wrap;border-top:1px solid #eee;padding-top:18px}</style></head>"
        f"<body><h1>{esc(title)}</h1>{cn}"
        f'<p class="meta">{esc(meta)}</p>{link}'
        f'<div class="abs">{esc(body)}</div></body></html>'
    )


def write_external_export(
    items: list[dict[str, Any]],
    out_dir: str | Path,
    *,
    on_progress: Callable[[str], None] | None = None,
    check_cancelled: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """把条目写进 ``out_dir``：每个日期子目录一份 ``papers_data.json`` + 逐篇正文。

    格式是**变体 A 扁平 list**（用户给的参考格式），字段与解析器对称，往返无损。

    **合并语义**：目标目录里已有的 ``papers_data.json`` 先读出来，按 ``item_key``
    建立索引，本次条目覆盖同键项、其余**原样保留** —— 绝不因为导出就把别人写的条目抹掉。
    返回 ``{ok, written, skipped, out_dir, error}``。
    """
    root = Path(str(out_dir)).expanduser()
    if not root.is_absolute():
        root = root.resolve()
    if not items:
        return {"ok": False, "written": 0, "skipped": 0, "out_dir": str(root), "error": "没有可导出的条目"}

    written = 0
    skipped = 0
    try:
        root.mkdir(parents=True, exist_ok=True)
        # 按日期子目录分组，减少 papers_data.json 的读写次数
        by_date: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            by_date.setdefault(_export_dir_date(item), []).append(item)

        total = len(items)
        for dir_date, group in by_date.items():
            if check_cancelled:
                check_cancelled()
            date_dir = root / dir_date
            date_dir.mkdir(parents=True, exist_ok=True)
            data_file = date_dir / "papers_data.json"

            # 已有条目：按 item_key 建索引（认不出的原样保留）
            merged: dict[str, dict[str, Any]] = {}
            for old in parse_papers_data(data_file, fallback_date=dir_date):
                merged[str(old.get("item_key") or "")] = old

            for item in group:
                if check_cancelled:
                    check_cancelled()
                key = str(item.get("item_key") or "")
                if not key:
                    skipped += 1
                    continue
                body_file = date_dir / body_filename(item)
                body_file.write_text(_render_body_html(item), encoding="utf-8")
                merged[key] = _item_to_papers_json(item)
                written += 1
                if on_progress:
                    on_progress(f"{str(item.get('title') or '')[:24]}（{written}/{total}）")

            payload = list(merged.values())
            tmp = data_file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(data_file)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc) or exc.__class__.__name__
        return {"ok": False, "written": written, "skipped": skipped,
                "out_dir": str(root), "error": msg}

    return {"ok": True, "written": written, "skipped": skipped, "out_dir": str(root), "error": ""}


# ── 进程内单例（惰性；测试可 reset_external_store / 传显式路径）────────

_store: ExternalStore | None = None
_store_lock = threading.Lock()


def get_external_store(db_path: str | Path | None = None) -> ExternalStore:
    """外部来源库单例；显式传 db_path 时创建独立实例（测试用）。"""
    global _store
    if db_path is not None:
        return ExternalStore(db_path)
    with _store_lock:
        if _store is None:
            _store = ExternalStore(_default_db_path())
        return _store


def reset_external_store() -> None:
    """清掉单例（测试隔离用）。"""
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None


__all__ = [
    "ExternalStore",
    "get_external_store",
    "reset_external_store",
    "scan_source",
    "write_external_export",
    "parse_papers_data",
    "read_external_body",
    "normalize_record",
    "normalize_url",
    "item_key_for",
    "safe_id",
    "body_filename",
    "normalize_path",
]
