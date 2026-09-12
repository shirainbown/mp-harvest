"""排序控件「两段 + 点当前段翻转」的状态机（2026-09）。

改之前是**两个控件表达四个状态**（下拉选维度 + 另一个按钮切方向）。现在合成
一个控件，规则只有一条：

    点**没选中**的段 = 换维度（用该维度的默认方向）
    点**已选中**的段 = 翻转方向

这条规则放在两个 store 里（`articles.pickSort` / `external.pickSort`），因为
「点了没反应 / 反应反了」正是本项目反复出问题的地方 —— 视图里的逻辑没法单测，
store 里的可以。

沿用 `test_weekly_selection.py` 的 esbuild + node 装置：**真实的 store 模块**，
不是复刻一份逻辑来测自己。缺 node / esbuild 时跳过。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from mp_harvest.tests._node_util import esbuild_bin

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "mp_harvest" / "frontend"
ESBUILD = esbuild_bin()
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or ESBUILD is None,
    reason="需要 node + frontend/node_modules（没装前端依赖时跳过）",
)

# config.ts 在模块加载时读 location.search，先补全局再动态 import。
_HARNESS = """
globalThis.location = { search: '', href: 'http://localhost/', origin: 'http://localhost' }
globalThis.window = globalThis
const { createPinia, setActivePinia } = await import('pinia')
const { useArticlesStore } = await import('./src/stores/articles.ts')
const { useExternalStore } = await import('./src/stores/external.ts')

const out = {}
setActivePinia(createPinia())

// ---- 历史文章：四态迁移 ----
//
// 顺序是**故意**的：每次「换维度」都发生在方向与目标默认值**不同**的时刻，
// 否则「不重置方向」这个 bug 正好被掩盖（第一版就是这么骗过自己的）。
const a = useArticlesStore()
out.aDefault = [a.sortBy, a.sortDir]     // time, desc
a.pickSort('name')                       // 换维度（当前 desc ≠ 名称默认 asc）
out.aToName = [a.sortBy, a.sortDir]
a.pickSort('name')                       // 已选中 → 翻转
out.aNameFlip = [a.sortBy, a.sortDir]
a.pickSort('name')                       // 再翻转回去（此时 dir 回到 asc）
out.aNameFlip2 = [a.sortBy, a.sortDir]
a.pickSort('time')                       // 换回时间（当前 asc ≠ 时间默认 desc）
out.aBackToTime = [a.sortBy, a.sortDir]
a.pickSort('time')                       // 已选中 → 翻转
out.aTimeFlip = [a.sortBy, a.sortDir]

// ---- 其他来源：同样的两段控件 ----
const e = useExternalStore()
out.eDefault = [e.sortBy, e.sortDir]

e.items = [
  { id: '1', title: 'Banana', date: '2026-09-01', authors: [], domain: '', primary_category: '', url: '' },
  { id: '2', title: 'apple',  date: '2026-09-03', authors: [], domain: '', primary_category: '', url: '' },
  { id: '3', title: 'Cherry', date: '2026-09-02', authors: [], domain: '', primary_category: '', url: '' },
]
const ids = () => e.visible.map((r) => r.id).join(',')

out.eTimeDesc = ids()                    // 默认：最新在前
e.pickSort('name')                       // 换维度（当前 desc ≠ 名称默认 asc）
out.eNameAfterSwitch = [e.sortBy, e.sortDir]
out.eByNameAsc = ids()                   // 标题 A→Z（zh 排序，大小写不敏感）
e.pickSort('name')
out.eByNameDesc = ids()                  // 已选中 → 翻转，Z→A
e.pickSort('name')
e.pickSort('time')                       // 换回时间（当前 asc ≠ 时间默认 desc）
out.eTimeAfterSwitch = [e.sortBy, e.sortDir]
out.eBackToTime = ids()                  // 最新在前
e.pickSort('time')
out.eTimeAsc = ids()                     // 已选中 → 翻转，最旧在前

// 搜索过滤与排序共存（过滤在前，排序在后，互不干扰）
e.q = 'an'
out.eFiltered = ids()

console.log(JSON.stringify(out))
"""


@pytest.fixture(scope="module")
def state() -> dict:
    """跑一遍探针，返回真实 store 的状态快照。"""
    harness = FRONTEND / "_sort_probe.mjs"
    bundled = FRONTEND / "_sort_probe.built.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    try:
        subprocess.run(
            [ESBUILD, "--bundle", "--platform=node", "--format=esm",
             f"--outfile={bundled}", str(harness), "--log-level=error"],
            cwd=str(FRONTEND), check=True, capture_output=True, text=True, timeout=120,
        )
        proc = subprocess.run(
            [str(NODE), str(bundled)],
            cwd=str(FRONTEND), check=True, capture_output=True, text=True, timeout=60,
        )
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        harness.unlink(missing_ok=True)
        bundled.unlink(missing_ok=True)


def test_articles_starts_with_time_desc(state):
    assert state["aDefault"] == ["time", "desc"]


def test_clicking_active_segment_flips_direction(state):
    """核心规则：点已经选中的那一段 = 翻转方向（这正是「合并成一个按钮」的代价与便利）。"""
    assert state["aNameFlip"] == ["name", "desc"]
    assert state["aNameFlip2"] == ["name", "asc"]
    assert state["aTimeFlip"] == ["time", "asc"]


def test_clicking_other_segment_switches_dimension(state):
    assert state["aToName"] == ["name", "asc"]


def test_switching_back_resets_to_default_direction(state):
    """换维度要回到该维度的默认方向，而不是记住上次的方向 —— 否则按钮标签会骗人。"""
    assert state["aBackToTime"] == ["time", "desc"]


def test_external_store_shares_the_same_rule(state):
    """两个列表页用同一个控件，规则必须一致（外部页原先只有方向一个维度）。"""
    assert state["eDefault"] == ["time", "desc"]
    assert state["eNameAfterSwitch"] == ["name", "asc"]
    assert state["eTimeAfterSwitch"] == ["time", "desc"]


def test_external_sorts_by_time(state):
    assert state["eTimeDesc"] == "2,3,1"      # 09-03 / 09-02 / 09-01
    assert state["eTimeAsc"] == "1,3,2"
    assert state["eBackToTime"] == "2,3,1"


def test_external_sorts_by_title(state):
    """外部条目没有「公众号名」可排，「按名称」排的是标题（论文列表这样才有用）。"""
    assert state["eByNameAsc"] == "2,1,3"     # apple / Banana / Cherry
    assert state["eByNameDesc"] == "3,1,2"


def test_search_filter_still_applies(state):
    """过滤在前、排序在后，加了按名称也不能把搜索弄丢。"""
    assert state["eFiltered"] == "1"          # 只有 Banana 含 "an"
