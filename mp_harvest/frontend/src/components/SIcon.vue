<script setup lang="ts">
// 线性图标（stroke 风格，24×24 viewBox，随文字颜色）。
//
// 为什么内联 SVG 而不引图标库（2026-09）：桌面端离线运行，内联 SVG 零依赖、
// 无字体/网络加载、可按 currentColor 自适应主题；图标集很小（11 个），
// 引入 lucide 之类的整包反而更重。
//
// 用法：<SIcon name="refresh" />，尺寸默认 14（跟 --fs-sm 文字齐平）。
// 纯装饰图标一律 aria-hidden；需要语义时由外层按钮的文案承担。
import { computed } from 'vue'

export type IconName =
  | 'refresh' | 'download' | 'sparkles'
  | 'eye' | 'eye-off'
  | 'chevron-down' | 'chevron-right'
  | 'check' | 'x' | 'info' | 'dot' | 'inbox'

const props = withDefaults(
  defineProps<{ name: IconName; size?: number | string; stroke?: number }>(),
  { size: 14, stroke: 1.8 },
)

/** 每个图标是一段 path 片段（Lucide/Feather 风格的 24 格线性图形） */
const PATHS: Record<IconName, string> = {
  // 凭证续约：环形箭头
  refresh:
    '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
  // 拉取历史：向下取回
  download:
    '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 19h16"/>',
  // AI 筛选：四角星
  sparkles:
    '<path d="M12 3.5 13.9 9.1a2 2 0 0 0 1.2 1.2l5.6 1.9-5.6 1.9a2 2 0 0 0-1.2 1.2L12 20.9l-1.9-5.6a2 2 0 0 0-1.2-1.2L3.3 12.2l5.6-1.9a2 2 0 0 0 1.2-1.2z"/>',
  eye:
    '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12"/><circle cx="12" cy="12" r="3"/>',
  'eye-off':
    '<path d="M10.7 6.2A8.6 8.6 0 0 1 12 6c6 0 9.5 6 9.5 6a15.5 15.5 0 0 1-3 3.7"/><path d="M6.6 6.9A15.6 15.6 0 0 0 2.5 12S6 18 12 18a8.7 8.7 0 0 0 3.5-.7"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/><path d="m3 3 18 18"/>',
  'chevron-down': '<path d="m6 9 6 6 6-6"/>',
  'chevron-right': '<path d="m9 18 6-6-6-6"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  x: '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
  info:
    '<circle cx="12" cy="12" r="9"/><path d="M12 11v5"/><path d="M12 8h.01"/>',
  // 实心小圆点：状态指示（用 fill，与线框图标区分）
  dot: '<circle cx="12" cy="12" r="5" fill="currentColor" stroke="none"/>',
  inbox:
    '<path d="M22 12h-6l-2 3h-4l-2-3H2"/><path d="M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.8 4H7.2a2 2 0 0 0-1.7 1.1z"/>',
}

const svg = computed(() => PATHS[props.name] ?? PATHS.info)
</script>

<template>
  <svg
    class="s-icon"
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    :stroke-width="stroke"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
    v-html="svg"
  />
</template>

<style scoped>
.s-icon {
  display: inline-block;
  vertical-align: -0.125em; /* 与文字基线对齐 */
  flex-shrink: 0;
}
</style>
