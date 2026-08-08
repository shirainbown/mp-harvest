<script setup lang="ts">
// EmptyState：空列表指引（§5.9）
defineProps<{ text: string }>()
</script>

<template>
  <div class="empty-hint">
    <div style="font-size:24px;margin-bottom:8px;opacity:.5">☰</div>
    {{ text }}
  </div>
</template>
