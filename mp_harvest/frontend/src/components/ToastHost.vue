<script setup lang="ts">
// Toast：右上角滑入，成功 2s / 错误 4s，最多叠 3 条（§5.9）
import { useUiStore } from '../stores/ui'
const ui = useUiStore()
</script>

<template>
  <Teleport to="body">
    <div class="toasts">
      <div v-for="t in ui.toasts" :key="t.id" class="toast" :class="{ out: t.out }">
        <span class="dot" :class="t.ok ? 'green' : 'red'"></span>
        <span>{{ t.msg }}</span>
      </div>
    </div>
  </Teleport>
</template>
