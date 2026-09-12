// 执行日志：把「发生过什么」摆给用户看（2026-09）。
//
// 后端在四处埋点（模型调用 / 筛选批次 / 任务起止 / 写操作）落到 SQLite；
// 这里只负责「拉一页 → 筛选 → 清空」。**不做本地缓存** —— 日志本来就是只增的，
// 每次拉最新的才准；页面关掉再打开也该看到最新的。
import { defineStore } from 'pinia'
import type { LogEvent, LogKindCount } from '../types'
import { call, rest } from '../api/rest'
import { useUiStore } from './ui'

/** 一页拉多少条。够回看一轮完整的周报生成，又不至于一次塞几千行进 DOM。 */
const PAGE = 200

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

/** epoch 秒 → `YYYY-MM-DD HH:MM:SS`（与错误中心的格式一致，复制出去可直接对时） */
export function formatTs(ts: number): string {
  const d = new Date(ts * 1000)
  if (Number.isNaN(d.getTime())) return ''
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}

/** 一条日志 → 可复制的纯文本（带时间、级别、类型；有上下文就附上 JSON） */
export function formatEvent(e: LogEvent): string {
  const head = `[${formatTs(e.ts)}] [${e.level}] ${e.kind} ${e.message}`
  const keys = Object.keys(e.data || {})
  return keys.length ? `${head}\n${JSON.stringify(e.data, null, 2)}` : head
}

export function formatEvents(list: LogEvent[]): string {
  return list.map(formatEvent).join('\n\n')
}

export const useLogsStore = defineStore('logs', {
  state: () => ({
    events: [] as LogEvent[],
    kinds: [] as LogKindCount[],
    total: 0,
    /** 空 = 全部；否则是**下限**（警告会连错误一起给，与后端一致） */
    level: '' as '' | 'debug' | 'info' | 'warn' | 'error',
    kind: '',
    q: '',
    loading: false,
    /** 是否**成功拉过一次**。骨架屏只在首次加载时出现 ——
     *  列表本来就空时（比如切筛选）再插一段骨架行，视觉上就是「闪一下」
     *  （2026-09 用户报的：日志全空时切 全部/信息 会闪）。 */
    loaded: false,
    /** 还有更早的没拉（决定「加载更多」是否可点） */
    hasMore: false,
  }),
  getters: {
    /** 要不要显示骨架屏。**只在首次加载时** ——
     *  列表本来就空时（比如切级别筛选）再插一段骨架行，视觉上就是「闪一下」。
     *  视图里的条件没法单测，放这儿才钉得住（同 `articles.pickSort` 的理由）。 */
    showSkeleton(state): boolean {
      return state.loading && !state.loaded
    },
    /** 类型下拉的选项：按前缀收成几组，免得几十项铺满屏幕 */
    kindOptions(state): Array<{ value: string; label: string }> {
      const out = [{ value: '', label: `全部类型（${state.total}）` }]
      for (const k of state.kinds) {
        if (!k.count) continue
        out.push({ value: k.kind, label: `${k.kind}（${k.count}）` })
      }
      return out
    },
  },
  actions: {
    _query(beforeId = 0): string {
      const q = new URLSearchParams({
        level: this.level,
        kind: this.kind,
        q: this.q,
        limit: String(PAGE),
      })
      if (beforeId) q.set('before_id', String(beforeId))
      return q.toString()
    },
    async load() {
      this.loading = true
      try {
        const r = await call(
          rest.get<{ events: LogEvent[]; total: number; kinds: LogKindCount[] }>(
            `/api/logs?${this._query()}`,
          ),
        )
        if (!r) return
        this.loaded = true
        this.events = r.events
        this.total = r.total
        this.kinds = r.kinds
        this.hasMore = r.events.length >= PAGE
      } finally {
        this.loading = false
      }
    },
    async loadMore() {
      if (!this.events.length || this.loading) return
      this.loading = true
      try {
        const last = this.events[this.events.length - 1]
        const r = await call(
          rest.get<{ events: LogEvent[] }>(`/api/logs?${this._query(last.id)}`),
        )
        if (!r) return
        this.events = [...this.events, ...r.events]
        this.hasMore = r.events.length >= PAGE
      } finally {
        this.loading = false
      }
    },
    async clear() {
      const r = await call(rest.del<{ ok: boolean; cleared: number }>('/api/logs'))
      if (!r) return
      this.events = []
      this.total = 0
      this.kinds = []
      this.hasMore = false
      useUiStore().toast(`已清空 ${r.cleared} 条日志`)
    },
  },
})
