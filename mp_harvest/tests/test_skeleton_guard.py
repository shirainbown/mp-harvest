"""骨架屏「只在首次加载时出现」（2026-09）。

用户报的：**执行日志全空时，切换 全部/信息 会闪一下**。根因是视图里的条件写成
`loading && !events.length` —— 列表本来就空，切筛选把 loading 置真，于是

    空状态 → 6 行骨架 → 空状态

中间那一步没有任何信息量，纯闪烁。同一模式在「设置 → 存储占用」也有一份。

改用 `loading && !loaded`（首次加载中才显示）。这条判断**任何既有测试都看不见**，
所以按项目既有做法（同 `articles.pickSort`：视图里的逻辑没法单测，store 里的可以）
把它提成 store 的 `showSkeleton` getter，在这里钉住。

**为什么必须有一个「已加载过 + 列表空 + 正在加载」的用例**：那正是出问题的那一格。
少了它，`loading && !events.length` 这个变异体在其余几格上表现完全一样，测不出来。

沿用 `test_sort_control.py` 的 esbuild + node 装置：驱动**真实的 store 模块**，
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
const { useLogsStore } = await import('./src/stores/logs.ts')
const { useStorageStore } = await import('./src/stores/storage.ts')

const out = {}
setActivePinia(createPinia())

/** 把一个 store 在四种「加载/已加载 × 空/非空」组合下过一遍。
 *  直接摆状态，不走 load() —— 要走的话得连 fetch 一起桩掉，
 *  而这里要钉的是**判断本身**，不是请求。 */
function sweep(s, fill) {
  const rows = []
  rows.push(['coldEmpty', s.showSkeleton])       // 没加载过、没数据
  s.loading = true
  rows.push(['firstLoad', s.showSkeleton])       // 首次加载中 → 该显示
  s.loaded = true
  rows.push(['reloadEmpty', s.showSkeleton])     // 已加载过、列表空、又在加载 ← 曾经闪的那一格
  if (fill) fill(s)
  rows.push(['reloadFilled', s.showSkeleton])    // 有内容时加载
  s.loading = false
  rows.push(['idle', s.showSkeleton])
  return Object.fromEntries(rows)
}

out.logs = sweep(useLogsStore(), (s) => {
  s.events = [{ id: 1, ts: 0, level: 'info', kind: 'action', message: 'm', data: {} }]
  s.total = 1
})
out.storage = sweep(useStorageStore(), (s) => {
  s.items = [{ key: 'k', label: 'l', path: 'p', size: 1, count: 1, safe: true, note: '' }]
})

console.log(JSON.stringify(out))
"""


@pytest.fixture(scope="module")
def state() -> dict:
    """跑一遍探针，返回两个 store 在四种组合下的骨架屏判断。"""
    harness = FRONTEND / "_skeleton_probe.mjs"
    bundled = FRONTEND / "_skeleton_probe.built.mjs"
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


def test_first_load_still_shows_skeleton(state):
    """别修过头：冷启动首次进来**该**有加载指示器，否则空列表和「正在加载」分不出来。"""
    assert state["logs"]["firstLoad"] is True
    assert state["storage"]["firstLoad"] is True


def test_filtering_an_empty_list_does_not_flash(state):
    """被修的那一格：已加载过 + 列表空 + 正在加载 → 不该再插骨架屏。

    变异体 `loading && !events.length` 只在这一格上暴露。"""
    assert state["logs"]["reloadEmpty"] is False
    assert state["storage"]["reloadEmpty"] is False


def test_no_skeleton_before_or_after_loading(state):
    """没在加载就不该有骨架屏（含冷启动、加载完成两个时刻）。"""
    for name in ("logs", "storage"):
        assert state[name]["coldEmpty"] is False, name
        assert state[name]["idle"] is False, name


def test_loading_with_content_does_not_blank_the_list(state):
    """列表有内容时再加载，不能把已有内容换成骨架屏（那是另一种闪）。"""
    assert state["logs"]["reloadFilled"] is False
    assert state["storage"]["reloadFilled"] is False
