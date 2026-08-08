"""任务查询 / 取消（设计稿 §3.2、§7.1）。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from mp_harvest.server.tasks import registry

router = APIRouter(tags=["tasks"])


@router.get("/api/tasks")
def list_tasks() -> dict:
    return {"tasks": [t.to_dict() for t in registry.list()]}


@router.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    task = registry.get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task.to_dict()


@router.post("/api/tasks/{task_id}/cancel")
def cancel_task(task_id: str) -> dict:
    """置取消标志；业务在分页/批次边界响应（§3.2）。"""
    task = registry.cancel(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"ok": True, "task_id": task_id, "status": task.status}
