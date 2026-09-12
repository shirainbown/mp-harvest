<script setup lang="ts">
// 页面：设置（GET/PUT /api/settings 扁平 KV）——导出目录、图片下载、AI 默认值、
// 网络代理、平台能力。2026-09 把原「网络设置」整页并入本页。
import { computed, onMounted, ref, watch } from 'vue'
import SButton from '../components/SButton.vue'
import SInput from '../components/SInput.vue'
import SSwitch from '../components/SSwitch.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import { useSettingsStore } from '../stores/settings'
import { fmtSize, useStorageStore } from '../stores/storage'
import { useUiStore } from '../stores/ui'
import { chooseDirectory } from '../api/desktop'

const settings = useSettingsStore()
const storage = useStorageStore()
const ui = useUiStore()

onMounted(() => {
  if (!settings.loaded) settings.load()
  void storage.load()
})

// 视图是 v-show 常驻挂载的，`onMounted` 只在应用启动时跑一次 —— 不补这个 watch，
// 用户删完文件再切回本页看到的还是启动那一刻的旧数字（2026-09 用户报的「删了导出
// 材料，界面完全没有变化」）。每次切到本页都重扫一遍磁盘。
watch(
  () => ui.view,
  (v) => {
    if (v === 'settings') void storage.load()
  },
)

// ---- 存储占用与清理（2026-09）----
//
// 只列**可重建**的项供勾选；账号/凭证/模型配置/CA/提示词/设置/文章缓存显示为
// 「保留」—— 删了要重新抓包或重新联网拉，不能混在「清缓存」里被顺手清掉。
const picked = ref(new Set<string>())
const confirmClean = ref(false)

const pickedSize = computed(() =>
  storage.items.filter((i) => picked.value.has(i.key)).reduce((n, i) => n + i.size, 0),
)

/** 选中的项里哪些是「代价高」的。确认框要把**每一项的代价**逐条列出来 ——
 *  只说一句「不可撤销」等于没说，用户根本不知道自己要重新抓包。 */
const pickedCostly = computed(() =>
  storage.items.filter((i) => i.costly && picked.value.has(i.key)),
)

function togglePick(key: string, on: boolean) {
  const s = new Set(picked.value)
  if (on) s.add(key)
  else s.delete(key)
  picked.value = s
}

async function doClean() {
  confirmClean.value = false
  const keys = [...picked.value]
  picked.value = new Set()
  await storage.clean(keys)
}

// ---- 网络代理（原「网络设置」页）----
function setMode(mode: 'direct' | 'system' | 'custom') {
  settings.network.mode = mode
  settings.saveNetwork()
}

const platformLines = computed(() => {
  const p = settings.platform
  if (!p) return []
  const osName = p.os === 'mac' ? 'macOS' : p.os === 'win' ? 'Windows' : p.os
  return [
    `系统：${p.os_version || osName} · 安装 CA ${p.ca_needs_admin ? '需' : '无需'}管理员 · 设置系统代理 ${p.proxy_needs_admin ? '需' : '无需'}管理员`,
    p.data_dir ? `数据目录：${p.data_dir}` : '',
    p.engine ? `渲染引擎：${p.engine}` : '',
  ].filter(Boolean)
})

/** 文本框失焦 / 开关切换 / 数字变更时保存；PUT 为整体覆盖，store 内合并全量 KV */
function save() {
  settings.savePrefs({})
}

async function chooseDir() {
  // 目录选择逻辑抽到 api/desktop.ts（「其他来源」页也要用同一套）。
  // 返回 {path:null, reason} 时要提示 —— 不然用户以为点了没反应。
  const { path, reason } = await chooseDirectory()
  if (reason) {
    ui.error(reason)
    return
  }
  if (path) {
    settings.prefs.exportDefaultDir = path
    save()
  }
  // path 为 null 且无 reason = 用户主动取消，不是错误，静默即可
}

function onBatchSize() {
  settings.prefs.aiBatchSize = Math.max(1, Math.min(200, Math.round(Number(settings.prefs.aiBatchSize) || 50)))
  save()
}
function onWorkers() {
  settings.prefs.aiWorkers = Math.max(1, Math.min(16, Math.round(Number(settings.prefs.aiWorkers) || 4)))
  save()
}

// ---- 拉取历史（2026-09）----
//
// 每个输入框失焦/回车时夹到合法区间再存：空框会得到 NaN，直接 PUT 上去就是
// 后端 400（整页设置都存不进去），所以这里必须兜住。
function clampInt(v: unknown, lo: number, hi: number, fallback: number): number {
  const n = Math.round(Number(v))
  if (!Number.isFinite(n)) return fallback
  return Math.max(lo, Math.min(hi, n))
}
function onDelayMin() {
  settings.prefs.fetchDelayMin = clampInt(settings.prefs.fetchDelayMin, 0, 300, 3)
  save()
}
function onDelayMax() {
  settings.prefs.fetchDelayMax = clampInt(settings.prefs.fetchDelayMax, 0, 300, 8)
  save()
}
function onCooldownPages() {
  settings.prefs.fetchCooldownPages = clampInt(settings.prefs.fetchCooldownPages, 0, 1000, 20)
  save()
}
function onCooldownSeconds() {
  settings.prefs.fetchCooldownSeconds = clampInt(settings.prefs.fetchCooldownSeconds, 0, 3600, 60)
  save()
}
function onRetries() {
  settings.prefs.fetchRetries = clampInt(settings.prefs.fetchRetries, 0, 10, 2)
  save()
}
function onMaxPages() {
  settings.prefs.fetchMaxPages = clampInt(settings.prefs.fetchMaxPages, 1, 1000, 100)
  save()
}
</script>

<template>
  <section class="view-root">
  <header class="page-header">
    <h1>设置</h1>
  </header>
  <div class="page-body">
    <!-- 加载失败错误条：不静默 -->
    <div v-if="settings.prefsError" class="status-fail" style="padding:var(--sp-2) var(--sp-3);border:1px solid var(--danger);border-radius:var(--radius-md)">
      {{ settings.prefsError }}
    </div>
    <SkeletonRows v-if="!settings.loaded && !settings.prefsError" :rows="3" />

    <template v-else>
      <div class="panel">
        <div class="panel-title">导出</div>
        <div class="mitm-row">
          <span class="form-label">默认目录</span>
          <SInput
            v-model="settings.prefs.exportDefaultDir"
            mono
            placeholder="留空 = 数据目录/exports/<公众号>"
            style="flex:1;min-width:240px"
            @blur="save"
          />
          <SButton size="sm" @click="chooseDir">选择目录…</SButton>
        </div>
        <div class="mitm-row" style="margin-top:var(--sp-2)">
          <SSwitch v-model="settings.prefs.exportDownloadImages" @click="save" />
          <span>导出时下载图片到本地（随 HTML 一起保存，离线可读）</span>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">AI 筛选默认值</div>
        <div class="mitm-row">
          <span class="form-label">每批篇数</span>
          <input
            v-model.number="settings.prefs.aiBatchSize"
            type="number"
            min="1"
            max="200"
            class="input"
            style="width:80px"
            @change="onBatchSize"
          />
          <span class="form-label">并发批数</span>
          <input
            v-model.number="settings.prefs.aiWorkers"
            type="number"
            min="1"
            max="16"
            class="input"
            style="width:72px"
            @change="onWorkers"
          />
        </div>
        <div class="mitm-row" style="margin-top:var(--sp-2)">
          <SSwitch v-model="settings.prefs.aiContinueContentFilter" @click="save" />
          <span>标题筛选完成后自动继续内容筛选</span>
        </div>
      </div>

      <!-- 拉取历史的节奏与容错（2026-09）。微信没有公开的限流文档，这些默认值
           是按社区实测的「别被封」建议给的；放开给用户是因为不同账号的历史
           长度差很多，而**被限流的代价（约 24 小时）远大于慢一点**。 -->
      <div class="panel">
        <div class="panel-title">拉取历史</div>
        <!-- 数字与它的单位/连字符各自包成一个不折行的整体：窗口一窄，flex 会从
             任意空隙处换行，把「8」和「秒」拆到两行 —— 那时用户根本认不出哪个
             数字配哪个单位（实测在窄窗口下就是这样）。 -->
        <div class="mitm-row">
          <span class="form-label">每页间隔</span>
          <span class="fetch-unit">
            <input v-model.number="settings.prefs.fetchDelayMin" type="number" min="0" max="300"
                   class="input" style="width:64px" @change="onDelayMin" />
            <span class="tertiary">~</span>
            <input v-model.number="settings.prefs.fetchDelayMax" type="number" min="0" max="300"
                   class="input" style="width:64px" @change="onDelayMax" />
            <span>秒</span>
          </span>
          <span class="tertiary" style="font-size:var(--fs-xs)">取随机值；固定间隔本身就是个可识别特征</span>
        </div>
        <div class="mitm-row" style="margin-top:var(--sp-2)">
          <span class="form-label">每翻</span>
          <span class="fetch-unit">
            <input v-model.number="settings.prefs.fetchCooldownPages" type="number" min="0" max="1000"
                   class="input" style="width:64px" @change="onCooldownPages" />
            <span>页歇</span>
            <input v-model.number="settings.prefs.fetchCooldownSeconds" type="number" min="0" max="3600"
                   class="input" style="width:72px" @change="onCooldownSeconds" />
            <span>秒</span>
          </span>
          <span class="tertiary" style="font-size:var(--fs-xs)">0 = 不额外歇</span>
        </div>
        <div class="mitm-row" style="margin-top:var(--sp-2)">
          <span class="form-label">单次最多翻</span>
          <span class="fetch-unit">
            <input v-model.number="settings.prefs.fetchMaxPages" type="number" min="1" max="1000"
                   class="input" style="width:72px" @change="onMaxPages" />
            <span>页</span>
          </span>
        </div>
        <div class="mitm-row" style="margin-top:var(--sp-2)">
          <span class="form-label">网络出错重试</span>
          <span class="fetch-unit">
            <input v-model.number="settings.prefs.fetchRetries" type="number" min="0" max="10"
                   class="input" style="width:64px" @change="onRetries" />
            <span>次</span>
          </span>
          <span class="tertiary" style="font-size:var(--fs-xs)">只对超时/连接中断重试，首次等 2 秒、之后翻倍</span>
        </div>
        <div class="tertiary" style="font-size:var(--fs-xs);line-height:1.7;margin-top:var(--sp-2)">
          微信没有公开的限流规则，调快有被限流的风险：<b>被限流后该微信号约 24 小时
          抓不了任何公众号</b>。被限流时应用<b>不会自动重试</b>（那样只会让封锁更久）。
          网络类错误（超时、连接中断）才重试，第一次等 2 秒、第二次 4 秒。
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">更新与下载代理</div>
        <div class="radio-row" @click="setMode('system')">
          <span class="radio" :class="{ on: settings.network.mode === 'system' }"></span>跟随系统代理
        </div>
        <div
          v-if="settings.network.mode === 'system'"
          class="tertiary"
          style="font-size:var(--fs-xs);padding-left:22px;margin-bottom:var(--sp-1)"
        >
          <template v-if="settings.systemProxy">
            当前系统代理：<span class="mono">{{ settings.systemProxy }}</span>
          </template>
          <template v-else>
            未检测到系统代理 —— 若检查更新/下载失败，请改用「自定义 HTTP 代理」
          </template>
        </div>
        <div class="radio-row" @click="setMode('direct')">
          <span class="radio" :class="{ on: settings.network.mode === 'direct' }"></span>直连（不使用代理）
        </div>
        <div class="radio-row" @click="setMode('custom')">
          <span class="radio" :class="{ on: settings.network.mode === 'custom' }"></span>自定义 HTTP 代理
        </div>
        <div class="mitm-row" style="margin-top:var(--sp-2)">
          <span class="form-label">地址</span>
          <SInput
            v-model="settings.network.proxy_url"
            mono
            width="280px"
            placeholder="http://127.0.0.1:7890"
            :disabled="settings.network.mode !== 'custom'"
            @blur="settings.saveNetwork()"
          />
          <SButton size="sm" :loading="settings.proxyTesting" :disabled="settings.network.mode !== 'custom'" @click="settings.testProxy()">
            测试连接
          </SButton>
        </div>
      </div>

      <div class="panel">
        <div class="panel-title">
          存储占用
          <span class="tertiary" style="font-weight:400;margin-left:8px">
            共 {{ fmtSize(storage.total_bytes) }} · 其中可清理 {{ fmtSize(storage.clearable_bytes) }}
          </span>
          <!-- 你在应用外面把文件删了（比如直接删导出的 HTML），数字不会自己变 ——
               重扫一遍磁盘。切回本页时也会自动重扫。 -->
          <SButton size="sm" variant="ghost" style="margin-left:8px"
                   :loading="storage.loading" @click="storage.load()">
            刷新
          </SButton>
        </div>
        <SkeletonRows v-if="storage.showSkeleton" :rows="3" />
        <template v-else>
          <!-- 空项不列（删光了就消失），所以全新安装时这里会一条都没有 ——
               得说一句，否则看着像加载失败 -->
          <div v-if="!storage.items.length" class="muted" style="font-size:var(--fs-sm)">
            本地还没产生什么数据。拉取文章、跑一次筛选或导出之后，这里会列出各项占用。
          </div>
          <div v-for="it in storage.items" :key="it.key" class="st-row">
            <input v-if="it.safe" type="checkbox" class="cb" :checked="picked.has(it.key)"
                   @change="togglePick(it.key, ($event.target as HTMLInputElement).checked)" />
            <span v-else class="st-keep" title="清掉就真没了，不提供清理">保留</span>
            <span class="st-label" :title="it.note">{{ it.label }}</span>
            <!-- 「代价高」要一眼看得出来，不然用户勾完才知道要重新抓包 -->
            <span v-if="it.costly" class="st-costly" title="可清理，但清掉后要重新采集">代价高</span>
            <span class="muted mono" style="font-size:var(--fs-xs);white-space:nowrap">
              {{ fmtSize(it.size) }}
            </span>
          </div>

          <!-- 确认区**独立成块**：代价高的项要逐条把代价写出来，塞在工具栏里挤不下 -->
          <div v-if="confirmClean" class="st-confirm">
            <div>确定清理这 {{ picked.size }} 项？<b>不可撤销</b>。</div>
            <template v-if="pickedCostly.length">
              <div style="margin-top:6px">
                其中 <b>{{ pickedCostly.length }}</b> 项<b>代价高</b>，清掉后要你自己重新采集：
              </div>
              <div v-for="it in pickedCostly" :key="it.key" class="st-costly-row">
                · <b>{{ it.label }}</b>：{{ it.note }}
              </div>
            </template>
            <div class="toolbar" style="margin-top:var(--sp-2)">
              <SButton size="sm" variant="danger" @click="doClean">确定清理</SButton>
              <SButton size="sm" variant="ghost" @click="confirmClean = false">取消</SButton>
            </div>
          </div>

          <div class="toolbar" style="margin-top:var(--sp-3)">
            <span class="tertiary" style="font-size:var(--fs-sm)">
              勾选的都是<b>可清理</b>的；标「保留」的（AI 模型配置、应用设置、自定义提示词、
              手工补录的链接）删了就真没了 —— 它们是你自己填/写的，没有别处能重新得到。
            </span>
            <span class="spacer"></span>
            <SButton v-if="!confirmClean" size="sm" variant="danger" :disabled="!picked.size"
                     @click="confirmClean = true">
              清理选中（{{ fmtSize(pickedSize) }}）
            </SButton>
          </div>
        </template>
      </div>

      <div class="panel">
        <div class="panel-title">平台能力</div>
        <SkeletonRows v-if="!settings.platform" :rows="3" />
        <div v-else class="cap-list">
          <span v-for="(l, i) in platformLines" :key="i" :class="{ mono: l.startsWith('数据目录') }">{{ l }}</span>
        </div>
      </div>
    </template>
  </div>
  </section>
</template>

<style scoped>
/* 「数字 + 单位」不拆行：窄窗口下 flex 会从任意空隙换行，把单位和它的数字
   分到两行，读的人得猜哪个配哪个（实测窄窗口下就是这样） */
.fetch-unit {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  white-space: nowrap;
}

/* 存储占用清单：一行一项，大小右对齐便于扫读 */
.st-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 0;
  font-size: var(--fs-sm);
}
.st-row .st-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
}
/* 不可清理的项：占住勾选框那一格，让两列对齐。
   宽度要够放两个中文字 —— 13px 会把「保留」竖排折成两行（实测截图看到的）。 */
.st-keep {
  flex-shrink: 0;
  width: 30px;
  font-size: var(--fs-xs);
  color: var(--text-tertiary);
  white-space: nowrap;
}
/* 「代价高」标记：勾之前就要看得见，别等确认框才知道要重新抓包 */
.st-costly {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  padding: 0 4px;
  border-radius: var(--radius-sm);
  background: rgba(201, 138, 44, 0.14);
  color: var(--warning);
  white-space: nowrap;
}
/* 确认区：整块铺开，代价逐条列出 */
.st-confirm {
  margin-top: var(--sp-3);
  padding: var(--sp-2) var(--sp-3);
  border: 1px solid var(--danger);
  border-radius: var(--radius-md);
  font-size: var(--fs-sm);
  color: var(--text-secondary);
}
.st-costly-row {
  font-size: var(--fs-xs);
  line-height: 1.7;
  color: var(--text-tertiary);
  padding-left: 6px;
}
</style>
