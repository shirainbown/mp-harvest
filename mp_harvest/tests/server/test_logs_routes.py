"""执行日志：端点契约 + **四处埋点确实触发**（server/routes/logs.py）。

埋点遍布主流程（模型调用、任务起止、写操作），所以这里重点不是「接口能返回」，
而是「事情真的被记下来了」—— 否则「用户看得到执行过程」这个诉求根本没落地。
"""

from __future__ import annotations

import json
from pathlib import Path

from .conftest import add_account, wait_task


def _events(client, auth, **params) -> list[dict]:
    r = client.get("/api/logs", params={**auth, **params})
    assert r.status_code == 200, r.text
    return r.json()["events"]


def _make_dir(tmp_path: Path) -> Path:
    root = tmp_path / "papers"
    d = root / "2026-09-07"
    d.mkdir(parents=True, exist_ok=True)
    (d / "papers_data.json").write_text(
        json.dumps([{"title": "甲", "abstract": "摘要", "url": "http://arxiv.org/abs/1",
                     "arxiv_id": "1v1"}], ensure_ascii=False),
        encoding="utf-8",
    )
    return root


# ── 端点 ──────────────────────────────────────────────────────────


def test_logs_endpoint_shape(client, auth):
    b = client.get("/api/logs", params=auth).json()
    assert set(b) == {"events", "total", "kinds"}
    assert isinstance(b["events"], list)


def test_logs_filters(client, auth):
    from mp_harvest.core import event_log as el

    el.log_event("info", "action", "普通动作")
    el.log_event("error", "task.error", "出错了")
    el.log_event("info", "ai.reply", "模型答了")

    assert {e["kind"] for e in _events(client, auth, limit=50)} >= {"action", "task.error", "ai.reply"}
    assert [e["kind"] for e in _events(client, auth, kind="task.error")] == ["task.error"]
    assert {e["level"] for e in _events(client, auth, level="warn")} == {"error"}
    assert [e["message"] for e in _events(client, auth, q="出错了")] == ["出错了"]


def test_logs_kinds_carry_counts(client, auth):
    from mp_harvest.core import event_log as el

    el.log_event("info", "action", "a1")
    el.log_event("info", "action", "a2")
    kinds = {k["kind"]: k["count"] for k in client.get("/api/logs", params=auth).json()["kinds"]}
    assert kinds.get("action") == 2


def test_logs_clear(client, auth):
    from mp_harvest.core import event_log as el

    el.log_event("info", "action", "x")
    assert client.delete("/api/logs", params=auth).json()["cleared"] >= 1
    assert _events(client, auth) == []


# ── 埋点 1：用户动作（HTTP 中间件）────────────────────────────────


def test_write_action_is_logged(client, auth):
    """写操作要被记下来 —— 这是「用户的一些动作」那条诉求的落点。"""
    add_account(client, auth)
    rows = [e for e in _events(client, auth, kind="action") if "POST" in e["message"]]
    assert rows, "POST 没有被记进执行日志"
    assert any(e["data"].get("path") == "/api/accounts" for e in rows)
    assert rows[0]["data"]["status"] < 400


def test_read_actions_are_not_logged(client, auth):
    """GET 是浏览不是动作 —— 记下来只会把日志淹掉。"""
    client.get("/api/accounts", params=auth)
    assert not [e for e in _events(client, auth, kind="action") if "GET" in e["message"]]


def test_failed_write_is_logged_as_warn_with_reason(client, auth):
    """失败的写操作更要记，而且**必须带原因** —— 那正是用户来问「为什么没反应」的事。

    只记 `POST → 400` 等于什么都没说；原因在 FastAPI 的 body.detail 里。
    """
    r = client.post("/api/external/sources", params=auth, json={"path": "/不存在的目录"})
    assert r.status_code >= 400
    rows = [e for e in _events(client, auth, kind="action")
            if e["data"].get("path") == "/api/external/sources"]
    assert rows and rows[0]["level"] == "warn" and rows[0]["data"]["status"] >= 400
    assert rows[0]["data"]["detail"], "失败原因没记下来"
    assert rows[0]["data"]["detail"] in rows[0]["message"]
    assert "/不存在的目录" in rows[0]["data"]["detail"]


def test_error_body_still_reaches_the_client(client, auth):
    """中间件把响应体读出来记日志，**必须原样还给客户端**。

    它挂在每一次请求上 —— 弄坏响应（body 变空、长度对不上）比日志缺失严重得多，
    而且症状会很隐蔽（前端只显示「请求失败」，看不到后端说了什么）。
    """
    r = client.post("/api/external/sources", params=auth, json={"path": "/不存在的目录"})
    assert r.status_code == 400
    assert "不存在的目录" in r.json()["detail"], f"客户端没拿到错误详情：{r.text!r}"
    assert r.headers.get("content-type", "").startswith("application/json")


def test_startup_token_never_lands_in_the_log(client, auth):
    """**启动 token 走的是查询串**（`?token=…`），绝不能进日志。

    本项目对凭证有历史教训（写注释时漏过内网 IP、缓存文件带密钥），
    而日志是最容易把它们顺手带出去的地方 —— 这条按「值」断言，不只按键名。
    """
    token = str(auth.get("token") or "")
    assert token, "auth fixture 里没有 token，这条测试就没意义了"
    add_account(client, auth)

    from mp_harvest.core import event_log as el

    blob = json.dumps(el.list_events(limit=200), ensure_ascii=False)
    assert token not in blob, "启动 token 泄漏进了执行日志"
    assert '"token": "***"' in blob or "***" in blob


# ── 埋点 2：任务生命周期 ──────────────────────────────────────────


def test_task_lifecycle_is_logged(client, auth, tmp_path):
    """每个后台任务的起止都要留痕 —— 这是「执行过程」的主干。"""
    src = client.post("/api/external/sources", params=auth,
                      json={"name": "论文", "path": str(_make_dir(tmp_path))}).json()
    wait_task(client.post(f"/api/external/sources/{src['id']}/scan", params=auth)
              .json()["task_id"])

    kinds = [e["kind"] for e in _events(client, auth, limit=100)]
    assert "task.start" in kinds, "任务的开始没有留痕"
    assert "task.done" in kinds, "任务的结束没有留痕"
    done = next(e for e in _events(client, auth, kind="task.done"))
    assert done["data"]["type"] == "external.scan"
    assert done["data"]["elapsed_ms"] >= 0


def test_task_failure_is_logged_as_error(client, auth, tmp_path, monkeypatch):
    """任务抛异常要记成 error（含原因），不能只在前端飘一下就没。"""
    src = client.post("/api/external/sources", params=auth,
                      json={"name": "论文", "path": str(_make_dir(tmp_path))}).json()
    import mp_harvest.core.external_sources as ext_mod

    monkeypatch.setattr(ext_mod, "scan_source",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("扫描炸了")))
    wait_task(client.post(f"/api/external/sources/{src['id']}/scan", params=auth)
              .json()["task_id"])

    errs = [e for e in _events(client, auth, kind="task.error") if e["level"] == "error"]
    assert errs and "扫描炸了" in errs[0]["message"]
