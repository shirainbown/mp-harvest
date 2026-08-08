"""在线更新：检查 + 下载（任务）（设计稿 §7.1）。

对应 platform.updater。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from mp_harvest.infra.platform import get_platform, paths
from mp_harvest.server.schemas import UpdateDownloadIn
from mp_harvest.server.tasks import Task, registry

router = APIRouter(tags=["update"])


def _settings_proxy() -> str:
    try:
        from mp_harvest.core import settings as settings_mod

        return str(settings_mod.load_settings().get("proxy") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


@router.get("/api/update/check")
def update_check() -> dict:
    result = get_platform().updater.check(proxy=_settings_proxy() or None)
    return result.to_dict()


@router.post("/api/update/download", status_code=202)
def update_download(body: UpdateDownloadIn) -> dict:
    updater = get_platform().updater
    proxy = (body.proxy or "").strip() or _settings_proxy() or None

    def work(task: Task) -> dict:
        def on_progress(done: int, total: int) -> None:
            task.check_cancelled()
            pct = (done / total * 100.0) if total else 0.0
            task.update(percent=pct, message=f"下载更新包 {done // 1024}KB/{total // 1024}KB")

        result = updater.download(body.zip_url, proxy=proxy, on_progress=on_progress)
        task.check_cancelled()
        if not result.ok:
            raise RuntimeError(result.error or result.message or "下载失败")
        return result.to_dict()

    task = registry.create("update.download", work)
    return {"task_id": task.id, "type": task.type}


def _find_downloaded_package():
    """data_dir()/update 下最新下载的更新包（两平台 asset_suffix 均为 .zip）。"""
    update_dir = paths.data_dir() / "update"
    if not update_dir.is_dir():
        return None
    candidates = [p for p in update_dir.glob("*.zip") if p.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@router.post("/api/update/apply")
def update_apply() -> dict:
    """应用已下载的更新（重启以应用）。未下载过更新包时返回 409。"""
    from mp_harvest.infra.platform.base import PlatformError

    pkg = _find_downloaded_package()
    if pkg is None:
        raise HTTPException(
            status_code=409, detail="尚未下载更新包，请先执行 /api/update/download"
        )
    try:
        # 真实实现：生成替换脚本 → 退出进程 → 替换安装 → 重启（不返回）
        get_platform().updater.apply(pkg)
    except PlatformError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    # 测试/fake 环境 apply 可能正常返回
    return {"ok": True, "package": str(pkg)}
