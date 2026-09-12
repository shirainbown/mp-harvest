"""正文 HTML 导出记录（SQLite，data/harvest.db）。

同一篇文章任何批次/视图重跑都映射同一文件名与记录（幂等，重跑即覆盖），
目录页 ``index.html`` 由本表按账号/目录生成，跨批次累积不丢历史。

设计要点：
- 连接级容错：任何 SQLite 故障都不阻断导出主流程（记录失败仅丢幂等加速）；
- 除任务书最小列（article_id/account_id/out_path/sha256/exported_at/bytes）外，
  附带 title/link/publish_ts/account_name 列，目录页无需回查文章缓存即可渲染。
"""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exports (
    article_id   TEXT NOT NULL,
    account_id   TEXT NOT NULL DEFAULT '',
    account_name TEXT NOT NULL DEFAULT '',
    title        TEXT NOT NULL DEFAULT '',
    link         TEXT NOT NULL DEFAULT '',
    publish_ts   INTEGER NOT NULL DEFAULT 0,
    keep         INTEGER,
    reason       TEXT NOT NULL DEFAULT '',
    out_path     TEXT NOT NULL,
    sha256       TEXT NOT NULL DEFAULT '',
    exported_at  INTEGER NOT NULL,
    bytes        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (article_id, out_path)
)
"""


def _default_db_path() -> Path:
    from mp_harvest.infra.platform import paths

    return paths.data_dir() / "harvest.db"


class ExportRecords:
    """导出记录表（单连接 + 锁；所有方法异常容错）。"""

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
            conn.execute(_SCHEMA)
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

    # ── 最小 API ──────────────────────────────────────────────────

    def record_export(
        self,
        *,
        article_id: str,
        out_path: str | Path,
        sha256: str = "",
        account_id: str = "",
        account_name: str = "",
        title: str = "",
        link: str = "",
        publish_ts: int = 0,
        keep: bool | None = None,
        reason: str = "",
        exported_at: int | None = None,
        bytes_count: int = 0,
    ) -> bool:
        """登记/覆盖一条导出记录（主键 article_id+out_path）。失败返回 False。"""
        try:
            with self._lock:
                self._connect().execute(
                    "INSERT OR REPLACE INTO exports "
                    "(article_id, account_id, account_name, title, link, publish_ts,"
                    " keep, reason, out_path, sha256, exported_at, bytes) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        str(article_id),
                        str(account_id or ""),
                        str(account_name or ""),
                        str(title or ""),
                        str(link or ""),
                        int(publish_ts or 0),
                        None if keep is None else (1 if keep else 0),
                        str(reason or ""),
                        str(out_path),
                        str(sha256 or ""),
                        int(exported_at if exported_at is not None else time.time()),
                        int(bytes_count or 0),
                    ),
                )
                self._conn.commit()
            return True
        except Exception:  # noqa: BLE001
            return False

    def find_export(self, article_id: str, out_path: str | Path) -> dict[str, Any] | None:
        """按 (article_id, out_path) 查记录；不存在/出错返回 None。"""
        try:
            with self._lock:
                row = self._connect().execute(
                    "SELECT * FROM exports WHERE article_id=? AND out_path=?",
                    (str(article_id), str(out_path)),
                ).fetchone()
            return dict(row) if row else None
        except Exception:  # noqa: BLE001
            return None

    def find_by_article(self, article_id: str) -> dict[str, Any] | None:
        """按 article_id 查已导出记录（优先返回文件仍存在的最近一条）。

        幂等跳过应以**文章**为单位，而不是 (文章, 文件名)：标题或链接参数漂移
        会算出新文件名，只按 (article_id, out_path) 查会当成新文章重复导出
        （2026-09 修复）。
        """
        try:
            with self._lock:
                rows = self._connect().execute(
                    "SELECT * FROM exports WHERE article_id=? ORDER BY exported_at DESC",
                    (str(article_id),),
                ).fetchall()
        except Exception:  # noqa: BLE001
            return None
        if not rows:
            return None
        for r in rows:
            if Path(str(r["out_path"])).is_file():
                return dict(r)
        return dict(rows[0])

    def list_exports(
        self,
        account_id: str | None = None,
        *,
        out_dir: str | Path | None = None,
    ) -> list[dict[str, Any]]:
        """列出导出记录（可按账号/目录过滤），按 publish_ts desc、exported_at desc。"""
        try:
            sql = "SELECT * FROM exports"
            cond: list[str] = []
            args: list[Any] = []
            if account_id:
                cond.append("account_id=?")
                args.append(str(account_id))
            if out_dir is not None:
                cond.append("out_path LIKE ? ESCAPE '\\'")
                # LIKE 的 _ / % 是通配符，必须转义，否则 out_dir 含 "_" 时
                # 会把同形兄弟目录（a_c vs abc）的记录也捞进来（2026-09 修复）
                prefix = str(out_dir).rstrip("/")
                prefix = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                args.append(prefix + "/%")
            if cond:
                sql += " WHERE " + " AND ".join(cond)
            sql += " ORDER BY publish_ts DESC, exported_at DESC"
            with self._lock:
                rows = self._connect().execute(sql, args).fetchall()
            return [dict(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def exported_article_ids(self) -> set[str]:
        """**文件仍然存在**的已导出文章 id 集合（供列表页显示「已导出」）。

        只按 ``article_id`` 归并：同一篇导出过多次（改了标题/换了目录）在这里
        只算一条，与 ``find_by_article`` 的「以文章为单位」口径一致。

        ⚠️ **必须逐个确认文件还在**。用户会把导出的 HTML 删掉，而记录还在
        ——2026-09 用户报的正是这个：删了导出材料，界面毫无变化。若返回
        「有记录」而不是「有文件」，列表就会一直挂着「已导出」而本地空空如也，
        比不显示更误导。

        （原 `remove_missing()` 想做同一件事，靠**删记录**让列表变干净；但它
        从来没被调用过，而且删记录是写操作、不该挂在读路径上。判定文件是否
        存在本来就该在**读**的时候做，于是删掉那个方法，只留这一个。）
        """
        out: set[str] = set()
        try:
            with self._lock:
                rows = self._connect().execute(
                    "SELECT DISTINCT article_id, out_path FROM exports"
                ).fetchall()
        except Exception:  # noqa: BLE001
            return out
        for r in rows:
            aid = str(r["article_id"] or "")
            if aid and Path(str(r["out_path"])).is_file():
                out.add(aid)
        return out


# ── 进程内单例（惰性；测试可 reset_records / 传显式路径）──────────────

_records: ExportRecords | None = None
_records_lock = threading.Lock()


def get_records(db_path: str | Path | None = None) -> ExportRecords:
    """导出记录单例；显式传 db_path 时创建独立实例（测试用）。"""
    global _records
    if db_path is not None:
        return ExportRecords(db_path)
    with _records_lock:
        if _records is None:
            _records = ExportRecords(_default_db_path())
        return _records


def reset_records() -> None:
    """清掉单例（测试隔离用）。"""
    global _records
    with _records_lock:
        if _records is not None:
            _records.close()
        _records = None
