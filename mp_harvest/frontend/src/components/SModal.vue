<script setup lang="ts">
// Modal：--shadow-modal、遮罩点击关、esc 关（§5.9）
import { onBeforeUnmount, onMounted } from 'vue'

const props = withDefaults(defineProps<{ open: boolean; closable?: boolean }>(), { closable: true })
const emit = defineEmits<{ close: [] }>()

function onKey(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.open && props.closable) emit('close')
}
onMounted(() => document.addEventListener('keydown', onKey))
onBeforeUnmount(() => document.removeEventListener('keydown', onKey))
</script>

<template>
  <Teleport to="body">
    <div class="overlay" :class="{ show: open }" @click="closable && emit('close')"></div>
    <div class="modal" :class="{ show: open }" role="dialog">
      <div v-if="$slots.head" class="modal-head"><slot name="head" /></div>
      <div class="modal-body"><slot /></div>
      <div v-if="$slots.foot" class="modal-foot"><slot name="foot" /></div>
    </div>
  </Teleport>
</template>
