<script setup lang="ts">
// 页面四：网络设置（§5.7）
import { computed, onMounted } from 'vue'
import SButton from '../components/SButton.vue'
import SInput from '../components/SInput.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import { useSettingsStore } from '../stores/settings'

const settings = useSettingsStore()

onMounted(() => {
  if (!settings.loaded) settings.load()
})

function setMode(mode: 'direct' | 'custom') {
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
</script>

<template>
  <section class="view-root">
  <header class="page-header">
    <h1>网络设置</h1>
  </header>
  <div class="page-body">
    <div class="panel">
      <div class="panel-title">更新与下载代理</div>
      <div class="radio-row" @click="setMode('direct')">
        <span class="radio" :class="{ on: settings.network.mode === 'direct' }"></span>直连 / 系统代理
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
      <div class="panel-title">平台能力 <span class="tertiary" style="font-weight:400">（由 GET /api/platform 渲染，随系统变化）</span></div>
      <SkeletonRows v-if="!settings.platform" :rows="3" />
      <div v-else class="cap-list">
        <span v-for="(l, i) in platformLines" :key="i" :class="{ mono: l.startsWith('数据目录') }">{{ l }}</span>
      </div>
    </div>
  </div>
  </section>
</template>
