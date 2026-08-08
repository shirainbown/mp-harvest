<script setup lang="ts">
// 检查更新 Modal（§5.8）：markdown-it 渲染更新说明 + 下载进度条 + 重启以应用
import { computed } from 'vue'
import MarkdownIt from 'markdown-it'
import SModal from '../components/SModal.vue'
import SButton from '../components/SButton.vue'
import { useSettingsStore } from '../stores/settings'
import { useUiStore } from '../stores/ui'

const ui = useUiStore()
const settings = useSettingsStore()
const md = new MarkdownIt({ html: false, linkify: true })

const notesHtml = computed(() => (settings.update?.notes ? md.render(settings.update.notes) : ''))
const downloading = computed(() => !!settings.updateTaskId)

function later() {
  ui.updateOpen = false
}
async function update() {
  await settings.downloadUpdate()
}
async function restart() {
  await settings.applyUpdate()
  ui.updateOpen = false
}
</script>

<template>
  <SModal :open="ui.updateOpen" @close="later">
    <template #head>
      发现新版本 <span class="badge m" style="margin-left:4px">{{ settings.update?.version }}</span>
    </template>

    <div v-if="notesHtml" v-html="notesHtml"></div>
    <template v-if="downloading || settings.updateReady">
      <div class="progress-track">
        <div class="progress-fill" :style="`width:${settings.updateProgress}%`"></div>
      </div>
      <span class="muted" style="font-size:var(--fs-sm)">
        <template v-if="downloading">正在下载 mp_harvest-{{ settings.update?.version }}.zip … {{ settings.updateProgress }}%（走所选代理）</template>
        <template v-else>下载完成，重启应用后生效</template>
      </span>
    </template>

    <template #foot>
      <SButton variant="ghost" @click="later">稍后</SButton>
      <SButton v-if="settings.updateReady" variant="primary" @click="restart">重启以应用</SButton>
      <SButton v-else variant="primary" :loading="downloading" @click="update">立即更新</SButton>
    </template>
  </SModal>
</template>
