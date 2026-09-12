"""周报「候选来源」全选 / 全不选的勾选状态机（2026-09 修）。

前端没有测试框架（`package.json` 只有 type-check / build），但**这块逻辑出过两次
「点了没反应」**：

- 最早的病根是「不勾 = 全部」—— 默认一个都不勾，于是「全部」链接在默认状态下
  点了等于没点（状态本来就是全部）。
- 现在默认全选、勾 = 纳入，`toggleAllAccounts` / `toggleAllSources` 是一键切换。

这里把**真实的 store 模块**用 esbuild 打包出来，在 node 里驱动它跑状态机 ——
跳过 Vue 组件层，但覆盖的正是不出声出问题的那部分。

环境缺 node / esbuild 时**跳过**，不让 `pytest` 反过来依赖 `npm install`。
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

# 在 node 里驱动真实 store 的探针。config.ts 在模块加载时读 location.search，
# 所以先补上这两个全局再动态 import。
_HARNESS = """
globalThis.location = { search: '', href: 'http://localhost/', origin: 'http://localhost' }
globalThis.window = globalThis
const { createPinia, setActivePinia } = await import('pinia')
const { useWeeklyStore } = await import('./src/stores/weekly.ts')

const ACC = ['a1', 'a2', 'a3'], SRC = ['s1', 's2']
const out = {}

setActivePinia(createPinia())
const s = useWeeklyStore()

s.seedSelection(ACC, SRC)
out.seededAccounts = [...s.accountIds].sort()
out.seededSources = [...s.sourceIds].sort()

// 全选 → 全不选 → 全选
out.toggle1 = s.toggleAllAccounts(ACC)
out.afterToggle1 = [...s.accountIds]
out.toggle2 = s.toggleAllAccounts(ACC)
out.afterToggle2 = [...s.accountIds].sort()

// 部分勾选时点「全选」应当补齐
s.accountIds = new Set(['a1'])
out.partialToggle = s.toggleAllAccounts(ACC)
out.partialAfter = [...s.accountIds].sort()

// 来源侧同一套逻辑
out.srcToggle1 = s.toggleAllSources(SRC)
out.srcAfter1 = [...s.sourceIds]
out.srcToggle2 = s.toggleAllSources(SRC)
out.srcAfter2 = [...s.sourceIds].sort()

// 种子只种一次：用户改过的不被覆盖
s.accountIds = new Set(['a2'])
s.seedSelection(ACC, SRC)
out.afterReseed = [...s.accountIds]

// 数据还没到（两个列表都空）时不该种
setActivePinia(createPinia())
const s3 = useWeeklyStore()
s3.seedSelection([], [])
out.emptySeedFlag = s3.selectionSeeded
out.emptySeedIds = [...s3.accountIds]
// 只有来源也算「数据到了」
s3.seedSelection([], SRC)
out.srcOnlySeeded = s3.selectionSeeded
out.srcOnlyIds = [...s3.sourceIds].sort()

console.log(JSON.stringify(out))
"""


@pytest.fixture(scope="module")
def state() -> dict:
    """跑一遍探针，返回真实 store 的状态快照。"""
    harness = FRONTEND / "_selection_probe.mjs"
    bundled = FRONTEND / "_selection_probe.built.mjs"
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


def test_default_selection_is_all(state):
    """默认全选是这次改动的核心：勾 = 纳入，所见即所得。

    旧行为是「空 = 全部」—— 默认一个都不勾，于是「全部」链接点了没反应。
    """
    assert state["seededAccounts"] == ["a1", "a2", "a3"]
    assert state["seededSources"] == ["s1", "s2"]


def test_toggle_all_alternates(state):
    """点一下取消、再点一下全选 —— 两个方向都要有可见变化。"""
    assert state["toggle1"] is False
    assert state["afterToggle1"] == []
    assert state["toggle2"] is True
    assert state["afterToggle2"] == ["a1", "a2", "a3"]


def test_partial_selection_fills_up(state):
    """只勾了一个时点「全选」应当补齐，而不是清空。"""
    assert state["partialToggle"] is True
    assert state["partialAfter"] == ["a1", "a2", "a3"]


def test_sources_toggle_same_way(state):
    assert state["srcToggle1"] is False
    assert state["srcAfter1"] == []
    assert state["srcToggle2"] is True
    assert state["srcAfter2"] == ["s1", "s2"]


def test_seed_does_not_clobber_user_choice(state):
    """种子只种一次 —— 列表刷新不能把用户的选择冲掉。"""
    assert state["afterReseed"] == ["a2"]


def test_seed_waits_for_data(state):
    """账号/来源都还没加载时不能种（否则种了个空，之后再也不种）。"""
    assert state["emptySeedFlag"] is False
    assert state["emptySeedIds"] == []
    # 只有来源也算数据到了
    assert state["srcOnlySeeded"] is True
    assert state["srcOnlyIds"] == ["s1", "s2"]
