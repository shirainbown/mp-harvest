<script setup lang="ts">
// Drawer：右侧 480px 滑出、遮罩点击关、esc 关（§5.9）
import { onBeforeUnmount, onMounted } from 'vue'

const props = withDefaults(defineProps<{ open: boolean; title: string }>(), {})
const emit = defineEmits<{ close: [] }>()

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.open) emit('close')
}
onMounted(() => document.addEventListener('keydown', onKey))
onBeforeUnmount(() => document.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <div class="overlay" :class="{ show: open }" @click="emit('close')"></div>
    <div class="drawer" :class="{ show: open }">
      <div class="drawer-head">
        {{ title }}
        <button class="btn btn-ghost btn-sm" @click="emit('close')">✕</button>
      </div>
      <div class="drawer-body"><slot /></div>
      <div v-if="$slots.foot" class="drawer-foot"><slot name="foot" /></div>
    </div>
  </Teleport>
</template>
