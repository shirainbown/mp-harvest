<script setup lang="ts">
// Popover：点击触发，外部点击/esc 关闭；用于删除二次确认、抓包指引 ⓘ（§5.9）
import { onBeforeUnmount, onMounted, ref } from 'vue'

const open = ref(false)
const anchor = ref<HTMLElement>()
const style = ref('')

function toggle(e: MouseEvent) {
  e.stopPropagation()
  open.value = !open.value
  if (open.value && anchor.value) {
    const r = anchor.value.getBoundingClientRect()
    const left = Math.min(r.left, window.innerWidth - 300)
    style.value = `left:${left}px;top:${r.bottom + 6}px`
  }
}
function close() {
  open.value = false
}
function onDoc() {
  close()
}
function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape') close()
}
onMounted(() => {
  document.addEventListener('click', onDoc)
  document.addEventListener('keydown', onKey)
})
onBeforeUnmount(() => {
  document.removeEventListener('click', onDoc)
  document.removeEventListener('keydown', onKey)
})
defineExpose({ close })
</script>

<template>
  <span ref="anchor" style="display:inline-flex" @click="toggle">
    <slot name="anchor" />
  </span>
  <Teleport to="body">
    <div v-if="open" class="popover" :style="style" @click.stop>
      <slot :close="close" />
    </div>
  </Teleport>
</template>
