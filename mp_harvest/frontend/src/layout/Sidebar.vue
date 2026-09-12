<script setup lang="ts">
// 侧边栏 200px：两组导航（主功能/配置）+ 错误中心 + 版本号（§5.3）
import { ref } from 'vue'
import { APP_VERSION } from '../config'
import SButton from '../components/SButton.vue'
import SIcon from '../components/SIcon.vue'
import SModal from '../components/SModal.vue'
import { useSettingsStore } from '../stores/settings'
import { formatError, formatErrors, useUiStore, type ViewId } from '../stores/ui'
import { copyText } from '../api/desktop'

const ui = useUiStore()
const settings = useSettingsStore()

/** 刚复制成功的那条错误 id（短暂把按钮切成对勾） */
const copiedId = ref(0)

async function copyOne(e: { id: number; time: number; msg: string }) {
  if (await copyText(formatError(e))) {
    copiedId.value = e.id
    setTimeout(() => {
      if (copiedId.value === e.id) copiedId.value = 0
    }, 1500)
  } else {
    ui.error('复制失败，请手动选择错误文本')
  }
}

async function copyAll() {
  const text = formatErrors([...ui.errors].reverse())   // 与界面展示顺序一致：新的在上
  if (await copyText(text)) ui.toast(`已复制 ${ui.errors.length} 条错误`)
  else ui.error('复制失败，请手动选择错误文本')
}

const groups: Array<Array<{ id: ViewId; label: string }>> = [
  [
    { id: 'credentials', label: '凭证管理' },
    { id: 'history', label: '历史文章' },
    { id: 'external', label: '其他来源' },
    { id: 'weekly', label: '周报生成' },
  ],
  [
    { id: 'ai', label: 'AI 模型' },
    { id: 'logs', label: '执行日志' },
    { id: 'settings', label: '设置' },
  ],
]

async function checkUpdate() {
  const r = await settings.checkUpdate()
  if (r === 'modal') ui.updateOpen = true
  else if (r === 'latest') ui.toast('已是最新版本')
}

function go(v: ViewId) {
  ui.go(v)
}

function fmtTime(ts: number) {
  const d = new Date(ts)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}
</script>

<template>
  <aside class="sidebar">
    <div class="brand" style="display:flex;align-items:center;gap:8px">
      <img src="/icon.png" alt="MP Harvest" style="width:22px;height:22px;border-radius:6px;flex-shrink:0" />
      <span>MP Harvest</span>
    </div>
    <nav v-for="(g, gi) in groups" :key="gi" class="nav-group">
      <div
        v-for="item in g"
        :key="item.id"
        class="nav-item"
        :class="{ active: ui.view === item.id }"
        tabindex="0"
        @click="go(item.id)"
        @keydown.enter.prevent="go(item.id)"
      >
        {{ item.label }}
      </div>
    </nav>
    <div class="nav-spacer"></div>
    <nav class="nav-group">
      <div class="nav-item" tabindex="0" @click="ui.errorCenterOpen = true" @keydown.enter.prevent="ui.errorCenterOpen = true">
        错误中心
        <span v-if="ui.errors.length" class="badge err-badge">{{ ui.errors.length > 99 ? '99+' : ui.errors.length }}</span>
      </div>
      <div class="nav-item" tabindex="0" @click="checkUpdate" @keydown.enter.prevent="checkUpdate">
        <span v-if="settings.updateChecking" class="spinner" style="margin-right:6px"></span>检查更新
      </div>
    </nav>
    <div class="version">{{ settings.platform?.version || APP_VERSION }}</div>
  </aside>

  <!-- 错误中心：最近错误（时间 + 消息），可清空 -->
  <SModal :open="ui.errorCenterOpen" @close="ui.errorCenterOpen = false">
    <template #head>错误中心（{{ ui.errors.length }}）</template>
    <div v-if="!ui.errors.length" class="muted" style="text-align:center;padding:var(--sp-3)">暂无记录的错误</div>
    <div v-else class="err-list">
      <div v-for="e in [...ui.errors].reverse()" :key="e.id" class="err-item">
        <span class="mono muted err-time">{{ fmtTime(e.time) }}</span>
        <span class="err-msg">{{ e.msg }}</span>
        <button class="err-copy" :class="{ ok: copiedId === e.id }"
                :title="copiedId === e.id ? '已复制' : '复制这条报错'" @click="copyOne(e)">
          <SIcon :name="copiedId === e.id ? 'check' : 'copy'" :size="13" />
        </button>
      </div>
    </div>
    <template #foot>
      <SButton variant="ghost" :disabled="!ui.errors.length" @click="ui.clearErrors()">清空</SButton>
      <SButton :disabled="!ui.errors.length" @click="copyAll()">复制全部</SButton>
      <SButton variant="primary" @click="ui.errorCenterOpen = false">关闭</SButton>
    </template>
  </SModal>
</template>

<style scoped>
.err-badge {
  margin-left: auto;
  background: var(--danger);
  color: #fff;
  font-family: var(--font-mono);
}
.err-list {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
  max-height: 320px;
  overflow-y: auto;
}
.err-item {
  display: flex;
  gap: var(--sp-2);
  align-items: flex-start;
  font-size: var(--fs-sm);
}
.err-time {
  flex-shrink: 0;
  font-size: var(--fs-xs);
  padding-top: 1px;
}
.err-msg {
  flex: 1;
  min-width: 0;          /* 不加则 flex 子项不肯收缩，长 URL 会把复制按钮挤出面板 */
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--text-primary);
}
/* 复制按钮：平时淡出，悬停该行才显形 —— 列表以读为主，不抢视线 */
.err-copy {
  flex-shrink: 0;
  border: none;
  background: none;
  color: var(--text-tertiary);
  cursor: pointer;
  padding: 2px;
  opacity: 0;
  transition: opacity var(--dur-fast) var(--ease);
}
.err-item:hover .err-copy,
.err-copy:focus-visible {
  opacity: 1;
}
/* 复制成功：对勾常显并转绿，不用再靠 title 才知道点到了 */
.err-copy.ok {
  opacity: 1;
  color: var(--success);
}
.err-copy:hover {
  color: var(--text-primary);
}
</style>
