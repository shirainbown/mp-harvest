import { defineStore } from 'pinia'
import type { AiStage, Article, ArticleView } from '../types'
import { call, rest } from '../api/rest'
import { useAccountsStore } from './accounts'
import { useTasksStore } from './tasks'
import { useUiStore } from './ui'

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
    list: [] as Article[],
    view: 'all' as ArticleView,
    aiStage: 'final' as AiStage,
    sortBy: 'time' as 'time' | 'name',
    sortDir: 'desc' as 'desc' | 'asc',
    selected: new Set<string>() as Set<string>,
    loading: false,
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
    async load(accountId?: string) {
      if (accountId !== undefined) this.accountId = accountId
      this.loading = true
      const r = await call(
        rest.get<Article[]>(
          `/api/articles?account_id=${encodeURIComponent(this.accountId)}&view=all&order=desc`,
        ),
      )
      if (r) this.list = r
      this.loading = false
    },
    /** 拉取历史 → Task + WS 进度（§5.5） */
    async fetchHistory() {
      if (!this.accountId || this.fetchTaskId) return
      const r = await call(rest.post<{ task_id: string }>('/api/history/fetch', { account_id: this.accountId, days: this.rangeDays }))
      if (!r) return
      this.fetchTaskId = r.task_id
      const ui = useUiStore()
      useTasksStore().track(r.task_id, 'history', {
        onDone: async (t) => {
          this.fetchTaskId = ''
          const res = (t.result || {}) as { added?: number; total?: number }
          ui.toast(`拉取完成：新增 ${res.added ?? 0} 篇，共 ${res.total ?? this.list.length} 篇`)
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
          days: this.rangeDays,
        }),
      )
      if (!r) return
      this.batchTaskId = r.task_id
      const ui = useUiStore()
      useTasksStore().track(r.task_id, 'history', {
        onDone: async (t) => {
          this.batchTaskId = ''
          const res = (t.result || {}) as { ok?: number; failed?: number; total?: number }
          ui.toast(`批量拉取完成：成功 ${res.ok ?? 0} / 失败 ${res.failed ?? 0}（共 ${res.total ?? accountIds.length} 个公众号）`)
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
    setStage(stage: AiStage) {
      this.aiStage = stage
      this.view = 'all'
    },
    /** AI 标题筛选：batch_size/workers 可调；includeContent=true 时标题完成后继续内容筛选（2026-08-16）。 */
    async aiFilter(batchSize = 50, workers = 4, includeContent = false) {
      if (this.aiTaskId) return
      const bs = Math.max(1, Math.min(200, Math.round(Number(batchSize) || 50)))
      const wk = Math.max(1, Math.min(16, Math.round(Number(workers) || 4)))
      const r = await call(
        rest.post<{ task_id: string }>('/api/ai/filter', {
          account_id: this.accountId,
          batch_size: bs,
          workers: wk,
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
          const res = (t.result || {}) as { kept?: number; keep?: number; drop?: number; cached?: number }
          const kept = res.kept ?? res.keep ?? 0
          this.aiStage = 'title'
          if (includeContent) {
            if (kept > 0) {
              ui.toast(`标题筛选完成：通过 ${kept} / 过滤 ${res.drop ?? '?'}，继续内容筛选…`)
              await this.contentFilter(bs, wk)
            } else {
              ui.toast(`标题筛选完成：通过 0 篇，跳过内容筛选`)
              await this.load()
            }
          } else {
            ui.toast(`标题筛选完成：通过 ${kept} / 过滤 ${res.drop ?? '?'}${res.cached ? `（缓存命中 ${res.cached}）` : ''}`)
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
    async contentFilter(batchSize = 30, workers = 4) {
      if (this.aiTaskId) return
      const bs = Math.max(1, Math.min(200, Math.round(Number(batchSize) || 30)))
      const wk = Math.max(1, Math.min(16, Math.round(Number(workers) || 4)))
      const r = await call(
        rest.post<{ task_id: string }>('/api/ai/filter-content', {
          account_id: this.accountId,
          batch_size: bs,
          workers: wk,
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
          this.aiStage = 'content'
          const res = (t.result || {}) as {
            kept?: number
            dropped?: number
            cached?: number
            fetch_failed?: number
          }
          const parts = [`内容筛选完成：通过 ${res.kept ?? '?'} / 过滤 ${res.dropped ?? '?'}`]
          if (res.cached) parts.push(`缓存命中 ${res.cached}`)
          if (res.fetch_failed) parts.push(`正文获取失败 ${res.fetch_failed}（已过滤）`)
          ui.toast(parts.join(' · '))
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
      return call(
        rest.get<string>(
          `/api/articles/export-list?account_id=${encodeURIComponent(this.accountId)}&view=${this.view}&format=${encodeURIComponent(this.listFormat)}&stage=${this.aiStage}`,
        ),
      )
    },
    async copyList() {
      const text = await this.exportListText()
      if (text !== null) {
        await navigator.clipboard.writeText(text)
        useUiStore().toast(`当前视图列表已复制（${LIST_FORMATS.find((f) => f.value === this.listFormat)?.label}）`)
      }
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
    async exportHtml(ids: string[], outDir?: string) {
      // 必须带 account_id，否则后端拿不到文章列表（2026-08-09 修复）
      const body: Record<string, unknown> = { account_id: this.accountId, stage: this.aiStage }
      if (ids.length) body.ids = ids
      else body.view = this.view
      if (outDir && outDir.trim()) body.out_dir = outDir.trim()
      const r = await call(rest.post<{ task_id: string }>('/api/articles/export-html', body))
      if (!r) return
      this.exportTaskId = r.task_id
      const ui = useUiStore()
      ui.toast(
        `开始导出 ${ids.length || this.counts[this.view]} 篇正文 HTML${body.out_dir ? ` → ${body.out_dir}` : ''}（任务已创建，可看进度）`,
      )
      useTasksStore().track(r.task_id, 'export', {
        onDone: (t) => {
          const res = (t.result || {}) as { dir?: string; count?: number }
          this.exportTaskId = ''
          ui.toast(`正文导出完成（${res.count ?? ''} 篇）→ ${res.dir || 'exports/'}（含 index.html 说明页）`)
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
        if (p.title_verdict !== undefined) {
          row.title_verdict = p.title_verdict
          row.title_reason = p.title_reason || row.title_reason
        }
        if (p.content_verdict !== undefined) {
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
