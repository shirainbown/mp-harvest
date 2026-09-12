"""前端接线契约：按钮 → 处理函数 → 真正干活的那次调用。

**为什么需要这一层**（2026-09）：前端没有测试框架，而连着出过两批
「点了没反应」的按钮。它们的病根不同，需要两种检查一起才拦得住：

1. 「打开报告 / 打开目录 / 打开 PDF」—— 处理函数接了线，但里面调的是
   走不通的路径（`openExternal('file://…')`，shell 侧只放行 http(s)）。
   → 靠**函数体**检查（`openPath` 里必须出现 `openLocalPath(`）。
2. 「候选来源 - 全部」—— 接线完全正确，**错的是语义**（「不勾 = 全部」，
   默认状态下点了等于没点）。静态检查抓不到，靠 `test_weekly_selection.py`
   在 node 里驱动真实 store 跑状态机。

这里做的是静态接线分析，**不是模拟点击**：把 `.vue` 里的 `@click` 表达式
剥出来，确认每个根标识符都在 `<script setup>` 里定义过。这样打错一个字母
（`@click="selectAllAccount"`）会立刻红，而不是等到用户点上去才发现没反应。

环境无关、零依赖、跑在既有 pytest 里。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "mp_harvest" / "frontend" / "src"

# 事件绑定：@click / @click.prevent / @keydown.enter.prevent …="表达式"
_BIND = re.compile(r'@([a-zA-Z]+)((?:\.[a-zA-Z]+)*)\s*=\s*"([^"]*)"')
_DECL = re.compile(r"\b(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)")
_IMPORT = re.compile(r"import\s+(?:([A-Za-z_$][\w$]*)\s*,?\s*)?(?:\{([^}]*)\})?\s*from")
_VFOR = re.compile(r'v-for\s*=\s*"\s*\(?([^)"]*?)\)?\s+(?:in|of)\s')
_PROPS = re.compile(r"defineProps\s*<\{([^}]*)\}|defineProps\s*\(\s*\{([^}]*)\}")
_AS_CAST = re.compile(r"\bas\s+(?:const\s+)?([A-Za-z_$][\w$]*)")
# 作用域插槽的解构：`<template #default="{ close }">` —— close 是插槽给的，不是声明的
_SLOT = re.compile(r'(?:#|v-slot:)[\w-]+\s*=\s*"\s*\{([^}]*)\}')

_KEYWORDS = {
    "true", "false", "null", "undefined", "this", "$event",
    "new", "typeof", "void", "as", "const", "return",
}

# 模板表达式里允许出现的 JS 全局。不放行的话 `new Set()` / `Date.now()` 会误报。
_GLOBALS = {
    "Set", "Map", "WeakMap", "Array", "Object", "JSON", "Math", "Date", "Number",
    "String", "Boolean", "Promise", "RegExp", "Error", "Symbol", "BigInt",
    "console", "window", "document", "navigator", "location", "history",
    "setTimeout", "clearTimeout", "setInterval", "clearInterval",
    "parseInt", "parseFloat", "isNaN", "isFinite", "encodeURIComponent",
    "decodeURIComponent", "URLSearchParams", "IntersectionObserver", "ResizeObserver",
    "File", "Blob", "FormData", "fetch", "requestAnimationFrame",
}

# 对象字面量的**键**（`{ id: s.id }` 里的 id）不是变量引用。仅在 `{` 或 `,`
# 之后才算键 —— 三元 `a ? b : c` 的 b 前面是 `?`，不会被误删。
_OBJ_KEY = re.compile(r"(?<=[{,])\s*([A-Za-z_$][\w$]*)\s*:")


def _names_in(text: str) -> set[str]:
    """这段代码里「能被模板引用到」的名字：变量/函数声明、import、v-for 变量、props。"""
    out = set(_DECL.findall(text))
    for m in _IMPORT.finditer(text):
        if m.group(1):
            out.add(m.group(1))
        for part in (m.group(2) or "").split(","):
            part = part.strip()
            if part:
                out.add(part.split(" as ")[-1].strip())
    for m in _VFOR.finditer(text):
        for v in m.group(1).split(","):
            v = v.strip().lstrip("{[").rstrip("}]")
            if re.fullmatch(r"[A-Za-z_$][\w$]*", v):
                out.add(v)
    for m in _PROPS.finditer(text):
        blob = m.group(1) or m.group(2) or ""
        for p in re.finditer(r"(?:^|[,;\n])\s*([A-Za-z_$][\w$]*)\s*[?]?\s*:", blob):
            out.add(p.group(1))
    for m in _SLOT.finditer(text):
        for v in m.group(1).split(","):
            v = v.strip()
            if re.fullmatch(r"[A-Za-z_$][\w$]*", v):
                out.add(v)
    return out


def _split_sfc(path: Path) -> tuple[str, str]:
    """返回 (script setup 源码, template 源码)；不是 setup 组件则返回空串。

    template 用**贪婪**匹配：组件里到处是嵌套的 `<template v-if=…>`，
    非贪婪会在第一个内层 `</template>` 就收尾，把后面半张模板整个切掉 ——
    那样「按钮找不到」会变成假阴性（本文件第一版就是这么骗过自己的）。
    """
    text = path.read_text(encoding="utf-8")
    sm = re.search(r"<script setup[^>]*>(.*?)</script>", text, re.S)
    tm = re.search(r"<template>(.*)</template>", text, re.S)
    return (sm.group(1), tm.group(1)) if sm and tm else ("", "")


def _vue_files() -> list[Path]:
    return sorted(SRC.rglob("*.vue"))


def _expr_roots(expr: str) -> set[str]:
    """表达式里**被引用到的**根标识符。

    `weekly.generate()` → weekly；`a.b.c` → a（成员名不算引用）；
    `{ id: s.id }` → s（`id` 是键，不是变量）。
    """
    cleaned = re.sub(r"`[^`]*`|'[^']*'|\"[^\"]*\"", " ", expr)
    cleaned = _OBJ_KEY.sub(" ", cleaned)
    return {m.group(1) for m in re.finditer(r"(?<![.\w$])([A-Za-z_$][\w$]*)", cleaned)}


def test_there_are_components_to_check():
    """护栏本身别空转：一个 .vue 都没扫到说明路径写错了。"""
    files = _vue_files()
    assert len(files) >= 10, f"只扫到 {len(files)} 个 .vue，路径大概是错的"


def test_every_event_handler_resolves():
    """模板里每个事件处理器的根标识符，都必须在 `<script setup>` 里定义过。

    打错字母、删了函数忘了改模板 —— 都会在这里红，而不是等用户点上去。
    """
    unresolved: list[str] = []
    for f in _vue_files():
        script, tpl = _split_sfc(f)
        if not script:
            continue
        known = _names_in(script) | _names_in(tpl)
        known |= set(_AS_CAST.findall(script)) | set(_AS_CAST.findall(tpl))   # TS 类型断言
        for m in _BIND.finditer(tpl):
            for root in _expr_roots(m.group(3)):
                if root in _KEYWORDS or root in _GLOBALS or root in known:
                    continue
                unresolved.append(
                    f"{f.relative_to(SRC)}: {m.group(0)[:60]} → 未定义 `{root}`"
                )
    assert not unresolved, "有事件处理器接不到东西：\n  " + "\n  ".join(unresolved)


# ── 具体按钮的接线契约 ──────────────────────────────────────────────
#
# 每个条目都是「出过问题 / 用户明确关心」的按钮。左：事件绑定里必须出现的东西；
# 右：这个处理函数的**函数体**里必须出现的那次调用 —— 光有函数不够，
# 函数体里调错了同样是「点了没反应」。

# (文件, 说明, 处理函数名, 期望绑定处数)
#
# **处数是必须的**：只断言「出现过」的话，同一文件里两个按钮共用一个处理函数时
# （openPath 就服务三个入口），删掉其中一个仍然是「出现过」—— 变异测试实测
# 删掉「打开目录」的绑定能全身而退。处数把「少了一个入口」也钉住。
BUTTON_BINDINGS: list[tuple[str, str, str, int]] = [
    # 三个入口共用 openPath：上次输出链接 / 打开报告 / 打开目录
    ("views/WeeklyView.vue", "打开往期报告 · 打开期目录 · 上次输出", "openPath", 3),
    ("views/WeeklyView.vue", "公众号「全选 / 全不选」", "selectAllAccounts", 1),
    ("views/WeeklyView.vue", "来源目录「全选 / 全不选」", "selectAllSources", 1),
    ("views/WeeklyView.vue", "选模板文件", "pickTemplate", 1),
    ("views/WeeklyView.vue", "用内置模板", "useBuiltinTemplate", 1),
    ("views/WeeklyView.vue", "生成第 N 期", "weekly.generate", 1),
    # 重新统计候选按钮 + 两个日期输入框的 @change
    ("views/WeeklyView.vue", "重新统计候选 / 改日期自动重算", "weekly.loadPreview", 3),
    ("views/WeeklyView.vue", "保存提示词", "weekly.savePrompt", 1),
    ("views/WeeklyView.vue", "恢复默认提示词", "weekly.restorePrompt", 1),
    ("views/WeeklyView.vue", "按当前模板重新渲染", "weekly.rerender", 1),
    ("components/ToastHost.vue", "复制这条报错", "copyToast", 1),
    # 「查看」+「✕」两处
    ("components/ToastHost.vue", "查看 / 关闭这条提示", "ui.dismissToast", 2),
    ("layout/Sidebar.vue", "错误中心 - 行内复制", "copyOne", 1),
    ("layout/Sidebar.vue", "错误中心 - 复制全部", "copyAll", 1),
    ("layout/Sidebar.vue", "错误中心 - 清空", "ui.clearErrors", 1),
    # 打开本地正文 + 打开 PDF
    ("views/ExternalView.vue", "打开本地正文 / PDF", "openLocal", 2),
    # 排序两段控件（2026-09 由「下拉 + 方向按钮」合并而来）——两个列表页各一处
    ("views/HistoryView.vue", "排序控件", "articles.pickSort", 1),
    ("views/ExternalView.vue", "排序控件", "ext.pickSort", 1),
    # 设置页「存储占用」（2026-09）：勾选框 + 清理按钮
    ("views/SettingsView.vue", "存储项勾选", "togglePick", 1),
    ("views/SettingsView.vue", "清理选中（二次确认后执行）", "doClean", 1),
    # 候选范围勾选框：切换后要**立刻重算**（否则「共 N 篇」还停在旧口径）
    ("views/WeeklyView.vue", "候选范围（只考虑筛选通过的）", "onOnlyKept", 1),
    # 打分速度两个输入框（每批篇数 / 并发请求数，2026-09）
    ("views/WeeklyView.vue", "打分批大小", "setScoreBatch", 1),
    ("views/WeeklyView.vue", "打分并发请求数", "setScoreWorkers", 1),
    # 执行日志页（2026-09）：查询按钮 + 搜索框回车各一处
    ("views/LogsView.vue", "查询（按钮 + 回车）", "logs.load", 2),
    ("views/LogsView.vue", "加载更多", "logs.loadMore", 1),
    ("views/LogsView.vue", "复制全部", "copyAll", 1),
    ("views/LogsView.vue", "清空（二次确认后执行）", "doClear", 1),
]

# (文件, 处理函数名, 函数体里必须出现的调用)
HANDLER_BODIES: list[tuple[str, str, str]] = [
    # 本地文件必须走 shell_open —— openExternal 只放行 http(s)，file:// 会被静默挡下
    ("views/WeeklyView.vue", "openPath", "openLocalPath("),
    ("views/ExternalView.vue", "openLocal", "openLocalPath("),
    # 「全选」必须是真开关（toggle），不是「清空 = 全部」那套旧语义
    ("views/WeeklyView.vue", "selectAllAccounts", "toggleAllAccounts("),
    ("views/WeeklyView.vue", "selectAllSources", "toggleAllSources("),
    # 复制一律走 copyText（带 execCommand 兜底、返回真实结果）
    ("components/ToastHost.vue", "copyToast", "copyText("),
    ("layout/Sidebar.vue", "copyOne", "copyText("),
    ("layout/Sidebar.vue", "copyAll", "copyText("),
    # 改完必须真的落盘（只在本地赋值的话，下次生成还是用旧值，且不会报错）
    ("views/WeeklyView.vue", "setScoreBatch", "savePrefs("),
    ("views/WeeklyView.vue", "setScoreWorkers", "savePrefs("),
    # 日志页的复制一律走 copyText（带 execCommand 兜底、返回真实结果）
    ("views/LogsView.vue", "copyAll", "copyText("),
    ("views/LogsView.vue", "copyOne", "copyText("),
    ("views/LogsView.vue", "doClear", "logs.clear("),
    # 清理必须真的打到后端（只在前端清列表等于骗人）
    ("views/SettingsView.vue", "doClean", "storage.clean("),
    ("views/WeeklyView.vue", "onOnlyKept", "loadPreview("),
]


# ── store 参数接线（扫 .ts）────────────────────────────────────────
#
# 上面扫的是 .vue，但有些参数只在 store 里拼进请求体。典型失败：界面加了勾选框、
# store 却没把这个字段发给后端 —— 后端有默认值兜底，于是**勾选框看着能点、
# 实际毫无作用**（正是本项目反复出现的「点了没反应」）。

STORE_PARAMS: list[tuple[str, str, str]] = [
    # (store 文件, 说明, 必须出现的片段)
    ("stores/weekly.ts", "生成时把「只考虑筛选通过的」发给后端",
     "only_kept: this.onlyKept"),
    ("stores/weekly.ts", "预览也走同一口径（否则数字与生成对不上）",
     "only_kept: String(this.onlyKept)"),
    ("stores/weekly.ts", "生成时把「补全正文」发给后端",
     "fetch_bodies: this.fetchBodies"),
]


@pytest.mark.parametrize("rel,label,snippet", STORE_PARAMS)
def test_store_sends_the_param(rel: str, label: str, snippet: str):
    """store 里必须真的把这个参数拼进请求 —— 否则勾选框是摆设。"""
    text = (SRC / rel).read_text(encoding="utf-8")
    assert snippet in text, f"{rel} 没有把「{label}」发出去（找不到 `{snippet}`）"


def _body_of(script: str, fn: str) -> str:
    """抠出 `function fn(...) { ... }` 的函数体（按大括号配对，含嵌套）。

    先跳过**参数表**再找 `{`：参数上常带对象字面量类型
    （`copyOne(e: { id: number; msg: string })`），直接找第一个 `{`
    会抠到那个类型标注上，函数体里有什么就都看不见了。
    """
    m = re.search(rf"(?:async\s+)?function\s+{re.escape(fn)}\s*\(", script)
    if not m:
        pytest.fail(f"找不到函数 `{fn}` 的定义")
    depth, k = 0, m.end() - 1
    while k < len(script):                     # 配平参数表的圆括号
        if script[k] == "(":
            depth += 1
        elif script[k] == ")":
            depth -= 1
            if depth == 0:
                break
        k += 1
    i = script.index("{", k)
    depth = 0
    for j in range(i, len(script)):
        if script[j] == "{":
            depth += 1
        elif script[j] == "}":
            depth -= 1
            if depth == 0:
                return script[i : j + 1]
    pytest.fail(f"函数 `{fn}` 的大括号不配对")


@pytest.mark.parametrize("rel,label,handler,count", BUTTON_BINDINGS)
def test_button_is_bound(rel: str, label: str, handler: str, count: int):
    """按钮必须真的绑到这个处理函数上，**且一个都不能少**。

    处数不是凑数：`openPath` 服务三个入口，只断言「出现过」的话删掉一个
    照样绿（变异测试实测过），而用户看到的正是「这个按钮点了没反应」。
    """
    _, tpl = _split_sfc(SRC / rel)
    assert tpl, f"{rel} 没有 template"
    pat = re.compile(rf'@[a-zA-Z]+(?:\.[a-zA-Z]+)*\s*=\s*"[^"]*\b{re.escape(handler)}\b')
    hits = pat.findall(tpl)
    assert hits, f"{rel} 里「{label}」没有绑到 `{handler}`"
    assert len(hits) == count, (
        f"{rel} 里绑到 `{handler}` 的有 {len(hits)} 处，期望 {count} 处（{label}）"
    )


@pytest.mark.parametrize("rel,fn,must_call", HANDLER_BODIES)
def test_handler_calls_the_right_thing(rel: str, fn: str, must_call: str):
    """处理函数体里必须出现那次关键调用 —— 接对了线但调错东西一样是死的。"""
    script, _ = _split_sfc(SRC / rel)
    assert script, f"{rel} 没有 script setup"
    body = _body_of(script, fn)
    assert must_call in body, f"{rel} 的 `{fn}` 里没有调用 `{must_call}`"


# ── 「只筛选中」的 ids 必须真的传下去（2026-09）────────────────────


def test_targeted_filter_buttons_pass_ids():
    """勾选筛选的两个按钮必须把 ids 传给 store。

    漏传的后果**完全不报错**：按钮照常转圈、任务照常完成，只是它筛了全部 ——
    用户以为只重跑了那几篇（比如正文抓失败的那几篇），实际整个范围又跑了一遍，
    还要重新联网拉那些没正文的文章。这比按钮没反应更难发现。
    """
    src = (SRC / "views" / "HistoryView.vue").read_text(encoding="utf-8")
    calls = re.findall(r"articles\.(aiFilter|contentFilter)\(([^)]*)\)", src)
    targeted = [(fn, args) for fn, args in calls if "pickedIds" in args]
    assert len(targeted) == 2, (
        f"「只筛选中」应当有标题 / 内容两个入口各传一次 ids，实际 {targeted}"
    )
    assert {fn for fn, _ in targeted} == {"aiFilter", "contentFilter"}, targeted
    # 传给 store 的必须是**数组快照**（pickedIds 是 computed），不能是响应式对象本身：
    # 关掉弹窗 / 清空勾选之后任务才真正开始跑，传引用等于传了个空数组
    assert all("pickedIds" in args for _, args in targeted), targeted


def test_whole_scope_filter_buttons_pass_no_ids():
    """页脚那两个「整范围」按钮不能带 ids —— 带上就变成「只筛上次勾选的」。"""
    src = (SRC / "views" / "HistoryView.vue").read_text(encoding="utf-8")
    for fn in ("aiFilter", "contentFilter"):
        for args in re.findall(rf"articles\.{fn}\(([^)]*)\)", src):
            if "pickedIds" in args:
                continue
            assert "ids" not in args, f"整范围的 {fn} 不该带 ids：{args}"


def test_template_panel_has_open_and_reveal():
    """模板面板要能「直接打开」和「在文件夹中显示」—— 两个入口，两种需求。

    只做「打开」的话，用户想看这文件在哪还得自己去翻路径；只做「定位」的话，
    想改模板又得多一步。另外路径必须是**可点的**（下划线样式 + @click），
    否则没人知道它能点。
    """
    src = (SRC / "views" / "WeeklyView.vue").read_text(encoding="utf-8")
    # 两个动作都接了真实路径（不是空串/占位）
    assert re.search(r"openLocalPath\(currentTemplate\.value\)", src), "打开模板没接当前模板路径"
    assert re.search(r"revealLocalPath\(currentTemplate\.value\)", src), "定位没接当前模板路径"
    # 路径只挂 dblclick、**不挂 click**（用户明确「只支持双击打开文件夹」）。
    # 单击开文件 + 双击开文件夹在浏览器里做不到干净：双击会先派发两次 click，
    # 要么把文件打开两遍，要么得延迟 250ms 让单击顿一下。
    assert '@dblclick="revealTemplate"' in src, "路径没有挂双击"
    assert not re.search(r'tpl-path[^>]*@click', src), "路径不该挂单击（会与双击打架）"
    # 文案说的是「当前使用的」那一份，不是「内置模板」——填了自定义模板时后者会误导
    assert "当前使用的模板" in src, "标签没写成「当前使用的模板」"
    assert "内置模板：" not in src, "还留着旧的「内置模板：」文案"
    # 不要高亮（用户要求）：没有虚线下划线、没有悬停变色
    css = src[src.index(".tpl-path"):]
    css = css[: css.index("}") + 1]
    assert "dashed" not in css and ":hover" not in css, f"路径仍被高亮：{css}"


def test_reveal_helper_posts_to_the_reveal_endpoint():
    """前端 helper 必须打 /api/shell/reveal，不能图省事复用 open（那是「打开」）。"""
    src = (SRC / "api" / "desktop.ts").read_text(encoding="utf-8")
    body = src[src.index("export async function revealLocalPath"):]
    body = body[: body.index("\n}")] if "\n}" in body else body
    assert "/api/shell/reveal" in body, "revealLocalPath 打的不是 reveal 端点"
    assert "/api/shell/open" not in body, "revealLocalPath 误用了 open 端点"
