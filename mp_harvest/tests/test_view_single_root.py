"""每个视图 SFC 必须只有**一个根节点**（2026-09）。

`App.vue` 用 `<XxxView v-show="ui.view === 'xxx'" />` 切页。Vue 的 `v-show` /
`class` 这类非 props 属性**只能落在单根组件上** —— 组件一旦有多个根节点，
Vue 会打一条运行时 warning 然后把属性**静默丢掉**：`v-show` 不生效，
于是所有页面**同时渲染**出来叠在一起。

踩到的经过：给「周报生成」加候选明细抽屉时，把 `<SDrawer>` 写在了
`</section>` **外面**，那一页就变成了多根节点。抽屉自己看着完全正常，
是截了另一页的图才发现的 —— 因为**这一个页面**的 v-show 失效会把**它自己**
一直显示出来。这类故障没有报错、没有类型错误、跑什么都绿，只能这么钉。

用 `@vue/compiler-sfc` 解析模板 AST 数根节点，比正则可靠（注释、换行、
`<template>` 里的条件分支都骗不过 AST）。缺依赖时跳过。

⚠️ **不用 esbuild 打包**（其余前端测试那套装置在这里不适用）：`@vue/compiler-sfc`
对 velocityjs / dustjs-linkedin 之类的模板引擎有一堆可选 `require`，esbuild 静态
解析时会把它们当硬依赖、直接打包失败。node 直接跑 `.mjs` 反而不碰这些问题 ——
需要的只是 node_modules 里的解析能力，不是打包。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "mp_harvest" / "frontend"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None or not (FRONTEND / "node_modules" / "@vue" / "compiler-sfc").exists(),
    reason="需要 node + @vue/compiler-sfc（没装前端依赖时跳过）",
)

# 1 = NodeTypes.ELEMENT；只数元素，注释/插值不算根
_HARNESS = """
import { parse } from '@vue/compiler-sfc'
import { readdirSync, readFileSync } from 'node:fs'

const out = {}
for (const f of readdirSync('src/views').filter((n) => n.endsWith('.vue'))) {
  const { descriptor, errors } = parse(readFileSync(`src/views/${f}`, 'utf-8'), { filename: f })
  if (errors.length) { out[f] = { parseErrors: errors.length }; continue }
  const tpl = descriptor.template
  if (!tpl || !tpl.ast) { out[f] = { roots: null }; continue }
  out[f] = { roots: tpl.ast.children.filter((n) => n.type === 1).length }
}
console.log(JSON.stringify(out))
"""


@pytest.fixture(scope="module")
def roots() -> dict:
    harness = FRONTEND / "_root_probe.mjs"
    harness.write_text(_HARNESS, encoding="utf-8")
    try:
        proc = subprocess.run(
            [str(NODE), str(harness)],
            cwd=str(FRONTEND), check=True, capture_output=True, text=True, timeout=120,
        )
        return json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        harness.unlink(missing_ok=True)


def test_probe_saw_every_view(roots):
    """先确认探针真的解析到了模板 —— 否则下面的断言会因为「一个都没读到」而空转。"""
    assert len(roots) >= 7, f"只读到 {len(roots)} 个视图，探针大概坏了：{roots}"
    assert not [f for f, r in roots.items() if r.get("parseErrors")], "有模板解析失败"


def test_every_view_has_exactly_one_root(roots):
    """根节点不是 1 的，`v-show` 会静默失效、该页永远显示在别人上面。"""
    bad = {f: r["roots"] for f, r in roots.items() if r.get("roots") != 1}
    assert not bad, (
        f"这些视图不是单根节点：{bad}。"
        "把多出来的元素（抽屉/弹层等）挪进根元素**内部** —— "
        "它们通常自己会 Teleport 出去，放里面不影响定位。"
    )
