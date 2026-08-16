<script setup lang="ts">
// 页面三：AI 模型（§5.6）
import { onMounted, ref } from 'vue'
import SButton from '../components/SButton.vue'
import SInput from '../components/SInput.vue'
import SPopover from '../components/SPopover.vue'
import SegmentedControl from '../components/SegmentedControl.vue'
import SSwitch from '../components/SSwitch.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import EmptyState from '../components/EmptyState.vue'
import { useSettingsStore } from '../stores/settings'
import { useUiStore } from '../stores/ui'
import type { AiModel } from '../types'

const settings = useSettingsStore()
const ui = useUiStore()

onMounted(() => {
  if (!settings.loaded) settings.load()
})

const formatOptions = [
  { value: 'openai', label: 'OpenAI 兼容' },
  { value: 'anthropic', label: 'Anthropic' },
]

// API Key 显隐
const visibleKeys = ref<Set<string>>(new Set())
function toggleKey(id: string) {
  if (visibleKeys.value.has(id)) visibleKeys.value.delete(id)
  else visibleKeys.value.add(id)
}

function testResultOf(m: AiModel) {
  return settings.testResults[m.id]
}

function testErrorOf(m: AiModel): string {
  const r = testResultOf(m)
  if (!r || r === 'testing') return ''
  const rr = r as { ok: boolean; error?: string; message?: string }
  return rr.error || rr.message || (rr.ok ? '' : '测试失败')
}

async function copyTestError(m: AiModel) {
  const text = testErrorOf(m)
  if (!text) return
  try {
    await navigator.clipboard.writeText(text)
    ui.toast('错误信息已复制')
  } catch {
    ui.error('复制失败，请手动选择错误文本')
  }
}

function fetchingOf(m: AiModel) {
  return !!settings.modelFetching[m.id]
}

function modelListOf(m: AiModel): string[] {
  return settings.modelLists[m.id] ?? []
}

function modelErrorOf(m: AiModel): string {
  return settings.modelErrors[m.id] ?? ''
}

function modelListId(m: AiModel): string {
  return `model-list-${m.id}`
}
</script>

<template>
  <section class="view-root">
  <header class="page-header">
    <h1>AI 模型</h1>
    <SButton size="sm" variant="primary" @click="settings.addModel()">+ 添加模型</SButton>
  </header>
  <div class="page-body">
    <SkeletonRows v-if="!settings.loaded" :rows="4" />
    <EmptyState v-else-if="!settings.models.length" text="先添加一个 AI 模型" />
    <template v-else>
      <div v-for="m in settings.models" :key="m.id" class="model-card">
        <div class="model-line">
          <SSwitch v-model="m.enabled" title="启用" />
          <span class="form-label">请求地址</span>
          <SInput v-model="m.base_url" mono placeholder="https://api.example.com" />
          <SButton size="sm" variant="primary" @click="settings.saveModels()">保存</SButton>
          <SButton size="sm" :loading="testResultOf(m) === 'testing'" @click="settings.testModel(m)">测试</SButton>
          <SPopover>
            <template #anchor><SButton size="sm" variant="danger">删除</SButton></template>
            <template #default="{ close }">
              <div style="margin-bottom:8px">确认删除该模型？</div>
              <div style="display:flex;justify-content:flex-end;gap:8px">
                <SButton size="sm" @click="close()">取消</SButton>
                <SButton size="sm" variant="danger" @click="close(); settings.removeModel(m.id)">删除</SButton>
              </div>
            </template>
          </SPopover>
        </div>
        <div class="model-line">
          <span class="form-label">API Key</span>
          <SInput
            v-model="m.api_key"
            mono
            :type="visibleKeys.has(m.id) ? 'text' : 'password'"
            placeholder="sk-…"
          />
          <SButton size="sm" variant="ghost" @click="toggleKey(m.id)">👁</SButton>
          <SegmentedControl v-model="m.format" :options="formatOptions" style="margin-left:8px" />
          <span class="form-label">模型</span>
          <SInput v-model="m.model" mono width="180px" placeholder="模型名或从列表选择" :list="modelListId(m)" />
          <datalist :id="modelListId(m)">
            <option v-for="name in modelListOf(m)" :key="name" :value="name" />
          </datalist>
          <SButton size="sm" variant="ghost" :loading="fetchingOf(m)" @click="settings.fetchModels(m)">获取列表</SButton>
          <span v-if="modelErrorOf(m)" class="status-fail">{{ modelErrorOf(m) }}</span>
          <template v-if="testResultOf(m) && testResultOf(m) !== 'testing'">
            <span v-if="(testResultOf(m) as any).ok" class="status-ok">● 可用 · {{ (testResultOf(m) as any).latency_ms }}ms</span>
            <span v-else class="status-fail model-test-fail">
              <span>● 失败 · </span>
              <code class="model-test-error">{{ testErrorOf(m) }}</code>
              <SButton size="sm" variant="ghost" @click="copyTestError(m)">复制</SButton>
            </span>
          </template>
        </div>
      </div>
    </template>

    <div class="panel">
      <div class="panel-title">
        筛选原则 <span class="tertiary" style="font-weight:400">— 输出格式由软件固定（严格 JSON），原则只管判定标准</span>
      </div>
      <textarea v-model="settings.principles" class="principles" spellcheck="false"></textarea>
      <div class="toolbar" style="margin-top:var(--sp-2)">
        <span class="spacer"></span>
        <SButton size="sm" variant="ghost" @click="settings.restorePrinciples()">恢复默认</SButton>
        <SButton size="sm" variant="primary" @click="settings.savePrinciples()">保存</SButton>
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">
        内容筛选原则 <span class="tertiary" style="font-weight:400">— 标题判定通过后，按正文做第二阶段筛选</span>
      </div>
      <textarea v-model="settings.contentPrinciples" class="principles" spellcheck="false"></textarea>
      <div class="toolbar" style="margin-top:var(--sp-2)">
        <span class="spacer"></span>
        <SButton size="sm" variant="ghost" @click="settings.restoreContentPrinciples()">恢复默认</SButton>
        <SButton size="sm" variant="primary" @click="settings.saveContentPrinciples()">保存</SButton>
      </div>
    </div>
  </div>
  </section>
</template>

<style scoped>
.model-test-fail {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
}

.model-test-error {
  user-select: all;
  cursor: text;
  word-break: break-all;
  white-space: normal;
  font-family: var(--font-mono);
  font-size: var(--fs-xs);
  color: var(--danger);
  background: var(--bg-hover);
  border-radius: var(--radius-sm);
  padding: 1px 4px;
}
</style>
