<script setup lang="ts">
// 页面：执行日志 —— 打包版里用户**唯一**能看到「刚才发生了什么」的地方。
//
// 打包版 `console=False`，代码里所有 `print` 都无处可去；任务、错误、AI 判定原先
// 只活在内存里（关窗即失、刷新即清）。后端在四处埋点（模型调用 / 筛选批次 /
// 任务起止 / 写操作）落进 SQLite，这一页把它们摆出来。
//
// 与「错误中心」的分工：错误中心管**需要你处理的错误**，日志管**发生过什么**。
import { onMounted, ref, watch } from 'vue'
import SButton from '../components/SButton.vue'
import SInput from '../components/SInput.vue'
import SegmentedControl from '../components/SegmentedControl.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import EmptyState from '../components/EmptyState.vue'
import type { LogEvent } from '../types'
import { formatEvents, formatTs, useLogsStore } from '../stores/logs'
import { useUiStore } from '../stores/ui'
import { copyText } from '../api/desktop'

const logs = useLogsStore()
const ui = useUiStore()

const LEVELS = [
  { value: '', label: '全部' },
  { value: 'info', label: '信息' },
  { value: 'warn', label: '警告' },
  { value: 'error', label: '错误' },
]

// 与 style.css 里既有 badge 变体同一套配色（含暗色适配用的都是主题变量）
const LEVEL_STYLE: Record<string, string> = {
  debug: 'background:var(--bg-hover);color:var(--text-tertiary)',
  info: 'background:rgba(47,111,237,.12);color:var(--accent)',
  warn: 'background:rgba(201,138,44,.14);color:var(--warning)',
  error: 'background:rgba(208,85,79,.14);color:var(--danger)',
}

/** 展开了详情的行 id */
const openIds = ref(new Set<number>())
const confirmClear = ref(false)

onMounted(() => {
  // 视图是 v-show 常驻挂载的，这里只在该页被切到时拉一次
  if (ui.view === 'logs') void logs.load()
})
watch(
  () => ui.view,
  (v) => {
    if (v === 'logs') void logs.load()
  },
)

function setLevel(v: string) {
  logs.level = v as typeof logs.level
  void logs.load()
}
function setKind(v: string) {
  logs.kind = v
  void logs.load()
}

function hasData(e: LogEvent): boolean {
  return Object.keys(e.data || {}).length > 0
}
function pretty(e: LogEvent): string {
  return JSON.stringify(e.data, null, 2)
}
function toggleOpen(id: number) {
  const s = new Set(openIds.value)
  if (s.has(id)) s.delete(id)
  else s.add(id)
  openIds.value = s
}

async function copyOne(e: LogEvent) {
  const text = JSON.stringify(e.data, null, 2)
  const ok = hasData(e)
    ? await copyText(`[${formatTs(e.ts)}] [${e.level}] ${e.kind} ${e.message}\n${text}`)
    : await copyText(`[${formatTs(e.ts)}] [${e.level}] ${e.kind} ${e.message}`)
  if (ok) ui.toast('已复制这条日志')
  else ui.error('复制失败，请手动选择文本')
}

async function copyAll() {
  if (!logs.events.length) return
  if (await copyText(formatEvents(logs.events))) {
    ui.toast(`已复制 ${logs.events.length} 条日志`)
  } else {
    ui.error('复制失败，请手动选择文本')
  }
}

async function doClear() {
  confirmClear.value = false
  openIds.value = new Set()
  await logs.clear()
}
</script>

<template>
  <section class="view-root">
    <header class="page-header">
      <h1>执行日志</h1>
      <span class="muted" style="font-size:var(--fs-sm)">
        用户操作、任务起止与失败、AI 调用与筛选结果 —— 全部保存在本机，不上传
      </span>
    </header>
    <div class="page-body">

    <div class="panel">
      <div class="panel-title">
        共 {{ logs.total }} 条
        <span v-if="logs.events.length < logs.total" class="tertiary" style="font-weight:400">
          （当前显示最近 {{ logs.events.length }} 条）
        </span>
      </div>

      <div class="toolbar">
        <span class="form-label">级别</span>
        <SegmentedControl :model-value="logs.level" :options="LEVELS"
                          @update:model-value="setLevel($event)" />
        <span class="form-label">类型</span>
        <select class="input btn-sm" style="height:24px;font-size:var(--fs-xs);max-width:220px"
                :value="logs.kind" @change="setKind(($event.target as HTMLSelectElement).value)">
          <option v-for="o in logs.kindOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
        </select>
        <SInput v-model="logs.q" placeholder="搜索内容 / 上下文…" style="width:220px"
                @keydown.enter="logs.load()" />
        <SButton size="sm" @click="logs.load()">查询</SButton>
        <span class="spacer"></span>
        <SButton size="sm" variant="ghost" :disabled="!logs.events.length" @click="copyAll">
          复制全部
        </SButton>
        <SButton v-if="!confirmClear" size="sm" variant="ghost" :disabled="!logs.total"
                 @click="confirmClear = true">
          清空
        </SButton>
        <template v-else>
          <span class="tertiary" style="font-size:var(--fs-sm)">确定清空全部日志？</span>
          <SButton size="sm" variant="danger" @click="doClear">确定清空</SButton>
          <SButton size="sm" variant="ghost" @click="confirmClear = false">取消</SButton>
        </template>
      </div>

      <!-- 只在**首次**加载时显示骨架屏：列表本来就空时再插一段，切筛选会闪 -->
      <SkeletonRows v-if="logs.showSkeleton" :rows="6" />
      <EmptyState v-else-if="!logs.events.length"
                  text="还没有日志。跑一次拉取、筛选或周报生成，这里就会记下来。" />
      <table v-else class="log-table">
        <thead>
          <tr>
            <th style="width:150px">时间</th>
            <th style="width:56px">级别</th>
            <th style="width:120px">类型</th>
            <th>内容</th>
            <th style="width:96px"></th>
          </tr>
        </thead>
        <tbody>
          <template v-for="e in logs.events" :key="e.id">
            <tr>
              <td class="mono" style="white-space:nowrap">{{ formatTs(e.ts) }}</td>
              <td>
                <span class="badge" :style="LEVEL_STYLE[e.level] || LEVEL_STYLE.debug">
                  {{ e.level }}
                </span>
              </td>
              <td class="mono">{{ e.kind }}</td>
              <td style="word-break:break-word">{{ e.message }}</td>
              <td style="white-space:nowrap;text-align:right">
                <SButton v-if="hasData(e)" size="sm" variant="ghost" @click="toggleOpen(e.id)">
                  {{ openIds.has(e.id) ? '收起' : '详情' }}
                </SButton>
                <SButton size="sm" variant="ghost" @click="copyOne(e)">复制</SButton>
              </td>
            </tr>
            <tr v-if="openIds.has(e.id)">
              <td colspan="5" style="background:var(--bg-app)">
                <pre class="log-data">{{ pretty(e) }}</pre>
              </td>
            </tr>
          </template>
        </tbody>
      </table>

      <div v-if="logs.hasMore" class="toolbar" style="justify-content:center;margin-top:var(--sp-3)">
        <SButton size="sm" :disabled="logs.loading" @click="logs.loadMore()">
          {{ logs.loading ? '加载中…' : '加载更多（更早的）' }}
        </SButton>
      </div>
    </div>
    </div>
  </section>
</template>

<style scoped>
.log-table {
  width: 100%;
  border-collapse: collapse;
  font-size: var(--fs-sm);
}
.log-table th {
  text-align: left;
  font-weight: 600;
  color: var(--text-secondary);
  padding: 6px 8px;
  border-bottom: 1px solid var(--border);
}
.log-table td {
  padding: 5px 8px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
  color: var(--text-primary);
}
/* 详情：模型原始返回可能很长，限高滚动而不是把整页撑开 */
.log-data {
  margin: 0;
  max-height: 320px;
  overflow: auto;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: var(--text-secondary);
}
</style>
