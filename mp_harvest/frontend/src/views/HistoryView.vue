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
import SBadge from '../components/SBadge.vue'
import ProgressInline from '../components/ProgressInline.vue'
import EmptyState from '../components/EmptyState.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import { LIST_FORMATS, useArticlesStore } from '../stores/articles'
import { useAccountsStore } from '../stores/accounts'
import { useTasksStore } from '../stores/tasks'
import { useSettingsStore } from '../stores/settings'
import { useUiStore } from '../stores/ui'
import { useTicker } from '../composables/useTicker'
import type { Article, ArticleView } from '../types'

const articles = useArticlesStore()
const accounts = useAccountsStore()
const tasks = useTasksStore()
const settings = useSettingsStore()
const ui = useUiStore()
const now = useTicker()

// ---- 公众号下拉：过期的灰显「需续约」（§5.5） ----
function acctExpired(id: string, expiresAt: number | null) {
  return !expiresAt || expires_at_ms(expiresAt) <= now.value * 1000
}
function expires_at_ms(e: number) {
  return e * 1000
}
watch(
  () => articles.accountId,
  (id) => {
    if (id !== undefined) articles.load(id)
  },
)
onMounted(async () => {
  if (!accounts.loaded) await accounts.load()
  if (!articles.accountId && accounts.valid.length) articles.accountId = accounts.valid[0].id
  if (articles.accountId) await articles.load()
})

const rangeOptions = [
  { value: '7', label: '近 7 天' },
  { value: '30', label: '近 30 天' },
  { value: '90', label: '近 90 天' },
]
const range = computed({
  get: () => String(articles.rangeDays),
  set: (v) => (articles.rangeDays = Number(v)),
})

// ---- 拉取进度（内联） ----
const fetchTask = computed(() => (articles.fetchTaskId ? tasks.tasks[articles.fetchTaskId] : null))
const exportTask = computed(() => (articles.exportTaskId ? tasks.tasks[articles.exportTaskId] : null))

// ---- 批量拉取（2026-08-09）----
const batchOpen = ref(false)
const batchSel = ref(new Set<string>())
const sortDirLabel = computed(() =>
  articles.sortBy === 'name' ? (articles.sortDir === 'asc' ? 'A→Z' : 'Z→A') : articles.sortDir === 'desc' ? '新→旧' : '旧→新',
)
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

// ---- 视图切换 ----
const viewTabs: Array<{ v: ArticleView; label: string }> = [
  { v: 'all', label: '全部' },
  { v: 'keep', label: '通过' },
  { v: 'drop', label: '过滤掉' },
]

// ---- 选择 & 导出 HTML ----
const selectedCount = computed(() => articles.selectedInView.length)
const exportBtnText = computed(() =>
  selectedCount.value ? `导出 HTML（已选 ${selectedCount.value}）` : '导出 HTML（未选择 = 当前视图全部）',
)
const confirmAllOpen = ref(false)
function clickExportHtml() {
  if (selectedCount.value) articles.exportHtml([...articles.selected])
  else confirmAllOpen.value = true
}
function confirmExportAll() {
  confirmAllOpen.value = false
  articles.exportHtml([])
}
function exportSingle(a: Article) {
  articles.exportHtml([a.id])
}

// ---- 导出全部正文到指定目录（2026-08-09） ----
const EXPORT_DIR_KEY = 'mp_harvest.export_dir'
const exportDirOpen = ref(false)
const exportDir = ref(localStorage.getItem(EXPORT_DIR_KEY) || '~/Downloads/mp-harvest-export')
function openExportDir() {
  exportDirOpen.value = true
}
function confirmExportDir() {
  exportDirOpen.value = false
  localStorage.setItem(EXPORT_DIR_KEY, exportDir.value)
  articles.exportHtml([], exportDir.value)
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
function mmdd(a: Article) {
  const d = new Date(a.date)
  if (isNaN(d.getTime())) return a.date
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}
const badgeVariant: Record<Article['source'], 'm' | 'g' | 'bu'> = { M: 'm', G: 'g', 补: 'bu' }
const badgeTip: Record<Article['source'], string> = { M: 'MITM 目击', G: 'getmsg 拉取', 补: '手动补录' }

function copyLink(a: Article) {
  navigator.clipboard.writeText(a.url)
  ui.toast('链接已复制')
}
function openArticle(a: Article) {
  window.open(a.url, '_blank', 'noopener')
}

// ---- 虚拟滚动：>500 条启用，行高固定 36px（§5.5/§5.10） ----
const scrollRef = ref<HTMLElement>()
const useVirtual = computed(() => articles.visible.length > 500)
const virtualizer = useVirtualizer(
  computed(() => ({
    count: useVirtual.value ? articles.visible.length : 0,
    getScrollElement: () => scrollRef.value ?? null,
    estimateSize: () => 36,
    overscan: 10,
  })),
)
const virtualItems = computed(() => (useVirtual.value ? virtualizer.value.getVirtualItems() : []))
const totalSize = computed(() => virtualizer.value.getTotalSize())

// AI 筛选弹层内原则预览
const principlesPreview = computed(() => settings.principles.slice(0, 200) + (settings.principles.length > 200 ? '…' : ''))

// 并行判定控制（2026-08-09）：每批篇数（默认 50）/ 并发批数
const aiBatchSize = ref(50)
const aiWorkers = ref(4)
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
        <span class="form-label">公众号</span>
        <select v-model="articles.accountId" class="input" style="width:200px">
          <option value="">全部公众号</option>
          <option v-for="a in accounts.list" :key="a.id" :value="a.id" :disabled="acctExpired(a.id, a.expires_at)">
            {{ a.name }}{{ acctExpired(a.id, a.expires_at) ? '（需续约）' : '' }}
          </option>
        </select>
        <span class="form-label">范围</span>
        <SegmentedControl v-model="range" :options="rangeOptions" />
        <SButton variant="primary" :disabled="!articles.accountId || !!fetchTask" @click="articles.fetchHistory()">
          ⟳ 拉取历史
        </SButton>
        <SButton variant="ghost" :disabled="!accounts.list.length || !!articles.batchTaskId" @click="batchOpen = true">
          批量拉取…
        </SButton>
        <ProgressInline
          v-if="fetchTask"
          :text="fetchTask.message || '拉取中…'"
          cancellable
          @cancel="articles.cancelFetch()"
        />
        <ProgressInline
          v-if="articles.batchTaskId"
          :text="(tasks.tasks[articles.batchTaskId]?.message) || '批量拉取中…'"
          cancellable
          @cancel="articles.cancelBatch()"
        />
      </div>
    </div>

    <!-- 工具条 -->
    <div class="panel" style="padding:var(--sp-2) var(--sp-4)">
      <div class="toolbar" style="border-bottom:1px solid var(--border);padding-bottom:var(--sp-2)">
        <div class="view-tabs">
          <span
            v-for="t in viewTabs"
            :key="t.v"
            class="view-tab"
            :class="{ active: articles.view === t.v }"
            @click="articles.setView(t.v)"
          >
            {{ t.label }} <span class="cnt">{{ articles.counts[t.v] }}</span>
          </span>
        </div>
        <span class="spacer"></span>
        <span v-if="articles.aiProgress" class="ai-progress"><span class="spinner"></span>{{ articles.aiProgress }}</span>
      </div>
      <div class="toolbar" style="padding-top:var(--sp-2)">
        <span class="muted" style="font-size:var(--fs-sm)">列表：</span>
        <select v-model="articles.listFormat" class="input btn-sm" style="height:24px;font-size:var(--fs-xs)">
          <option v-for="f in LIST_FORMATS" :key="f.value" :value="f.value">{{ f.label }}</option>
        </select>
        <SButton size="sm" :disabled="!articles.accountId" @click="articles.copyList()">复制</SButton>
        <STooltip text="始终只导出当前视图">
          <SButton size="sm" :disabled="!articles.accountId" @click="articles.exportList()">导出</SButton>
        </STooltip>
        <SButton size="sm" variant="ghost" :disabled="!articles.accountId" @click="suppOpen = true">+ 补录链接</SButton>
        <SButton size="sm" variant="ghost" :disabled="!articles.accountId" @click="articles.load()">刷新</SButton>
        <span class="muted" style="font-size:var(--fs-sm)">排序：</span>
        <select
          class="input btn-sm"
          style="height:24px;font-size:var(--fs-xs);width:80px"
          :value="articles.sortBy"
          @change="articles.setSortBy(($event.target as HTMLSelectElement).value as 'time' | 'name')"
        >
          <option value="time">按时间</option>
          <option value="name">按名称</option>
        </select>
        <SButton size="sm" variant="ghost" @click="articles.toggleSortDir()">{{ sortDirLabel }} ▾</SButton>
        <span class="spacer"></span>
        <span class="muted" style="font-size:var(--fs-sm)">正文：</span>
        <SButton size="sm" variant="ghost" @click="articles.selectAllVisible()">全选</SButton>
        <SButton size="sm" variant="ghost" @click="articles.clearSelection()">取消选择</SButton>
        <SButton size="sm" variant="primary" :disabled="!articles.accountId || !articles.visible.length" @click="clickExportHtml()">{{ exportBtnText }}</SButton>
        <SButton size="sm" variant="ghost" :disabled="!articles.accountId || !articles.visible.length" @click="openExportDir">导出到目录…</SButton>
        <ProgressInline
          v-if="exportTask"
          :text="exportTask.message || '导出中…'"
          cancellable
          @cancel="articles.cancelExport()"
        />
        <span style="width:8px"></span>
        <SPopover>
          <template #anchor><SButton size="sm" :disabled="!articles.accountId || !!articles.aiTaskId">✦ AI 筛选</SButton></template>
          <template #default="{ close }">
            <div style="margin-bottom:8px">
              将按筛选原则判定全部文章：<br />
              <span class="tertiary mono" style="white-space:pre-wrap">{{ principlesPreview || '（未配置原则）' }}</span>
            </div>
            <div class="toolbar" style="margin-bottom:8px">
              <span class="form-label">每批篇数</span>
              <input v-model.number="aiBatchSize" type="number" min="1" max="200" class="input" style="width:72px" />
              <span class="form-label">并发批数</span>
              <input v-model.number="aiWorkers" type="number" min="1" max="16" class="input" style="width:64px" />
              <span class="tertiary" style="font-size:var(--fs-xs)">同时提交的批数越多，并发请求越多</span>
            </div>
            <div style="display:flex;justify-content:flex-end;gap:8px">
              <SButton size="sm" @click="close()">取消</SButton>
              <SButton size="sm" variant="primary" @click="close(); articles.aiFilter(aiBatchSize, aiWorkers)">开始判定</SButton>
            </div>
          </template>
        </SPopover>
        <SButton size="sm" variant="ghost" @click="ui.go('ai')">⚙ 模型设置</SButton>
      </div>
    </div>

    <!-- 文章表格 -->
    <div class="art-table">
      <div class="art-head"><span></span><span>公众号</span><span>标题</span><span>AI 理由</span><span>时间</span><span>来源</span><span></span></div>
      <div ref="scrollRef" class="art-scroll">
        <SkeletonRows v-if="articles.loading" :rows="8" />
        <EmptyState v-else-if="!articles.visible.length" text="先选择公众号并拉取历史（可「批量拉取…」一次拉多个）" />
        <!-- 虚拟滚动（>500 条） -->
        <div v-else-if="useVirtual" :style="`height:${totalSize}px;position:relative`">
          <div
            v-for="vr in virtualItems"
            :key="articles.visible[vr.index].id"
            class="art-row"
            :style="`position:absolute;top:0;left:0;width:100%;transform:translateY(${vr.start}px)`"
          >
            <span>
              <input
                type="checkbox"
                class="cb"
                :checked="articles.selected.has(articles.visible[vr.index].id)"
                @change="articles.toggleSelect(articles.visible[vr.index].id, ($event.target as HTMLInputElement).checked)"
              />
            </span>
            <span class="muted" style="font-size:var(--fs-xs);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              {{ articles.visible[vr.index].account_name || '—' }}
            </span>
            <STooltip :text="articles.visible[vr.index].title" style="min-width:0">
              <span class="art-title">{{ articles.visible[vr.index].title }}</span>
            </STooltip>
            <STooltip v-if="articles.visible[vr.index].reason" :text="articles.visible[vr.index].reason" style="min-width:0">
              <span class="art-reason">{{ articles.visible[vr.index].reason }}</span>
            </STooltip>
            <span v-else class="art-reason"></span>
            <span class="mono muted">{{ mmdd(articles.visible[vr.index]) }}</span>
            <STooltip :text="badgeTip[articles.visible[vr.index].source]">
              <SBadge :variant="badgeVariant[articles.visible[vr.index].source]">{{ articles.visible[vr.index].source }}</SBadge>
            </STooltip>
            <span class="row-actions">
              <SButton size="sm" variant="ghost" @click="openArticle(articles.visible[vr.index])">打开</SButton>
              <SButton size="sm" variant="ghost" @click="copyLink(articles.visible[vr.index])">复制</SButton>
              <SButton size="sm" variant="ghost" @click="exportSingle(articles.visible[vr.index])">导出</SButton>
            </span>
          </div>
        </div>
        <!-- 直接渲染（≤500 条） -->
        <template v-else>
          <div v-for="a in articles.visible" :key="a.id" class="art-row">
            <span>
              <input
                type="checkbox"
                class="cb"
                :checked="articles.selected.has(a.id)"
                @change="articles.toggleSelect(a.id, ($event.target as HTMLInputElement).checked)"
              />
            </span>
            <span class="muted" style="font-size:var(--fs-xs);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              {{ a.account_name || '—' }}
            </span>
            <STooltip :text="a.title" style="min-width:0"><span class="art-title">{{ a.title }}</span></STooltip>
            <STooltip v-if="a.reason" :text="a.reason" style="min-width:0"><span class="art-reason">{{ a.reason }}</span></STooltip>
            <span v-else class="art-reason"></span>
            <span class="mono muted">{{ mmdd(a) }}</span>
            <STooltip :text="badgeTip[a.source]"><SBadge :variant="badgeVariant[a.source]">{{ a.source }}</SBadge></STooltip>
            <span class="row-actions">
              <SButton size="sm" variant="ghost" @click="openArticle(a)">打开</SButton>
              <SButton size="sm" variant="ghost" @click="copyLink(a)">复制</SButton>
              <SButton size="sm" variant="ghost" @click="exportSingle(a)">导出</SButton>
            </span>
          </div>
        </template>
      </div>
    </div>
  </div>

  <!-- 补录链接 Modal -->
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

  <!-- 导出全部确认 Modal -->
  <SModal :open="confirmAllOpen" @close="confirmAllOpen = false">
    <template #head>导出当前视图全部</template>
    未勾选任何文章，将导出当前视图全部 <b style="color:var(--text-primary)">{{ articles.counts[articles.view] }}</b> 篇正文 HTML，是否继续？
    <template #foot>
      <SButton variant="ghost" @click="confirmAllOpen = false">取消</SButton>
      <SButton variant="primary" @click="confirmExportAll">导出全部</SButton>
    </template>
  </SModal>

  <!-- 导出全部正文到指定目录 Modal -->
  <SModal :open="exportDirOpen" @close="exportDirOpen = false">
    <template #head>导出全部正文到指定目录</template>
    <div style="display:flex;flex-direction:column;gap:8px">
      <span>
        将当前视图全部 <b style="color:var(--text-primary)">{{ articles.counts[articles.view] }}</b>
        篇正文导出为 HTML 到目标目录，并在目录内生成
        <span class="mono">index.html</span> 说明页（可搜索/排序，含本地正文与原文链接）。
      </span>
      <SInput v-model="exportDir" mono placeholder="~/Downloads/mp-harvest-export" />
      <span class="muted" style="font-size:var(--fs-sm)">支持 <span class="mono">~</span> 展开；目录不存在会自动创建。</span>
    </div>
    <template #foot>
      <SButton variant="ghost" @click="exportDirOpen = false">取消</SButton>
      <SButton variant="primary" @click="confirmExportDir">导出到目录</SButton>
    </template>
  </SModal>

  <!-- 批量拉取 Modal -->
  <SModal :open="batchOpen" @close="batchOpen = false">
    <template #head>批量拉取历史</template>
    <div style="display:flex;flex-direction:column;gap:8px">
      <span class="muted" style="font-size:var(--fs-sm)">
        勾选本次要拉取的公众号，将逐个拉取最近 {{ range }} 天历史（进度在工具条实时显示）。
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
      <SButton variant="primary" @click="confirmBatch">开始批量拉取</SButton>
    </template>
  </SModal>
  </section>
</template>
