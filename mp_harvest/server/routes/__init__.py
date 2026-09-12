"""REST 路由汇总（设计稿 §7.1）。"""

from mp_harvest.server.routes import (
    accounts,
    ai,
    export,
    external,
    history,
    logs,
    mitm,
    platform,
    settings,
    storage,
    tasks,
    update,
    weekly,
)

ALL_ROUTERS = [
    platform.router,
    accounts.router,
    mitm.router,
    history.router,
    export.router,
    external.router,
    weekly.router,
    ai.router,
    settings.router,
    update.router,
    tasks.router,
    logs.router,
    storage.router,
]

__all__ = ["ALL_ROUTERS"]
