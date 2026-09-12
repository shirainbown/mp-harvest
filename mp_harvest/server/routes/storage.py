"""本地数据占用与清理（2026-09）。

起因：用户问「把本地缓存删了会怎样」，一查发现 data/ 有 815MB —— 其中 806MB
是从来没清理过的历史更新包，而界面上根本看不到「什么占了多大、哪些能安全清」。

契约：
- ``GET /api/storage`` —— 清单（含可清理标记、代价标记与说明），按占用降序
- ``POST /api/storage/clean`` —— 清理选中的项

**三档**（详见 ``core/storage`` 的模块注释）：代价低 / 代价高但可清 / 不可再生。
后两类也会出现在清单里（``safe=false`` 的标「保留」），让用户看得见全貌，
而不是清完才发现少了东西。

⚠️ 本模块**必须**在删完磁盘之后顺手放掉内存态（``_reset_memory``）：
只删文件的话，进程里的旧数据会在下一次保存时把它写回来 ——「清了又长回来」，
而且只长回被碰到的那部分，看着像没清干净（2026-09 实测）。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from mp_harvest.core import storage as storage_mod
from mp_harvest.server import state
from mp_harvest.server.schemas import StorageCleanIn

router = APIRouter(tags=["storage"])


def _mitm_running() -> bool:
    """抓包服务是否在跑。查不到就当没跑（清理本身不该因为读状态失败而卡住）。"""
    try:
        return bool(state.get_mitm().running)
    except Exception:  # noqa: BLE001
        return False


def _reset_memory(names: list[str]) -> None:
    """按 ``core.storage`` 给的名单调用 ``state`` 里的复位函数。

    名单是**代码里写死的常量**（``core._MEMORY_RESET`` 的值），不是用户输入，
    所以 getattr 取函数是安全的；取不到就跳过，不让清理因此报错。
    """
    for name in names:
        fn = getattr(state, name, None)
        if not callable(fn):
            continue
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


@router.get("/api/storage")
def get_storage() -> dict:
    """各类本地数据的占用。``clearable_bytes`` 是「可清理」的总量（含代价高的）。"""
    return storage_mod.summary()


@router.post("/api/storage/clean")
def clean_storage(body: StorageCleanIn) -> dict:
    """清理选中的数据。

    只接受 ``safe=true`` 的键（``core.storage.clear`` 会挡掉其余的）——
    这一层守卫挡在删除动作前面，避免前端传错就把不可再生的清了。
    """
    # 抓包运行中不清理 CA：文件删了，进程里那份证书还在用；而下次启动会生成一个
    # **新的** CA，系统钥匙串里信任的仍是旧的 —— 抓包会静默失败，比不让清更糟。
    if "mitm_conf" in body.keys and _mitm_running():
        raise HTTPException(status_code=409, detail="正在抓包，请先停止抓包再清理 CA 证书")

    res = storage_mod.clear(body.keys)
    # 只对**真的删掉了**的项复位内存；没删成的保持原样，免得白丢内存副本
    _reset_memory(storage_mod.memory_reset_for(res.get("removed") or []))
    return res


__all__ = ["router"]
