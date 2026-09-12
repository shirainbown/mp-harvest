"""本地数据占用与清理（2026-09）。

起因：用户问「把本地缓存删了会怎样」。查下来两件事都不体面：

1. ``data/`` 涨到 **815MB，其中 806MB 是历史更新包** —— 每点一次「立即更新」
   就留一个 ~50MB 的 zip，从来没有清理过（见 ``infra.platform.base.prune_update_packages``）；
2. 界面上没有任何地方能看到「什么占了多大、哪些能安全清」，只能去翻文件系统。

这里给出**可清理清单** + 清理动作。

**清单只收可重建的**：账号与凭证、模型配置（含密钥）、CA、自定义提示词、设置、
文章缓存一律不在可清列 —— 那些删了就真没了，或者要重新抓包/重拉。它们仍然会
出现在清单里（标 ``safe=False``），是为了让用户**看见**「这些不归清理管」，
而不是清完才发现少了东西。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 单文件型数据（key → 文件名）
_SINGLE_FILES = {
    "ai_title_cache": "ai_filter_cache.json",
    "ai_content_cache": "ai_content_filter_cache.json",
    "export_records": "harvest.db",
    "events": "events.db",
}


def _size_of(p: Path) -> tuple[int, int]:
    from mp_harvest.infra.platform.base import dir_size

    if p.is_dir():
        return dir_size(p)
    try:
        return (p.stat().st_size, 1) if p.is_file() else (0, 0)
    except OSError:
        return 0, 0


def _data_dir() -> Path:
    from mp_harvest.infra.platform import paths

    return paths.data_dir()


def inventory() -> list[dict[str, Any]]:
    """各类数据的占用与可清理性（按占用降序）。

    每项：``{key, label, path, size, count, safe, note}``。
    ``safe=True`` 才允许清理 —— 前端只给这些画勾选框。
    """
    d = _data_dir()
    items: list[dict[str, Any]] = []

    def add(key: str, label: str, path: Path, *, safe: bool, note: str) -> None:
        size, count = _size_of(path)
        items.append({
            "key": key, "label": label, "path": str(path),
            "size": size, "count": count, "safe": safe, "note": note,
        })

    add("update", "更新包（已下载的安装包，装完就没用）", d / "update",
        safe=True, note="每次「立即更新」都会下一个 ~50MB 的包")
    add("ai_title_cache", "AI 标题判定缓存", d / _SINGLE_FILES["ai_title_cache"],
        safe=True, note="删掉后下次筛选要重新判定（会调用模型）")
    add("ai_content_cache", "AI 内容判定缓存", d / _SINGLE_FILES["ai_content_cache"],
        safe=True, note="删掉后下次内容筛选要重新判定")
    add("weekly_cache", "周报打分 / 解读缓存", d / "weekly" / "cache.json",
        safe=True, note="删掉后下次生成要重新打分（会调用模型）")
    add("events", "执行日志", d / _SINGLE_FILES["events"],
        safe=True, note="只是历史记录，删掉不影响任何功能")
    add("export_records", "正文导出记录", d / _SINGLE_FILES["export_records"],
        safe=True, note="删掉后重复导出**会把已导出的文章再下一遍**；目录页也不再累积")
    # 导出的 HTML 本身 —— 2026-09 补。原先只列了记录库（12KB），真正占地方的
    # 导出文件（实测 55 篇 2.6MB，且会随导出的批次持续涨）一项都没列。
    add("exports", "导出的文章 HTML", d / "exports",
        safe=True, note="删掉后重新导出要联网重拉；历史列表的「已导出」标记随之消失")

    # 以下**不可清理**，列出来是为了让用户看见「这些不归清理管」
    add("articles_cache", "文章缓存（含已抓正文）", d / "articles_cache",
        safe=False, note="拉取与正文都在这里，删了要重新联网拉")
    add("external_db", "其他来源登记与索引", d / "external.db",
        safe=False, note="删了要重新登记目录并扫描（磁盘上的目录不受影响）")
    add("accounts", "公众号与凭证", d / "accounts.json",
        safe=False, note="删了要重新添加公众号并重新抓包")
    add("ai_models", "AI 模型配置", d / "ai_models.json",
        safe=False, note="含 API Key，删了要重新填写")
    add("settings", "应用设置", d / "settings.json",
        safe=False, note="删除后会静默回退默认值")
    add("prompts", "周报自定义提示词", d / "weekly" / "prompts.json",
        safe=False, note="删除后会静默回退内置默认标准")
    add("mitm_conf", "抓包 CA 证书", d / "mitm_conf",
        safe=False, note="删了要重新安装 CA（需要管理员密码）")

    # 材料已经删光的项**不列出来**（2026-09 用户要求：「本地保存的材料都已经
    # 删除了，那么就移除」）。判据是磁盘上的实际占用，不是「这个键配过没有」——
    # 所以删掉导出的 HTML 再刷新，那一项就自己消失了。
    items = [i for i in items if int(i["size"]) > 0 or int(i["count"]) > 0]
    items.sort(key=lambda x: -int(x["size"]))
    return items


def clear(keys: list[str]) -> dict[str, Any]:
    """清理选中的可重建数据，返回 ``{ok, freed, removed, errors}``。

    **只吃** :func:`inventory` 里 ``safe=True`` 的键 —— 传了别的键一律忽略。
    这一层挡在删除动作前面，避免哪天前端传错就把账号清了。
    """
    allowed = {i["key"] for i in inventory() if i["safe"]}
    d = _data_dir()
    freed = 0
    removed: list[str] = []
    errors: list[str] = []

    for key in keys:
        if key not in allowed:
            errors.append(f"{key}：不在可清理范围内，已忽略")
            continue
        try:
            if key == "update":
                n = _clear_dir(d / "update")
                freed += n
                removed.append(key)
                continue
            if key == "weekly_cache":
                n = _clear_file(d / "weekly" / "cache.json")
                freed += n
                removed.append(key)
                continue
            name = _SINGLE_FILES.get(key)
            if not name:
                errors.append(f"{key}：未知项")
                continue
            if key == "events":
                # 走连接关掉再删，免得留下半开的句柄
                from mp_harvest.core import event_log as el

                el.reset_event_log()
            n = _clear_file(d / name)
            freed += n
            removed.append(key)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{key}：{exc}")

    return {"ok": not errors, "freed": freed, "removed": removed, "errors": errors}


def _clear_file(p: Path) -> int:
    """删单个文件，返回释放的字节数（不存在算 0）。"""
    try:
        if not p.is_file():
            return 0
        size = p.stat().st_size
        p.unlink()
        return size
    except OSError:
        return 0


def _clear_dir(p: Path) -> int:
    """清空目录内容（保留目录本身），返回释放的字节数。"""
    from mp_harvest.infra.platform.base import dir_size

    try:
        if not p.is_dir():
            return 0
        total, _ = dir_size(p)
        for child in list(p.iterdir()):
            try:
                if child.is_file() or child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    import shutil

                    shutil.rmtree(child, ignore_errors=True)
            except OSError:
                continue
        return total
    except OSError:
        return 0


def summary() -> dict[str, Any]:
    """给界面用：清单 + 可清理总量 + 总占用。"""
    items = inventory()
    return {
        "items": items,
        "clearable_bytes": sum(int(i["size"]) for i in items if i["safe"]),
        "total_bytes": sum(int(i["size"]) for i in items),
    }


__all__ = ["clear", "inventory", "summary"]
