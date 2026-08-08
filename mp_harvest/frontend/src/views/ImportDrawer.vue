<script setup lang="ts">
// 批量导入 Drawer（§5.4）：textarea 粘贴 + txt/csv/json 文件 + 解析预览（去重标记）+ 导入 N 条
import { computed, ref, watch } from 'vue'
import SDrawer from '../components/SDrawer.vue'
import SButton from '../components/SButton.vue'
import EmptyState from '../components/EmptyState.vue'
import { useAccountsStore } from '../stores/accounts'
import { useUiStore } from '../stores/ui'
import type { ImportItem } from '../types'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ 'update:open': [v: boolean] }>()

const accounts = useAccountsStore()
const ui = useUiStore()

const text = ref('')
const items = ref<ImportItem[]>([])
const parsing = ref(false)
const importing = ref(false)
let debounce: ReturnType<typeof setTimeout> | null = null

const newCount = computed(() => items.value.filter((i) => !i.dup).length)

// 300ms 防抖解析预览（§5.10.6）
watch(text, (v) => {
  if (debounce) clearTimeout(debounce)
  debounce = setTimeout(() => preview(v), 300)
})

async function preview(t: string) {
  if (!t.trim()) {
    items.value = []
    return
  }
  parsing.value = true
  items.value = await accounts.importPreview(t)
  parsing.value = false
}

/** 文件导入：txt/csv 按行解析、json 取 [{name,url}] */
async function pickFile(e: Event) {
  const input = e.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  const ext = file.name.split('.').pop()?.toLowerCase()
  if (!['txt', 'csv', 'json'].includes(ext || '')) {
    ui.error('仅支持 .txt / .csv / .json 文件')
    return
  }
  const content = await file.text()
  if (ext === 'json') {
    try {
      const arr = JSON.parse(content) as Array<{ name?: string; url?: string }>
      text.value = arr.map((x) => `${x.name || ''} ${x.url || ''}`.trim()).join('\n')
    } catch {
      ui.error('JSON 文件解析失败')
    }
  } else {
    text.value = content
  }
}

async function doImport() {
  importing.value = true
  const r = await accounts.importConfirm(items.value)
  importing.value = false
  if (r) close()
}

function close() {
  emit('update:open', false)
  text.value = ''
  items.value = []
}

function short(u: string) {
  return u.replace(/^https?:\/\//, '').slice(0, 26) + '…'
}
void props
</script>

<template>
  <SDrawer :open="open" title="批量导入公众号" @close="close">
    <div>
      <div class="panel-title">粘贴「名称 + 链接」（每行一条，空格/逗号分隔）</div>
      <textarea
        v-model="text"
        class="principles"
        style="min-height:120px;font-family:var(--font-ui)"
        placeholder="科技新知  https://mp.weixin.qq.com/s/a1B2c3&#10;智东西    https://mp.weixin.qq.com/s/d4E5f6"
      ></textarea>
    </div>
    <div>
      <div class="panel-title">或从文件导入</div>
      <div class="toolbar">
        <label>
          <input type="file" accept=".txt,.csv,.json" style="display:none" @change="pickFile" />
          <span class="btn btn-secondary btn-sm">选择文件…</span>
        </label>
        <span class="tertiary" style="font-size:var(--fs-xs)">支持 .txt / .csv / .json</span>
      </div>
    </div>
    <div>
      <div class="panel-title">
        解析预览（URL + 名称双重去重）<span v-if="parsing" class="spinner" style="margin-left:6px"></span>
      </div>
      <div class="acct-table">
        <div class="acct-head" style="grid-template-columns:1fr 1.4fr 90px"><span>名称</span><span>链接</span><span>结果</span></div>
        <EmptyState v-if="!items.length" text="粘贴或选择文件后自动解析" />
        <div v-for="(it, i) in items" :key="i" class="acct-row" style="grid-template-columns:1fr 1.4fr 90px">
          <span class="acct-name">{{ it.name }}</span>
          <span class="acct-biz mono">{{ short(it.url) }}</span>
          <span v-if="it.dup" style="color:var(--warning);font-size:var(--fs-sm)">重复</span>
          <span v-else class="status-ok">新增</span>
        </div>
      </div>
    </div>
    <template #foot>
      <SButton variant="ghost" @click="close">取消</SButton>
      <SButton variant="primary" :disabled="!newCount" :loading="importing" @click="doImport">导入 {{ newCount }} 条</SButton>
    </template>
  </SDrawer>
</template>
