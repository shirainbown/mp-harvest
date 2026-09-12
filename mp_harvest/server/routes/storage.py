"""本地数据占用与清理（2026-09）。

起因：用户问「把本地缓存删了会怎样」，一查发现 data/ 有 815MB —— 其中 806MB
是从来没清理过的历史更新包，而界面上根本看不到「什么占了多大、哪些能安全清」。

契约：
- ``GET /api/storage`` —— 清单（含可清理标记与说明），按占用降序
- ``POST /api/storage/clean`` —— 清理选中的项

**不可清理的项（账号/凭证/模型配置/CA/提示词/设置/文章缓存）也会出现在清单里**，
带 ``safe=false``：让用户看得见「这些不归清理管」，而不是清完才发现少了东西。
"""

from __future__ import annotations

from fastapi import APIRouter

from mp_harvest.core import storage as storage_mod
from mp_harvest.server.schemas import StorageCleanIn

router = APIRouter(tags=["storage"])


@router.get("/api/storage")
def get_storage() -> dict:
    """各类本地数据的占用。``clearable_bytes`` 是「可安全清理」的总量。"""
    return storage_mod.summary()


@router.post("/api/storage/clean")
def clean_storage(body: StorageCleanIn) -> dict:
    """清理选中的可重建数据。

    只接受 ``safe=true`` 的键（``core.storage.clear`` 会挡掉其余的）——
    这一层守卫挡在删除动作前面，避免前端传错就把账号清了。
    """
    return storage_mod.clear(body.keys)


__all__ = ["router"]
