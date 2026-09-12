import { defineStore } from 'pinia'
import type { AiStage, Article, ArticleView } from '../types'
import { call, rest, LONG_TIMEOUT } from '../api/rest'
import { copyText } from '../api/desktop'
import { useAccountsStore } from './accounts'
import { useTasksStore } from './tasks'
import { useUiStore } from './ui'
// weekly 不 import articles（单向），所以这里不会成环
import { useWeeklyStore } from './weekly'

export const LIST_FORMATS = [
  { value: 'md', label: 'Markdown' },
  { value: 'json', label: 'JSON' },
  { value: 'csv', label: 'CSV' },
  { value: 'tsv', label: 'TSV' },
  { value: 'links', label: '纯链接' },
  { value: 'title+links', label: '标题+链接' },
] as const

export const useArticlesStore = defineStore('articles', {
  state: () => ({
    accountId: '',
    rangeDays: 7,
    /** 拉取范围模式：days = 近 N 天；custom = 自定义日期范围（2026-08-23） */
    rangeMode: 'days' as 'days' | 'custom',
    customStart: '' as string, // YYYY-MM-DD
    customEnd: '' as string,
    /** 缓存文章时间筛选：all / latest（最近一次拉取）/ custom（发布日期范围） */
    timeFilter: 'all' as 'all' | 'latest' | 'custom',
    filterStart: '' as string,
    filterEnd: '' as string,
    list: [] as Article[],
    view: 'all' as ArticleView,
    aiStage: 'final' as AiStage,
    sortBy: 'time' as 'time' | 'name',
    sortDir: 'desc' as 'desc' | 'asc',
    selected: new Set<string>() as Set<string>,
    loading: false,
    /** 拉取请求在途（POST 已发出、task_id 未返回）；防双击重复建任务 */
    fetchPending: false,
    fetchTaskId: '',
    batchTaskId: '',
    exportTaskId: '',
    aiTaskId: '',
    aiProgress: '',
    listFormat: 'md' as string,
  }),
  getters: {
    /** 当前阶段的统计标签（all/keep/drop/pending） */
    counts(): Record<ArticleView, number> {
      const finalBase = this.list
      const titleBase = this.list
      const contentBase = this.list.filter((a) => a.title_verdict === 'keep')
      if (this.aiStage === 'title') {
        return {
          all: titleBase.length,
          keep: titleBase.filter((a) => a.title_verdict === 'keep').length,
          drop: titleBase.filter((a) => a.title_verdict === 'drop').length,
          pending: titleBase.filter((a) => a.title_verdict == null).length,
        }
      }
      if (this.aiStage === 'content') {
        return {
          all: contentBase.length,
          keep: contentBase.filter((a) => a.content_verdict === 'keep').length,
          drop: contentBase.filter((a) => a.content_verdict === 'drop').length,
          pending: contentBase.filter((a) => a.content_verdict == null).length,
        }
      }
      return {
        all: finalBase.length,
        keep: finalBase.filter((a) => a.verdict === 'keep').length,
        drop: finalBase.filter((a) => a.verdict === 'drop').length,
        pending: finalBase.filter((a) => a.verdict == null).length,
      }
    },
    /** 当前阶段的视图标签定义 */
    stageTabs(): Array<{ v: ArticleView; label: string }> {
      if (this.aiStage === 'title') {
        return [
          { v: 'all', label: '全部' },
          { v: 'keep', label: '标题通过' },
          { v: 'drop', label: '标题过滤' },
        ]
      }
      if (this.aiStage === 'content') {
        return [
          { v: 'all', label: '标题通过' },
          { v: 'keep', label: '内容通过' },
          { v: 'drop', label: '内容过滤' },
          { v: 'pending', label: '待内容筛选' },
        ]
      }
      return [
        { v: 'all', label: '全部' },
        { v: 'keep', label: '通过' },
        { v: 'drop', label: '过滤掉' },
      ]
    },
    /** 当前阶段某行展示的判定字段 */
    verdictOf(): (a: Article) => 'keep' | 'drop' | null {
      if (this.aiStage === 'title') return (a) => a.title_verdict
      if (this.aiStage === 'content') return (a) => a.content_verdict
      return (a) => a.verdict
    },
    visible(): Article[] {
      const verdictOf = this.verdictOf
      let rows: Article[]
      if (this.aiStage === 'content') {
        const base = this.list.filter((a) => a.title_verdict === 'keep')
        rows = this.view === 'all' ? [...base] : base.filter((a) => verdictOf(a) === this.view)
        if (this.view === 'pending') rows = base.filter((a) => a.content_verdict == null)
      } else {
        rows = this.view === 'all' ? [...this.list] : this.list.filter((a) => verdictOf(a) === this.view)
      }
      rows.sort((a, b) => {
        if (this.sortBy === 'name') {
          const byName = (a.account_name || '').localeCompare(b.account_name || '', 'zh')
          if (byName) return byName * (this.sortDir === 'asc' ? 1 : -1)
          return Date.parse(b.date) - Date.parse(a.date) // 组内按时间新→旧
        }
        const byTime = Date.parse(b.date) - Date.parse(a.date)
        if (byTime) return byTime * (this.sortDir === 'desc' ? 1 : -1)
        return (a.account_name || '').localeCompare(b.account_name || '', 'zh')
      })
      return rows
    },
    selectedInView(): Article[] {
      return this.visible.filter((a) => this.selected.has(a.id))
    },
  },
  actions: {
    /** 时间筛选 → query 串字段（GET） */
    _timeFilterQuery(): Record<string, string> {
      if (this.timeFilter === 'latest') return { latest_fetch: 'true' }
      if (this.timeFilter === 'custom' && this.filterStart)
        return { start_date: this.filterStart, end_date: this.filterEnd }
      return {}
    },
    /** 时间筛选 → POST body 字段，保证看到的=筛选的=导出的 */
    _timeFilterBody(): Record<string, unknown> {
      if (this.timeFilter === 'latest') return { latest_fetch: true }
      if (this.timeFilter === 'custom' && this.filterStart)
        return { start_date: this.filterStart, end_date: this.filterEnd }
      return {}
    },
    /** 拉取请求体：custom 模式发 start_date/end_date，否则发 days */
    _fetchBody(): Record<string, unknown> {
      if (this.rangeMode === 'custom' && this.customStart)
        return { start_date: this.customStart, end_date: this.customEnd }
      return { days: this.rangeDays }
    },
    async load(accountId?: string) {
      if (accountId !== undefined) this.accountId = accountId
      this.loading = true
      try {
        const q = new URLSearchParams({
          account_id: this.accountId,
          view: 'all',
          order: 'desc',
          ...this._timeFilterQuery(),
        })
        const r = await call(rest.get<Article[]>(`/api/articles?${q}`))
        if (r) this.list = r
      } finally {
        this.loading = false
      }
    },
    /** 拉取历史 → Task + WS 进度（§5.5） */
    async fetchHistory() {
      // fetchPending 覆盖「POST 已发出但 task_id 还没回来」的空窗期：
      // 原先只靠 fetchTaskId 守卫，快速双击会创建两个任务，而只有后一个 id
      // 被记住 —— 前一个完成时把 fetchTaskId 清空，按钮在第二个任务仍在跑时
      // 就恢复可点（2026-09 修复）
      if (!this.accountId || this.fetchTaskId || this.fetchPending) return
      this.fetchPending = true
      let r: { task_id: string } | null = null
      try {
        r = await call(
          rest.post<{ task_id: string }>('/api/history/fetch', {
            account_id: this.accountId,
            ...this._fetchBody(),
          }, { timeout: LONG_TIMEOUT }),
        )
      } finally {
        this.fetchPending = false
      }
      if (!r) return
      this.fetchTaskId = r.task_id
      const ui = useUiStore()
      useTasksStore().track(r.task_id, 'history', {
        onDone: async (t) => {
          this.fetchTaskId = ''
          const res = (t.result || {}) as {
            ok?: boolean
            error?: string
            warning?: string
            truncated?: boolean
            notice?: string
            added?: number
            total?: number
            /** 被微信限流（后端 `rate_limited`）—— 提示语要劝阻重试，见下 */
            rate_limited?: boolean
          }
          // 被限流的应对与「凭证过期」**完全相反**：不是重试，是停手等着。
          // 所以这里不能只丢一句红色报错 —— 那会诱导用户马上再点一次，而
          // 每点一次封锁就更久（社区实测）。标题直接写「先别急着重试」，
          // 正文用后端那句能指导行动的话（等 24 小时 / 换号）。
          if (res.rate_limited) {
            ui.error(`被微信限流了 —— 请先别重复拉取。\n${res.error || ''}`.trim())
          }
          // 后端契约：拉取失败时 result 为 {ok:false, error, warning?}
          else if (res.ok === false) {
            ui.error(`拉取历史失败：${res.error || '未知错误'}${res.warning ? `\n${res.warning}` : ''}`)
          } else {
            ui.toast(
              `拉取完成：新增 ${res.added ?? 0} 篇，共 ${res.total ?? this.list.length} 篇` +
                (res.notice ? `（${res.notice}）` : ''),
            )
            // 只有 truncated 才是「可能没拉完」的问题；notice（如「已合并补录/抓包
            // N 篇」）是好消息，不能弹红 —— 两者原先共用 warning 字段，导致一次
            // 完全成功的拉取被渲染成「拉取未完整」（2026-09 修复）
            if (res.truncated) ui.error(`拉取未完整：${res.warning || '已达翻页上限'}`)
          }
          await this.load()
        },
        onError: (t) => {
          this.fetchTaskId = ''
          if (t.status === 'cancelled') ui.error('已取消拉取（保留已获取的文章）')
        },
      })
    },
    async cancelFetch() {
      if (this.fetchTaskId) await useTasksStore().cancel(this.fetchTaskId)
    },
    /** 批量拉取：勾选多个公众号 → 聚合任务逐个拉取（2026-08-09 新增） */
    async fetchBatch(accountIds: string[]) {
      if (!accountIds.length || this.batchTaskId) return
      const r = await call(
        rest.post<{ task_id: string }>('/api/history/fetch-batch', {
          account_ids: accountIds,
          ...this._fetchBody(),
        }, { timeout: LONG_TIMEOUT }),
      )
      if (!r) return
      this.batchTaskId = r.task_id
      const ui = useUiStore()
      useTasksStore().track(r.task_id, 'history', {
        onDone: async (t) => {
          this.batchTaskId = ''
          const res = (t.result || {}) as {
            ok?: number
            failed?: number
            total?: number
            results?: { name?: string; rate_limited?: boolean; error?: string }[]
          }
          ui.toast(`批量拉取完成：成功 ${res.ok ?? 0} / 失败 ${res.failed ?? 0}（共 ${res.total ?? accountIds.length} 个公众号）`)
          // 批量里被限流时，只说「失败 3」等于没说：用户会以为是个别账号的问题，
          // 换个账号再点一次 —— 而限流是**微信号级**的，换哪个都一样失败。
          const limited = (res.results || []).filter((x) => x.rate_limited)
          if (limited.length) {
            ui.error(
              `有 ${limited.length} 个公众号被微信限流了 —— 请先别重复拉取。\n` +
                `${limited[0].error || ''}`.trim(),
            )
          }
          await useAccountsStore().load() // 名称可能被官方昵称覆盖
          await this.load()
        },
        onError: () => {
          this.batchTaskId = ''
        },
      })
    },
    async cancelBatch() {
      if (this.batchTaskId) await useTasksStore().cancel(this.batchTaskId)
    },
    setSortBy(by: 'time' | 'name') {
      this.sortBy = by
      // 切维度时重置为该维度的默认方向：时间默认最新在前、名称默认 A→Z
      this.sortDir = by === 'time' ? 'desc' : 'asc'
    },
    toggleSortDir() {
      this.sortDir = this.sortDir === 'desc' ? 'asc' : 'desc'
    },
    /** 排序控件的**一次点击**：点已选中的段翻转方向，点另一段换维度。
     *
     * 放在 store 而不是视图里：这是「点了没反应」的高发区，逻辑得能单测
     * （`tests/test_sort_control.py` 用 node 驱动真实模块跑）。
     */
    pickSort(by: 'time' | 'name') {
      if (this.sortBy === by) this.toggleSortDir()
      else this.setSortBy(by)
    },
    setStage(stage: AiStage) {
      this.aiStage = stage
      this.view = 'all'
    },
    /** AI 标题筛选：batch_size/workers 可调；includeContent=true 时标题完成后继续内容筛选（2026-08-16）。 */
    /** AI 标题筛选。ids 非空 = 只筛这几篇（「只筛选中」）。 */
    async aiFilter(batchSize = 50, workers = 4, includeContent = false, ids: string[] = []) {
      if (this.aiTaskId) return
      const bs = Math.max(1, Math.min(200, Math.round(Number(batchSize) || 50)))
      const wk = Math.max(1, Math.min(16, Math.round(Number(workers) || 4)))
      const r = await call(
        rest.post<{ task_id: string }>('/api/ai/filter', {
          account_id: this.accountId,
          batch_size: bs,
          workers: wk,
          ids,
          ...this._timeFilterBody(),
        }),
      )
      if (!r) return
      this.aiTaskId = r.task_id
      const ui = useUiStore()
      useTasksStore().track(r.task_id, 'ai', {
        onProgress: (t) => {
          this.aiProgress = t.message
        },
        onDone: async (t) => {
          this.aiTaskId = ''
          this.aiProgress = ''
          // 后端契约：{ok, kept, dropped, cached, judged, errors[]}
          // 原先读的是 res.drop（后端叫 dropped）→ 提示恒为「过滤 ?」；
          // 且从不读 ok/errors → 模型全挂也显示成「通过 0 篇」的成功（2026-09 修复）
          const res = (t.result || {}) as {
            ok?: boolean
            kept?: number
            dropped?: number
            cached?: number
            errors?: string[]
          }
          const kept = res.kept ?? 0
          const dropped = res.dropped ?? 0
          // 用 setStage 而非直接赋值：它会一并复位 view，否则若当前停在
          // pending 等视图，切到 title 阶段后列表会空白（2026-09 修复）
          this.setStage('title')
          const failed = res.ok === false || (res.errors?.length ?? 0) > 0
          const head = `标题筛选：通过 ${kept} / 过滤 ${dropped}${res.cached ? `（缓存命中 ${res.cached}）` : ''}`
          if (failed) {
            ui.error(`${head}\n${(res.errors || ['模型调用失败，本轮未完成判定']).join('\n')}`)
          }
          if (includeContent) {
            if (kept > 0) {
              if (!failed) ui.toast(`${head}，继续内容筛选…`)
              await this.contentFilter(bs, wk)
            } else {
              if (!failed) ui.toast('标题筛选完成：通过 0 篇，跳过内容筛选')
              await this.load()
            }
          } else {
            if (!failed) ui.toast(head)
            await this.load()
          }
        },
        onError: () => {
          this.aiTaskId = ''
          this.aiProgress = ''
        },
      })
    },
    /** AI 内容筛选（第二阶段）：只对当前 keep=true 的文章拉正文并判定（2026-08-16）。 */
    /** 内容筛选（第二阶段）。ids 非空 = 只筛这几篇（「只筛选中」）。 */
    async contentFilter(batchSize = 30, workers = 4, ids: string[] = []) {
      if (this.aiTaskId) return
      const bs = Math.max(1, Math.min(200, Math.round(Number(batchSize) || 30)))
      const wk = Math.max(1, Math.min(16, Math.round(Number(workers) || 4)))
      const r = await call(
        rest.post<{ task_id: string }>('/api/ai/filter-content', {
          account_id: this.accountId,
          batch_size: bs,
          workers: wk,
          ids,
          ...this._timeFilterBody(),
        }),
      )
      if (!r) return
      this.aiTaskId = r.task_id
      const ui = useUiStore()
      useTasksStore().track(r.task_id, 'ai', {
        onProgress: (t) => {
          this.aiProgress = t.message
        },
        onDone: async (t) => {
          this.aiTaskId = ''
          this.aiProgress = ''
          this.setStage('content')
          const res = (t.result || {}) as {
            ok?: boolean
            kept?: number
            dropped?: number
            cached?: number
            fetch_failed?: number
            errors?: string[]
          }
          const parts = [`内容筛选：通过 ${res.kept ?? '?'} / 过滤 ${res.dropped ?? '?'}`]
          if (res.cached) parts.push(`缓存命中 ${res.cached}`)
          // 正文抓取失败的**不再计为丢弃**：它们留在「待内容筛选」，可重新运行
          if (res.fetch_failed) parts.push(`正文获取失败 ${res.fetch_failed}（保留待筛选，可重跑）`)
          const failed = res.ok === false || (res.errors?.length ?? 0) > 0
          if (failed) {
            useUiStore().error(`${parts.join(' · ')}\n${(res.errors || []).join('\n')}`)
          } else {
            ui.toast(parts.join(' · '))
          }
          await this.load()
        },
        onError: () => {
          this.aiTaskId = ''
          this.aiProgress = ''
        },
      })
    },
    async supplement(url: string) {
      const art = await call(rest.post<Article>('/api/articles/supplement', { account_id: this.accountId, url }))
      if (art) {
        this.list.unshift(art)
        useUiStore().toast('补录链接已加入列表')
      }
    },
    /** 列表导出 / 复制：始终只导出当前视图（§5.5） */
    async exportListText(): Promise<string | null> {
      const q = new URLSearchParams({
        account_id: this.accountId,
        view: this.view,
        format: this.listFormat,
        stage: this.aiStage,
        ...this._timeFilterQuery(),
      })
      return call(rest.get<string>(`/api/articles/export-list?${q}`, { timeout: LONG_TIMEOUT }))
    },
    async copyList() {
      const text = await this.exportListText()
      if (text === null) return
      const label = LIST_FORMATS.find((f) => f.value === this.listFormat)?.label
      // 看真实结果：剪贴板被拒时不能再弹「已复制」
      if (await copyText(text)) useUiStore().toast(`当前视图列表已复制（${label}）`)
      else useUiStore().error('复制失败，请改用「导出列表文件」')
    },
    async exportList() {
      const text = await this.exportListText()
      if (text === null) return
      // 浏览器环境无法直接写文件，下载为附件
      const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `articles-${this.view}.${this.listFormat === 'md' ? 'md' : this.listFormat === 'json' ? 'json' : 'txt'}`
      a.click()
      URL.revokeObjectURL(a.href)
      useUiStore().toast(`已导出当前视图 ${this.counts[this.view]} 条（仅导出当前视图）`)
    },
    /** 正文 HTML 导出（§6）：ids 为空 = 当前视图全部（调用方已确认）；
     *  outDir 指定目标目录，后端会在其中生成 index.html 说明页（2026-08-09） */
    /**
     * 从**本地列表**删掉这几篇（2026-09，用户要求「有些文章我认为可以删掉」）。
     *
     * 语义是用户选定的：**只动本地缓存** —— 文章还在微信那边，下次「拉取历史」
     * 会重新抓到。所以提示语里必须把这句话带上，否则用户下次看到它回来会当成 bug。
     */
    async remove(ids: string[]) {
      if (!ids.length) return 0
      const r = await call(
        rest.post<{ ok: boolean; removed: number }>('/api/articles/delete', {
          account_id: this.accountId,
          ids,
        }),
      )
      if (!r) return 0
      const gone = new Set(ids)
      this.list = this.list.filter((a) => !gone.has(a.id))
      // 选中集合也要清 —— 不清的话「已选 N」会指着已经不在列表里的 id
      for (const id of gone) this.selected.delete(id)
      this.refreshWeeklyCandidates()
      useUiStore().toast(
        `已从本地列表删除 ${r.removed} 篇（下次「拉取历史」会重新抓到）`,
      )
      return r.removed
    },

    /**
     * 文章列表变了 → 周报的候选数就过期了，主动重算。
     *
     * 为什么不靠「切到周报页时刷新」：视图是 `v-show` 常驻挂载的，那个刷新得挂在
     * watch 上、每个视图各写一遍，**漏了就静默过期** —— 2026-09 在「存储占用」和
     * 「周报候选」上各漏过一次，用户看到的都是「我明明删了，界面没变」。
     *
     * 放在动作里还有两个好处：周报页那层 watch 测不了（没有 DOM 测试装置），
     * 而这里**能**；而且用户不用先切过去再切回来才看到更新。
     *
     * `if (wk.preview)`：没加载过就别发这个请求 —— 用户还没打开过周报页时，
     * 没必要为了一个他看不见的数字去查一遍候选。
     */
    refreshWeeklyCandidates() {
      const wk = useWeeklyStore()
      if (wk.preview) void wk.loadPreview()
    },
    async exportHtml(ids: string[], outDir?: string) {
      // 必须带 account_id，否则后端拿不到文章列表（2026-08-09 修复）
      const body: Record<string, unknown> = {
        account_id: this.accountId,
        stage: this.aiStage,
        ...this._timeFilterBody(),
      }
      if (ids.length) body.ids = ids
      else body.view = this.view
      if (outDir && outDir.trim()) body.out_dir = outDir.trim()
      const r = await call(rest.post<{ task_id: string }>('/api/articles/export-html', body, { timeout: LONG_TIMEOUT }))
      if (!r) return
      this.exportTaskId = r.task_id
      const ui = useUiStore()
      ui.toast(
        `开始导出 ${ids.length || this.counts[this.view]} 篇正文 HTML${body.out_dir ? ` → ${body.out_dir}` : ''}（任务已创建，可看进度）`,
      )
      useTasksStore().track(r.task_id, 'export', {
        onDone: (t) => {
          // 后端契约：{ok, exported, skipped, failed, errors[], out_dir}
          const res = (t.result || {}) as {
            ok?: boolean
            exported?: number
            skipped?: number
            failed?: number
            errors?: string[]
            out_dir?: string
            dir?: string
            count?: number
          }
          this.exportTaskId = ''
          const failed = res.failed ?? 0
          const exported = res.exported ?? res.count ?? 0
          const skipped = res.skipped ?? 0
          // 「跳过」必须显示（2026-09 修复）：全部命中导出记录时 exported=0，
          // 原先的提示会变成「导出完成（0 篇）」，看起来像失败，而它恰恰是
          // 导出记录库在正常工作（同一批文章不重复下载）
          const skippedNote = skipped ? ` / 跳过 ${skipped} 篇（已导出过）` : ''
          if (res.ok === false || failed > 0) {
            const lines = [
              `正文导出：成功 ${exported}${skippedNote} / 失败 ${failed}`,
              ...(res.errors || []),
            ]
            useUiStore().error(lines.join('\n')) // 不自动消失，可滚动查看 errors 列表
          } else {
            ui.toast(
              `正文导出完成（${exported} 篇${skippedNote}）→ ${res.out_dir || res.dir || 'exports/'}（含 index.html 说明页）`,
            )
          }
        },
        onError: () => {
          this.exportTaskId = ''
        },
      })
    },
    async cancelExport() {
      const t = this.exportTaskId
      if (!t) return
      this.exportTaskId = ''
      await useTasksStore().cancel(t)
    },
    setView(v: ArticleView) {
      this.view = v
    },
    /** 每批 AI 判定完成即实时合并：更新对应阶段字段，并重算最终 verdict/reason */
    onAiBatch(
      account_id: string,
      articles: Array<{
        id: string
        verdict: Article['verdict']
        reason: string
        title_verdict?: Article['title_verdict']
        title_reason?: string
        content_verdict?: Article['content_verdict']
        content_reason?: string
      }>,
    ) {
      if (account_id !== this.accountId || !articles.length) return
      const byId = new Map(articles.map((a) => [a.id, a]))
      for (const row of this.list) {
        const p = byId.get(row.id)
        if (!p) continue
        // 用 `!= null` 而非 `!== undefined`：后端在非本阶段的字段上发的是
        // **null**（不是缺席），`null !== undefined` 成立 → 会把 title_verdict
        // 刷成 null，跑内容筛选时列表和计数集体塌陷（2026-09 修复）
        if (p.title_verdict != null) {
          row.title_verdict = p.title_verdict
          row.title_reason = p.title_reason || row.title_reason
        }
        if (p.content_verdict != null) {
          row.content_verdict = p.content_verdict
          row.content_reason = p.content_reason || row.content_reason
        }
        // 重算最终判定：内容优先，标题其次
        const contentVerdict = row.content_verdict
        const titleVerdict = row.title_verdict
        if (contentVerdict !== null && contentVerdict !== undefined) {
          row.verdict = contentVerdict
          row.reason = row.content_reason || ''
        } else if (titleVerdict !== null && titleVerdict !== undefined) {
          row.verdict = titleVerdict
          row.reason = row.title_reason || ''
        } else {
          row.verdict = p.verdict
          row.reason = p.reason
        }
      }
    },
    toggleSelect(id: string, on: boolean) {
      if (on) this.selected.add(id)
      else this.selected.delete(id)
    },
    selectAllVisible() {
      for (const a of this.visible) this.selected.add(a.id)
    },
    clearSelection() {
      this.selected.clear()
    },
  },
})
