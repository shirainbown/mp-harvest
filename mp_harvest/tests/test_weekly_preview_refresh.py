"""周报候选的刷新与「建议值只种一次」（2026-09）。

用户报的：「已经删除了文章信息之后，周报生成中还是显示有候选文章」。

根因与「存储占用」那次同一类：所有视图是 `v-show` 常驻挂载的，`onMounted` 只在
应用启动时跑一次，而 `WeeklyView` 拉候选写的是 `if (!weekly.preview)` —— 于是
「候选共 N 篇」永远停在首次加载时的数字。删完文章切过去，看到的还是旧的。
修法是切回本页时重算（`WeeklyView` 里 watch `ui.view`）。

**顺带挖出一个既有 bug**：重算要调 `loadPreview()`，而它里面
``this.issueNum = r.suggested_issue`` 是**无条件赋值** —— 用户手改期号之后，
随便动一下「只看未筛」或勾一个来源就会被打回建议值。所以「重算」的前提是
后端建议值必须**只种一次**。

这里用 esbuild + node 装置驱动**真实的 store**，并把 `fetch` 桩掉
（`api/rest.ts` 就是走 fetch 的，桩得住）。缺 node / esbuild 时跳过。
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

_HARNESS = """
globalThis.location = { search: '', href: 'http://localhost/', origin: 'http://localhost' }
globalThis.window = globalThis

let calls = 0
let payload = {
  from_date: '2026-09-01', to_date: '2026-09-07', suggested_issue: 17,
  out_dir: '/tmp/out', selected_count: 15, total: 40, wechat: 40, arxiv: 0,
  external_other: 0, account_counts: {}, source_counts: {},
  template_path: 'x', template_is_custom: false, template_exists: true,
}
// rest.ts 走 fetch；headers.get 必须实现，否则 request() 读 content-type 时抛，
// call() 吞掉异常返回 null —— 那样 store 根本没被驱动，测试会静默空转
globalThis.fetch = async (url) => {
  calls++
  const isDelete = String(url).includes('/api/articles/delete')
  return {
    ok: true, status: 200,
    headers: { get: () => 'application/json' },
    json: async () => (isDelete ? { ok: true, removed: 1 } : payload),
  }
}

const { createPinia, setActivePinia } = await import('pinia')
const { useWeeklyStore } = await import('./src/stores/weekly.ts')
setActivePinia(createPinia())
const w = useWeeklyStore()
const out = {}

await w.loadPreview()
out.firstIssue = w.issueNum
out.firstTotal = w.preview ? w.preview.total : null

// 用户手改期号，然后**又一次 loadPreview**（改日期 / 勾来源 / 切回本页都会走）
w.issueNum = 5
payload = { ...payload, total: 33, suggested_issue: 99 }
await w.loadPreview()
out.afterEditIssue = w.issueNum
out.afterTotal = w.preview ? w.preview.total : null
out.calls = calls

// 目录与篇数同理：用户改过就不该被后端值顶掉
w.outDir = '/user/picked'
w.selectedCount = 30
await w.loadPreview()
out.afterEditDir = w.outDir
out.afterEditCount = w.selectedCount

// ── 文章列表变了 → 周报候选要跟着重算 ──
//
// 这是用户报的那条：「删除了文章信息之后，周报生成中还是显示有候选文章」。
// 不靠「切到周报页时刷新」（那层 watch 没有 DOM 装置测不了），而是让**动作**
// 主动作废 —— 放在 store 里才钉得住。
const { useArticlesStore } = await import('./src/stores/articles.ts')
const a = useArticlesStore()
a.list = [{ id: 'art1', title: 'T', url: '', date: '', source: 'G',
            verdict: null, reason: '', title_verdict: null, title_reason: '',
            content_verdict: null, content_reason: '', exported: false }]
a.accountId = 'acc1'

payload = { ...payload, total: 7 }
out.beforeDelete = w.preview.total
await a.remove(['art1'])
// 重算是 fire-and-forget（`void wk.loadPreview()`，不该让删除动作等它），
// 所以要放一拍再读；不放的话读到的是重算前的旧值，测试会假红
await new Promise((r) => setTimeout(r, 10))
out.afterDelete = w.preview ? w.preview.total : null
out.removedToast = a.list.length

// 没加载过周报预览时不该白发请求去查候选（用户还没打开过那一页）
const w2 = useWeeklyStore()
setActivePinia(createPinia())          // 换一个全新的 pinia，模拟「从没打开过周报页」
const a2 = useArticlesStore()
const w3 = useWeeklyStore()
out.freshPreview = w3.preview
const callsBefore = calls
a2.list = [{ id: 'art2', title: 'T2', url: '', date: '', source: 'G',
             verdict: null, reason: '', title_verdict: null, title_reason: '',
             content_verdict: null, content_reason: '', exported: false }]
await a2.remove(['art2'])
out.extraCallsWhenNeverLoaded = calls - callsBefore
void w2

console.log(JSON.stringify(out))
"""


@pytest.fixture(scope="module")
def state() -> dict:
    harness = FRONTEND / "_weekly_probe.mjs"
    bundled = FRONTEND / "_weekly_probe.built.mjs"
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


def test_preview_reloads_with_fresh_counts(state):
    """再次 loadPreview 必须拿到**新的**候选数 —— 这是「切回本页重算」的地基。

    桩里的 total 从 40 改成 33 来模拟「文章被删了」。若这里还是 40，说明刷新
    根本没生效，用户看到的就还是旧数字。
    """
    assert state["firstTotal"] == 40
    assert state["afterTotal"] == 33, "再次加载没拿到新的候选数"
    # 读到 calls 时已发生两次 loadPreview；要有第二次，才算「真的重新请求了」，
    # 而不是被某种短路挡掉（那样 total 也永远是旧的）
    assert state["calls"] == 2, state


def test_suggested_issue_is_seeded_only_once(state):
    """后端建议值**只种一次** —— 用户改过的期号不能被刷新打回建议值。

    原先 `this.issueNum = r.suggested_issue` 是无条件赋值：改完期号再动一下
    筛选就变回去了（「我明明改了」）。而「切回本页重算」会让这种情况从偶发
    变成每次必现，所以这条是刷新方案的前提。
    """
    assert state["firstIssue"] == 17, "首次该填后端建议的期号"
    assert state["afterEditIssue"] == 5, "用户改过的期号被建议值顶掉了"


def test_other_seeded_inputs_also_stay(state):
    """目录与篇数同理：用户改过就不能被顶掉。"""
    assert state["afterEditDir"] == "/user/picked"
    assert state["afterEditCount"] == 30


def test_deleting_articles_recomputes_weekly_candidates(state):
    """删掉文章后，周报的候选数要**自己**跟着变。

    用户报的：「已经删除了文章信息之后，周报生成中还是显示有候选文章」。
    不靠「切到周报页时刷新」—— 那层 watch 没有 DOM 装置测不了、也容易漏；
    由删除动作主动重算，才钉得住。
    """
    assert state["beforeDelete"] == 33
    assert state["afterDelete"] == 7, "删完文章后周报候选数没重算"
    assert state["removedToast"] == 0, "本地列表该把那篇摘掉"


def test_no_extra_request_when_weekly_was_never_opened(state):
    """用户从没打开过周报页时，删除不该白发一次查候选的请求。"""
    assert state["freshPreview"] is None, "前提：新 pinia 里周报还没加载过"
    assert state["extraCallsWhenNeverLoaded"] == 1, (
        "只该有删除本身那一个请求；多出来的是白查了一次候选"
    )
