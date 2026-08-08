"""REST 路由汇总（设计稿 §7.1）。"""

from mp_harvest.server.routes import (
    accounts,
    ai,
    export,
    history,
    mitm,
    platform,
    settings,
    tasks,
    update,
)

ALL_ROUTERS = [
    platform.router,
    accounts.router,
    mitm.router,
    history.router,
    export.router,
    ai.router,
    settings.router,
    update.router,
    tasks.router,
]

__all__ = ["ALL_ROUTERS"]
