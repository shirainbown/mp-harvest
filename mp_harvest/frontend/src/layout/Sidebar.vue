<script setup lang="ts">
// 侧边栏 200px：三组导航（主功能/配置/系统）+ 版本号（§5.3）
import { APP_VERSION } from '../config'
import { useSettingsStore } from '../stores/settings'
import { useUiStore, type ViewId } from '../stores/ui'

const ui = useUiStore()
const settings = useSettingsStore()

const groups: Array<Array<{ id: ViewId; label: string }>> = [
  [
    { id: 'credentials', label: '凭证管理' },
    { id: 'history', label: '历史文章' },
  ],
  [
    { id: 'ai', label: 'AI 模型' },
    { id: 'network', label: '网络设置' },
  ],
]

async function checkUpdate() {
  const r = await settings.checkUpdate()
  if (r === 'modal') ui.updateOpen = true
  else if (r === 'latest') ui.toast('✓ 已是最新版本')
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
        @click="ui.go(item.id)"
      >
        {{ item.label }}
      </div>
    </nav>
    <div class="nav-spacer"></div>
    <nav class="nav-group">
      <div class="nav-item" @click="checkUpdate">
        <span v-if="settings.updateChecking" class="spinner" style="margin-right:6px"></span>检查更新
      </div>
    </nav>
    <div class="version">{{ settings.platform?.version || APP_VERSION }}</div>
  </aside>
</template>
