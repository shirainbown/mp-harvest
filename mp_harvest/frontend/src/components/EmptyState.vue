<script setup lang="ts">
// EmptyState：空列表指引（§5.9）
defineProps<{ text: string }>()
import SIcon from './SIcon.vue'
</script>

<template>
  <div class="empty-hint">
    <div style="margin-bottom:10px;opacity:.45;color:var(--text-tertiary)">
      <SIcon name="inbox" :size="26" />
    </div>
    {{ text }}
  </div>
</template>
