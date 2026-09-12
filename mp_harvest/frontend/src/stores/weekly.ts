// 周报生成：预览候选 → 生成 → 重渲染（改模板不花钱）。
import { defineStore } from 'pinia'
import type {
  WeeklyCandidates,
  WeeklyIssue,
  WeeklyPreview,
  WeeklyPrompt,
  WeeklyResult,
} from '../types'
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
    /** 生成前给只有标题/摘要的候选补抓正文（2026-09）。默认开：
     *  没有正文时打分与解读都是瞎猜 —— 有实测数据的文章会被判成「无量化数据」，
     *  厂商宣传稿也认不出来。抓到的正文会写回缓存，下次免费。 */
    fetchBodies: true,
    /** 只用 AI 筛选未否掉的文章当候选（未判定照收）。默认开（2026-09）：
     *  用户实际遇到的是「窗口内 37 篇候选里有 33 篇是我早就筛掉的」—— 周报把
     *  筛掉的重打一遍分（花钱）还放进了报告，那次筛选等于白做。 */
    onlyKept: true,
    // ---- 来源勾选（**勾了才算**，默认全选）----
    //
    // 2026-09 改：原先是「空 = 全部」，默认一个都不勾 —— 「全部」链接在默认
    // 状态下点了等于没点（本来就是全部），表现成「按钮没反应」。
    // 现在默认全选、勾 = 纳入，所见即所得。后端契约不变（空列表仍当「全部」），
    // 但前端全不勾时会拦住生成并提示，不会把空列表发出去。
    accountIds: new Set<string>(),
    sourceIds: new Set<string>(),
    /** 默认勾选是否已种下 —— 用户改过就不再覆盖 */
    selectionSeeded: false,
    // ---- 模板 ----
    templatePath: '',
    // ---- 提示词 ----
    prompts: {} as Record<string, WeeklyPrompt>,
    /** 已保存快照：与 prompts[k].text 比对得出「有没有未保存改动」 */
    savedPrompts: {} as Record<string, string>,
    // ---- 数据 ----
    preview: null as WeeklyPreview | null,
    /** 候选逐篇明细（抽屉打开时才有；关掉不清空，省一次往返） */
    candidates: null as WeeklyCandidates | null,
    loadingCandidates: false,
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
    /**
     * 种下默认的全选（每个公众号与外部来源都勾上）。**只种一次** ——
     * 之后用户手动改过的选择不会被列表刷新冲掉。
     */
    seedSelection(accountIds: string[], sourceIds: string[]) {
      if (this.selectionSeeded) return
      if (!accountIds.length && !sourceIds.length) return   // 数据还没到，等下次
      this.accountIds = new Set(accountIds)
      this.sourceIds = new Set(sourceIds)
      this.selectionSeeded = true
    },
    /**
     * 「全选 / 全不选」一键切换（公众号）。返回切换后是否处于全选。
     *
     * 放在 store 而不是视图里：视图里的逻辑没法单测，而这正是出过问题的地方。
     */
    toggleAllAccounts(allIds: string[]): boolean {
      const all = allIds.length > 0 && this.accountIds.size === allIds.length
      this.accountIds = all ? new Set<string>() : new Set(allIds)
      return !all
    },
    toggleAllSources(allIds: string[]): boolean {
      const all = allIds.length > 0 && this.sourceIds.size === allIds.length
      this.sourceIds = all ? new Set<string>() : new Set(allIds)
      return !all
    },
    // ---- 预览 ----
    /** 候选查询参数。preview 与 candidates **必须**用同一份 —— 两处各拼一次的话，
     *  改了口径只改一处，就会出现「面板写 5 篇、抽屉里 3 篇」这种对不上的怪象。 */
    _candQuery(): URLSearchParams {
      return new URLSearchParams({
        from_date: this.fromDate,
        to_date: this.toDate,
        account_ids: [...this.accountIds].join(','),
        source_ids: [...this.sourceIds].join(','),
        only_kept: String(this.onlyKept),
      })
    },
    async loadPreview() {
      this.loadingPreview = true
      try {
        const q = this._candQuery()
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
    /** 拉候选**逐篇明细**（打开抽屉时才调）。与 preview 用同一份查询参数，
     *  所以「共 N 篇」和抽屉里的条数必然一致。 */
    async loadCandidates() {
      this.loadingCandidates = true
      try {
        const r = await call(
          rest.get<WeeklyCandidates>(`/api/weekly/candidates?${this._candQuery()}`),
        )
        if (r) this.candidates = r
      } finally {
        this.loadingCandidates = false
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
      // 兜底：界面上按钮已禁用，但别让别的调用路径把「一个都没选」当成
      // 空列表发出去 —— 后端把空列表读作「全部」，会静默生成一份全量周报
      if (!this.accountIds.size && !this.sourceIds.size) {
        ui.error('未选择任何来源，请至少勾选一个公众号或来源目录')
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
            fetch_bodies: this.fetchBodies,
            only_kept: this.onlyKept,
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
