<script setup lang="ts">
// Button：primary/secondary/ghost/danger 四变体；loading 内置 spinner 禁用（§5.9）
withDefaults(
  defineProps<{
    variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
    size?: 'md' | 'sm'
    loading?: boolean
    disabled?: boolean
  }>(),
  { variant: 'secondary', size: 'md', loading: false, disabled: false },
)
const cls: Record<string, string> = {
  primary: 'btn-primary',
  secondary: 'btn-secondary',
  ghost: 'btn-ghost',
  danger: 'btn-danger-ghost',
}
</script>

<template>
  <button class="btn" :class="[cls[variant], size === 'sm' && 'btn-sm']" :disabled="disabled || loading">
    <span v-if="loading" class="spinner" :style="variant === 'primary' ? 'border-top-color:#fff' : ''"></span>
    <slot />
  </button>
</template>
