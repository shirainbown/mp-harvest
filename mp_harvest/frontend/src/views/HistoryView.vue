<script setup lang="ts">
// 页面二：历史文章（§5.5）
import { computed, onMounted, ref, watch } from 'vue'
import { useVirtualizer } from '@tanstack/vue-virtual'
import SButton from '../components/SButton.vue'
import SInput from '../components/SInput.vue'
import SModal from '../components/SModal.vue'
import SPopover from '../components/SPopover.vue'
import STooltip from '../components/STooltip.vue'
import SegmentedControl from '../components/SegmentedControl.vue'
import SortControl from '../components/SortControl.vue'
import SBadge from '../components/SBadge.vue'
import SIcon from '../components/SIcon.vue'
import ProgressInline from '../components/ProgressInline.vue'
import EmptyState from '../components/EmptyState.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import { LIST_FORMATS, useArticlesStore } from '../stores/articles'
import { useExternalStore } from '../stores/external'
import { copyText, openExternal } from '../api/desktop'
import { useAccountsStore } from '../stores/accounts'
import { useTasksStore } from '../stores/tasks'
import { useSettingsStore } from '../stores/settings'
import { useUiStore } from '../stores/ui'
import { useTicker } from '../composables/useTicker'
import type { Article, ArticleView } from '../types'

const articles = useArticlesStore()
const ext = useExternalStore()
const accounts = useAccountsStore()
const tasks = useTasksStore()
const settings = useSettingsStore()
const ui = useUiStore()
const now = useTicker()

// ---- 来源筛选：公众号 / 其他来源 / 全部（2026-09）----
//
// 回流是**纯前端**的：GET /api/articles 一行不改，切到「其他来源」时表格换数据源。
// 这样微信那条链路（含它全部的测试）行为完全不变，回归面最小。
const sourceScope = ref<'wechat' | 'external' | 'all'>('wechat')
const sourceScopeOptions = [
  { value: 'wechat', label: '公众号' },
  { value: 'external', label: '其他来源' },
  { value: 'all', label: '全部' },
]
/** 外部条目的 id 集合：勾选/判定路由靠它区分一行属于哪边，比猜 id 格式可靠 */
const extIds = computed(() => new Set(ext.items.map((i) => i.id)))
const rowIsExternal = (a: { id: string }) => extIds.value.has(a.id)

/** 表格数据源。仅公众号时与改造前完全一致 */
const rows = computed<Article[]>(() => {
  if (sourceScope.value === 'wechat') return articles.visible
  if (sourceScope.value === 'external') return ext.visible
  // 全部：两边合并后按时间统一排序（外部条目的 date 与微信一样是 ISO）
  return [...articles.visible, ...ext.visible].sort(
    (a, b) => Date.parse(b.date || '') - Date.parse(a.date || ''),
  )
})

/** 「全部」下动作按钮无处可去（导出/AI 都要先确定来源），一律禁用并说明 */
const mixedScope = computed(() => sourceScope.value === 'all')
const wechatOnly = computed(() => sourceScope.value === 'external')

function setSourceScope(v: string) {
  sourceScope.value = v as 'wechat' | 'external' | 'all'
  // 切来源时清空勾选：两个 Set 各自独立，留着旧来源的 id 会串味
  articles.clearSelection()
  ext.clearSelection()
  if (v !== 'wechat' && !ext.sources.length) void ext.loadAll()
  else if (v !== 'wechat') void ext.load()
}

function toggleRow(a: Article, on: boolean) {
  if (rowIsExternal(a)) ext.toggleSelect(a.id, on)
  else articles.toggleSelect(a.id, on)
}

function selectAllRows() {
  if (sourceScope.value === 'external') ext.selectAllVisible()
  else if (sourceScope.value === 'wechat') articles.selectAllVisible()
  else rows.value.forEach((a) => toggleRow(a, true))
}

/** 已选数量：按当前来源范围取（两个 store 的勾选各自独立） */
const selectedCount = computed(() => {
  if (sourceScope.value === 'external') return ext.selectedInView.length
  if (sourceScope.value === 'all') return articles.selectedInView.length + ext.selectedInView.length
  return articles.selectedInView.length
})

// ---- 公众号下拉：过期的灰显「需续约」（§5.5） ----
function acctExpired(id: string, expiresAt: number | null) {
  return !expiresAt || expires_at_ms(expiresAt) <= now.value * 1000
}
function expires_at_ms(e: number) {
  return e * 1000
}
const currentExpired = computed(() => {
  const a = accounts.list.find((x) => x.id === articles.accountId)
  return a ? acctExpired(a.id, a.expires_at) : false
})
watch(
  () => articles.accountId,
  (id) => {
    if (id !== undefined) articles.load(id)
  },
)
onMounted(async () => {
  if (!accounts.loaded) await accounts.load()
  // 优先选有效账号；如果全部过期，也允许选中第一个账号查看已缓存的历史文章
  if (!articles.accountId && accounts.list.length) {
    articles.accountId = (accounts.valid[0] || accounts.list[0]).id
  }
  if (articles.accountId) await articles.load()
})

const rangeOptions = [
  { value: '7', label: '近 7 天' },
  { value: '30', label: '近 30 天' },
  { value: '90', label: '近 90 天' },
  { value: 'custom', label: '自定义' },
]
function todayStr() {
  const d = new Date()
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}
const range = computed({
  get: () => (articles.rangeMode === 'custom' ? 'custom' : String(articles.rangeDays)),
  set: (v) => {
    if (v === 'custom') {
      articles.rangeMode = 'custom'
      if (!articles.customEnd) articles.customEnd = todayStr()
    } else {
      articles.rangeMode = 'days'
      articles.rangeDays = Number(v)
    }
  },
})
const customRangeInvalid = computed(
  () => articles.rangeMode === 'custom' && !articles.customStart,
)
const rangeLabel = computed(() =>
  articles.rangeMode === 'custom'
    ? ` ${articles.customStart || '?'} ~ ${articles.customEnd || '今天'} 的`
    : `最近 ${articles.rangeDays} 天`,
)

// ---- 缓存文章时间筛选（2026-08-23）：全部 / 最近拉取 / 自定义发布日期范围 ----
const filterOptions = [
  { value: 'all', label: '全部' },
  { value: 'latest', label: '最近拉取' },
  { value: 'custom', label: '自定义' },
]
function setTimeFilter(v: string) {
  articles.timeFilter = v as 'all' | 'latest' | 'custom'
  if (v === 'custom' && !articles.filterEnd) articles.filterEnd = todayStr()
  if (v !== 'custom' || articles.filterStart) articles.load()
}

// ---- 拉取进度（内联） ----
const fetchTask = computed(() => (articles.fetchTaskId ? tasks.tasks[articles.fetchTaskId] : null))
const exportTask = computed(() => (articles.exportTaskId ? tasks.tasks[articles.exportTaskId] : null))
const aiTask = computed(() => (articles.aiTaskId ? tasks.tasks[articles.aiTaskId] : null))
const titleKeepCount = computed(() => articles.list.filter((a) => a.title_verdict === 'keep').length)

// ---- 批量拉取（2026-08-09）----
const batchOpen = ref(false)
const batchSel = ref(new Set<string>())
function toggleBatchSel(id: string, on: boolean) {
  const s = new Set(batchSel.value)
  if (on) s.add(id)
  else s.delete(id)
  batchSel.value = s
}
function selectAllBatch() {
  batchSel.value = new Set(accounts.list.filter((a) => !acctExpired(a.id, a.expires_at)).map((a) => a.id))
}
function confirmBatch() {
  const ids = [...batchSel.value]
  if (!ids.length) {
    ui.error('请至少勾选一个公众号')
    return
  }
  batchOpen.value = false
  articles.fetchBatch(ids)
}

// ---- 拉取入口统一（2026-09）----
// 原先：下拉选「全部公众号」时 accountId 为空串，而「拉取历史」的 disabled 是
// `!accountId` —— 于是想看聚合列表恰恰不能拉取，必须绕到「批量拉取…」再勾一遍。
// 现在：下拉决定范围 —— 选中具体公众号 = 拉它一个；选「全部公众号」= 拉取全部
//（自动跳过凭证过期的）；只有想拉其中一个子集时才用「选择公众号拉取…」。
const fetchableAccounts = computed(() =>
  accounts.list.filter((a) => !acctExpired(a.id, a.expires_at)),
)
const isAggregate = computed(() => !articles.accountId)
const fetchBusy = computed(
  () => !!fetchTask.value || !!articles.batchTaskId || articles.fetchPending,
)
const fetchDisabled = computed(
  () =>
    !accounts.list.length ||
    customRangeInvalid.value ||
    fetchBusy.value ||
    (isAggregate.value ? fetchableAccounts.value.length === 0 : currentExpired.value),
)
const fetchLabel = computed(() =>
  isAggregate.value && fetchableAccounts.value.length
    ? `拉取全部公众号（${fetchableAccounts.value.length}）`
    : '拉取历史',
)
function startFetch() {
  if (isAggregate.value) articles.fetchBatch(fetchableAccounts.value.map((a) => a.id))
  else articles.fetchHistory()
}

// ---- 视图切换 ----
const stageTabs = [
  { value: 'final', label: '最终结果' },
  { value: 'title', label: '标题筛选' },
  { value: 'content', label: '内容筛选' },
]
const viewTabs = computed(() => articles.stageTabs)

/** 「正文没拿到」的说明 —— 这几篇一直停在待筛选，用户有权知道为什么。
 *
 * 原先这种情况什么都不显示（内容判定没写、正文也没拿到），界面上跟「你还没
 * 跑过筛选」长得一样，于是只能去执行日志里翻（2026-09 用户问「为什么没被剔除」）。
 * 文案里带上次数和「已放弃自动重试」——放弃是**不会自己恢复**的状态，
 * 不说清楚用户会一直等它好。
 */
function bodyFailLabel(a: Article): string {
  if (!a.body_error) return ''
  const n = a.body_fail_count || 0
  return a.body_give_up
    ? `正文取不到（试过 ${n} 次，已停止自动重试）：${a.body_error}`
    : `正文没拿到（第 ${n} 次）：${a.body_error}`
}

function rowReason(a: Article) {
  // 外部条目不参与「阶段」切换（那是公众号两阶段筛选的概念），直接取最有信息量的一条
  if (rowIsExternal(a)) return a.content_reason || a.title_reason || a.reason || '未判定'
  if (articles.aiStage === 'title') return a.title_reason || '未做标题筛选'
  if (articles.aiStage === 'content') {
    return a.content_reason || bodyFailLabel(a) || '未做内容筛选'
  }
  return a.reason || bodyFailLabel(a)
}

// ---- 选择 & 导出 HTML ----
//
// 2026-09 合并：原先正文导出有两个入口 —— 「导出 HTML」（没勾选时弹确认框、用设置里的
// 默认目录）与「导出到目录…」（弹目录框、不确认）。两者调的是同一个 exportHtml，
// 只是参数收集方式不同，于是出现「走哪条路决定你被不被拦一下」的怪现象。
// 现在只有一条：一律弹目录框（预填默认目录），目录可见可改，弹窗本身就是确认。
/** 「只筛选中」用的勾选集合（AI 筛选只处理公众号文章，外部来源不走这条路）。 */
const pickedWechat = computed(() => articles.selectedInView)
const pickedIds = computed(() => pickedWechat.value.map((a) => a.id))
/** 勾选的里面有几篇通过了标题阶段 —— 内容筛选只处理这些，一篇都没有就别让点 */
const pickedTitleKeep = computed(
  () => pickedWechat.value.filter((a) => a.title_verdict === 'keep').length,
)

const exportCount = computed(() => articles.selectedInView.length || articles.counts[articles.view])
function exportSingle(a: Article) {
  // 外部条目不能走 /api/articles/export-html：那条路会用公众号解析器去抓
  // arxiv.org 的链接，产出一堆垃圾或直接报错。写回请到「其他来源」页做。
  if (rowIsExternal(a)) {
    ui.error('外部来源条目请到「其他来源」页写回目录')
    return
  }
  articles.exportHtml([a.id])
}

// ---- 从本地列表删除（2026-09）----
//
// 用户要求：「有些文章我认为认定可以删掉」。语义是他选定的 —— **只动本地缓存**，
// 文章还在微信那边，下次「拉取历史」会重新抓到。确认框里必须写出这一句，
// 否则下次看到它回来会当成 bug 报。
const deleteOpen = ref(false)
const deleteIds = ref<string[]>([])

function askDelete(ids: string[]) {
  if (!ids.length) return
  deleteIds.value = ids
  deleteOpen.value = true
}
async function doDelete() {
  const ids = deleteIds.value
  deleteOpen.value = false
  deleteIds.value = []
  await articles.remove(ids)
}

// ---- 导出全部正文到指定目录（2026-08-09）；默认目录取「设置」页配置的 export.default_dir ----
const exportDirOpen = ref(false)
const exportDir = ref('~/Downloads/mp-harvest-export')
watch(
  () => settings.prefsLoaded,
  (v) => {
    if (v && settings.prefs.exportDefaultDir) exportDir.value = settings.prefs.exportDefaultDir
  },
)
function openExportDir() {
  exportDirOpen.value = true
}
function confirmExportDir() {
  exportDirOpen.value = false
  // 勾选了就只导勾选的，否则整视图 —— 与弹窗里的文案严格一致
  articles.exportHtml(
    articles.selectedInView.map((a) => a.id),
    exportDir.value,
  )
}

// ---- 补录链接 ----
const suppOpen = ref(false)
const suppUrl = ref('')
async function submitSupplement() {
  if (!/^https?:\/\/mp\.weixin\.qq\.com\//.test(suppUrl.value.trim())) {
    ui.error('请填写有效的公众号文章链接')
    return
  }
  await articles.supplement(suppUrl.value.trim())
  suppUrl.value = ''
  suppOpen.value = false
}

// ---- 行渲染辅助 ----
/** 时间列：只有**发布时间**才显示 MM-DD。
 *
 * 抓包目击/补录的行没有发布时间（微信的链接里就没有），后端把 ``date`` 退成了
 * 「看到的时刻」用于排序与筛选。把那个时刻当发布时间显示出来是在骗人 ——
 * 一篇几个月前的文章会挂着今天的日期（2026-09 用户报的「时间不对」）。
 * 不知道就写「未知」，鼠标悬停给出目击时刻。 */
function mmdd(a: Article) {
  if (a.has_publish_time === false) return '未知'
  const d = new Date(a.date)
  if (isNaN(d.getTime())) return a.date
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
/** 时间列的悬停说明：把「为什么没有时间」讲清楚 */
function timeTip(a: Article) {
  if (a.has_publish_time !== false) return a.date
  const seen = (a.seen_at || a.date || '').replace('T', ' ')
  return seen ? `微信没给发布时间（这篇文章是 ${seen} 抓到的）` : '微信没给发布时间'
}
const badgeVariant: Record<Article['source'], 'm' | 'g' | 'bu' | 'x'> = {
  M: 'm', G: 'g', 补: 'bu', 外: 'x',
}
const badgeTip: Record<Article['source'], string> = {
  M: '抓包目击：浏览这篇文章时代理看到的，微信只给了链接（没有发布时间）',
  G: '历史拉取：从公众号历史接口拉到的',
  补: '手动补录：你贴的链接',
  外: '其他来源（外部目录）',
}
/** 徽标上显示的字。
 *
 * 2026-09 之前显示的是后端给的 M/G/补/外 单字母 —— 用户看到「M」只能来问
 * 「这是不是坏了」。字母本身是有含义的（后端契约里的取值），留在这里做映射，
 * 界面上说人话。 */
const badgeText: Record<Article['source'], string> = {
  M: '抓包', G: '拉取', 补: '补录', 外: '其他',
}

async function copyLink(a: Article) {
  // 原先是「不 await + 无条件弹『已复制』」——剪贴板被拒时会骗用户。改为看真实结果
  if (await copyText(a.url)) ui.toast('链接已复制')
  else ui.error('复制失败，请手动选择链接文本')
}
async function openArticle(a: Article) {
  if (!(await openExternal(a.url))) ui.error('打开失败：该文章没有可用的链接')
}

// ---- 虚拟滚动：>500 条启用，行高固定 36px（§5.5/§5.10） ----
const scrollRef = ref<HTMLElement>()
// 行高以 rows（当前来源范围的数据源）为准 —— 外部条目也要能虚拟滚动
const useVirtual = computed(() => rows.value.length > 500)
const virtualizer = useVirtualizer(
  computed(() => ({
    count: useVirtual.value ? rows.value.length : 0,
    getScrollElement: () => scrollRef.value ?? null,
    estimateSize: () => 36,
    overscan: 10,
  })),
)
const virtualItems = computed(() => (useVirtual.value ? virtualizer.value.getVirtualItems() : []))
const totalSize = computed(() => virtualizer.value.getTotalSize())

// AI 筛选弹层内原则预览
const principlesPreview = computed(() => settings.principles.slice(0, 200) + (settings.principles.length > 200 ? '…' : ''))
const contentPrinciplesPreview = computed(() => settings.contentPrinciples.slice(0, 200) + (settings.contentPrinciples.length > 200 ? '…' : ''))

// 并行判定控制（2026-08-09）：每批篇数 / 并发批数，默认值来自「设置」页
const aiBatchSize = ref(50)
const aiWorkers = ref(4)
// AI 筛选弹窗开关（2026-08-16 由 Popover 改为 Modal，避免内容过多显示不全）
const aiFilterOpen = ref(false)
// 标题筛选完成后是否继续内容筛选（默认取自 ai.continue_content_filter，改动画立即保存）
const aiIncludeContent = ref(true)
watch(
  () => settings.prefsLoaded,
  (v) => {
    if (!v) return
    aiBatchSize.value = settings.prefs.aiBatchSize
    aiWorkers.value = settings.prefs.aiWorkers
    aiIncludeContent.value = settings.prefs.aiContinueContentFilter
  },
)
function toggleAiIncludeContent() {
  settings.savePrefs({ aiContinueContentFilter: aiIncludeContent.value })
}
</script>

<template>
  <section class="view-root">
  <header class="page-header">
    <h1>历史文章</h1>
  </header>
  <div class="page-body">
    <!-- 拉取控制 -->
    <div class="panel">
      <div class="fetch-bar">
        <span class="form-label">来源</span>
        <SegmentedControl
          :model-value="sourceScope"
          :options="sourceScopeOptions"
          @update:model-value="setSourceScope($event)"
        />
        <template v-if="sourceScope === 'wechat'">
        <span class="form-label">公众号</span>
        <select v-model="articles.accountId" class="input" style="width:200px">
          <option value="">全部公众号</option>
          <option v-for="a in accounts.list" :key="a.id" :value="a.id">
            {{ a.name }}{{ acctExpired(a.id, a.expires_at) ? '（需续约）' : '' }}
          </option>
        </select>
        <span class="form-label">范围</span>
        <SegmentedControl v-model="range" :options="rangeOptions" />
        <template v-if="articles.rangeMode === 'custom'">
          <input v-model="articles.customStart" type="date" class="input" style="width:140px" />
          <span class="tertiary">至</span>
          <input v-model="articles.customEnd" type="date" class="input" style="width:140px" />
        </template>
        <SButton variant="primary" :disabled="fetchDisabled" @click="startFetch()">
          {{ fetchLabel }}
        </SButton>
        <span v-if="currentExpired" class="tertiary" style="font-size:var(--fs-xs)">
          凭证已过期，仅显示已缓存的历史文章；续约后可拉取新文章
        </span>
        <SButton variant="ghost" :disabled="!accounts.list.length || !!articles.batchTaskId" @click="batchOpen = true">
          选择公众号拉取…
        </SButton>
        <ProgressInline
          v-if="fetchTask"
          :text="fetchTask.message || '拉取中…'"
          cancellable
          @cancel="articles.cancelFetch()"
        />
        <ProgressInline
          v-if="articles.batchTaskId"
          :text="(tasks.tasks[articles.batchTaskId]?.message) || '拉取中…'"
          cancellable
          @cancel="articles.cancelBatch()"
        />
        </template>
        <span v-else class="tertiary" style="font-size:var(--fs-xs)">
          非公众号来源在这里只读浏览；登记目录、扫描、写回请到
          <a href="#" style="color:var(--accent)" @click.prevent="ui.go('external')">「其他来源」</a>页
        </span>
      </div>
    </div>

    <!-- 工具条 -->
    <div class="panel" style="padding:var(--sp-2) var(--sp-4)">
      <div v-if="sourceScope === 'wechat'" class="toolbar" style="border-bottom:1px solid var(--border);padding-bottom:var(--sp-2)">
        <span class="muted" style="font-size:var(--fs-sm)">阶段：</span>
        <SegmentedControl
          :model-value="articles.aiStage"
          :options="stageTabs"
          @update:model-value="articles.setStage($event as 'final' | 'title' | 'content')"
        />
        <span class="spacer"></span>
        <span v-if="aiTask" class="ai-progress"><span class="spinner"></span>{{ articles.aiProgress || aiTask.message }} {{ Math.round(aiTask.percent) }}%</span>
      </div>
      <div v-if="sourceScope === 'wechat'" class="toolbar" style="padding-top:var(--sp-2)">
        <div class="view-tabs">
          <span
            v-for="t in viewTabs"
            :key="t.v"
            class="view-tab"
            :class="{ active: articles.view === t.v }"
            tabindex="0"
            @click="articles.setView(t.v)"
            @keydown.enter.prevent="articles.setView(t.v)"
          >
            {{ t.label }} <span class="cnt">{{ articles.counts[t.v] }}</span>
          </span>
        </div>
      </div>
      <div class="toolbar" style="padding-top:var(--sp-2)">
        <template v-if="sourceScope === 'wechat'">
        <span class="muted" style="font-size:var(--fs-sm)">列表：</span>
        <select v-model="articles.listFormat" class="input btn-sm" style="height:24px;font-size:var(--fs-xs)">
          <option v-for="f in LIST_FORMATS" :key="f.value" :value="f.value">{{ f.label }}</option>
        </select>
        <SButton size="sm" :disabled="!articles.visible.length" @click="articles.copyList()">复制</SButton>
        <SButton size="sm" variant="ghost" :disabled="!articles.accountId" @click="suppOpen = true">+ 补录链接</SButton>
        <SButton size="sm" variant="ghost" :disabled="!accounts.list.length" @click="articles.load()">刷新</SButton>
        <span class="muted" style="font-size:var(--fs-sm)">筛选：</span>
        <SegmentedControl
          :model-value="articles.timeFilter"
          :options="filterOptions"
          @update:model-value="setTimeFilter($event)"
        />
        <template v-if="articles.timeFilter === 'custom'">
          <input
            v-model="articles.filterStart"
            type="date"
            class="input btn-sm"
            style="height:24px;font-size:var(--fs-xs);width:130px"
            @change="articles.filterStart && articles.load()"
          />
          <span class="tertiary" style="font-size:var(--fs-xs)">至</span>
          <input
            v-model="articles.filterEnd"
            type="date"
            class="input btn-sm"
            style="height:24px;font-size:var(--fs-xs);width:130px"
            @change="articles.filterStart && articles.load()"
          />
        </template>
        <span class="muted" style="font-size:var(--fs-sm)">排序：</span>
        <SortControl
          :by="articles.sortBy"
          :dir="articles.sortDir"
          @pick="articles.pickSort($event)"
        />
        </template>
        <span class="spacer"></span>
        <span v-if="mixedScope" class="tertiary" style="font-size:var(--fs-xs)">
          「全部」下只能浏览；导出与 AI 筛选请先选定来源
        </span>
        <SButton size="sm" variant="ghost" @click="selectAllRows()">全选</SButton>
        <SButton
          size="sm"
          variant="ghost"
          @click="sourceScope === 'external' ? ext.clearSelection() : articles.clearSelection()"
        >
          取消选择
        </SButton>
        <span class="badge sel-badge" title="当前视图已选">{{ selectedCount }}</span>
        <template v-if="sourceScope === 'wechat'">
        <SPopover>
          <template #anchor>
            <SButton size="sm" variant="primary" :disabled="!articles.visible.length">导出 <SIcon name="chevron-down" :size="12" /></SButton>
          </template>
          <template #default="{ close }">
            <div class="menu">
              <div class="menu-item" @click="close(); openExportDir()">
                导出正文…<template v-if="selectedCount">（已选 {{ selectedCount }}）</template><template v-else>（当前视图全部）</template>
              </div>
              <div class="menu-item" @click="close(); articles.exportList()">导出列表文件</div>
            </div>
          </template>
        </SPopover>
        <ProgressInline
          v-if="exportTask"
          :text="exportTask.message || '导出中…'"
          cancellable
          @cancel="articles.cancelExport()"
        />
        <span style="width:8px"></span>
        <SButton size="sm" :disabled="!accounts.list.length || !!articles.aiTaskId" @click="aiFilterOpen = true"><SIcon name="sparkles" :size="12" /> AI 筛选</SButton>
        <!-- 只删「选中的」：没选就不给点，免得手一滑把整个视图清了 -->
        <span style="width:8px"></span>
        <SButton size="sm" variant="ghost" :disabled="!selectedCount"
                 @click="askDelete(articles.selectedInView.map((a) => a.id))">
          删除选中{{ selectedCount ? `（${selectedCount}）` : '' }}
        </SButton>
        </template>
        <SButton
          v-else-if="sourceScope === 'external'"
          size="sm"
          variant="ghost"
          @click="ui.go('external')"
        >
          去「其他来源」页操作 →
        </SButton>
      </div>
    </div>

    <!-- 文章表格 -->
    <div class="art-table">
      <div ref="scrollRef" class="art-scroll">
        <!-- 表头放在**滚动容器里面**（sticky 吸顶）：它必须和数据行同宽。
            放在外面时，一旦行数多到出现纵向滚动条，滚动区就比 .art-table 窄
            （实测 macOS 上差 8px），表头的 fr 列随之宽松一点 —— 整列向右漂移
            最多 8px，越靠右越明显（2026-09 用户报的「和下方的内容不匹配」）。
            8 格对应 .art-row 的 8 个子元素（列宽见 style.css 的 grid-template-columns）。 -->
        <div class="art-head"><span></span><span>{{ sourceScope === 'wechat' ? '公众号' : '来源' }}</span><span>标题</span><span>AI 理由</span><span>时间</span><span>来源</span><span></span><span></span></div>
        <SkeletonRows v-if="articles.loading || ext.loading" :rows="8" />
        <EmptyState v-else-if="!rows.length" :text="sourceScope === 'wechat' ? '选好公众号后点「拉取历史」；想看全部账号就先在下拉里选「全部公众号」' : '这个范围还没有条目；到「其他来源」页登记目录并扫描'" />
        <!-- 虚拟滚动（>500 条） -->
        <div v-else-if="useVirtual" :style="`height:${totalSize}px;position:relative`">
          <div
            v-for="vr in virtualItems"
            :key="rows[vr.index].id"
            class="art-row"
            :style="`position:absolute;top:0;left:0;width:100%;transform:translateY(${vr.start}px)`"
            @dblclick="openArticle(rows[vr.index])"
          >
            <span>
              <input
                type="checkbox"
                class="cb"
                :checked="articles.selected.has(rows[vr.index].id) || ext.selected.has(rows[vr.index].id)"
                @change="toggleRow(rows[vr.index], ($event.target as HTMLInputElement).checked)"
              />
            </span>
            <span class="muted" style="font-size:var(--fs-xs);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              {{ rows[vr.index].account_name || '—' }}
            </span>
            <STooltip :text="rows[vr.index].title" style="min-width:0">
              <span class="art-title">{{ rows[vr.index].title }}</span>
            </STooltip>
            <STooltip v-if="rowReason(rows[vr.index])" :text="rowReason(rows[vr.index])" style="min-width:0">
              <span class="art-reason">{{ rowReason(rows[vr.index]) }}</span>
            </STooltip>
            <span v-else class="art-reason"></span>
            <span class="mono muted" :title="timeTip(rows[vr.index])">{{ mmdd(rows[vr.index]) }}</span>
            <STooltip :text="badgeTip[rows[vr.index].source]">
              <SBadge :variant="badgeVariant[rows[vr.index].source]">{{ badgeText[rows[vr.index].source] }}</SBadge>
            </STooltip>
            <!-- 本地还留着导出文件才显示。删掉文件后点「刷新」它就消失 ——
                 标记由后端按**文件是否存在**判定，不是查导出记录表里有没有行 -->
            <SBadge v-if="rows[vr.index].exported" variant="g" title="本地已存有这篇的 HTML">已导出</SBadge>
            <span v-else></span>
            <span class="row-actions">
              <SButton size="sm" variant="ghost" @click="openArticle(rows[vr.index])">打开</SButton>
              <SButton size="sm" variant="ghost" @click="copyLink(rows[vr.index])">复制</SButton>
              <SButton size="sm" variant="ghost" @click="exportSingle(rows[vr.index])">导出</SButton>
              <SButton size="sm" variant="ghost" @click="askDelete([rows[vr.index].id])">删除</SButton>
            </span>
          </div>
        </div>
        <!-- 直接渲染（≤500 条） -->
        <template v-else>
          <div v-for="a in rows" :key="a.id" class="art-row" @dblclick="openArticle(a)">
            <span>
              <input
                type="checkbox"
                class="cb"
                :checked="articles.selected.has(a.id) || ext.selected.has(a.id)"
                @change="toggleRow(a, ($event.target as HTMLInputElement).checked)"
              />
            </span>
            <span class="muted" style="font-size:var(--fs-xs);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              {{ a.account_name || '—' }}
            </span>
            <STooltip :text="a.title" style="min-width:0"><span class="art-title">{{ a.title }}</span></STooltip>
            <STooltip v-if="rowReason(a)" :text="rowReason(a)" style="min-width:0"><span class="art-reason">{{ rowReason(a) }}</span></STooltip>
            <span v-else class="art-reason"></span>
            <span class="mono muted" :title="timeTip(a)">{{ mmdd(a) }}</span>
            <STooltip :text="badgeTip[a.source]"><SBadge :variant="badgeVariant[a.source]">{{ badgeText[a.source] }}</SBadge></STooltip>
            <SBadge v-if="a.exported" variant="g" title="本地已存有这篇的 HTML">已导出</SBadge>
            <span v-else></span>
            <span class="row-actions">
              <SButton size="sm" variant="ghost" @click="openArticle(a)">打开</SButton>
              <SButton size="sm" variant="ghost" @click="copyLink(a)">复制</SButton>
              <SButton size="sm" variant="ghost" @click="exportSingle(a)">导出</SButton>
              <SButton size="sm" variant="ghost" @click="askDelete([a.id])">删除</SButton>
            </span>
          </div>
        </template>
      </div>
    </div>
  </div>

  <!-- 补录链接 Modal -->
  <!-- 删除确认：必须写明「只删本地」——这是用户选定的语义，不说清下次看到它
       被重新抓回来会当成 bug -->
  <SModal :open="deleteOpen" @close="deleteOpen = false">
    <template #head>从本地列表删除</template>
    <div style="display:flex;flex-direction:column;gap:8px">
      <span>确定删除这 <b>{{ deleteIds.length }}</b> 篇？</span>
      <span class="tertiary" style="font-size:var(--fs-sm)">
        只删本地这一份清单。<b>文章还在微信那边，下次「拉取历史」会重新抓到它</b> ——
        想让它不再出现，得用「筛选」把它标成过滤掉。
      </span>
      <div class="toolbar" style="justify-content:flex-end;margin-top:var(--sp-2)">
        <SButton size="sm" variant="ghost" @click="deleteOpen = false">取消</SButton>
        <SButton size="sm" variant="danger" @click="doDelete">确定删除</SButton>
      </div>
    </div>
  </SModal>

  <SModal :open="suppOpen" @close="suppOpen = false">
    <template #head>补录链接</template>
    <div style="display:flex;flex-direction:column;gap:8px">
      <span>粘贴一条公众号文章链接，将直接加入当前列表（来源标记为「补」）。</span>
      <SInput v-model="suppUrl" mono placeholder="https://mp.weixin.qq.com/s/…" @enter="submitSupplement" />
    </div>
    <template #foot>
      <SButton variant="ghost" @click="suppOpen = false">取消</SButton>
      <SButton variant="primary" @click="submitSupplement">补录</SButton>
    </template>
  </SModal>

  <!-- 导出正文到目录 Modal（2026-09 起是正文导出的唯一入口）-->
  <SModal :open="exportDirOpen" @close="exportDirOpen = false">
    <template #head>导出正文到目录</template>
    <div style="display:flex;flex-direction:column;gap:8px">
      <span>
        将
        <b style="color:var(--text-primary)">{{ selectedCount ? `已勾选的 ${selectedCount}` : `当前视图全部 ${articles.counts[articles.view]}` }}</b>
        篇正文导出为 HTML 到目标目录，并在目录内生成
        <span class="mono">index.html</span> 说明页（可搜索/排序，含本地正文与原文链接）。
      </span>
      <SInput v-model="exportDir" mono placeholder="~/Downloads/mp-harvest-export" />
      <span class="muted" style="font-size:var(--fs-sm)">
        支持 <span class="mono">~</span> 展开；目录不存在会自动创建。
        已导出过的文章会自动跳过，不重复联网。
      </span>
    </div>
    <template #foot>
      <SButton variant="ghost" @click="exportDirOpen = false">取消</SButton>
      <SButton variant="primary" @click="confirmExportDir">开始导出</SButton>
    </template>
  </SModal>

  <!-- AI 筛选 Modal（两阶段：标题筛选 → 内容筛选） -->
  <SModal :open="aiFilterOpen" @close="aiFilterOpen = false">
    <template #head>AI 筛选</template>
    <div style="display:flex;flex-direction:column;gap:var(--sp-2)">
      <span class="muted" style="font-size:var(--fs-sm)">
        不勾选 = 对当前列表（具体公众号或「全部公众号」）整体筛选；
        在表格里勾选之后，可以只筛勾选的那几篇。
      </span>
      <span class="muted" style="font-size:var(--fs-sm)">
        当前文章 {{ articles.list.length }} 篇；标题通过 {{ titleKeepCount }} 篇。
      </span>
      <!-- 只筛选中（2026-09）：正文抓取失败之类的情况只需要重跑那几篇，
           整体重跑虽然靠缓存不花 AI 的钱，但会重新去拉那些没拿到正文的。
           放在这里而不是页脚：页脚那三个是「整范围」的动作，这两种范围
           混在一排按钮里容易点错。 -->
      <div v-if="sourceScope !== 'external' && pickedWechat.length" class="toolbar"
           style="padding:var(--sp-2);border:1px solid var(--border);border-radius:var(--radius-md)">
        <span class="form-label">已勾选 {{ pickedWechat.length }} 篇</span>
        <SButton size="sm" variant="ghost"
                 @click="aiFilterOpen = false; articles.aiFilter(aiBatchSize, aiWorkers, false, pickedIds)"
        >只筛这 {{ pickedWechat.length }} 篇标题</SButton>
        <SButton size="sm" variant="ghost"
                 :disabled="!pickedTitleKeep"
                 :title="pickedTitleKeep ? '只对勾选中标题通过的文章执行' : '勾选的里面没有通过标题筛选的文章'"
                 @click="aiFilterOpen = false; articles.contentFilter(aiBatchSize, aiWorkers, pickedIds)"
        >只筛这 {{ pickedTitleKeep }} 篇内容</SButton>
      </div>
      <div>
        <span class="form-label">第一阶段：标题筛选原则</span>
        <div class="ai-principle-preview">{{ principlesPreview || '（未配置原则）' }}</div>
      </div>
      <div>
        <span class="form-label">第二阶段：内容筛选原则（只对标题通过的文章生效）</span>
        <div class="ai-principle-preview">{{ contentPrinciplesPreview || '（未配置内容原则）' }}</div>
      </div>
      <div class="toolbar">
        <span class="form-label">每批篇数</span>
        <input v-model.number="aiBatchSize" type="number" min="1" max="200" class="input" style="width:72px" />
        <span class="form-label">并发批数</span>
        <input v-model.number="aiWorkers" type="number" min="1" max="16" class="input" style="width:64px" />
        <span class="tertiary" style="font-size:var(--fs-xs)">同时提交的批数越多，并发请求越多</span>
      </div>
      <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input v-model="aiIncludeContent" type="checkbox" class="cb" @change="toggleAiIncludeContent" />
        <span>标题筛选完成后自动继续内容筛选</span>
      </label>
    </div>
    <template #foot>
      <SButton variant="ghost" @click="aiFilterOpen = false">取消</SButton>
      <SButton
        variant="ghost"
        :disabled="!titleKeepCount || !articles.list.length"
        :title="titleKeepCount ? '只对标题通过的文章执行' : '请先执行标题筛选'"
        @click="aiFilterOpen = false; articles.contentFilter(aiBatchSize, aiWorkers)"
      >仅内容筛选（{{ titleKeepCount }}）</SButton>
      <SButton
        variant="primary"
        :disabled="!articles.list.length"
        :title="articles.list.length ? '' : '请先拉取历史，列表中还没有文章'"
        @click="aiFilterOpen = false; articles.aiFilter(aiBatchSize, aiWorkers, aiIncludeContent)"
      >开始标题筛选{{ aiIncludeContent ? ' + 内容' : '' }}</SButton>
    </template>
  </SModal>

  <!-- 批量拉取 Modal -->
  <SModal :open="batchOpen" @close="batchOpen = false">
    <template #head>选择要拉取的公众号</template>
    <div style="display:flex;flex-direction:column;gap:8px">
      <span class="muted" style="font-size:var(--fs-sm)">
        勾选本次要拉取的公众号，将逐个拉取{{ rangeLabel }}历史（进度在工具条实时显示）。
      </span>
      <div style="max-height:300px;overflow-y:auto;display:flex;flex-direction:column;gap:4px">
        <label v-for="a in accounts.list" :key="a.id" style="display:flex;align-items:center;gap:8px;cursor:pointer">
          <input
            type="checkbox"
            class="cb"
            :checked="batchSel.has(a.id)"
            :disabled="acctExpired(a.id, a.expires_at)"
            @change="toggleBatchSel(a.id, ($event.target as HTMLInputElement).checked)"
          />
          <span>{{ a.name }}</span>
          <span v-if="acctExpired(a.id, a.expires_at)" class="muted" style="font-size:var(--fs-xs)">（需续约，暂不可拉取）</span>
        </label>
      </div>
      <div class="toolbar">
        <SButton size="sm" variant="ghost" @click="selectAllBatch()">全选</SButton>
        <SButton size="sm" variant="ghost" @click="batchSel = new Set()">清空</SButton>
        <span class="spacer"></span>
        <span class="muted" style="font-size:var(--fs-sm)">已选 {{ batchSel.size }} 个</span>
      </div>
    </div>
    <template #foot>
      <SButton variant="ghost" @click="batchOpen = false">取消</SButton>
      <SButton variant="primary" @click="confirmBatch">开始拉取（{{ batchSel.size }}）</SButton>
    </template>
  </SModal>
  </section>
</template>
