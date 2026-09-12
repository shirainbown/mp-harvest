<script setup lang="ts">
// SortControl：把「排序维度」与「方向」合成**一个**控件（§6，2026-09）。
//
// 改之前是两个控件表达四个状态（下拉选维度 + 另一个按钮切方向），用户吐槽
// 「在时间排序时，完全可以合并为一个按钮」。这里的规则只有一条：
//
//   点**没选中**的段 = 换维度（用该维度的默认方向）
//   点**已选中**的段 = 翻转方向
//
// 方向只显示在**当前**维度上 —— 没选中的那段标个方向是骗人的。
// 键盘行为与 SegmentedControl 保持一致（Enter / Space）。
import { computed } from 'vue'

const props = defineProps<{
  by: 'time' | 'name'
  dir: 'asc' | 'desc'
}>()
const emit = defineEmits<{ pick: [by: 'time' | 'name'] }>()

/** 时间默认最新在前（新→旧），名称默认 A→Z —— 与两个 store 的 setSortBy 一致 */
function arrow(by: 'time' | 'name', dir: 'asc' | 'desc'): string {
  if (by === 'name') return dir === 'asc' ? 'A→Z' : 'Z→A'
  return dir === 'desc' ? '新→旧' : '旧→新'
}

const options = computed(() => [
  {
    by: 'time' as const,
    label: props.by === 'time' ? `时间 ${arrow('time', props.dir)}` : '时间',
    tip: props.by === 'time' ? '再点一下切换方向' : '按发布时间排序',
  },
  {
    by: 'name' as const,
    label: props.by === 'name' ? `名称 ${arrow('name', props.dir)}` : '名称',
    tip: props.by === 'name' ? '再点一下切换方向' : '按名称排序',
  },
])

function pick(by: 'time' | 'name') {
  emit('pick', by)
}
</script>

<template>
  <div class="seg">
    <span
      v-for="o in options"
      :key="o.by"
      class="seg-item"
      :class="{ active: o.by === by }"
      :title="o.tip"
      tabindex="0"
      @click="pick(o.by)"
      @keydown.enter.prevent="pick(o.by)"
      @keydown.space.prevent="pick(o.by)"
      >{{ o.label }}</span
    >
  </div>
</template>
