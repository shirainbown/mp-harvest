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
import { MOCK } from '../config'

// 模型自动保存防抖计时器 / 装载抑制标志（模块级，同 tasks.ts pendingTimers）
let saveTimer: ReturnType<typeof setTimeout> | null = null
let suppressModelAutosave = false

export const useSettingsStore = defineStore('settings', {
  state: () => ({
    models: [] as AiModel[],
    principles: '',
    defaultPrinciples: '',
    /** 最近一次落盘的原则文本：与 principles 比对得出「有没有未保存的改动」 */
    savedPrinciples: '',
    contentPrinciples: '',
    defaultContentPrinciples: '',
    savedContentPrinciples: '',
    // 默认「跟随系统代理」：国内用户开着 Clash 时，检查更新/下载开箱即用
    network: { mode: 'system', proxy_url: '' } as NetworkSettings,
    /** 后端探测到的系统代理（只读展示，帮助排查「跟随了但没代理」） */
    systemProxy: '' as string,
    platform: null as PlatformInfo | null,
    testResults: {} as Record<string, ModelTestResult | 'testing'>,
    modelLists: {} as Record<string, string[]>,
    modelFetching: {} as Record<string, boolean>,
    modelErrors: {} as Record<string, string>,
    proxyTesting: false,
    loaded: false,
    // 应用设置（GET/PUT /api/settings，扁平 KV；PUT 为整体覆盖，故保存时总是合并全量）
    prefs: {
      exportDefaultDir: '',
      exportDownloadImages: true,
      aiBatchSize: 50,
      aiWorkers: 4,
      aiContinueContentFilter: true,
    },
    prefsLoaded: false,
    prefsError: '',
    rawSettings: {} as Record<string, unknown>,
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
        call(
          rest.get<{
            settings: Partial<NetworkSettings> & { proxy?: string } & Record<string, unknown>
            /** 后端探测到的系统代理（只读，帮助排查「跟随了但连不上」） */
            system_proxy?: string
          }>('/api/settings'),
        ),
        call(rest.get<PlatformInfo>('/api/platform')),
      ])
      // 装载期间抑制模型自动保存（deep watch 会随赋值触发）
      suppressModelAutosave = true
      try {
        if (models) this.models = models.models
      } finally {
        suppressModelAutosave = false
      }
      if (principles) {
        this.principles = principles.text
        this.savedPrinciples = principles.text
        this.defaultPrinciples = principles.default ?? principles.text
      }
      if (contentPrinciples) {
        this.contentPrinciples = contentPrinciples.text
        this.savedContentPrinciples = contentPrinciples.text
        this.defaultContentPrinciples = contentPrinciples.default ?? contentPrinciples.text
      }
      if (network) {
        const s = network.settings || {}
        this.network = {
          mode: s.mode === 'custom' ? 'custom' : s.mode === 'direct' ? 'direct' : 'system',
          proxy_url: s.proxy ?? s.proxy_url ?? '',
        }
        this.systemProxy = String(network.system_proxy || '')
        this._applyRawSettings(s)
      } else if (!MOCK) {
        this.prefsError = '设置加载失败：无法连接后端，以下为默认值'
      }
      if (platform) this.platform = platform
      this.loaded = true
    },
    /** 解析 /api/settings 的扁平 KV（缺失键用默认值） */
    _applyRawSettings(s: Record<string, unknown>) {
      this.rawSettings = { ...s }
      this.prefs.exportDefaultDir = String(s['export.default_dir'] ?? '')
      this.prefs.exportDownloadImages = s['export.download_images'] !== false
      this.prefs.aiBatchSize = Number(s['ai.batch_size']) || 50
      this.prefs.aiWorkers = Number(s['ai.workers']) || 4
      this.prefs.aiContinueContentFilter = s['ai.continue_content_filter'] !== false
      this.prefsLoaded = true
    },
    /** 保存应用设置：合并全量 KV 后整体 PUT（后端 save 为覆盖式） */
    async savePrefs(patch: Partial<typeof this.prefs>): Promise<boolean> {
      Object.assign(this.prefs, patch)
      const merged = this._mergedSettings()
      const r = await call(rest.put('/api/settings', merged))
      if (r !== null) {
        this.rawSettings = merged
        this.prefsError = ''
        return true
      }
      this.prefsError = '设置保存失败，请重试'
      return false
    },
    _mergedSettings(): Record<string, unknown> {
      return {
        ...this.rawSettings,
        'export.default_dir': this.prefs.exportDefaultDir,
        'export.download_images': this.prefs.exportDownloadImages,
        'ai.batch_size': this.prefs.aiBatchSize,
        'ai.workers': this.prefs.aiWorkers,
        'ai.continue_content_filter': this.prefs.aiContinueContentFilter,
      }
    },
    async saveModels(silent = false) {
      // 服务端契约为裸数组（API.md §7.1）
      const r = await call(rest.put('/api/ai/models', this.models))
      if (r !== null && !silent) useUiStore().toast('模型配置已保存')
      return r !== null
    },
    /** 模型卡片任意字段变更 → 防抖自动保存全部（800ms） */
    scheduleSaveModels() {
      if (!this.loaded || suppressModelAutosave) return
      if (saveTimer) clearTimeout(saveTimer)
      saveTimer = setTimeout(() => {
        saveTimer = null
        void this.saveModels()
      }, 800)
    },
    async addModel() {
      this.models.push({
        id: `m${Date.now()}`,
        enabled: true,
        base_url: '',
        api_key: '',
        format: 'openai',
        model: '',
      })
      // 立即持久化，避免点其他卡片保存时把空白卡片状态搞混
      await this.saveModels(true)
      useUiStore().toast('已添加模型并保存，请填写配置')
    },
    async removeModel(id: string) {
      this.models = this.models.filter((m) => m.id !== id)
      await this.saveModels(true)  // 已有「已删除模型」toast，避免双弹
      useUiStore().toast('已删除模型')
    },
    async testModel(m: AiModel) {
      this.testResults[m.id] = 'testing'
      const r = await call(rest.post<ModelTestResult>('/api/ai/models/test', { ...m }))
      this.testResults[m.id] = r
        ? {
            ok: r.ok,
            latency_ms: r.latency_ms,
            error: r.error || r.message || (r.ok ? undefined : '测试失败'),
          }
        : { ok: false, error: '请求失败' }
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
      const text = this.principles
      const r = await call(rest.put('/api/ai/principles', { text }))
      if (r !== null) {
        this.savedPrinciples = text
        useUiStore().toast('筛选原则已保存（ai_principles.txt）')
      }
    },
    /** 恢复默认并立即落盘 */
    async restorePrinciples() {
      this.principles = this.defaultPrinciples
      const r = await call(rest.put('/api/ai/principles', { text: this.principles }))
      if (r !== null) {
        this.savedPrinciples = this.principles
        useUiStore().toast('已恢复默认原则并保存')
      }
    },
    async saveContentPrinciples() {
      const text = this.contentPrinciples
      const r = await call(rest.put('/api/ai/content-principles', { text }))
      if (r !== null) {
        this.savedContentPrinciples = text
        useUiStore().toast('内容筛选原则已保存（ai_content_principles.txt）')
      }
    },
    /** 恢复默认并立即落盘 */
    async restoreContentPrinciples() {
      this.contentPrinciples = this.defaultContentPrinciples
      const r = await call(rest.put('/api/ai/content-principles', { text: this.contentPrinciples }))
      if (r !== null) {
        this.savedContentPrinciples = this.contentPrinciples
        useUiStore().toast('已恢复默认内容原则并保存')
      }
    },
    async saveNetwork() {
      // 服务端 settings 存储用 proxy 字段（更新下载走 settings.proxy）；
      // save 为覆盖式，必须带上已有 KV，避免抹掉导出/AI 设置
      this.rawSettings = { ...this.rawSettings, mode: this.network.mode, proxy: this.network.proxy_url }
      await call(rest.put('/api/settings', this._mergedSettings()))
    },
    async testProxy() {
      this.proxyTesting = true
      const r = await call(
        rest.post<{ ok: boolean; latency_ms?: number; message?: string }>(
          '/api/settings/test-proxy',
          { proxy: this.network.proxy_url },
        ),
      )
      this.proxyTesting = false
      if (!r) return
      // 失败时把后端给的原因带上（形如「代理不可达 127.0.0.1:7890：Connection refused」），
      // 原先只显示「连接失败」，最有用的那截被丢了（2026-09）
      if (r.ok) useUiStore().toast(`连接成功 · 延迟 ${r.latency_ms ?? '?'}ms`)
      else useUiStore().error(`代理连接失败：${r.message || '未知原因'}`)
    },
    // ---- 检查更新（§5.8） ----
    async checkUpdate(): Promise<'modal' | 'latest' | 'fail'> {
      this.updateChecking = true
      const r = await call(rest.get<UpdateCheckResult>('/api/update/check'))
      this.updateChecking = false
      if (!r) return 'fail'
      if (r.ok === false) {
        // 报错要尽可能详细：message 是人话，error 是技术细节（HTTP 状态 / 异常原文）。
        // 之前只显示 message，把 error 里最有排查价值的部分丢了（2026-09）。
        const msg = r.message || '检查更新失败'
        const detail = r.error && !msg.includes(r.error) ? `\n${r.error}` : ''
        useUiStore().error(msg + detail)
        return 'fail'
      }
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
    async cancelUpdateDownload() {
      if (!this.updateTaskId) return
      const taskId = this.updateTaskId
      this.updateTaskId = ''
      this.updateProgress = 0
      await useTasksStore().cancel(taskId)
      useUiStore().toast('已停止下载')
    },
    async applyUpdate() {
      const r = await call(rest.post('/api/update/apply'))
      if (r !== null) useUiStore().toast('即将退出并自动替换重启')
    },
  },
})
