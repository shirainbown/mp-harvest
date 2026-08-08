<script setup lang="ts">
// Badge：来源徽章 M/G/补 与通用计数徽章（§5.9）
withDefaults(defineProps<{ variant?: 'default' | 'm' | 'g' | 'bu' }>(), { variant: 'default' })
</script>

<template>
  <span class="badge" :class="variant !== 'default' && variant"><slot /></span>
</template>
