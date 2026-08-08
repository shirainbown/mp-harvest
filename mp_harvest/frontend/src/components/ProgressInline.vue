<script setup lang="ts">
// ProgressInline：工具条内联文字进度（不打断工具条布局，§5.9）
defineProps<{ text: string; cancellable?: boolean }>()
const emit = defineEmits<{ cancel: [] }>()
</script>

<template>
  <span class="task-inline">
    <span class="spinner"></span>{{ text }}
    <button v-if="cancellable" class="btn btn-ghost btn-sm" @click="emit('cancel')">取消</button>
  </span>
</template>
