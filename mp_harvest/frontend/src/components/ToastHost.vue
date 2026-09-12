<script setup lang="ts">
// Toast：右上角滑入，成功 2.5s / 错误 8s 自动消失（也可手动 ✕），最多叠 5 条（§5.9）。
// 错误不再常驻：完整记录在「错误中心」，堆叠条数多时提供一键关闭（2026-09）。
// 错误条带「复制」：报错当下就能揣走全文（2026-09）。
import { ref } from 'vue'
import { useUiStore } from '../stores/ui'
import { copyText } from '../api/desktop'
import SIcon from './SIcon.vue'
const ui = useUiStore()

/** 刚复制成功的那条 toast id（短暂把「复制」换成「已复制」） */
const copiedId = ref(0)

async function copyToast(t: { id: number; msg: string }) {
  if (await copyText(t.msg)) {
    copiedId.value = t.id
    setTimeout(() => {
      if (copiedId.value === t.id) copiedId.value = 0
    }, 1500)
  } else {
    // copyText 已经做了 WKWebView 兜底；仍失败就明确说，别假装成功
    ui.error('复制失败，请手动选择错误文本')
  }
}
</script>

<template>
  <Teleport to="body">
    <div class="toasts">
      <button v-if="ui.toasts.length > 1" class="toast-clear" @click="ui.dismissAllToasts()">
        全部关闭（{{ ui.toasts.length }}）
      </button>
      <div v-for="t in ui.toasts" :key="t.id" class="toast" :class="{ out: t.out, sticky: t.sticky }">
        <span class="dot" :class="t.ok ? 'green' : 'red'"></span>
        <span class="toast-msg">{{ t.msg }}</span>
        <template v-if="t.sticky">
          <button class="toast-link" title="复制这条报错" @click="copyToast(t)">
            {{ copiedId === t.id ? '已复制' : '复制' }}
          </button>
          <button v-if="ui.errors.length" class="toast-link" @click="ui.dismissToast(t.id); ui.errorCenterOpen = true">查看</button>
          <button class="toast-close" title="关闭" @click="ui.dismissToast(t.id)"><SIcon name="x" :size="12" /></button>
        </template>
      </div>
    </div>
  </Teleport>
</template>
