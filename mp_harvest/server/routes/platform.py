"""GET /api/platform —— 平台能力矩阵（设计稿 §4 权限 UX）。"""

from __future__ import annotations

from fastapi import APIRouter

from mp_harvest.infra.platform import get_platform

router = APIRouter(tags=["platform"])


@router.get("/api/platform")
def platform_info() -> dict:
    return get_platform().info()
