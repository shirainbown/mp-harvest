<script setup lang="ts">
// 开关（模型启用）
defineProps<{ modelValue: boolean; title?: string }>()
const emit = defineEmits<{ 'update:modelValue': [v: boolean] }>()
</script>

<template>
  <span class="switch" :class="{ on: modelValue }" :title="title" @click="emit('update:modelValue', !modelValue)"></span>
</template>
