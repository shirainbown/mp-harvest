<script setup lang="ts">
// Skeleton：列表加载骨架行，不用全屏 spinner（§5.9）
withDefaults(defineProps<{ rows?: number }>(), { rows: 6 })
</script>

<template>
  <div>
    <div v-for="i in rows" :key="i" class="skel-row">
      <span class="skel-block" :style="`width:${((i * 37) % 40) + 35}%`"></span>
      <span class="skel-block" style="width:18%"></span>
      <span class="skel-block" style="width:10%"></span>
    </div>
  </div>
</template>
