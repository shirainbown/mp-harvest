import { defineStore } from 'pinia'
import type {
  AiModel,
  ModelFetchResult,
  ModelTestResult,
  NetworkSettings,
  PlatformInfo,
  UpdateCheckResult,
} from '../types'
import { call, rest } from '../api/rest'
import { useTasksStore } from './tasks'
import { useUiStore } from './ui'

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    models: [] as AiModel[],
    principles: '',
    defaultPrinciples: '',
    contentPrinciples: '',
    defaultContentPrinciples: '',
    network: { mode: 'direct', proxy_url: '' } as NetworkSettings,
    platform: null as PlatformInfo | null,
    testResults: {} as Record<string, ModelTestResult | 'testing'>,
    modelLists: {} as Record<string, string[]>,
    modelFetching: {} as Record<string, boolean>,
    modelErrors: {} as Record<string, string>,
    proxyTesting: false,
    loaded: false,
    // 更新
    updateChecking: false,
    update: null as UpdateCheckResult | null,
    updateTaskId: '',
    updateProgress: 0,
    updateReady: false,
  }),
  actions: {
    async load() {
      const [models, principles, contentPrinciples, network, platform] = await Promise.all([
        call(rest.get<{ models: AiModel[] }>('/api/ai/models')),
        call(rest.get<{ text: string; default: string }>('/api/ai/principles')),
        call(rest.get<{ text: string; default: string }>('/api/ai/content-principles')),
        call(rest.get<{ settings: Partial<NetworkSettings> & { proxy?: string } }>('/api/settings')),
        call(rest.get<PlatformInfo>('/api/platform')),
      ])
      if (models) this.models = models.models
      if (principles) {
        this.principles = principles.text
        this.defaultPrinciples = principles.default ?? principles.text
      }
      if (contentPrinciples) {
        this.contentPrinciples = contentPrinciples.text
        this.defaultContentPrinciples = contentPrinciples.default ?? contentPrinciples.text
      }
      if (network) {
        const s = network.settings || {}
        this.network = {
          mode: s.mode === 'custom' ? 'custom' : 'direct',
          proxy_url: s.proxy ?? s.proxy_url ?? '',
        }
      }
      if (platform) this.platform = platform
      this.loaded = true
    },
    async saveModels(silent = false) {
      // 服务端契约为裸数组（API.md §7.1）
      const r = await call(rest.put('/api/ai/models', this.models))
      if (r !== null && !silent) useUiStore().toast('已保存模型配置')
    },
    addModel() {
      this.models.push({
        id: `m${Date.now()}`,
        enabled: true,
        base_url: '',
        api_key: '',
        format: 'openai',
        model: '',
      })
      useUiStore().toast('已添加空白模型卡片，请填写配置')
    },
    async removeModel(id: string) {
      this.models = this.models.filter((m) => m.id !== id)
      await this.saveModels(true)  // 已有「已删除模型」toast，避免双弹
      useUiStore().toast('已删除模型')
    },
    async testModel(m: AiModel) {
      this.testResults[m.id] = 'testing'
      const r = await call(rest.post<ModelTestResult>('/api/ai/models/test', { ...m }))
      this.testResults[m.id] = r ?? { ok: false, error: '请求失败' }
    },
    /** 按 base_url + api_key 拉取可用模型列表（OpenAI 兼容 /models） */
    async fetchModels(m: AiModel) {
      this.modelFetching[m.id] = true
      this.modelErrors[m.id] = ''
      const r = await call(
        rest.post<ModelFetchResult>('/api/ai/models/fetch', {
          base_url: m.base_url,
          api_key: m.api_key,
          format: m.format,
        }),
      )
      this.modelFetching[m.id] = false
      if (r && r.ok) {
        this.modelLists[m.id] = r.models ?? []
        if (this.modelLists[m.id].length) {
          useUiStore().toast(`已获取 ${this.modelLists[m.id].length} 个模型，请选择`)
        }
      } else {
        this.modelLists[m.id] = []
        this.modelErrors[m.id] = r?.message || '获取模型列表失败'
      }
    },
    async savePrinciples() {
      const r = await call(rest.put('/api/ai/principles', { text: this.principles }))
      if (r !== null) useUiStore().toast('筛选原则已保存（ai_principles.txt）')
    },
    restorePrinciples() {
      this.principles = this.defaultPrinciples
      useUiStore().toast('已恢复默认原则')
    },
    async saveContentPrinciples() {
      const r = await call(rest.put('/api/ai/content-principles', { text: this.contentPrinciples }))
      if (r !== null) useUiStore().toast('内容筛选原则已保存（ai_content_principles.txt）')
    },
    restoreContentPrinciples() {
      this.contentPrinciples = this.defaultContentPrinciples
      useUiStore().toast('已恢复默认内容原则')
    },
    async saveNetwork() {
      // 服务端 settings 存储用 proxy 字段（更新下载走 settings.proxy）
      await call(rest.put('/api/settings', { mode: this.network.mode, proxy: this.network.proxy_url }))
    },
    async testProxy() {
      this.proxyTesting = true
      const r = await call(
        rest.post<{ ok: boolean; latency_ms?: number }>('/api/settings/test-proxy', {
          proxy: this.network.proxy_url,
        }),
      )
      this.proxyTesting = false
      if (r) useUiStore().toast(r.ok ? `连接成功 · 延迟 ${r.latency_ms ?? '?'}ms` : '连接失败', r.ok)
    },
    // ---- 检查更新（§5.8） ----
    async checkUpdate(): Promise<'modal' | 'latest' | 'fail'> {
      this.updateChecking = true
      const r = await call(rest.get<UpdateCheckResult>('/api/update/check'))
      this.updateChecking = false
      if (!r || r.ok === false) return 'fail'
      this.update = r
      return r.available ? 'modal' : 'latest'
    },
    async downloadUpdate() {
      const r = await call(
        rest.post<{ task_id: string }>('/api/update/download', { zip_url: this.update?.zip_url }),
      )
      if (!r) return
      this.updateTaskId = r.task_id
      this.updateProgress = 0
      const ui = useUiStore()
      useTasksStore().track(r.task_id, 'update', {
        onProgress: (t) => {
          this.updateProgress = t.percent
        },
        onDone: () => {
          this.updateTaskId = ''
          this.updateProgress = 100
          this.updateReady = true
          ui.toast('下载完成，重启后生效')
        },
        onError: () => {
          this.updateTaskId = ''
        },
      })
    },
    async applyUpdate() {
      const r = await call(rest.post('/api/update/apply'))
      if (r !== null) useUiStore().toast('即将退出并自动替换重启')
    },
  },
})
