"""本地数据占用与清理（2026-09）。

起因：用户问「把本地缓存删了会怎样」。查下来两件事都不体面：

1. ``data/`` 涨到 **815MB，其中 806MB 是历史更新包** —— 每点一次「立即更新」
   就留一个 ~50MB 的 zip，从来没有清理过（见 ``infra.platform.base.prune_update_packages``）；
2. 界面上没有任何地方能看到「什么占了多大、哪些能安全清」，只能去翻文件系统。

这里给出**清单** + 清理动作。

**分三档**（2026-09 从「能清 / 不能清」两档拆开）：

- 代价低 —— 删了只是重新生成（更新包、各类 AI 缓存、执行日志、导出记录与 HTML）
- **代价高但可清** —— 要重新抓包 / 重新联网拉，得用户动手（公众号与凭证、
  文章缓存、其他来源索引、抓包 CA）。它们带 ``costly=True``，前端要把代价
  写进确认框，而不是替用户决定「不许清」
- **不可再生** —— 提示词、模型配置、应用设置、手工补录的链接。``safe=False``，
  不提供清理。列出来是为了让用户**看见**「这些不归清理管」，而不是清完才发现少了。

拆档的原因：原先只有两档，于是「代价高」被当成「不允许」用了 —— 用户问
「为什么公众号和历史文章不能清」，那确实是他自己的数据，该由他决定。

⚠️ 清理**只删磁盘**。进程里的内存态要由调用方另清（见 :func:`memory_reset_for`），
否则下一次保存会把刚删的文件写回来 —— 清了又长回来，看着像没清干净。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

# 可清理项的落盘形态：key → (类型, data_dir 下的相对路径)。
#
# **这张表与 inventory() 里 safe=True 的项必须一一对应**（有测试钉住）。
# 只往清单里加项、忘了往这里加，界面上就会出现「勾了没反应」的假按钮 ——
# 2026-09 真的发生过：加了「导出的文章 HTML」，却在 clear() 里落到「未知项」，
# 点了什么都不删，而当时的测试只验了「列得出来」、没验「清得掉」。
_CLEARABLE: dict[str, tuple[str, str]] = {
    "update":           ("dir",  "update"),
    "exports":          ("dir",  "exports"),
    "articles_cache":   ("dir",  "articles_cache"),
    "mitm_conf":        ("dir",  "mitm_conf"),
    "ai_title_cache":   ("file", "ai_filter_cache.json"),
    "ai_content_cache": ("file", "ai_content_filter_cache.json"),
    "weekly_cache":     ("file", "weekly/cache.json"),
    "events":           ("file", "events.db"),
    "export_records":   ("file", "harvest.db"),
    "external_db":      ("file", "external.db"),
    "accounts":         ("file", "accounts.json"),
}

# 清理之后必须一并放掉的**内存态**（key → server.state 里的复位函数名）。
# 不放掉的话，进程里的旧数据会在下一次保存时把刚删的文件写回来 ——
# 「清了又长回来」，而且只长回被碰到的那部分，看着像没清干净（2026-09 实测）。
# 路由层执行（core 不能 import server），见 server/routes/storage.py。
_MEMORY_RESET = {
    "articles_cache": "forget_articles",
    "accounts": "forget_store",
    "external_db": "forget_external",
    "mitm_conf": "forget_mitm",
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


def path_of(key: str) -> Path:
    """可清理项的落盘路径（路径也由 ``_CLEARABLE`` 一处给出，清单与清理不会各写各的）。"""
    return _data_dir() / _CLEARABLE[key][1]


def inventory(*, include_empty: bool = False) -> list[dict[str, Any]]:
    """各类数据的占用与可清理性（按占用降序）。

    ``include_empty=True`` 时**不过滤空项**，返回的是「声明了的全部项」——
    给不变量测试与 :func:`clear` 的守卫用。默认为 False：界面不列空项，
    免得用户看到一堆 0 B 的条目（2026-09 用户要求「删光了就移除」）。

    每项：``{key, label, path, size, count, safe, costly, note}``。

    **三档**（2026-09 从两档拆开）：
    - ``safe=True, costly=False`` —— 直接清，代价只是重新生成（更新包、各类缓存、日志）
    - ``safe=True, costly=True``  —— **可清但代价高**：删了要重新抓包 / 重新联网拉，
                                     得用户动手，所以确认框里必须把代价写清楚
    - ``safe=False``              —— **不可再生**：提示词、模型配置、设置、补录链接，
                                     删了没法重新得到，不提供清理

    拆档之前只有「能清 / 不能清」两种，用户问「为什么公众号和历史文章不能清」——
    因为「代价高」被当成「不允许」用了。可那两样确实是他的数据，该由他决定。
    """
    d = _data_dir()
    items: list[dict[str, Any]] = []

    def add(key: str, label: str, path: Path, *, safe: bool, note: str,
            costly: bool = False) -> None:
        size, count = _size_of(path)
        items.append({
            "key": key, "label": label, "path": str(path),
            "size": size, "count": count, "safe": safe, "costly": costly, "note": note,
        })

    # ── 代价低：删了只是重新生成 ──
    add("update", "更新包（已下载的安装包，装完就没用）", d / "update",
        safe=True, note="每次「立即更新」都会下一个 ~50MB 的包")
    add("exports", "导出的文章 HTML", d / "exports",
        safe=True, note="删掉后重新导出要联网重拉；历史列表的「已导出」标记随之消失")
    add("ai_title_cache", "AI 标题判定缓存", path_of("ai_title_cache"),
        safe=True, note="删掉后下次筛选要重新判定（会调用模型）")
    add("ai_content_cache", "AI 内容判定缓存", path_of("ai_content_cache"),
        safe=True, note="删掉后下次内容筛选要重新判定")
    add("weekly_cache", "周报打分 / 解读缓存", path_of("weekly_cache"),
        safe=True, note="删掉后下次生成要重新打分（会调用模型）")
    add("events", "执行日志", path_of("events"),
        safe=True, note="只是历史记录，删掉不影响任何功能")
    add("export_records", "正文导出记录", path_of("export_records"),
        safe=True, note="删掉后重复导出**会把已导出的文章再下一遍**；目录页也不再累积")

    # ── 代价高：可清，但要重新采集（用户自己动手）──
    add("articles_cache", "文章缓存（含已抓正文与 AI 判定）", path_of("articles_cache"),
        safe=True, costly=True,
        note="删了要重新联网拉历史；**AI 判定结果（留/删 + 理由）也在这些行里**，"
             "一并消失，要重新跑筛选（会调用模型）")
    add("external_db", "其他来源登记与索引", path_of("external_db"),
        safe=True, costly=True,
        note="删了要重新登记来源目录并扫描（磁盘上的目录本身不受影响）")
    add("accounts", "公众号与凭证", path_of("accounts"),
        safe=True, costly=True,
        note="删了要重新添加公众号并**重新抓包**（装 CA + 设系统代理 + 打开微信走一遍）")
    add("mitm_conf", "抓包 CA 证书", path_of("mitm_conf"),
        safe=True, costly=True,
        note="删了要重新生成并安装 CA（需要管理员密码）。"
             "**抓包运行中不能清**；系统钥匙串里对旧 CA 的信任也不会被撤销")

    # ── 不可再生：删了没法重新得到，列出来是为了让用户看见「这些不归清理管」──
    add("ai_models", "AI 模型配置", d / "ai_models.json",
        safe=False, note="含 API Key，是你手填的，删了要重新填写")
    add("settings", "应用设置", d / "settings.json",
        safe=False, note="删了会静默回退默认值，你的选择就没了")
    add("prompts", "周报自定义提示词", d / "weekly" / "prompts.json",
        safe=False, note="是你写的判定标准，删了会回退内置默认，改不回来")
    add("sightings", "手工补录的链接", d / "article_sightings.json",
        safe=False, note="你手工补录的文章，没有别的地方能重新拉到 —— 删了就真没了")

    # 材料已经删光的项**不列出来**（2026-09 用户要求：「本地保存的材料都已经
    # 删除了，那么就移除」）。判据是磁盘上的实际占用，不是「这个键配过没有」——
    # 所以删掉导出的 HTML 再刷新，那一项就自己消失了。
    if not include_empty:
        items = [i for i in items if int(i["size"]) > 0 or int(i["count"]) > 0]
    items.sort(key=lambda x: -int(x["size"]))
    return items


def memory_reset_for(keys: list[str]) -> list[str]:
    """这些键清完之后需要复位的内存态（``server.state`` 里的函数名）。

    **只返回名字、不执行** —— core 不能 import server（依赖方向是反的），
    由路由层拿着这份名单去调。不复位的话，进程里的旧数据会在下一次保存时把
    刚删的文件写回来：清了又长回来，而且只长回被碰到的那部分（2026-09 实测）。
    """
    return [_MEMORY_RESET[k] for k in keys if k in _MEMORY_RESET]


def clear(keys: list[str]) -> dict[str, Any]:
    """清理选中的数据，返回 ``{ok, freed, removed, errors}``。

    **只吃** ``_CLEARABLE`` 里的键（＝清单里 ``safe=True`` 的那些）—— 传别的
    一律忽略。这一层挡在删除动作前面，避免哪天前端传错就把不可再生的清了。

    落盘形态全从 ``_CLEARABLE`` 查，**没有按 key 分支的特例** —— 之前那版只处理
    `update` / `weekly_cache` + 一张单文件表，新加的「导出的文章 HTML」（目录型）
    就落到了「未知项」，界面上成了个勾了没反应的假按钮。
    """
    d = _data_dir()
    freed = 0
    removed: list[str] = []
    errors: list[str] = []

    for key in keys:
        spec = _CLEARABLE.get(key)
        if spec is None:
            errors.append(f"{key}：不在可清理范围内，已忽略")
            continue
        kind, rel = spec
        try:
            if key == "events":
                # 先关连接再删，免得留下一个指向已删 inode 的句柄（新写入会进虚空）
                from mp_harvest.core import event_log as el

                el.reset_event_log()
            n = _clear_dir(d / rel) if kind == "dir" else _clear_file(d / rel)
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
