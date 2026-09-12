"""执行日志（2026-09）：只读查询 + 清空。

埋点分布在四处，本模块只负责把库里的东西交给前端：

- ``core/ai_filter._call_model`` —— 每次模型调用（含原始返回、HTTP 错误体）
- ``core/ai_filter.judge_articles`` —— 每批筛选的通过/过滤汇总
- ``server/tasks.TaskRegistry._run`` —— 每个后台任务的起止与结果
- ``server/app.create_app`` 的中间件 —— 每个写操作（用户动作）

日志**只增不改**，也不参与任何业务判断；查询与清空都走 ``core/event_log``，
那里已经保证「任何故障都不抛」。
"""

from __future__ import annotations

from fastapi import APIRouter

from mp_harvest.core import event_log as el

router = APIRouter(tags=["logs"])


@router.get("/api/logs")
def list_logs(
    level: str = "",
    kind: str = "",
    q: str = "",
    limit: int = 200,
    before_id: int = 0,
) -> dict:
    """时间倒序取一批事件。

    - ``level`` 是**下限**：传 ``warn`` 会同时返回 warn 与 error
      （用户想看「有什么不对」时不该漏掉最严重的）。
    - ``before_id`` 是游标：传上一批最后一条的 id 取更早的（「加载更多」）。
    - ``kinds`` 顺带回出现过的类型与条数，前端筛选下拉直接用，不必硬编码。
    """
    return {
        "events": el.list_events(
            level=level, kind=kind, q=q, limit=limit, before_id=before_id
        ),
        "total": el.event_count(),
        "kinds": el.event_kinds(),
    }


@router.delete("/api/logs")
def clear_logs() -> dict:
    """清空全部日志。返回删掉的行数。"""
    return {"ok": True, "cleared": el.clear_events()}


__all__ = ["router"]
