"""执行日志（core/event_log.py）。

这三条是**硬约束**，不是锦上添花 —— 埋点遍布主流程，日志坏了绝不能反过来
把生成搞坏；而日志又是密钥最容易泄漏出去的地方：

1. 任何故障都不抛、不阻断
2. 内部失败不再回调自身（否则递归）
3. **绝不写敏感内容**
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from mp_harvest.core import event_log as el  # noqa: E402


@pytest.fixture()
def store(tmp_path) -> el.EventLog:
    return el.EventLog(tmp_path / "events.db")


# ── 基本读写 ──────────────────────────────────────────────────────


def test_write_and_list_newest_first(store):
    store.write(level="info", kind="action", message="第一条")
    store.write(level="info", kind="task", message="第二条")
    rows = store.list()
    assert [r["message"] for r in rows] == ["第二条", "第一条"]
    assert rows[0]["kind"] == "task" and rows[0]["level"] == "info"
    assert rows[0]["ts"] > 0


def test_data_roundtrips_as_json(store):
    store.write(level="info", kind="ai.call", message="调用",
                data={"model": "deepseek", "elapsed_ms": 1234, "ok": True})
    d = store.list()[0]["data"]
    assert d == {"model": "deepseek", "elapsed_ms": 1234, "ok": True}


def test_unserializable_data_keeps_the_message(store):
    """data 里有不可序列化的对象时退成 {}，但**消息必须留下** —— 消息才是有用的部分。"""
    class Weird:
        pass

    store.write(level="info", kind="x", message="照样要记下来", data={"obj": Weird()})
    rows = store.list()
    assert len(rows) == 1 and rows[0]["message"] == "照样要记下来"


def test_level_filter_is_a_lower_bound(store):
    """选 warn 要能同时看到 error —— 用户想看「有什么不对」时不该漏掉最严重的。"""
    for lv in el.LEVELS:
        store.write(level=lv, kind="k", message=f"{lv} 级")
    assert {r["level"] for r in store.list(level="warn")} == {"warn", "error"}
    assert len(store.list(level="debug")) == 4
    assert [r["level"] for r in store.list(level="error")] == ["error"]


def test_kind_filter_and_search(store):
    store.write(level="info", kind="ai.reply", message="模型返回了东西", data={"raw": "光刻机"})
    store.write(level="info", kind="task", message="任务完成")
    assert len(store.list(kind="ai.reply")) == 1
    assert len(store.list(q="光刻")) == 1, "搜索要能命中 data 里的内容"
    assert len(store.list(q="任务")) == 1
    assert store.list(q="不存在的东西") == []


def test_search_covers_every_visible_column(store):
    """搜索要覆盖**列表里显示出来的每一列**。

    用户报的：搜 `04` 找不到界面上写着 `16:40:04` 的那条日志，只能怀疑搜索坏了。
    根因是库里存的是 epoch 整数，而 `q` 只比 message 与 data —— 时间、类型、
    级别这三列**看得见却搜不到**。

    每一条都拿**界面上真正显示的那串字**去搜，而不是自己另算一个格式：
    前端 `formatTs()` 与后端 `_fmt_local_time()` 一旦漂移，用户照着屏幕抄下来的
    时间就搜不到，而这条路径没有别的办法发现。
    """
    from mp_harvest.core.event_log import _fmt_local_time

    store.write(level="warn", kind="ai.reply", message="模型返回了东西", data={"raw": "光刻机"})
    row = store.list()[0]
    shown = _fmt_local_time(row["ts"])
    assert len(shown) == 19, shown          # YYYY-MM-DD HH:MM:SS

    # 时间：整串、日期段、时分秒片段都要能搜到
    assert store.list(q=shown), f"完整时间搜不到：{shown}"
    assert store.list(q=shown[:10]), "日期部分搜不到"
    assert store.list(q=shown[-2:]), "秒的片段搜不到（用户报的就是这个）"
    assert store.list(q=shown[11:16]), "时:分 搜不到"
    # 类型与级别（这两列也显示在列表里）
    assert store.list(q="ai.reply"), "类型搜不到"
    assert store.list(q="warn"), "级别搜不到"
    # 原有的两列别被改坏
    assert store.list(q="模型"), "message 搜不到"
    assert store.list(q="光刻"), "data 上下文搜不到"
    # 不该命中的仍然不命中
    assert store.list(q="绝不存在的字符串") == []


def test_search_escapes_like_wildcards(store):
    """`%` 与 `_` 是 LIKE 的通配符 —— 不转义的话搜 `100%` 会连 `1000` 一起命中。

    样本必须**同时具备「该命中的」与「不该命中的」**：只放一个的话，转不转义
    结果都一样，测试就是空转（第一版正是这么骗过自己的）。
    """
    store.write(level="info", kind="k", message="含有 100% 的消息")
    store.write(level="info", kind="k", message="含有 1000 的消息")
    assert [r["message"] for r in store.list(q="100%")] == ["含有 100% 的消息"]

    store.write(level="info", kind="k", message="代号 a_b")
    store.write(level="info", kind="k", message="代号 axb")
    assert [r["message"] for r in store.list(q="a_b")] == ["代号 a_b"]


def test_cursor_pagination(store):
    for i in range(5):
        store.write(level="info", kind="k", message=f"第{i}条")
    page1 = store.list(limit=2)
    assert [r["message"] for r in page1] == ["第4条", "第3条"]
    page2 = store.list(limit=2, before_id=page1[-1]["id"])
    assert [r["message"] for r in page2] == ["第2条", "第1条"]


def test_clear(store):
    store.write(level="info", kind="k", message="x")
    assert store.clear() == 1
    assert store.list() == []


def test_prunes_to_keep_limit(store, monkeypatch):
    """超过上限要**滚动丢弃最旧的**，不然库会一直涨。"""
    monkeypatch.setattr(el, "LOG_KEEP", 10)
    for i in range(25):
        store.write(level="info", kind="k", message=f"第{i}条")
    rows = store.list(limit=100)
    assert len(rows) == 10
    assert rows[0]["message"] == "第24条"      # 最新的留着
    assert rows[-1]["message"] == "第15条"     # 最旧的十条被剪掉


# ── 铁律 3：绝不写敏感内容 ────────────────────────────────────────


@pytest.mark.parametrize("key", [
    "api_key", "apiKey", "API_KEY", "Authorization", "share_token",
    "password", "secret", "cookie", "credentials",
])
def test_secrets_are_redacted_by_key_name(store, key):
    """按**键名子串**打码 —— 各家模型键名不统一，白名单一定会漏。"""
    # 样本必须是**明显的假值** —— 别拿任何真实 key 当测试数据，哪怕只是写过期的
    secret = "sk-FAKE-must-never-appear-in-the-log"
    store.write(level="info", kind="ai.call", message="调用", data={key: secret})
    blob = json.dumps(store.list()[0]["data"], ensure_ascii=False)
    assert secret not in blob, f"{key} 没被打码"
    assert el._REDACTED in blob


def test_secrets_redacted_in_nested_structures(store):
    """嵌套里也要洗 —— 模型配置就常常是一层套一层。"""
    store.write(level="info", kind="x", message="m", data={
        "models": [{"name": "a", "api_key": "sk-nested-leak"},
                   {"name": "b", "headers": {"Authorization": "Bearer sk-header-leak"}}],
        "meta": {"tokens": {"access_token": "sk-token-leak"}},
    })
    blob = json.dumps(store.list()[0]["data"], ensure_ascii=False)
    for leaked in ("sk-nested-leak", "sk-header-leak", "sk-token-leak"):
        assert leaked not in blob, f"{leaked} 漏了出去"


def test_non_secret_fields_are_kept(store):
    """别把该留的也码掉 —— 排查问题全靠这些。"""
    store.write(level="info", kind="ai.call", message="m",
                data={"model": "deepseek-chat", "elapsed_ms": 900, "prompt_chars": 1234})
    d = store.list()[0]["data"]
    assert d["model"] == "deepseek-chat" and d["elapsed_ms"] == 900


# ── 铁律 1 / 2：出故障也不能影响主流程 ────────────────────────────


def test_write_failure_never_raises(monkeypatch):
    """库文件坏了也得安静地失败 —— 埋点在主流程里，抛出去就是整个功能崩。"""
    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: _ExplodingLog())
    el.log_event("info", "k", "message")     # 不抛即通过


def test_log_event_does_not_recurse(monkeypatch):
    """内部失败**不能再回调 log_event** —— 否则日志故障会变成无限递归。"""
    calls = {"n": 0}
    real = el.log_event

    def counting(*a, **k):
        calls["n"] += 1
        if calls["n"] > 5:
            raise AssertionError("log_event 递归了")
        return real(*a, **k)

    monkeypatch.setattr(el, "get_event_log", lambda *a, **k: _ExplodingLog())
    monkeypatch.setattr(el, "log_event", counting)
    el.log_event("info", "k", "message")     # 走的是真实现，但内部不许再调 counting
    assert calls["n"] == 1


def test_read_failure_returns_empty(tmp_path, monkeypatch):
    log = el.EventLog(tmp_path / "e.db")
    monkeypatch.setattr(log, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("坏了")))
    assert log.list() == []
    assert log.clear() == 0
    assert log.count() == 0
    assert log.write(level="info", kind="k", message="m") == 0


class _ExplodingLog:
    def write(self, **kw):
        raise RuntimeError("库坏了")

    def list(self, **kw):
        raise RuntimeError("库坏了")

    def clear(self):
        raise RuntimeError("库坏了")
