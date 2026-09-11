<script setup lang="ts">
// Toast：右上角滑入，成功 2.5s 自动消失、错误不自动消失（手动 ✕ 关闭），最多叠 5 条（§5.9）
import { useUiStore } from '../stores/ui'
const ui = useUiStore()
</script>

<template>
  <Teleport to="body">
    <div class="toasts">
      <div v-for="t in ui.toasts" :key="t.id" class="toast" :class="{ out: t.out, sticky: t.sticky }">
        <span class="dot" :class="t.ok ? 'green' : 'red'"></span>
        <span class="toast-msg">{{ t.msg }}</span>
        <template v-if="t.sticky">
          <button v-if="ui.errors.length" class="toast-link" @click="ui.dismissToast(t.id); ui.errorCenterOpen = true">查看</button>
          <button class="toast-close" title="关闭" @click="ui.dismissToast(t.id)">✕</button>
        </template>
      </div>
    </div>
  </Teleport>
</template>
