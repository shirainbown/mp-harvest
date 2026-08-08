"""任务注册表（设计稿 §3.2）。

- REST handler 毫秒级返回；耗时工作（拉历史/AI筛选/导出/更新下载）创建
  :class:`Task` 并立即返回 ``task_id``。
- 进度变化 → WS ``task.progress``；完成 → ``task.done``；失败/取消 → ``task.error``。
- 取消：``POST /api/tasks/{id}/cancel`` 置 ``cancel_event``，业务在分页/批次
  边界调用 ``task.check_cancelled()``（抛 :class:`TaskCancelled`）响应取消。
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from mp_harvest.server.ws import broadcast_event


class TaskCancelled(Exception):
    """业务在取消标志置位时抛出，任务进入 cancelled 状态。"""


@dataclass
class Task:
    id: str
    type: str
    percent: float = 0.0
    message: str = ""
    status: str = "pending"  # pending | running | done | error | cancelled
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    cancel_event: threading.Event = field(default_factory=threading.Event)

    def update(self, percent: float | None = None, message: str | None = None) -> None:
        """业务进度回调：更新并广播 task.progress。"""
        if percent is not None:
            self.percent = max(0.0, min(100.0, float(percent)))
        if message is not None:
            self.message = str(message)
        broadcast_event(
            "task.progress",
            {"task_id": self.id, "percent": self.percent, "message": self.message},
        )

    def check_cancelled(self) -> None:
        """分页/批次边界调用；已取消则抛 :class:`TaskCancelled`。"""
        if self.cancel_event.is_set():
            raise TaskCancelled("任务已取消")

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "percent": self.percent,
            "message": self.message,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
        }


WorkFn = Callable[[Task], Any]


class TaskRegistry:
    """线程池 + 任务表（进程内单例）。"""

    def __init__(self, max_workers: int = 4) -> None:
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="mp_harvest-task"
        )
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()

    def create(self, task_type: str, fn: WorkFn) -> Task:
        """登记并提交任务，立即返回 Task（调用方取 ``task.id`` 返回前端）。"""
        task = Task(id=uuid.uuid4().hex[:12], type=task_type)
        with self._lock:
            self._tasks[task.id] = task
        self._executor.submit(self._run, task, fn)
        return task

    def _run(self, task: Task, fn: WorkFn) -> None:
        task.status = "running"
        try:
            result = fn(task)
        except TaskCancelled:
            task.status = "cancelled"
            task.error = "任务已取消"
            broadcast_event("task.error", {"task_id": task.id, "error": task.error})
            return
        except Exception as exc:  # noqa: BLE001
            task.status = "error"
            task.error = str(exc) or exc.__class__.__name__
            broadcast_event("task.error", {"task_id": task.id, "error": task.error})
            return
        # 业务静默返回时若已被取消，也按取消处理
        if task.cancel_event.is_set():
            task.status = "cancelled"
            task.error = "任务已取消"
            broadcast_event("task.error", {"task_id": task.id, "error": task.error})
            return
        task.status = "done"
        task.percent = 100.0
        task.result = result
        broadcast_event("task.done", {"task_id": task.id, "result": result})

    def cancel(self, task_id: str) -> Task | None:
        """置取消标志（幂等）。任务不存在返回 None。"""
        task = self.get(task_id)
        if task is None:
            return None
        if task.status in ("pending", "running"):
            task.cancel_event.set()
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self) -> list[Task]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.created_at, reverse=True)

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)


registry = TaskRegistry()
