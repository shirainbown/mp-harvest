<script setup lang="ts">
// SegmentedControl：时间范围 / 接口格式 / 视图切换共用（§5.9）
defineProps<{
  modelValue: string
  options: Array<{ value: string; label: string }>
}>()
const emit = defineEmits<{ 'update:modelValue': [v: string] }>()

function pick(v: string) {
  emit('update:modelValue', v)
}
</script>

<template>
  <div class="seg">
    <span
      v-for="o in options"
      :key="o.value"
      class="seg-item"
      :class="{ active: o.value === modelValue }"
      tabindex="0"
      @click="pick(o.value)"
      @keydown.enter.prevent="pick(o.value)"
      @keydown.space.prevent="pick(o.value)"
      >{{ o.label }}</span
    >
  </div>
</template>
