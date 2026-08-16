// 与设计稿 §7 API 契约对应的 TS 类型

export interface Account {
  id: string
  name: string
  url: string
  __biz?: string
  /** epoch 秒；无有效凭证为 null */
  expires_at: number | null
  /** 等待抓包中（添加/续约后） */
  pending?: boolean
}

export type ArticleSource = 'M' | 'G' | '补'
export type ArticleView = 'all' | 'keep' | 'drop'

export interface Article {
  id: string
  account_id: string
  /** 跨账号聚合视图（全部公众号）时用于显示/按名称排序（2026-08-09） */
  account_name?: string
  title: string
  url: string
  /** ISO 日期或 epoch 秒，渲染为 MM-DD */
  date: string
  source: ArticleSource
  /** AI 判定：keep / drop / null（未判定） */
  verdict: 'keep' | 'drop' | null
  /** AI 理由 */
  reason: string
}

export interface TaskInfo {
  task_id: string
  kind: string
  percent: number
  message: string
  status: 'running' | 'done' | 'error' | 'cancelled'
  result?: unknown
  error?: string
}

export interface AiModel {
  id: string
  name?: string
  enabled: boolean
  base_url: string
  api_key: string
  format: 'openai' | 'anthropic'
  model: string
}

export interface ModelTestResult {
  ok: boolean
  latency_ms?: number
  error?: string
}

export interface ModelFetchResult {
  ok: boolean
  models: string[]
  message?: string
}

export interface PlatformInfo {
  os: 'mac' | 'win' | string
  os_version?: string
  ca_needs_admin: boolean
  proxy_needs_admin: boolean
  data_dir?: string
  engine?: string
  version?: string
}

export interface NetworkSettings {
  mode: 'direct' | 'custom'
  proxy_url: string
}

export interface MitmStatus {
  running: boolean
  port: number
}

export interface CaStatus {
  trusted: boolean
}

export interface UpdateCheckResult {
  ok: boolean
  available: boolean
  version?: string
  notes?: string // markdown
  current_version?: string
  zip_url?: string
  error?: string | null
}

// ---- WS 事件（§7.2） ----
export type WsEvent =
  | { type: 'task.progress'; task_id: string; percent: number; message: string }
  | { type: 'task.done'; task_id: string; result?: unknown }
  | { type: 'task.error'; task_id: string; error: string }
  | {
      type: 'ai.batch'
      account_id: string
      articles: Array<{ id: string; verdict: 'keep' | 'drop' | null; reason: string }>
      /** 'title' = 标题筛选批次；'content' = 内容筛选批次（2026-08-16 新增） */
      stage?: 'title' | 'content'
    }
  | { type: 'credential.captured'; account_id: string; expires_at: number }
  | { type: 'credential.expired'; account_id: string }
  | { type: 'accounts.changed'; account_id: string }
  | { type: 'mitm.status'; running: boolean; port: number }
  | { type: 'clipboard.credential'; name: string; url: string }

// ---- 批量导入 ----
export interface ImportItem {
  name: string
  url: string
  dup: boolean
}
