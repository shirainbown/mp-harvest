<script setup lang="ts">
// Input：13px、focus 强调色描边、错误态红描边 + 下方红字（§5.9）
withDefaults(
  defineProps<{
    modelValue: string
    placeholder?: string
    type?: string
    mono?: boolean
    error?: string
    disabled?: boolean
    width?: string
    /** 关联 <datalist> 的 id：可输入也可从建议中选择 */
    list?: string
  }>(),
  { type: 'text', mono: false, error: '', disabled: false, placeholder: '', width: '', list: '' },
)
const emit = defineEmits<{
  'update:modelValue': [v: string]
  enter: []
  blur: []
}>()
</script>

<template>
  <span style="display:inline-flex;flex-direction:column;gap:2px">
    <input
      class="input"
      :class="{ mono, error: !!error }"
      :style="width ? `width:${width}` : ''"
      :type="type"
      :value="modelValue"
      :placeholder="placeholder"
      :disabled="disabled"
      :list="list"
      @input="emit('update:modelValue', ($event.target as HTMLInputElement).value)"
      @keydown.enter="emit('enter')"
      @blur="emit('blur')"
    />
    <span v-if="error" style="color:var(--danger);font-size:var(--fs-xs)">{{ error }}</span>
  </span>
</template>
