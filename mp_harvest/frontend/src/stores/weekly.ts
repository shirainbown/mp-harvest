// 周报生成：预览候选 → 生成 → 重渲染（改模板不花钱）。
import { defineStore } from 'pinia'
import type { WeeklyIssue, WeeklyPreview, WeeklyPrompt, WeeklyResult } from '../types'
import { LONG_TIMEOUT, call, rest } from '../api/rest'
import { useTasksStore } from './tasks'
import { useUiStore } from './ui'

function todayStr(offsetDays = 0): string {
  const d = new Date()
  d.setDate(d.getDate() + offsetDays)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`
}

export const useWeeklyStore = defineStore('weekly', {
  state: () => ({
    // ---- 本期设置 ----
    issueNum: 1,
    fromDate: todayStr(-6),
    toDate: todayStr(),
    selectedCount: 15,
    outDir: '',
    reportTitle: '',
    downloadImages: false,
    // ---- 来源勾选（空 = 全部）----
    accountIds: new Set<string>(),
    sourceIds: new Set<string>(),
    // ---- 模板 ----
    templatePath: '',
    // ---- 提示词 ----
    prompts: {} as Record<string, WeeklyPrompt>,
    /** 已保存快照：与 prompts[k].text 比对得出「有没有未保存改动」 */
    savedPrompts: {} as Record<string, string>,
    // ---- 数据 ----
    preview: null as WeeklyPreview | null,
    issues: [] as WeeklyIssue[],
    loadingPreview: false,
    loadingIssues: false,
    genTaskId: '',
    renderTaskId: '',
    stageMsg: '',
    lastResult: null as WeeklyResult | null,
  }),
  getters: {
    /** 提示词面板：保存按钮是否可点 */
    promptDirty(): (key: string) => boolean {
      return (key: string) => {
        const cur = this.prompts[key]
        if (!cur) return false
        return cur.text !== (this.savedPrompts[key] ?? '')
      }
    },
  },
  actions: {
    // ---- 预览 ----
    async loadPreview() {
      this.loadingPreview = true
      try {
        const q = new URLSearchParams({
          from_date: this.fromDate,
          to_date: this.toDate,
          account_ids: [...this.accountIds].join(','),
          source_ids: [...this.sourceIds].join(','),
        })
        const r = await call(rest.get<WeeklyPreview>(`/api/weekly/preview?${q}`))
        if (r) {
          this.preview = r
          // 首次进来时把后端建议的期号/目录/篇数填上；用户改过就不覆盖
          if (!this.outDir) this.outDir = r.out_dir
          if (!this.selectedCount || this.selectedCount === 15) this.selectedCount = r.selected_count
          this.issueNum = r.suggested_issue
          if (!this.templatePath && r.template_is_custom) this.templatePath = r.template_path
        }
      } finally {
        this.loadingPreview = false
      }
    },
    async loadIssues() {
      this.loadingIssues = true
      try {
        const r = await call(rest.get<WeeklyIssue[]>('/api/weekly/issues'))
        if (r) this.issues = r
      } finally {
        this.loadingIssues = false
      }
    },
    async loadAll() {
      await Promise.all([this.loadPreview(), this.loadIssues(), this.loadPrompts()])
    },
    // ---- 提示词 ----
    async loadPrompts() {
      const r = await call(rest.get<{ prompts: Record<string, WeeklyPrompt> }>('/api/weekly/prompts'))
      if (!r) return
      this.prompts = r.prompts
      this.savedPrompts = Object.fromEntries(
        Object.entries(r.prompts).map(([k, v]) => [k, v.text]),
      )
    },
    async savePrompt(key: string) {
      const cur = this.prompts[key]
      if (!cur) return
      const r = await call(rest.put<{ ok: boolean; pruned: number }>('/api/weekly/prompts', {
        key,
        text: cur.text,
      }))
      if (!r) return
      this.savedPrompts[key] = cur.text
      const stage = { scoring: '打分', detail: '深度解读', brief: '其他摘要' }[key]
      // 让用户明确知道「改了哪段、哪段的缓存失效了」——这正是提示词可编辑的代价
      useUiStore().toast(
        stage ? `${cur.label}已保存；${stage}缓存已失效，下次生成会按新标准重算` : `${cur.label}已保存`,
      )
    },
    restorePrompt(key: string) {
      const cur = this.prompts[key]
      if (!cur) return
      cur.text = cur.default
    },
    // ---- 生成 ----
    async generate() {
      if (this.genTaskId) return
      const ui = useUiStore()
      if (!this.fromDate || !this.toDate) {
        ui.error('请先选择日期区间')
        return
      }
      if (!this.preview?.total) {
        ui.error('所选范围内没有候选文章，请调整日期区间或来源勾选')
        return
      }
      const r = await call(
        rest.post<{ task_id: string; total: number }>(
          '/api/weekly/generate',
          {
            issue_num: this.issueNum,
            from_date: this.fromDate,
            to_date: this.toDate,
            selected_count: this.selectedCount,
            account_ids: [...this.accountIds],
            source_ids: [...this.sourceIds],
            out_dir: this.outDir,
            template_path: this.templatePath,
            report_title: this.reportTitle,
            download_images: this.downloadImages,
          },
          { timeout: LONG_TIMEOUT },
        ),
      )
      if (!r) return
      this.genTaskId = r.task_id
      useTasksStore().track(r.task_id, 'weekly.generate', {
        onProgress: (t) => {
          this.stageMsg = t.message || ''
        },
        onDone: async (t) => {
          this.genTaskId = ''
          this.stageMsg = ''
          this.lastResult = (t.result || {}) as WeeklyResult
          await Promise.all([this.loadPreview(), this.loadIssues()])
          this._reportOutcome('周报生成')
        },
        onError: () => {
          this.genTaskId = ''
          this.stageMsg = ''
        },
      })
    },
    async cancelGenerate() {
      if (!this.genTaskId) return
      const id = this.genTaskId
      this.genTaskId = ''
      this.stageMsg = ''
      await useTasksStore().cancel(id)
      useUiStore().toast('已停止生成')
    },
    // ---- 重渲染（只换模板，不调用 AI）----
    async rerender(issueDir: string) {
      if (this.renderTaskId) return
      const r = await call(
        rest.post<{ task_id: string }>(
          '/api/weekly/render',
          { issue_dir: issueDir, template_path: this.templatePath },
          { timeout: LONG_TIMEOUT },
        ),
      )
      if (!r) return
      this.renderTaskId = r.task_id
      useTasksStore().track(r.task_id, 'weekly.render', {
        onProgress: (t) => {
          this.stageMsg = t.message || ''
        },
        onDone: (t) => {
          this.renderTaskId = ''
          this.stageMsg = ''
          this.lastResult = (t.result || {}) as WeeklyResult
          this._reportOutcome('重新渲染')
        },
        onError: () => {
          this.renderTaskId = ''
          this.stageMsg = ''
        },
      })
    },
    /** 统一播报结果：成功给细节，失败**把后端原文带出来**（模板错误最有价值） */
    _reportOutcome(what: string) {
      const ui = useUiStore()
      const r = this.lastResult
      if (!r) return
      if (r.missing_vars?.length) {
        ui.error(
          `模板引用了未知变量：${r.missing_vars.join('、')}\n` +
            '这些位置会渲染成空白。请检查模板里的变量名是否拼写正确。',
        )
        return
      }
      if (r.ok === false) {
        ui.error(`${what}失败：${r.error || '未知原因'}`)
        return
      }
      const bits = [`精选 ${r.selected ?? 0} 篇`, `其他入选 ${r.others ?? 0} 篇`]
      if (r.dropped) bits.push(`剔除不相关 ${r.dropped} 篇`)
      if (r.archived !== undefined) bits.push(`归档原文 ${r.archived} 篇`)
      if (r.failed) bits.push(`失败 ${r.failed} 篇`)
      const lines = [`${what}完成：${bits.join('，')}`, `输出：${r.issue_dir || ''}`]
      if (r.errors?.length) lines.push(`部分失败：${r.errors.slice(0, 3).join('；')}`)
      if (r.failed) ui.error(lines.join('\n'))
      else ui.toast(lines.join('　'))
    },
  },
})
