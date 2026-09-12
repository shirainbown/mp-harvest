<script setup lang="ts">
// 页面：其他来源（外部目录）—— 登记目录 / 扫描建库 / 浏览 / AI 筛选 / 写回。
//
// 与「历史文章」的分工：这里管非公众号来源（典型是 arXiv 论文流水线的
// YYYY-MM-DD/papers_data.json 目录）。微信文章那条链路一行不改。
import { computed, onMounted, ref } from 'vue'
import SButton from '../components/SButton.vue'
import SInput from '../components/SInput.vue'
import SModal from '../components/SModal.vue'
import SBadge from '../components/SBadge.vue'
import SIcon from '../components/SIcon.vue'
import SSwitch from '../components/SSwitch.vue'
import STooltip from '../components/STooltip.vue'
import SegmentedControl from '../components/SegmentedControl.vue'
import SortControl from '../components/SortControl.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import EmptyState from '../components/EmptyState.vue'
import ProgressInline from '../components/ProgressInline.vue'
import { useExternalStore } from '../stores/external'
import { useTasksStore } from '../stores/tasks'
import { useUiStore } from '../stores/ui'
import { chooseDirectory, copyText, openExternal, openLocalPath } from '../api/desktop'

const ext = useExternalStore()
const tasks = useTasksStore()
const ui = useUiStore()

onMounted(() => {
  // v-show 常驻挂载：只在首次进来时拉一次，避免每次切页都打后端
  if (!ext.sources.length) void ext.loadAll()
})

// ---- 格式说明（2026-09）----
//
// 用户要一份「照着写就能导入」的格式说明。内容由后端给（`/api/external/format`），
// 前端只负责展示 —— 硬编码在前端的话，文档与解析器迟早各说各话。
// 默认收起：它是参考材料，不是每次都要看的东西。
const formatOpen = ref(false)

function toggleFormat() {
  formatOpen.value = !formatOpen.value
  if (formatOpen.value) void ext.loadFormat()
}

async function copyExample() {
  const text = ext.format?.example || ''
  if (!text) return
  if (await copyText(text)) ui.toast('已复制格式示例')
  else ui.error('复制失败，可手动选中复制')
}

// ---- 添加目录 ----
const addOpen = ref(false)
const addName = ref('')
const addPath = ref('')

function openAdd() {
  addName.value = ''
  addPath.value = ''
  addOpen.value = true
}

async function pickDir(target: 'add' | 'export') {
  const { path, reason } = await chooseDirectory()
  if (reason) {
    ui.error(reason)
    return
  }
  if (!path) return // 用户取消
  if (target === 'add') addPath.value = path
  else exportDir.value = path
}

async function confirmAdd() {
  const path = addPath.value.trim()
  if (!path) {
    ui.error('请填写目录路径')
    return
  }
  const ok = await ext.addSource(addName.value.trim(), path)
  if (!ok) return
  addOpen.value = false
  ui.toast('已登记目录，点「扫描」把里面的文章读进来')
}

// ---- 改名（就地编辑）----
// store 里一直有 renameSource，但没有任何入口 —— 来源目录只能扫描和移除，
// 登记时随手起的名字再也改不掉（2026-09 接上）。
const editingId = ref('')
const editingName = ref('')

function startRename(s: { id: string; name: string }) {
  editingId.value = s.id
  editingName.value = s.name
}

async function commitRename(id: string, original: string) {
  // Enter 会先清 editingId 触发 input 卸载 → 再触发一次 blur，这里挡住第二次
  if (editingId.value !== id) return
  editingId.value = ''
  const name = editingName.value.trim()
  if (!name || name === original) return
  if (await ext.renameSource(id, name)) ui.toast('已改名')
}

// ---- 移除 ----
const removeTarget = ref<{ id: string; name: string } | null>(null)

async function confirmRemove() {
  const t = removeTarget.value
  if (!t) return
  removeTarget.value = null
  if (await ext.removeSource(t.id)) ui.toast('已移除登记（磁盘上的文件未改动）')
}

// ---- 写回 ----
const exportOpen = ref(false)
const exportDir = ref('')
const exportDate = ref('')

function openExport() {
  exportDate.value = ''
  exportOpen.value = true
}

async function confirmExport() {
  exportOpen.value = false
  await ext.exportItems(exportDir.value.trim(), exportDate.value.trim())
}

// ---- AI 筛选 ----
const aiOpen = ref(false)
const aiStage = ref<'title' | 'content'>('title')
// SInput 的 modelValue 是 string；数字在提交时再钳位转换
const aiBatch = ref('50')
const aiWorkers = ref('4')

function _clamp(v: string, lo: number, hi: number, fallback: number): number {
  const n = Math.round(Number(v))
  if (!Number.isFinite(n)) return fallback
  return Math.max(lo, Math.min(hi, n))
}

function confirmAi() {
  aiOpen.value = false
  void ext.aiFilter(
    aiStage.value,
    _clamp(aiBatch.value, 1, 200, 50),
    _clamp(aiWorkers.value, 1, 16, 4),
  )
}

// ---- 工具条 ----
const q = computed({
  get: () => ext.q,
  set: (v: string) => {
    ext.q = v
  },
})
const sourceOptions = computed(() => [
  { value: '', label: `全部来源（${ext.sources.length}）` },
  ...ext.sources.map((s) => ({ value: s.id, label: s.name || s.path })),
])

const scanTask = computed(() => tasks.tasks[ext.scanTaskId])
const exportTask = computed(() => tasks.tasks[ext.exportTaskId])
const aiTask = computed(() => tasks.tasks[ext.aiTaskId])
const selectedCount = computed(() => ext.selectedInView.length)

function scannedAt(s: { last_scan_at: number }): string {
  if (!s.last_scan_at) return '未扫描'
  const d = new Date(s.last_scan_at * 1000)
  return `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

function day(date: string): string {
  const t = Date.parse(date || '')
  if (!t) return '—'
  const d = new Date(t)
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

function authorsText(authors: string[]): string {
  const list = authors || []
  if (!list.length) return ''
  return list.length > 2 ? `${list[0]} 等 ${list.length} 人` : list.join('、')
}

function rowReason(a: { verdict: string | null; reason: string; title_reason: string; content_reason: string }): string {
  return a.content_reason || a.title_reason || a.reason || ''
}

async function openItem(a: { url: string }) {
  if (!a.url) {
    ui.error('该条目没有原文链接')
    return
  }
  if (!(await openExternal(a.url))) ui.error('打开失败：链接无法在浏览器中打开')
}

async function openLocal(path: string, what: string) {
  if (!path) {
    ui.error(`该条目没有本地${what}`)
    return
  }
  // 走 shell_open，不走 openExternal('file://')：后者只放行 http(s)，会被静默挡下
  const { ok, reason } = await openLocalPath(path)
  if (!ok) ui.error(reason || `打开本地${what}失败`)
}

async function copyLink(a: { url: string }) {
  if (!a.url) {
    ui.error('该条目没有链接')
    return
  }
  const ok = await copyText(a.url)
  if (ok) ui.toast('链接已复制')
  else ui.error('复制失败（剪贴板不可用），可手动选中复制')
}
</script>

<template>
  <section class="view-root">
    <header class="page-header">
      <h1>其他来源</h1>
      <SButton size="sm" @click="openAdd">+ 登记目录</SButton>
    </header>
    <div class="page-body">
      <!-- 格式说明（可折叠）：照着写就能导入。内容来自后端，与解析器同源 -->
      <div class="panel" style="padding:var(--sp-2) var(--sp-4)">
        <div class="toolbar" style="cursor:pointer" @click="toggleFormat">
          <SIcon :name="formatOpen ? 'chevron-down' : 'chevron-right'" :size="12" />
          <span style="font-weight:500">格式说明</span>
          <span class="tertiary" style="font-size:var(--fs-sm)">
            —— 目录里放什么、每个字段是什么意思、怎么加原文
          </span>
        </div>
        <template v-if="formatOpen">
          <SkeletonRows v-if="!ext.format" :rows="3" />
          <div v-else class="fmt-body">
            <div class="muted" style="font-size:var(--fs-sm)">
              每个日期子目录里放一份
              <span v-for="(f, i) in ext.format.filenames" :key="f">
                <span class="mono">{{ f }}</span><template v-if="i < ext.format.filenames.length - 1"> 或 </template>
              </span>
              （内容一样，挑一个名字即可）。下面这些字段<b>都可以不写</b>，
              但至少要有一个标题或链接。
            </div>
            <table class="fmt-table">
              <thead>
                <tr><th style="width:150px">字段</th><th style="width:70px">必填</th><th>说明</th></tr>
              </thead>
              <tbody>
                <tr v-for="f in ext.format.fields" :key="f.name">
                  <td class="mono">{{ f.name }}</td>
                  <td class="tertiary">{{ f.need || '—' }}</td>
                  <td>{{ f.desc }}</td>
                </tr>
              </tbody>
            </table>
            <div class="toolbar" style="margin-top:var(--sp-3)">
              <span style="font-weight:500">完整示例</span>
              <span class="tertiary" style="font-size:var(--fs-sm)">
                —— 三种情形各一条：带 PDF 原文的、带 HTML 原文的、只有摘要的
              </span>
              <span class="spacer"></span>
              <SButton size="sm" variant="ghost" @click="copyExample">复制示例</SButton>
            </div>
            <pre class="ai-principle-preview">{{ ext.format.example }}</pre>
          </div>
        </template>
      </div>

      <!-- 来源目录 -->
      <div class="panel">
        <div class="panel-title">来源目录</div>
        <div v-if="!ext.sources.length" class="muted" style="font-size:var(--fs-sm)">
          还没有登记目录。点右上角「+ 登记目录」选一个目录
          （其下按 <span class="mono">YYYY-MM-DD/</span> 分日期子目录，每个子目录里放一份
          <span class="mono">papers_data.json</span>）。格式见下面的「格式说明」，
          登记后点「扫描」把条目读进来。
        </div>
        <div v-else class="ext-table">
          <div class="ext-head">
            <span>名称</span><span>路径</span><span>条目</span>
            <span>上次扫描</span><span>启用</span><span></span>
          </div>
          <div v-for="s in ext.sources" :key="s.id" class="ext-row">
            <input
              v-if="editingId === s.id"
              v-model="editingName"
              class="input"
              style="height:24px;font-size:var(--fs-xs);width:100%"
              @keydown.enter="commitRename(s.id, s.name)"
              @keydown.esc="editingId = ''"
              @blur="commitRename(s.id, s.name)"
            />
            <STooltip v-else :text="`${s.name || '（未命名）'} · 点击改名`" style="min-width:0">
              <span class="acct-name" style="cursor:text" @click="startRename(s)">
                {{ s.name || '（未命名）' }}
              </span>
            </STooltip>
            <STooltip :text="s.path" style="min-width:0">
              <span class="ext-path">{{ s.path }}</span>
            </STooltip>
            <span class="mono muted">{{ s.item_count }}</span>
            <span class="muted" style="font-size:var(--fs-xs)">
              {{ scannedAt(s) }}
              <template v-if="s.last_scan_error">
                <br /><span class="ext-err">{{ s.last_scan_error }}</span>
              </template>
            </span>
            <SSwitch
              :model-value="Number(s.enabled) === 1"
              @update:model-value="ext.toggleSource(s.id, $event as boolean)"
            />
            <span class="row-actions">
              <ProgressInline
                v-if="ext.scanningId === s.id && scanTask"
                :text="ext.aiProgress || scanTask.message || '扫描中…'"
                cancellable
                @cancel="ext.cancelScan()"
              />
              <template v-else>
                <SButton size="sm" variant="ghost" :disabled="!!ext.scanTaskId" @click="ext.scan(s.id)">
                  扫描
                </SButton>
                <SButton size="sm" variant="ghost" @click="removeTarget = { id: s.id, name: s.name || s.path }">
                  移除
                </SButton>
              </template>
            </span>
          </div>
        </div>
      </div>

      <!-- 工具条 -->
      <div class="panel" style="padding:var(--sp-2) var(--sp-4)">
        <div class="toolbar">
          <span class="form-label">来源</span>
          <select
            class="input"
            style="width:200px"
            :value="ext.sourceId"
            @change="ext.setSource(($event.target as HTMLSelectElement).value)"
          >
            <option v-for="o in sourceOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
          </select>
          <SInput v-model="q" placeholder="搜索标题 / 作者 / 领域…" style="width:200px" />
          <span class="form-label">排序</span>
          <SortControl :by="ext.sortBy" :dir="ext.sortDir" @pick="ext.pickSort($event)" />
          <span class="spacer"></span>
          <SButton size="sm" variant="ghost" @click="ext.selectAllVisible()">全选</SButton>
          <SButton size="sm" variant="ghost" @click="ext.clearSelection()">取消选择</SButton>
          <span class="badge sel-badge" title="当前视图已选">{{ selectedCount }}</span>
          <!-- 单项下拉没意义：原来是个只含一条的菜单，白白多一次点击（2026-09 合并） -->
          <SButton size="sm" variant="primary" :disabled="!ext.visible.length" @click="openExport()">
            写回…
          </SButton>
          <ProgressInline
            v-if="exportTask"
            :text="exportTask.message || '写回中…'"
            cancellable
            @cancel="ext.cancelExport()"
          />
          <SButton size="sm" :disabled="!ext.visible.length || !!ext.aiTaskId" @click="aiOpen = true">
            <SIcon name="sparkles" :size="12" /> AI 筛选
          </SButton>
        </div>
        <div v-if="aiTask" class="toolbar" style="padding-top:var(--sp-2)">
          <span class="ai-progress">
            <span class="spinner"></span>{{ ext.aiProgress || aiTask.message }} {{ Math.round(aiTask.percent) }}%
          </span>
        </div>
      </div>

      <!-- 条目表格 -->
      <div class="art-table">
        <div class="art-head">
          <span></span><span>来源</span><span>标题</span><span>作者 / 领域</span>
          <span>日期</span><span>判定</span><span></span>
        </div>
        <div class="art-scroll">
          <SkeletonRows v-if="ext.loading" :rows="8" />
          <EmptyState
            v-else-if="!ext.visible.length"
            :text="ext.sources.length ? '这个来源还没有条目，点上面的「扫描」读一次' : '先登记一个来源目录'"
          />
          <template v-else>
            <div v-for="a in ext.visible" :key="a.id" class="art-row" @dblclick="openItem(a)">
              <span>
                <input
                  type="checkbox"
                  class="cb"
                  :checked="ext.selected.has(a.id)"
                  @change="ext.toggleSelect(a.id, ($event.target as HTMLInputElement).checked)"
                />
              </span>
              <span class="muted" style="font-size:var(--fs-xs);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                {{ a.account_name || '—' }}
              </span>
              <STooltip :text="a.title" style="min-width:0">
                <span class="art-title">{{ a.title }}</span>
              </STooltip>
              <STooltip :text="[authorsText(a.authors), a.domain, a.primary_category].filter(Boolean).join(' · ')" style="min-width:0">
                <span class="art-reason">
                  {{ authorsText(a.authors) || '—' }}<template v-if="a.domain"> · {{ a.domain }}</template>
                </span>
              </STooltip>
              <span class="mono muted">{{ day(a.date) }}</span>
              <STooltip :text="rowReason(a) || '未判定'">
                <SBadge :variant="a.verdict === 'keep' ? 'g' : a.verdict === 'drop' ? 'm' : 'x'">
                  {{ a.verdict === 'keep' ? '通过' : a.verdict === 'drop' ? '过滤' : '待判' }}
                </SBadge>
              </STooltip>
              <span class="row-actions">
                <SButton size="sm" variant="ghost" @click="openItem(a)">原文</SButton>
                <SButton
                  v-if="a.body_path"
                  size="sm"
                  variant="ghost"
                  @click="openLocal(a.body_path, '正文')"
                >
                  正文
                </SButton>
                <!-- 「本地原文」而不是「PDF」：原文可能是 PDF / HTML / TXT / MD
                     （2026-09 泛化）。也不叫「原文」—— 那个名字已经被上面打开
                     在线链接的按钮占了。 -->
                <SButton
                  v-if="a.fulltext_path || a.pdf_path"
                  size="sm"
                  variant="ghost"
                  @click="openLocal(a.fulltext_path || a.pdf_path, '本地原文')"
                >
                  本地原文
                </SButton>
                <SButton size="sm" variant="ghost" @click="copyLink(a)">复制</SButton>
              </span>
            </div>
          </template>
        </div>
      </div>
    </div>

    <!-- 登记目录 Modal -->
    <SModal :open="addOpen" @close="addOpen = false">
      <template #head>登记外部来源目录</template>
      <div style="display:flex;flex-direction:column;gap:8px">
        <span>
          选一个目录，其下按 <span class="mono">YYYY-MM-DD/</span> 分日期子目录，
          每个子目录里放一份 <span class="mono">papers_data.json</span>。
          论文、技术文章、网页存档都行 —— 格式见「格式说明」面板，可一键复制示例。
        </span>
        <div class="mitm-row">
          <span class="form-label" style="width:48px">名称</span>
          <SInput v-model="addName" placeholder="留空则用目录名" style="flex:1" />
        </div>
        <div class="mitm-row">
          <span class="form-label" style="width:48px">路径</span>
          <SInput v-model="addPath" mono placeholder="~/Downloads/mp-harvest-sources" style="flex:1" />
          <SButton size="sm" @click="pickDir('add')">选择目录…</SButton>
        </div>
        <span class="muted" style="font-size:var(--fs-sm)">
          支持 <span class="mono">~</span> 展开。登记只是记下路径，<b>不会改动目录里的任何文件</b>。
        </span>
      </div>
      <template #foot>
        <SButton variant="ghost" @click="addOpen = false">取消</SButton>
        <SButton variant="primary" @click="confirmAdd">登记</SButton>
      </template>
    </SModal>

    <!-- 移除确认 -->
    <SModal :open="!!removeTarget" @close="removeTarget = null">
      <template #head>移除来源目录</template>
      <div style="display:flex;flex-direction:column;gap:8px">
        <span>
          将移除登记 <b style="color:var(--text-primary)">{{ removeTarget?.name }}</b>
          及其在库里的条目索引。
        </span>
        <span class="muted" style="font-size:var(--fs-sm)">
          <b>磁盘上的目录和文件不会被删除</b>，随时可以重新登记。
        </span>
      </div>
      <template #foot>
        <SButton variant="ghost" @click="removeTarget = null">取消</SButton>
        <SButton variant="primary" @click="confirmRemove">移除</SButton>
      </template>
    </SModal>

    <!-- 写回目录 Modal -->
    <SModal :open="exportOpen" @close="exportOpen = false">
      <template #head>写回外部目录</template>
      <div style="display:flex;flex-direction:column;gap:8px">
        <span>
          把当前视图的
          <b style="color:var(--text-primary)">{{ selectedCount || ext.visible.length }}</b>
          条写成 <span class="mono">YYYY-MM-DD/papers_data.json</span>（元数据）+ 逐篇正文 HTML。
        </span>
        <div class="mitm-row">
          <span class="form-label" style="width:48px">目录</span>
          <SInput v-model="exportDir" mono placeholder="留空 = 设置里的默认目录/其他来源/…" style="flex:1" />
          <SButton size="sm" @click="pickDir('export')">选择目录…</SButton>
        </div>
        <div class="mitm-row">
          <span class="form-label" style="width:48px">日期目录</span>
          <SInput v-model="exportDate" mono placeholder="留空 = 按条目自身日期（YYYY-MM-DD）" style="flex:1" />
        </div>
        <span class="muted" style="font-size:var(--fs-sm)">
          目标目录里已有的 <span class="mono">papers_data.json</span> 会按条目合并，
          <b>不属于本次导出的条目原样保留</b>。
        </span>
      </div>
      <template #foot>
        <SButton variant="ghost" @click="exportOpen = false">取消</SButton>
        <SButton variant="primary" @click="confirmExport">写回</SButton>
      </template>
    </SModal>

    <!-- AI 筛选 Modal -->
    <SModal :open="aiOpen" @close="aiOpen = false">
      <template #head>AI 筛选（其他来源）</template>
      <div style="display:flex;flex-direction:column;gap:8px">
        <span>
          对<b style="color:var(--text-primary)">{{ selectedCount || ext.visible.length }}</b>
          条运行筛选原则。判定结果与公众号文章**共用同一套缓存**，同一篇不会重复判定。
        </span>
        <div class="mitm-row">
          <span class="form-label" style="width:60px">阶段</span>
          <SegmentedControl
            :model-value="aiStage"
            :options="[
              { value: 'title', label: '标题筛选' },
              { value: 'content', label: '内容筛选' },
            ]"
            @update:model-value="aiStage = $event as 'title' | 'content'"
          />
        </div>
        <div class="mitm-row">
          <span class="form-label" style="width:60px">每批篇数</span>
          <SInput v-model="aiBatch" style="width:90px" />
          <span class="form-label">并发批数</span>
          <SInput v-model="aiWorkers" style="width:90px" />
        </div>
        <span v-if="aiStage === 'content'" class="muted" style="font-size:var(--fs-sm)">
          正文优先读本地正文文件，没有就用摘要（未写回过的目录读到的是论文摘要）——
          <b>不需要先做标题筛选</b>，外部条目取正文不联网。
        </span>
      </div>
      <template #foot>
        <SButton variant="ghost" @click="aiOpen = false">取消</SButton>
        <SButton variant="primary" @click="confirmAi">开始筛选</SButton>
      </template>
    </SModal>
  </section>
</template>

<style scoped>
/* 格式说明面板（2026-09）。只在本页用，所以留在组件里，不进全局 style.css */
.fmt-body {
  padding-top: var(--sp-2);
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.fmt-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-sm);
}
.fmt-table th {
  text-align: left;
  font-weight: 600;
  color: var(--text-secondary);
  padding: 4px 8px;
  border-bottom: 1px solid var(--border);
}
.fmt-table td {
  padding: 4px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
  color: var(--text-secondary);
  line-height: 1.6;
}
/* 示例块复用全局 .ai-principle-preview 的外观（等宽、限高、可滚动），
   这里只把限高放宽一点 —— 示例有二十多行，140px 看不全 */
.fmt-body .ai-principle-preview {
  max-height: 300px;
}
</style>
