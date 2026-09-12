// 「其他来源」目录（外部来源）：登记 / 扫描 / 浏览 / AI 筛选 / 写回。
//
// 为什么不复用 useArticlesStore：两个视图在 App.vue 里都是 v-show 常驻挂载的，
// 共用一个 list 会互相覆盖（切页回来数据就串了）。这里独立一套状态。
import { defineStore } from 'pinia'
import type { ExternalFormat, ExternalItem, ExternalSource } from '../types'
import { LONG_TIMEOUT, call, rest } from '../api/rest'
import { useTasksStore } from './tasks'
import { useUiStore } from './ui'

/** 搜索/排序都在前端做：列表本来就整份在内存里（与 articles.ts 的 visible 同思路） */
export const useExternalStore = defineStore('external', {
  state: () => ({
    sources: [] as ExternalSource[],
    /** 空 = 全部来源 */
    sourceId: '',
    items: [] as ExternalItem[],
    /** 格式说明（展开「格式说明」面板时才拉） */
    format: null as ExternalFormat | null,
    q: '',
    // 排序维度与方向（2026-09：原先只有方向，与「历史文章」页的排序控件对不齐）
    sortBy: 'time' as 'time' | 'name',
    sortDir: 'desc' as 'asc' | 'desc',
    selected: new Set<string>() as Set<string>,
    loading: false,
    /** 正在扫描的来源 id（用来只在那一行显示进度） */
    scanningId: '',
    scanTaskId: '',
    exportTaskId: '',
    aiTaskId: '',
    aiProgress: '',
  }),
  getters: {
    currentSource(state): ExternalSource | null {
      return state.sources.find((s) => s.id === state.sourceId) || null
    },
    enabledSources(state): ExternalSource[] {
      return state.sources.filter((s) => Number(s.enabled) === 1)
    },
    /** 搜索 + 排序后的可见条目 */
    visible(state): ExternalItem[] {
      const needle = state.q.trim().toLowerCase()
      let rows = state.items
      if (needle) {
        rows = rows.filter((r) =>
          [r.title, r.domain, r.primary_category, r.url, ...(r.authors || [])]
            .join(' ')
            .toLowerCase()
            .includes(needle),
        )
      }
      // 外部条目没有「公众号名」可排，「按名称」排的是**标题**（论文列表这样才有用）。
      //
      // ⚠️ 两个维度的比较器「自然方向」是相反的：`localeCompare` 天然升序
      // （a<b 给负数），而 `Date.parse(b) - Date.parse(a)` 天然降序。所以方向
      // 系数要分别算 —— 用一个系数乘两边，时间排序会**整个反过来**（有测试钉住）。
      const nameDir = state.sortDir === 'asc' ? 1 : -1
      const timeDir = state.sortDir === 'desc' ? 1 : -1
      return [...rows].sort((a, b) => {
        if (state.sortBy === 'name') {
          const byName = (a.title || '').localeCompare(b.title || '', 'zh')
          if (byName) return byName * nameDir
          return Date.parse(b.date || '') - Date.parse(a.date || '') // 同名按时间新→旧
        }
        const byTime = Date.parse(b.date || '') - Date.parse(a.date || '')
        if (byTime) return byTime * timeDir
        return (a.title || '').localeCompare(b.title || '', 'zh')
      })
    },
    selectedInView(state): ExternalItem[] {
      return this.visible.filter((a) => state.selected.has(a.id))
    },
  },
  actions: {
    async loadSources() {
      const r = await call(rest.get<ExternalSource[]>('/api/external/sources'))
      if (r) this.sources = r
    },
    /** 排序控件的**一次点击**：点已选中的段翻转方向，点另一段换维度（与 articles 同款逻辑） */
    pickSort(by: 'time' | 'name') {
      if (this.sortBy === by) this.toggleSortDir()
      else this.setSortBy(by)
    },
    setSortBy(by: 'time' | 'name') {
      this.sortBy = by
      // 切维度时重置为该维度的默认方向：时间默认最新在前、名称默认 A→Z
      this.sortDir = by === 'time' ? 'desc' : 'asc'
    },
    toggleSortDir() {
      this.sortDir = this.sortDir === 'desc' ? 'asc' : 'desc'
    },
    async load() {
      this.loading = true
      try {
        // order 固定 desc：真正的排序在前端 visible 里做（下面这行只决定拉取顺序）
        const q = new URLSearchParams({ source_id: this.sourceId, order: 'desc' })
        const r = await call(rest.get<ExternalItem[]>(`/api/external/items?${q}`))
        if (r) this.items = r
      } finally {
        this.loading = false
      }
    },
    /** 首屏：来源 + 条目一起拉 */
    async loadAll() {
      await this.loadSources()
      await this.load()
    },
    /** 拉格式说明（字段表 + 示例）。由后端给，前端不硬编码 —— 免得文档与
     *  解析器各写各的。只在用户展开说明面板时调，不占启动时间。 */
    async loadFormat() {
      if (this.format) return          // 一份就够，不必每次展开都拉
      const r = await call(rest.get<ExternalFormat>('/api/external/format'))
      if (r) this.format = r
    },
    /** 切换来源时清空勾选 —— 否则旧来源的 id 会留在 Set 里串味 */
    async setSource(id: string) {
      this.sourceId = id
      this.clearSelection()
      await this.load()
    },
    async addSource(name: string, path: string): Promise<boolean> {
      const r = await call(rest.post<ExternalSource>('/api/external/sources', { name, path }))
      if (!r) return false
      await this.loadSources()
      return true
    },
    async renameSource(id: string, name: string): Promise<boolean> {
      const r = await call(rest.patch<ExternalSource>(`/api/external/sources/${id}`, { name }))
      if (!r) return false
      await this.loadSources()
      return true
    },
    async toggleSource(id: string, enabled: boolean): Promise<boolean> {
      const r = await call(
        rest.patch<ExternalSource>(`/api/external/sources/${id}`, { enabled }),
      )
      if (!r) return false
      await this.loadSources()
      await this.load()
      return true
    },
    async removeSource(id: string): Promise<boolean> {
      const r = await call(rest.del<{ ok: boolean }>(`/api/external/sources/${id}`))
      if (!r) return false
      if (this.sourceId === id) this.sourceId = ''
      await this.loadAll()
      return true
    },
    // ---- 扫描 ----
    async scan(sourceId: string) {
      if (this.scanTaskId) return
      const ui = useUiStore()
      const r = await call(
        rest.post<{ task_id: string }>(
          `/api/external/sources/${sourceId}/scan`,
          undefined,
          { timeout: LONG_TIMEOUT },
        ),
      )
      if (!r) return
      this.scanTaskId = r.task_id
      this.scanningId = sourceId
      useTasksStore().track(r.task_id, 'external.scan', {
        onProgress: (t) => {
          this.aiProgress = t.message || ''
        },
        onDone: async (t) => {
          this.scanTaskId = ''
          this.scanningId = ''
          this.aiProgress = ''
          const res = t.result as
            | { ok?: boolean; seen?: number; new?: number; removed?: number; error?: string }
            | undefined
          await this.loadAll()
          if (res?.ok === false) {
            ui.error(`扫描失败：${res.error || '未知原因'}`)
          } else if (res) {
            const bits = [`扫到 ${res.seen ?? 0} 条`, `新增 ${res.new ?? 0} 条`]
            if (res.removed) bits.push(`清理失效 ${res.removed} 条`)
            ui.toast(`扫描完成：${bits.join('，')}`)
          }
        },
        onError: () => {
          this.scanTaskId = ''
          this.scanningId = ''
          this.aiProgress = ''
        },
      })
    },
    async cancelScan() {
      if (!this.scanTaskId) return
      const id = this.scanTaskId
      this.scanTaskId = ''
      this.scanningId = ''
      this.aiProgress = ''
      await useTasksStore().cancel(id)
      useUiStore().toast('已停止扫描')
    },
    // ---- AI 筛选 ----
    async aiFilter(stage: 'title' | 'content', batchSize?: number, workers?: number) {
      if (this.aiTaskId) return
      const ui = useUiStore()
      const ids = this.selectedInView.length
        ? this.selectedInView.map((a) => a.id)
        : this.visible.map((a) => a.id)
      if (!ids.length) {
        ui.error('没有可筛选的条目')
        return
      }
      const r = await call(
        rest.post<{ task_id: string; total: number }>(
          '/api/external/filter',
          {
            source_id: this.sourceId,
            ids,
            stage,
            batch_size: batchSize ?? null,
            workers: workers ?? null,
          },
          { timeout: LONG_TIMEOUT },
        ),
      )
      if (!r) return
      this.aiTaskId = r.task_id
      const label = stage === 'content' ? '内容筛选' : '标题筛选'
      useTasksStore().track(r.task_id, 'external.filter', {
        onProgress: (t) => {
          this.aiProgress = t.message || `${label}中…`
        },
        onDone: async (t) => {
          this.aiTaskId = ''
          this.aiProgress = ''
          const res = t.result as
            | { judged?: number; cached?: number; errors?: string[] }
            | undefined
          await this.load()
          if (res?.errors?.length) {
            ui.error(`${label}完成，但有 ${res.errors.length} 条出错：${res.errors[0]}`)
          } else {
            ui.toast(
              `${label}完成：判定 ${res?.judged ?? 0} 条` +
                (res?.cached ? `，复用缓存 ${res.cached} 条` : ''),
            )
          }
        },
        onError: () => {
          this.aiTaskId = ''
          this.aiProgress = ''
        },
      })
    },
    // ---- 写回目录 ----
    async exportItems(outDir: string, dateDir = '') {
      if (this.exportTaskId) return
      const ui = useUiStore()
      const rows = this.selectedInView.length ? this.selectedInView : this.visible
      if (!rows.length) {
        ui.error('没有可导出的条目')
        return
      }
      const r = await call(
        rest.post<{ task_id: string; total: number }>(
          '/api/external/export',
          {
            source_id: this.sourceId,
            ids: rows.map((a) => a.id),
            out_dir: outDir,
            date_dir: dateDir,
          },
          { timeout: LONG_TIMEOUT },
        ),
      )
      if (!r) return
      this.exportTaskId = r.task_id
      useTasksStore().track(r.task_id, 'external.export', {
        onProgress: (t) => {
          this.aiProgress = t.message || ''
        },
        onDone: (t) => {
          this.exportTaskId = ''
          this.aiProgress = ''
          const res = t.result as
            | { ok?: boolean; written?: number; out_dir?: string; error?: string }
            | undefined
          if (res?.ok === false) {
            ui.error(`写回失败：${res.error || '未知原因'}`)
          } else {
            ui.toast(`已写回 ${res?.written ?? 0} 条到 ${res?.out_dir || outDir}`)
          }
        },
        onError: () => {
          this.exportTaskId = ''
          this.aiProgress = ''
        },
      })
    },
    async cancelExport() {
      // 视图里的 ProgressInline 一直是 cancellable 的，但之前没绑 @cancel ——
      // 取消按钮渲染出来了却点了没反应（2026-09 修复）
      const id = this.exportTaskId
      if (!id) return
      this.exportTaskId = ''
      this.aiProgress = ''
      await useTasksStore().cancel(id)
      useUiStore().toast('已停止写回')
    },
    // ---- 勾选 ----
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
