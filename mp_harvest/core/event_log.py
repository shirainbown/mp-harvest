"""执行日志：把「发生过什么」落盘，供用户在应用内回看（2026-09）。

**为什么需要它**：打包版 ``packaging/MP_Harvest.spec`` 是 ``console=False`` ——
从 Finder 启动的 .app 没有控制台，代码里那些 ``print("[mp_harvest] …")``
**全部无处可去**（仓库里 ``import logging`` 更是零命中）。后台任务、错误、
AI 判定结果又都只活在内存里：关窗即失，前端刷新即清。用户跑完一期周报，
除了产物本身什么都看不到，出了问题无从判断。

**形态**：SQLite ``data/events.db``，照 ``core/export_records.py`` 的既有惯例 ——
单连接 + ``RLock`` + 每个方法 try/except 返回空值，**绝不阻断主流程**。
插入时按 id 剪枝到 :data:`LOG_KEEP` 行，不会无限膨胀。

**三条铁律**（都有测试钉住）：

1. **绝不抛异常、绝不阻塞** —— 日志写不进去也不能影响生成。
2. **绝不自噬** —— 内部失败只 ``print`` 一行到 stdout，**不再回调自身**，
   否则日志故障会变成递归。
3. **绝不写敏感内容** —— :func:`_redact` 递归清洗 ``api_key`` / ``Authorization``
   / ``token`` 这类键。本项目对凭证有历史教训（``accounts.json``、
   ``ai_models.json`` 都含密钥），而日志正是最容易把它们顺手带出去的地方。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

# 保留的最大行数：一轮周报约 50 条、一轮 AI 筛选约 30 条，够回溯很久
LOG_KEEP = 5000

# 级别由轻到重。筛选语义是「**不低于**所选级别」——选 warn 时也看得到 error。
LEVELS = ("debug", "info", "warn", "error")

# AI 原始返回的截断长度：够看清模型答了什么、又不至于让库失控
AI_REPLY_CHARS = 2000

_MAX_MESSAGE = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      INTEGER NOT NULL,
    level   TEXT NOT NULL,
    kind    TEXT NOT NULL,
    message TEXT NOT NULL,
    data    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_id ON events(id DESC);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, id DESC);
"""

# 键名**命中子串**即打码 —— 各家模型的键名不统一（api_key / apiKey / Authorization
# / share_token…），白名单一定会漏，宁可多码几个。
_SECRET_KEYS = ("api_key", "apikey", "authorization", "password", "passwd",
                "token", "secret", "cookie", "credential")
_REDACTED = "***"
_MAX_DEPTH = 6


def _fmt_local_time(ts: Any) -> str:
    """epoch 秒 → 本地时区的 ``YYYY-MM-DD HH:MM:SS``。

    **格式必须与前端 ``stores/logs.ts`` 的 ``formatTs()`` 逐字一致** —— 它就是
    列表里显示的那串字，搜索比的就是它。两处各写一种格式的话，用户照着屏幕上
    抄下来的时间会搜不到（而这条路径没有别的办法发现）。
    """
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(ts)))
    except Exception:  # noqa: BLE001
        return ""


def _redact(value: Any, _depth: int = 0) -> Any:
    """递归清洗敏感字段，返回**可 JSON 序列化**的结构。"""
    if _depth > _MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k).lower()
            out[str(k)] = (
                _REDACTED if any(s in key for s in _SECRET_KEYS) else _redact(v, _depth + 1)
            )
        return out
    if isinstance(value, (list, tuple)):
        return [_redact(v, _depth + 1) for v in list(value)[:50]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)[:500]


def _default_db_path() -> Path:
    from mp_harvest.infra.platform import paths

    return paths.data_dir() / "events.db"


class EventLog:
    """事件表（单连接 + 锁；所有方法异常容错，绝不抛）。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            # 搜索要能按「界面上显示的那个时间」搜 —— 库里存的是 epoch 整数，
            # 跟列表里的 16:40:04 完全对不上（用户搜 "04" 搜不到就是这个原因）。
            # 注册成 SQL 函数而不是取回来在 Python 里过滤：过滤必须留在 SQL 侧，
            # 否则 LIMIT / 游标分页的语义就变了（先取 200 条再筛，会漏）。
            conn.create_function("local_time", 1, _fmt_local_time, deterministic=True)
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

    # ── 写 ────────────────────────────────────────────────────────

    def write(self, *, level: str, kind: str, message: str, data: Any = None) -> int:
        """追加一条事件，返回新行 id（失败返回 0）。

        序列化与写库**各自**兜底：``data`` 里有不可序列化的对象时退成 ``{}``，
        而不是把整条事件丢掉 —— 消息本身才是最有用的部分。
        """
        try:
            payload = json.dumps(_redact(data) if data else {}, ensure_ascii=False, default=str)
        except Exception:  # noqa: BLE001
            payload = "{}"
        try:
            with self._lock:
                conn = self._connect()
                cur = conn.execute(
                    "INSERT INTO events (ts, level, kind, message, data) VALUES (?,?,?,?,?)",
                    (int(time.time()), str(level), str(kind), str(message)[:_MAX_MESSAGE], payload),
                )
                # 只留最新的 LOG_KEEP 行（AUTOINCREMENT 保证 id 单调）
                conn.execute(
                    "DELETE FROM events WHERE id <= (SELECT MAX(id) FROM events) - ?",
                    (LOG_KEEP,),
                )
                conn.commit()
                return int(cur.lastrowid or 0)
        except Exception:  # noqa: BLE001
            return 0

    def clear(self) -> int:
        """清空，返回删掉的行数（失败返回 0）。"""
        try:
            with self._lock:
                conn = self._connect()
                cur = conn.execute("DELETE FROM events")
                conn.commit()
                return int(cur.rowcount or 0)
        except Exception:  # noqa: BLE001
            return 0

    # ── 读 ────────────────────────────────────────────────────────

    def list(
        self,
        *,
        level: str = "",
        kind: str = "",
        q: str = "",
        limit: int = 200,
        before_id: int = 0,
    ) -> list[dict[str, Any]]:
        """时间倒序取一批；``before_id`` 为游标（取更早的）。

        ``level`` 是**下限**：传 ``warn`` 会同时返回 warn 与 error。
        """
        sql = "SELECT * FROM events"
        cond: list[str] = []
        args: list[Any] = []
        if level in LEVELS:
            tail = LEVELS[LEVELS.index(level):]
            cond.append("level IN (" + ",".join("?" * len(tail)) + ")")
            args.extend(tail)
        if kind:
            cond.append("kind = ?")
            args.append(str(kind))
        if q:
            # **列表里能看见的每一列都要能搜到**。原先只搜 message 与 data，
            # 于是「类型」和「时间」看着在眼前却搜不出来 —— 用户搜 "04" 想找
            # 16:40:04 那条，得到空结果，只能怀疑搜索坏了。
            # 时间走 local_time() 转成本地时区的显示串再比（见 _connect）。
            needle = (
                "%" + str(q).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            cond.append(
                "(message LIKE ? ESCAPE '\\' OR data LIKE ? ESCAPE '\\'"
                " OR kind LIKE ? ESCAPE '\\' OR level LIKE ? ESCAPE '\\'"
                " OR local_time(ts) LIKE ? ESCAPE '\\')"
            )
            args.extend([needle] * 5)
        if before_id:
            cond.append("id < ?")
            args.append(int(before_id))
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(max(1, min(1000, int(limit))))
        try:
            with self._lock:
                rows = self._connect().execute(sql, args).fetchall()
            return [self._row(r) for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def kinds(self) -> list[dict[str, Any]]:
        """出现过的事件类型 + 各自条数（前端筛选下拉用，不必硬编码一份表）。

        类型是埋点自己起的（``action`` / ``task.done`` / ``ai.reply``…），
        加新埋点不该还要回来改前端。
        """
        try:
            with self._lock:
                rows = self._connect().execute(
                    "SELECT kind, COUNT(*) AS n FROM events GROUP BY kind ORDER BY n DESC"
                ).fetchall()
            return [{"kind": str(r["kind"]), "count": int(r["n"])} for r in rows]
        except Exception:  # noqa: BLE001
            return []

    def count(self) -> int:
        try:
            with self._lock:
                row = self._connect().execute("SELECT COUNT(*) AS n FROM events").fetchone()
            return int(row["n"]) if row else 0
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        try:
            d["data"] = json.loads(d.get("data") or "{}")
        except Exception:  # noqa: BLE001
            d["data"] = {}
        return d


# ── 进程内单例 + 永不抛的入口 ─────────────────────────────────────

_log: EventLog | None = None
_log_lock = threading.Lock()


def get_event_log(db_path: str | Path | None = None) -> EventLog:
    """事件库单例；显式传 ``db_path`` 时创建独立实例（测试用）。"""
    global _log
    if db_path is not None:
        return EventLog(db_path)
    with _log_lock:
        if _log is None:
            _log = EventLog(_default_db_path())
        return _log


def reset_event_log() -> None:
    """清掉单例（测试隔离用）。"""
    global _log
    with _log_lock:
        if _log is not None:
            _log.close()
        _log = None


def log_event(level: str, kind: str, message: str, data: Any = None) -> None:
    """记一条事件。**永不抛异常** —— 调用点遍布主流程，这是硬约束。

    埋点很多（每次模型调用、每个任务起止、每个写操作），任何一个调用点都不该
    因为日志的问题而失败，所以这里连 ``get_event_log()`` 都包在 try 里。
    """
    try:
        get_event_log().write(level=level, kind=kind, message=message, data=data)
    except Exception:  # noqa: BLE001
        # 铁律 2：这里**不能再调 log_event**（递归）。兜底只打一行到 stdout ——
        # 开发模式看得见，打包版丢掉也不影响主流程。
        try:
            print(f"[event_log] 写入失败: {kind} {message}"[:200], flush=True)
        except Exception:  # noqa: BLE001
            pass


def list_events(**kw: Any) -> list[dict[str, Any]]:
    """单例上的 :meth:`EventLog.list`（路由用它，省得自己取单例）。"""
    try:
        return get_event_log().list(**kw)
    except Exception:  # noqa: BLE001
        return []


def clear_events() -> int:
    """单例上的 :meth:`EventLog.clear`。"""
    try:
        return get_event_log().clear()
    except Exception:  # noqa: BLE001
        return 0


def event_kinds() -> list[dict[str, Any]]:
    """单例上的 :meth:`EventLog.kinds`。"""
    try:
        return get_event_log().kinds()
    except Exception:  # noqa: BLE001
        return []


def event_count() -> int:
    """单例上的 :meth:`EventLog.count`。"""
    try:
        return get_event_log().count()
    except Exception:  # noqa: BLE001
        return 0


__all__ = [
    "AI_REPLY_CHARS",
    "EventLog",
    "LEVELS",
    "LOG_KEEP",
    "clear_events",
    "event_count",
    "event_kinds",
    "get_event_log",
    "list_events",
    "log_event",
    "reset_event_log",
]
