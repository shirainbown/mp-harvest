<script setup lang="ts">
// SegmentedControl：时间范围 / 接口格式 / 视图切换共用（§5.9）
defineProps<{
  modelValue: string
  options: Array<{ value: string; label: string }>
}>()
const emit = defineEmits<{ 'update:modelValue': [v: string] }>()
</script>

<template>
  <div class="seg">
    <span
      v-for="o in options"
      :key="o.value"
      class="seg-item"
      :class="{ active: o.value === modelValue }"
      @click="emit('update:modelValue', o.value)"
      >{{ o.label }}</span
    >
  </div>
</template>
