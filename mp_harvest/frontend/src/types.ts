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
  /** 添加时 MITM 代理 best-effort 启动失败的原因（成功时无此字段） */
  mitm_message?: string
}

// 外 = 其他来源（用户登记的外部目录，2026-09）
export type ArticleSource = 'M' | 'G' | '补' | '外'
export type ArticleView = 'all' | 'keep' | 'drop' | 'pending'
export type AiStage = 'final' | 'title' | 'content'

export interface Article {
  id: string
  account_id: string
  /** 跨账号聚合视图（全部公众号）时用于显示/按名称排序（2026-08-09） */
  account_name?: string
  title: string
  url: string
  /** ISO 日期或 epoch 秒，渲染为 MM-DD */
  date: string
  /** 最近一次被抓取到的时间（ISO）；旧缓存可能为空（2026-08-23） */
  fetched_at?: string
  source: ArticleSource
  /** 最终 AI 判定：keep / drop / null（未判定） */
  verdict: 'keep' | 'drop' | null
  /** 最终 AI 理由 */
  reason: string
  /** 标题筛选判定（第一阶段） */
  title_verdict: 'keep' | 'drop' | null
  title_reason: string
  /** 内容筛选判定（第二阶段；未做内容筛选为 null） */
  content_verdict: 'keep' | 'drop' | null
  content_reason: string
  /** 本地**还留着**这篇导出的 HTML（2026-09）。后端每次请求都按文件是否存在
   *  重新判定 —— 把导出文件删掉再刷新，这个标记就会消失。 */
  exported: boolean
}

// ---- 其他来源（外部目录，2026-09）----

/** 登记的一个外部来源目录（其下按 YYYY-MM-DD 分日期子目录） */
export interface ExternalSource {
  id: string
  name: string
  path: string
  /** SQLite 的 0/1 */
  enabled: number
  added_at: number
  last_scan_at: number
  /** 上次扫描：扫到多少条 / 其中新增多少条 */
  last_scan_seen: number
  last_scan_new: number
  last_scan_error: string
  item_count: number
}

/** 外部来源条目：与 Article 同形，另带 arXiv 元数据 */
export interface ExternalItem extends Article {
  arxiv_id: string
  domain: string
  primary_category: string
  authors: string[]
  categories: string[]
  /** 所在的日期子目录（YYYY-MM-DD） */
  dir_date: string
  /** 本地 PDF / 正文文件（可能为空） */
  pdf_path: string
  body_path: string
  item_key: string
}

// ---- 周报（2026-09）----

export interface WeeklyPreview {
  from_date: string
  to_date: string
  suggested_issue: number
  out_dir: string
  selected_count: number
  total: number
  wechat: number
  arxiv: number
  external_other: number
  /** 各账号/各来源目录在**当前区间**内的候选数（口径与 total 一致，去重后统计） */
  account_counts: Record<string, number>
  source_counts: Record<string, number>
  template_path: string
  template_is_custom: boolean
  template_exists: boolean
}

/** 候选**逐篇**明细（2026-09）。两块判定来自不同阶段：
 *  `verdict`/`verdict_reason` = AI 筛选（该不该进候选池）；
 *  `score`/`reason` = 周报打分（排多少名、算不算半导体）。
 *  打分只可能来自**缓存** —— 生成前本来就不存在这次的分数，没打过的 `scored=false`。 */
export interface WeeklyCandidate {
  key: string
  title: string
  title_cn: string
  source: string
  source_id: string
  kind: string
  date: string
  publish_ts: number
  url: string
  /** AI 筛选：true 留 / false 删 / null 未判定 */
  verdict: boolean | null
  verdict_reason: string
  /** 是否已有**当前提示词指纹**下的打分 */
  scored: boolean
  score: number | null
  semiconductor: boolean | null
  domain: string
  business_tags: string[]
  reason: string
}

export interface WeeklyCandidates {
  from_date: string
  to_date: string
  only_kept: boolean
  total: number
  scored: number
  items: WeeklyCandidate[]
}

export interface WeeklyIssue {
  issue_num: number
  date: string
  dir: string
  name: string
  report: string
  has_snapshot: boolean
}

/** 一段可编辑提示词：当前文本 + 内置默认（供「恢复默认」） */
export interface WeeklyPrompt {
  text: string
  default: string
  label: string
}

/** 生成/重渲染任务的结果 */
export interface WeeklyResult {
  ok?: boolean
  error?: string
  issue_dir?: string
  report_path?: string
  total?: number
  selected?: number
  others?: number
  dropped?: number
  archived?: number
  failed?: number
  errors?: string[]
  /** 模板引用了但数据模型没提供的变量名（拼写错误在这里暴露） */
  missing_vars?: string[]
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
  message?: string
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
  /** direct=不走代理；system=跟随系统代理（macOS 读 scutil）；custom=用下面填的地址 */
  mode: 'direct' | 'system' | 'custom'
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
  message?: string
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
      articles: Array<{
        id: string
        verdict: 'keep' | 'drop' | null
        reason: string
        title_verdict?: 'keep' | 'drop' | null
        title_reason?: string
        content_verdict?: 'keep' | 'drop' | null
        content_reason?: string
      }>
      /** 'title' = 标题筛选批次；'content' = 内容筛选批次 */
      stage?: 'title' | 'content'
    }
  | { type: 'credential.captured'; account_id: string; expires_at: number }
  | { type: 'credential.expired'; account_id: string }
  | { type: 'accounts.changed'; account_id: string }
  | { type: 'mitm.status'; running: boolean; port: number }

// ---- 批量导入 ----
export interface ImportItem {
  name: string
  url: string
  dup: boolean
}

// ---- 执行日志（2026-09）----
export interface LogEvent {
  id: number
  /** epoch 秒 */
  ts: number
  level: 'debug' | 'info' | 'warn' | 'error'
  /** 埋点类型：action / task.start / task.done / task.error / ai.call / ai.reply / ai.verdict / ai.error */
  kind: string
  message: string
  /** 结构化上下文（后端已脱敏；可能含模型原始返回，长文本） */
  data: Record<string, unknown>
}

export interface LogKindCount {
  kind: string
  count: number
}
