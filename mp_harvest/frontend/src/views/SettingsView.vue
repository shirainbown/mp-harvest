<script setup lang="ts">
// 页面：设置（GET/PUT /api/settings 扁平 KV）——导出目录、图片下载、AI 默认值、
// 网络代理、平台能力。2026-09 把原「网络设置」整页并入本页。
import { computed, onMounted } from 'vue'
import SButton from '../components/SButton.vue'
import SInput from '../components/SInput.vue'
import SSwitch from '../components/SSwitch.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import { useSettingsStore } from '../stores/settings'
import { useUiStore } from '../stores/ui'
import { chooseDirectory } from '../api/desktop'

const settings = useSettingsStore()
const ui = useUiStore()

onMounted(() => {
  if (!settings.loaded) settings.load()
})

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
