<script setup lang="ts">
// 页面五：设置（GET/PUT /api/settings 扁平 KV）——导出目录、图片下载、AI 默认值
import { onMounted } from 'vue'
import SButton from '../components/SButton.vue'
import SInput from '../components/SInput.vue'
import SSwitch from '../components/SSwitch.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import { useSettingsStore } from '../stores/settings'
import { useUiStore } from '../stores/ui'

const settings = useSettingsStore()
const ui = useUiStore()

onMounted(() => {
  if (!settings.loaded) settings.load()
})

/** 文本框失焦 / 开关切换 / 数字变更时保存；PUT 为整体覆盖，store 内合并全量 KV */
function save() {
  settings.savePrefs({})
}

async function chooseDir() {
  // 桌面壳（pywebview）暴露原生目录选择时优先使用；否则退化为手输。
  // 壳侧 JsApi.choose_directory 由 shell/main.py 通过 js_api 注册。
  const w = window as unknown as { pywebview?: { api?: { choose_directory?: () => Promise<string> } } }
  if (w.pywebview?.api?.choose_directory) {
    try {
      const dir = await w.pywebview.api.choose_directory()
      if (dir) {
        settings.prefs.exportDefaultDir = dir
        save()
      }
      // 用户取消（返回空串）不是错误，静默即可
    } catch (e) {
      ui.error(`目录选择失败：${e instanceof Error ? e.message : String(e)}（可手动输入路径）`)
    }
  } else {
    // 浏览器/开发模式下确实没有原生选择器 —— 用错误样式，别让用户以为已成功
    ui.error('当前环境不支持原生目录选择，请手动输入路径')
  }
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
        <div class="muted" style="font-size:var(--fs-xs);margin-top:var(--sp-2)">
          数据目录：<span class="mono">{{ settings.platform?.data_dir || '（未知）' }}</span>
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
    </template>
  </div>
  </section>
</template>
