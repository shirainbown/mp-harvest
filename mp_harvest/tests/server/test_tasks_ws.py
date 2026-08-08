"""任务注册表 + WS 广播契约（设计稿 §3.2 / §7.2）。"""

from __future__ import annotations

import threading
import time

from mp_harvest.tests.server.conftest import wait_task


def test_task_get_and_list(client, auth):
    from mp_harvest.server.tasks import registry

    t = registry.create("test.demo", lambda task: {"n": 1})
    task = wait_task(t.id)
    assert task.status == "done"

    resp = client.get(f"/api/tasks/{t.id}", params=auth)
    assert resp.status_code == 200
    assert resp.json()["type"] == "test.demo"

    resp = client.get("/api/tasks", params=auth)
    assert any(x["id"] == t.id for x in resp.json()["tasks"])


def test_task_get_404(client, auth):
    resp = client.get("/api/tasks/nope", params=auth)
    assert resp.status_code == 404


def test_task_cancel_semantics(client, auth):
    """cancel 置标志 → 业务 check_cancelled 抛 TaskCancelled → cancelled 态。"""
    from mp_harvest.server.tasks import registry

    def work(task):
        for _ in range(200):
            task.check_cancelled()
            time.sleep(0.02)
        return "unreachable"

    t = registry.create("test.slow", work)
    time.sleep(0.05)
    resp = client.post(f"/api/tasks/{t.id}/cancel", params=auth)
    assert resp.status_code == 200
    task = wait_task(t.id)
    assert task.status == "cancelled"
    assert task.result is None


def test_task_cancel_unknown_404(client, auth):
    resp = client.post("/api/tasks/nope/cancel", params=auth)
    assert resp.status_code == 404


def test_task_error_broadcast_status(client, auth):
    from mp_harvest.server.tasks import registry

    def boom(task):
        raise RuntimeError("炸了")

    t = registry.create("test.boom", boom)
    task = wait_task(t.id)
    assert task.status == "error"
    assert "炸了" in task.error


def test_ws_receives_broadcast_event(client, auth):
    """broadcast_event → 已连接客户端收到 {type, ...payload}。"""
    from mp_harvest.server.ws import broadcast_event

    with client.websocket_connect("/ws", params=auth) as ws:
        broadcast_event("mitm.status", {"running": True, "port": 8088})
        msg = ws.receive_json()
        assert msg["type"] == "mitm.status"
        assert msg["running"] is True
        assert msg["port"] == 8088


def test_ws_task_progress_events(client, auth):
    """任务进度/完成经 WS 推送（§7.2 task.progress / task.done）。"""
    from mp_harvest.server.tasks import registry

    with client.websocket_connect("/ws", params=auth) as ws:
        def work(task):
            task.update(percent=50, message="一半了")
            return {"ok": True}

        t = registry.create("test.progress", work)
        seen: set[str] = set()
        deadline = time.time() + 5
        while time.time() < deadline and "task.done" not in seen:
            msg = ws.receive_json()
            if msg["type"] == "task.progress":
                assert msg["percent"] == 50
                assert msg["message"] == "一半了"
            seen.add(msg["type"])
        assert {"task.progress", "task.done"} <= seen
        wait_task(t.id)
