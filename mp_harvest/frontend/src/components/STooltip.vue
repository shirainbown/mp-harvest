<script setup lang="ts">
// Tooltip：400ms 延迟（§5.9），用于标题全文、错误摘要
import { onBeforeUnmount, ref } from 'vue'

defineProps<{ text: string }>()

const show = ref(false)
const style = ref('')
let timer: ReturnType<typeof setTimeout> | null = null

function enter(e: MouseEvent) {
  const r = (e.currentTarget as HTMLElement).getBoundingClientRect()
  timer = setTimeout(() => {
    const left = Math.min(r.left, window.innerWidth - 330)
    style.value = `left:${left}px;top:${r.bottom + 6}px`
    show.value = true
  }, 400)
}
function leave() {
  if (timer) clearTimeout(timer)
  timer = null
  show.value = false
}
onBeforeUnmount(leave)
</script>

<template>
  <span style="display:inline-flex;min-width:0" @mouseenter="enter" @mouseleave="leave">
    <slot />
    <Teleport to="body">
      <div v-if="show" class="tooltip" :style="style">{{ text }}</div>
    </Teleport>
  </span>
</template>
