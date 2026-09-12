<script setup lang="ts">
// 页面：周报生成 —— 选区间与来源 → AI 打分选题 → 按模板渲染 → 归档原文。
//
// 设计要点（2026-09）：
// - **模板是唯一真相**：界面上的「预览」用后端渲染，改模板立刻能看效果，不调用 AI。
// - **提示词可人工编辑**，但输出 JSON 格式由后端固定拼接，改坏标准不会改崩解析。
//   改了哪段，哪段的缓存自动失效（界面会明确提示）。
import { computed, onMounted, ref, watch } from 'vue'
import SButton from '../components/SButton.vue'
import SInput from '../components/SInput.vue'
import SIcon from '../components/SIcon.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import EmptyState from '../components/EmptyState.vue'
import ProgressInline from '../components/ProgressInline.vue'
import SegmentedControl from '../components/SegmentedControl.vue'
import SDrawer from '../components/SDrawer.vue'
import SBadge from '../components/SBadge.vue'
import STooltip from '../components/STooltip.vue'
import type { ExternalSource, WeeklyCandidate } from '../types'
import { useWeeklyStore } from '../stores/weekly'
import { useAccountsStore } from '../stores/accounts'
import { useExternalStore } from '../stores/external'
import { useTasksStore } from '../stores/tasks'
import { useSettingsStore } from '../stores/settings'
import { useUiStore } from '../stores/ui'
import { chooseDirectory, chooseFile, openLocalPath } from '../api/desktop'

const weekly = useWeeklyStore()
const accounts = useAccountsStore()
const ext = useExternalStore()
const tasks = useTasksStore()
const settings = useSettingsStore()
const ui = useUiStore()

onMounted(async () => {
  if (!accounts.loaded) await accounts.load()
  if (!ext.sources.length) await ext.loadAll()
  if (!settings.loaded) settings.load()
  // 默认全选（只种一次 —— 用户改过就不覆盖）
  weekly.seedSelection(accounts.list.map((a) => a.id), ext.sources.map((s) => s.id))
  if (!weekly.preview) await weekly.loadAll()
})

// 视图是 v-show 常驻挂载的，`onMounted` 只在应用启动时跑一次 —— 所以「候选共 N 篇」
// 会一直停在首次加载时的数字。用户删掉文章再切过来，看到的还是旧的（2026-09 用户报的）。
// 每次切到本页都重算一遍候选；`inputsSeeded` 保证这不会覆盖他改过的期号/目录/篇数。
watch(
  () => ui.view,
  (v) => {
    if (v === 'weekly') void weekly.loadPreview()
  },
)

// ---- 打分速度（**持久设置**，不是本期参数）----
//
// 与同一个面板里的「精选篇数」语义不同：那个是本次请求的参数，这两个改完立即
// 存盘、对之后每一期都生效。界面上明确写出来，免得以为只影响这一次。
function _clampInt(v: string, lo: number, hi: number, fallback: number): number {
  const n = Math.round(Number(v))
  if (!Number.isFinite(n)) return fallback
  return Math.max(lo, Math.min(hi, n))
}
/** 切换候选范围要**立刻重算** —— 否则「共 N 篇候选」还停在旧口径，对不上 */
function onOnlyKept(on: boolean) {
  weekly.onlyKept = on
  void weekly.loadPreview()
}

// ---- 候选明细（2026-09）----
//
// 起因：用户问「能不能看到文章判定的原因」。筛选理由本来就在列表页有，但**打分
// 理由**只活在生成的 HTML 和缓存文件里 —— 生成前「候选 37 篇」是个点不开的数字。
// 这里把它摊开：每篇的筛选判定 + 打分理由（有缓存的话）。
//
// 打分只可能来自缓存：生成前不存在这次的分数，没打过的老实标「生成时打分」。
const showCands = ref(false)

async function openCands() {
  showCands.value = true
  await weekly.loadCandidates()   // 每次打开都重拉，免得看到过期的口径
}

/** 只列「有打分」的那部分，供抽屉里的分组计数 */
const scoredCount = computed(
  () => weekly.candidates?.items.filter((i) => i.scored).length ?? 0,
)

/** 一行的副标题：中文译名优先，没有就不显示 */
function candTitle(c: WeeklyCandidate): string {
  return c.title_cn && c.title_cn !== c.title ? c.title_cn : ''
}

/** 筛选判定 → 徽章文案与配色（SBadge 的变体只有 m/g/bu/x/default）。
 *  **未判定 ≠ 被否掉**，必须分开显示 —— 混成一个颜色会让人以为没筛过的也被筛掉了。 */
function verdictView(c: WeeklyCandidate): { text: string; variant: 'g' | 'x' | 'bu' } {
  if (c.verdict === true) return { text: '筛:留', variant: 'g' }
  if (c.verdict === false) return { text: '筛:删', variant: 'x' }
  return { text: '筛:未判定', variant: 'bu' }
}

function setScoreBatch(v: string) {
  void settings.savePrefs({ weeklyScoreBatchSize: _clampInt(v, 1, 20, 8) })
}
function setScoreWorkers(v: string) {
  void settings.savePrefs({ weeklyWorkers: _clampInt(v, 1, 16, 4) })
}

// ---- 来源勾选（**勾了才算**）----
//
// 2026-09 改：原先是「不勾 = 全部」，默认一个都不勾。于是「全部」这个链接
// 在默认状态下点了等于没点（状态本来就是全部），用户看到的就是「点了没反应」。
// 现在默认全选，勾 = 纳入、取消勾 = 排除，所见即所得。
const allAccounts = computed(
  () => accounts.list.length > 0 && weekly.accountIds.size === accounts.list.length,
)
const allSources = computed(
  () => ext.sources.length > 0 && weekly.sourceIds.size === ext.sources.length,
)
// ---- 逐行的「本区间 N 篇」----
//
// 2026-09：原先只有外部来源行显示一个数字，而且那是**全库条目数**（不分日期），
// 与表头的「共 N 篇候选」（区间内）是两回事 —— 用户看到「arxiv_paper 59」和
// 「论文 0」并排，以为坏了。现在每行都显示同一口径：**当前区间内的候选数**。
function accountCount(id: string): number {
  return weekly.preview?.account_counts?.[id] ?? 0
}
function sourceCount(id: string): number {
  return weekly.preview?.source_counts?.[id] ?? 0
}
/** 该目录一共收了多少条（不分日期）—— 用来解释「为什么本区间是 0 篇」 */
function sourceTotal(s: ExternalSource): number {
  return Number(s.item_count || 0)
}
function sourceTip(s: ExternalSource): string {
  return (
    `${s.name || s.path}\n本区间 ${sourceCount(s.id)} 篇\n该目录共 ${sourceTotal(s)} 条（不分日期）`
  )
}

/** 有外部目录、但本区间一条都没进来 —— 说清楚缘由，免得看着像坏了 */
const sourceHint = computed(() => {
  const p = weekly.preview
  if (!p || p.arxiv + p.external_other > 0) return ''
  const total = ext.sources.reduce((n, s) => n + sourceTotal(s), 0)
  if (!total) return ''
  return (
    `其他来源目录里共有 ${total} 条，但日期都不在所选区间内 —— 日期区间筛的是内容自身的` +
    `日期，不是它所在的目录名（目录名是流水线把它收进来的那天）。`
  )
})

/** 一个来源都没勾 —— 生成不了，界面要明确说出来而不是让后端当「全部」处理 */
const noneSelected = computed(
  () => accounts.list.length + ext.sources.length > 0
    && weekly.accountIds.size === 0 && weekly.sourceIds.size === 0,
)

/** 勾选变了才值得重新统计；全不勾时不请求（后端把空列表当「全部」） */
function refreshPreview() {
  if (!noneSelected.value) void weekly.loadPreview()
}

function toggleAccount(id: string, on: boolean) {
  if (on) weekly.accountIds.add(id)
  else weekly.accountIds.delete(id)
  refreshPreview()
}
function toggleSource(id: string, on: boolean) {
  if (on) weekly.sourceIds.add(id)
  else weekly.sourceIds.delete(id)
  refreshPreview()
}
function selectAllAccounts() {
  weekly.toggleAllAccounts(accounts.list.map((a) => a.id))
  refreshPreview()
}
function selectAllSources() {
  weekly.toggleAllSources(ext.sources.map((s) => s.id))
  refreshPreview()
}

// ---- 目录 / 模板文件 ----
async function pickOutDir() {
  const { path, reason } = await chooseDirectory()
  if (reason) {
    ui.error(reason)
    return
  }
  if (path) weekly.outDir = path
}
async function pickTemplate() {
  const { path, reason } = await chooseFile('html')
  if (reason) {
    ui.error(reason)
    return
  }
  if (path) weekly.templatePath = path
}
function useBuiltinTemplate() {
  weekly.templatePath = ''
}

// ---- 提示词展开状态 ----
const openPrompt = ref<string>('')
function togglePrompt(k: string) {
  openPrompt.value = openPrompt.value === k ? '' : k
}
const promptKeys = computed(() => Object.keys(weekly.prompts))

const genTask = computed(() => (weekly.genTaskId ? tasks.tasks[weekly.genTaskId] : null))
const renderTask = computed(() => (weekly.renderTaskId ? tasks.tasks[weekly.renderTaskId] : null))
const busy = computed(() => !!weekly.genTaskId || !!weekly.renderTaskId)

/** 打开本地文件/目录（往期报告、期目录、上次输出）—— 走后端 shell_open，
 *  不能走 openExternal('file://')：shell 侧只放行 http(s)，file:// 会被静默挡下。 */
async function openPath(p: string) {
  if (!p) {
    ui.error('没有可打开的路径')
    return
  }
  const { ok, reason } = await openLocalPath(p)
  if (!ok) ui.error(reason || '打开失败')
}
</script>

<template>
  <section class="view-root">
    <header class="page-header">
      <h1>周报生成</h1>
      <span class="muted" style="font-size:var(--fs-sm)">
        选日期区间与来源 → AI 打分选题 → 按模板渲染 → 连同原文一起归档
      </span>
    </header>
    <div class="page-body">
      <SkeletonRows v-if="!weekly.preview && weekly.loadingPreview" :rows="6" />

      <template v-else>
        <!-- 本期设置 -->
        <div class="panel">
          <div class="panel-title">本期设置</div>
          <div class="mitm-row">
            <span class="form-label">期号</span>
            <input v-model.number="weekly.issueNum" type="number" min="1" class="input" style="width:76px" />
            <span class="tertiary" style="font-size:var(--fs-xs)">第 N 期（按输出目录里的往期自动推算，可改）</span>
          </div>
          <div class="mitm-row" style="margin-top:var(--sp-2)">
            <span class="form-label">日期区间</span>
            <input v-model="weekly.fromDate" type="date" class="input" style="width:140px"
                   @change="weekly.loadPreview()" />
            <span class="tertiary">至</span>
            <input v-model="weekly.toDate" type="date" class="input" style="width:140px"
                   @change="weekly.loadPreview()" />
            <span class="form-label">精选篇数</span>
            <input v-model.number="weekly.selectedCount" type="number" min="1" max="100"
                   class="input" style="width:70px" />
          </div>
          <div class="mitm-row" style="margin-top:var(--sp-2)">
            <span class="form-label">输出目录</span>
            <SInput v-model="weekly.outDir" mono placeholder="留空 = 设置里的默认周报目录"
                    style="flex:1;min-width:240px" />
            <SButton size="sm" @click="pickOutDir">选择目录…</SButton>
          </div>
          <div class="mitm-row" style="margin-top:var(--sp-2)">
            <span class="form-label">报告标题</span>
            <SInput v-model="weekly.reportTitle" placeholder="留空用设置里的默认标题"
                    style="flex:1;min-width:240px" />
          </div>
          <div class="mitm-row" style="margin-top:var(--sp-2)">
            <span class="form-label">候选范围</span>
            <label class="ck-row" style="padding:0">
              <input type="checkbox" class="cb" :checked="weekly.onlyKept"
                     @change="onOnlyKept(($event.target as HTMLInputElement).checked)" />
              <span style="font-size:var(--fs-sm)">只考虑 AI 筛选没被过滤掉的文章</span>
            </label>
            <span class="tertiary" style="font-size:var(--fs-xs)">
              你在「历史文章」里筛掉的不再进候选（<b>没筛过的照收</b>）。取消勾选 = 让周报
              从区间内全部文章里自己挑。
            </span>
          </div>
          <div class="mitm-row" style="margin-top:var(--sp-2)">
            <span class="form-label">补全正文</span>
            <label class="ck-row" style="padding:0">
              <input type="checkbox" class="cb" :checked="weekly.fetchBodies"
                     @change="weekly.fetchBodies = ($event.target as HTMLInputElement).checked" />
              <span style="font-size:var(--fs-sm)">生成前给只有标题/摘要的候选抓正文</span>
            </label>
            <span class="tertiary" style="font-size:var(--fs-xs)">
              没有正文时打分与解读只能看标题 —— 有实测数据的文章会被判成「未给出量化数据」，
              厂商宣传稿也认不出来。抓到的正文会写回缓存，下次不再重抓。
            </span>
          </div>
          <div class="mitm-row" style="margin-top:var(--sp-2)">
            <span class="form-label">打分速度</span>
            <span class="tertiary" style="font-size:var(--fs-xs)">每批</span>
            <input :value="settings.prefs.weeklyScoreBatchSize" type="number" min="1" max="20"
                   class="input" style="width:68px"
                   @change="setScoreBatch(($event.target as HTMLInputElement).value)" />
            <span class="tertiary" style="font-size:var(--fs-xs)">篇</span>
            <span class="form-label">并发请求</span>
            <input :value="settings.prefs.weeklyWorkers" type="number" min="1" max="16"
                   class="input" style="width:68px"
                   @change="setScoreWorkers(($event.target as HTMLInputElement).value)" />
            <span class="tertiary" style="font-size:var(--fs-xs)">
              条 · <b>保存为默认</b>（对之后每期生效）。批越大请求越少，并发越大越容易触发模型限流。
            </span>
          </div>
        </div>

        <!-- 候选与来源 -->
        <div class="panel">
          <div class="panel-title">
            候选来源
            <span v-if="noneSelected" class="badge bu" style="margin-left:8px">未选择任何来源</span>
            <template v-else>
              <!-- 口径写在标题里：这个数字是**日期区间内**的候选数，
                   与每行末尾的数字同源（下面的分组标题也各有一份） -->
              <span class="badge" style="margin-left:8px">
                本区间共 {{ weekly.preview?.total ?? 0 }} 篇候选
              </span>
              <!-- 点开看逐篇：凭什么在池子里、怎么被判的、打了多少分 -->
              <SButton v-if="weekly.preview?.total" size="sm" variant="ghost"
                       style="margin-left:8px" @click="openCands">
                查看明细
              </SButton>
            </template>
          </div>
          <div class="mitm-row" style="align-items:flex-start">
            <!-- 公众号条目多，给更宽的一列（约 2:1）；两块各自多列 + 各自滚动 -->
            <div class="src-col" style="flex:2">
              <div class="muted src-head">
                <span>
                  公众号 <span class="tertiary">{{ accounts.list.length }} 个</span>
                  <span class="tertiary">· 本区间 {{ weekly.preview?.wechat ?? 0 }} 篇</span>
                </span>
                <a href="#" @click.prevent="selectAllAccounts">
                  {{ allAccounts ? '全不选' : '全选' }}
                </a>
              </div>
              <EmptyState v-if="!accounts.list.length" text="还没有添加公众号" />
              <div v-else class="ck-grid">
                <label v-for="a in accounts.list" :key="a.id" class="ck-row">
                  <input type="checkbox" class="cb" :checked="weekly.accountIds.has(a.id)"
                         @change="toggleAccount(a.id, ($event.target as HTMLInputElement).checked)" />
                  <span class="acct-name" :title="a.name">{{ a.name }}</span>
                  <span class="muted mono cnt">{{ accountCount(a.id) }}</span>
                </label>
              </div>
            </div>
            <div class="src-col" style="flex:1">
              <div class="muted src-head">
                <span>
                  其他来源目录 <span class="tertiary">{{ ext.sources.length }} 个</span>
                  <span class="tertiary">
                    · 本区间 {{ (weekly.preview?.arxiv ?? 0) + (weekly.preview?.external_other ?? 0) }} 篇
                  </span>
                </span>
                <a href="#" @click.prevent="selectAllSources">
                  {{ allSources ? '全不选' : '全选' }}
                </a>
              </div>
              <EmptyState v-if="!ext.sources.length" text="还没有登记外部来源目录" />
              <div v-else class="ck-grid">
                <label v-for="s in ext.sources" :key="s.id" class="ck-row">
                  <input type="checkbox" class="cb" :checked="weekly.sourceIds.has(s.id)"
                         @change="toggleSource(s.id, ($event.target as HTMLInputElement).checked)" />
                  <span class="acct-name" :title="sourceTip(s)">{{ s.name || s.path }}</span>
                  <span class="muted mono cnt">{{ sourceCount(s.id) }}</span>
                </label>
              </div>
            </div>
          </div>
          <div v-if="sourceHint" class="muted" style="font-size:var(--fs-sm);margin-top:var(--sp-2)">
            {{ sourceHint }}
          </div>
          <div v-if="noneSelected" class="muted" style="font-size:var(--fs-sm);margin-top:var(--sp-2)">
            一个来源都没勾 —— 上面的勾选决定哪些内容参与选题，至少勾一个才能生成。
          </div>
          <div v-else-if="!weekly.preview?.total" class="muted" style="font-size:var(--fs-sm);margin-top:var(--sp-2)">
            当前条件下没有候选文章 —— 检查日期区间是否覆盖了已拉取/已扫描的内容。
          </div>
        </div>

        <!-- 模板 -->
        <div class="panel">
          <div class="panel-title">模板</div>
          <div class="mitm-row">
            <span class="form-label">模板文件</span>
            <SInput v-model="weekly.templatePath" mono
                    placeholder="留空 = 使用内置模板" style="flex:1;min-width:240px" />
            <SButton size="sm" @click="pickTemplate">选择文件…</SButton>
            <SButton size="sm" variant="ghost" @click="useBuiltinTemplate">用内置模板</SButton>
          </div>
          <div class="muted" style="font-size:var(--fs-xs);margin-top:var(--sp-2);line-height:1.7">
            内置模板：<span class="mono">{{ weekly.preview?.template_path || '（未知）' }}</span>
            <template v-if="weekly.preview && !weekly.preview.template_exists">
              <br /><span class="status-fail">内置模板缺失，生成会失败</span>
            </template>
            <br />
            <b>模板是唯一真相</b>：里面的循环、条件和字段就是最终输出。改完点下面任意一期的
            「按当前模板重新渲染」即可看效果 —— <b>不调用 AI，不花钱</b>；
            写错变量名会明确告诉你哪个名字有问题，不会静默变空白。
          </div>
        </div>

        <!-- 生成 / 重渲染 -->
        <div class="panel">
          <div class="panel-title">生成</div>
          <div class="mitm-row">
            <SButton variant="primary" :disabled="busy || noneSelected || !weekly.preview?.total"
                     @click="weekly.generate()">
              生成第 {{ weekly.issueNum }} 期
            </SButton>
            <SButton size="sm" variant="ghost" :disabled="weekly.loadingPreview"
                     @click="weekly.loadPreview()">重新统计候选</SButton>
            <ProgressInline v-if="genTask" :text="weekly.stageMsg || genTask.message || '生成中…'"
                            cancellable @cancel="weekly.cancelGenerate()" />
            <ProgressInline v-else-if="renderTask"
                            :text="weekly.stageMsg || renderTask.message || '渲染中…'" />
          </div>
          <div v-if="weekly.lastResult" class="muted" style="font-size:var(--fs-xs);margin-top:var(--sp-2)">
            <template v-if="weekly.lastResult.issue_dir">
              上次输出：<a href="#" class="mono" @click.prevent="openPath(weekly.lastResult!.issue_dir!)">
                {{ weekly.lastResult.issue_dir }}
              </a>
            </template>
            <template v-if="weekly.lastResult.missing_vars?.length">
              <br /><span class="status-fail">
                模板引用了未知变量：{{ weekly.lastResult.missing_vars.join('、') }}
              </span>
            </template>
          </div>
        </div>

        <!-- 提示词 -->
        <div class="panel">
          <div class="panel-title">
            提示词
            <span class="tertiary" style="font-weight:400;margin-left:8px">
              — 输出 JSON 格式由软件固定，这里编辑的是判定标准
            </span>
          </div>
          <div class="muted" style="font-size:var(--fs-xs);margin-bottom:var(--sp-2);line-height:1.7">
            改了哪一段，<b>该段的缓存就会失效</b>，下次生成按新标准重算（其他段仍命中缓存，不重复花钱）；
            改回原文还能命中旧缓存。
          </div>
          <div v-for="k in promptKeys" :key="k" style="border-top:1px solid var(--border)">
            <div class="toolbar" style="padding:var(--sp-2) 0;cursor:pointer" @click="togglePrompt(k)">
              <SIcon :name="openPrompt === k ? 'chevron-down' : 'chevron-right'" :size="12" />
              <span>{{ weekly.prompts[k].label }}</span>
              <span v-if="weekly.promptDirty(k)" class="badge bu">未保存</span>
            </div>
            <div v-if="openPrompt === k" style="padding-bottom:var(--sp-2)">
              <textarea v-model="weekly.prompts[k].text" class="principles" spellcheck="false"></textarea>
              <div class="toolbar" style="margin-top:var(--sp-2)">
                <span class="spacer"></span>
                <SButton size="sm" variant="ghost" @click="weekly.restorePrompt(k)">恢复默认</SButton>
                <SButton size="sm" variant="primary" :disabled="!weekly.promptDirty(k)"
                         @click="weekly.savePrompt(k)">保存</SButton>
              </div>
            </div>
          </div>
        </div>

        <!-- 往期 -->
        <div class="panel">
          <div class="panel-title">往期</div>
          <EmptyState v-if="!weekly.issues.length" text="还没有生成过周报" />
          <div v-else class="ext-table">
            <div class="ext-head" style="grid-template-columns:70px 110px 1fr auto">
              <span>期号</span><span>日期</span><span>目录</span><span></span>
            </div>
            <div v-for="it in weekly.issues" :key="it.dir" class="ext-row"
                 style="grid-template-columns:70px 110px 1fr auto">
              <span class="mono">第{{ it.issue_num }}期</span>
              <span class="mono muted">{{ it.date }}</span>
              <span class="ext-path">{{ it.name }}</span>
              <span class="row-actions">
                <SButton v-if="it.report" size="sm" variant="ghost" @click="openPath(it.report)">打开报告</SButton>
                <SButton size="sm" variant="ghost" @click="openPath(it.dir)">打开目录</SButton>
                <SButton v-if="it.has_snapshot" size="sm" variant="ghost"
                         :disabled="busy" @click="weekly.rerender(it.dir)">
                  按当前模板重新渲染
                </SButton>
              </span>
            </div>
          </div>
        </div>
      </template>
    </div>
  <!-- 候选明细（2026-09）：生成前摊开每篇「为什么在池子里、怎么被判的」。
       ⚠️ 必须留在 `.view-root` **内部**：App.vue 用 `<WeeklyView v-show>` 切页，
       放到 section 外面会让本组件变成多根节点、v-show 静默失效 —— 表现是这一页
       和别的页同时渲染出来（实测踩到过）。抽屉自己是 Teleport 到 body 的，
       放在这儿不影响它的定位。 -->
  <SDrawer :open="showCands" title="候选明细" @close="showCands = false">
    <SkeletonRows v-if="weekly.loadingCandidates" :rows="6" />
    <EmptyState v-else-if="!weekly.candidates?.items.length"
                text="当前条件下没有候选文章 —— 检查日期区间是否覆盖了已拉取/已扫描的内容。" />
    <template v-else>
      <div class="cand-sum">
        共 <b>{{ weekly.candidates.total }}</b> 篇 ·
        已有打分 <b>{{ scoredCount }}</b> 篇
        <template v-if="scoredCount < weekly.candidates.total">
          ，其余 <b>{{ weekly.candidates.total - scoredCount }}</b> 篇<b>生成时</b>才打分
        </template>
      </div>
      <!-- 这句是防误解的关键：生成前不存在这次的分数，不是「丢了」 -->
      <div class="cand-note">
        打分理由来自缓存，只有与**当前提示词**对得上的才有 —— 改过提示词或首次跑都会显示
        「待打分」，那是正常的，不是缺陷。筛选判定则一直都有。
      </div>

      <div v-for="c in weekly.candidates.items" :key="c.key" class="cand-row">
        <!-- 一行元信息：日期 · 筛选判定 · 来源 · 分数/标签 -->
        <div class="cand-head">
          <span class="mono muted cand-date">{{ c.date }}</span>
          <SBadge :variant="verdictView(c).variant">{{ verdictView(c).text }}</SBadge>
          <span class="muted cand-src" :title="c.source">{{ c.source || c.kind }}</span>
          <template v-if="c.scored">
            <span class="cand-score">{{ c.score }}</span>
            <SBadge v-if="c.semiconductor === false" variant="bu">非半导体</SBadge>
            <span v-for="t in c.business_tags" :key="t" class="badge">{{ t }}</span>
          </template>
          <span v-else class="tertiary">待打分</span>
        </div>
        <div class="cand-title">
          <STooltip :text="c.title" style="min-width:0"><span>{{ c.title }}</span></STooltip>
        </div>
        <div v-if="candTitle(c)" class="cand-sub">{{ candTitle(c) }}</div>
        <!-- 两种理由各占一行、各带标签：抽屉只有 480px，**必须**说清哪条是哪个阶段的
             —— 「筛选」决定进不进池子、「入选」是打分时写的，混着看会误判 -->
        <div v-if="c.verdict_reason" class="cand-line">
          <span class="cand-tag">筛选理由</span>{{ c.verdict_reason }}
        </div>
        <div v-if="c.reason" class="cand-line">
          <span class="cand-tag">入选理由</span>{{ c.reason }}
        </div>
      </div>
    </template>
  </SDrawer>
  </section>
</template>

<style scoped>
/* 候选明细抽屉 */
.cand-sum {
  font-size: var(--fs-sm);
  color: var(--text-secondary);
  margin-bottom: 4px;
}
.cand-note {
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  line-height: 1.6;
  padding-bottom: var(--sp-2);
  border-bottom: 1px solid var(--border);
  margin-bottom: var(--sp-2);
}
/* 一篇一块：上下留白分组，比表格窄屏下好读（抽屉只有 480px） */
.cand-row {
  padding: var(--sp-2) 0;
  border-bottom: 1px solid var(--border);
}
.cand-row:last-child {
  border-bottom: 0;
}
.cand-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 2px;
}
.cand-date {
  font-size: var(--fs-xs);
  white-space: nowrap;
}
/* 带标签的理由行：标签定宽 + 正文可换行（理由要能读全，不截断） */
.cand-line {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  line-height: 1.65;
  margin-top: 3px;
  word-break: break-word;
}
.cand-tag {
  display: inline-block;
  margin-right: 6px;
  padding: 0 4px;
  border-radius: var(--radius-sm);
  background: var(--bg-hover);
  color: var(--text-tertiary);
  white-space: nowrap;
}
.cand-title {
  font-size: var(--fs-sm);
  color: var(--text-primary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cand-sub {
  font-size: var(--fs-xs);
  color: var(--text-secondary);
  margin-top: 1px;
}
.cand-src {
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cand-score {
  font-weight: 600;
  color: var(--accent);
}

/* 候选来源（2026-09 重排）：改之前两块是「flex:1 的列 + 内部块级堆叠」——
   结构上就排不出多列，29 个公众号会把面板拉成一长条，而右边只有 1 个目录。 */
.src-col {
  min-width: 240px;
  display: flex;
  flex-direction: column;
}
.src-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: var(--fs-sm);
  margin-bottom: 6px;
}
/* 自适应多列；限高 + 滚动，条目再多也不会把面板撑长 */
.ck-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
  gap: 0 10px;
  max-height: 168px;
  overflow-y: auto;
  padding-right: 4px;
}
/* 行尾的「本区间 N 篇」：等宽 + 右对齐，数字竖着能对齐才扫得快 */
.ck-row .cnt {
  margin-left: auto;
  padding-left: 6px;
  font-size: var(--fs-xs);
  flex-shrink: 0;
}
.ck-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 0;
  font-size: var(--fs-sm);
  cursor: pointer;
  /* 网格项默认 min-width:auto —— 少了这行，超长公众号名会把整列撑宽而不是
     走 .acct-name 的省略号（.acct-name 自己是 overflow:hidden 的 flex 子项，
     自动最小尺寸已经是 0，所以只需要在这一层放行） */
  min-width: 0;
}
</style>
