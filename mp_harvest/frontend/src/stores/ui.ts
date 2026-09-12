import { defineStore } from 'pinia'

export type ViewId =
  | 'credentials'
  | 'history'
  | 'external'
  | 'weekly'
  | 'ai'
  | 'logs'
  | 'settings'

export interface ToastItem {
  id: number
  msg: string
  ok: boolean
  /** 滑出动画中 */
  out: boolean
  /** 错误条不自动消失，需手动 ✕ 关闭 */
  sticky: boolean
}

export interface ErrorRecord {
  id: number
  time: number
  msg: string
}

let seq = 0

/** 每条 toast 的自动消失定时器（同 id 重复出现时要能重置，见 toast()） */
const dismissTimers: Record<number, ReturnType<typeof setTimeout>> = {}

const SUCCESS_MS = 2500
/** 错误多留一会儿（要读文字），但**不再永不消失** —— 记录由错误中心负责 */
const ERROR_MS = 8000
/** 详情越长留越久（多一行 / 多几十字各加一点），上限 20s */
const ERROR_MS_MAX = 20000

/** 按内容长度决定错误条停留时长：详细的报错需要更长的阅读时间（2026-09） */
export function errorDuration(msg: string): number {
  const text = String(msg ?? '')
  const extra = Math.max(0, text.split('\n').length - 1) * 2500
    + Math.max(0, text.length - 60) * 30
  return Math.min(ERROR_MS + extra, ERROR_MS_MAX)
}

/** 一位补零 */
function p2(n: number): string {
  return String(n).padStart(2, '0')
}

/**
 * 把一条错误记录格式化成可粘贴的文本。
 *
 * 带**完整日期时间**而不只是时刻：错误中心里可能横跨好几天，
 * 只给 HH:MM:SS 的话对方无法判断是哪一次。
 */
export function formatError(e: { time: number; msg: string }): string {
  const d = new Date(e.time)
  const ts = `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} `
    + `${p2(d.getHours())}:${p2(d.getMinutes())}:${p2(d.getSeconds())}`
  return `[${ts}] ${e.msg}`
}

/** 多条错误拼成一段（用空行分隔，便于阅读） */
export function formatErrors(list: { time: number; msg: string }[]): string {
  return list.map(formatError).join('\n\n')
}

export const useUiStore = defineStore('ui', {
  state: () => ({
    view: 'credentials' as ViewId,
    toasts: [] as ToastItem[],
    /** 错误中心：最近错误（时间 + 消息），供侧边栏入口查看 */
    errors: [] as ErrorRecord[],
    errorCenterOpen: false,
    updateOpen: false,
  }),
  actions: {
    go(v: ViewId) {
      this.view = v
    },
    /**
     * 成功 2.5s / 错误 8s 自动消失；最多叠 5 条。
     *
     * 去重（2026-09 修复）：一次操作常常并发打多个请求（accounts.load 3 个、
     * settings.load 5 个），失败时每个请求各弹一条**完全相同**的错误 ——
     * 表现就是「触发一次同时弹出好几条」。同一条消息在 DEDUPE_MS 内重复出现
     * 时只延长它自己的显示时间，不再叠新条、也不重复记账。
     */
    toast(msg: string, ok = true) {
      const text = String(msg ?? '')
      const dup = this.toasts.find((x) => x.ok === ok && x.msg === text && !x.out)
      if (dup) {
        this._scheduleDismiss(dup, ok ? SUCCESS_MS : errorDuration(dup.msg))
        return
      }
      const t: ToastItem = { id: ++seq, msg: text, ok, out: false, sticky: !ok }
      this.toasts.push(t)
      if (!ok) {
        this.errors.push({ id: t.id, time: Date.now(), msg: text })
        while (this.errors.length > 50) this.errors.shift()
      }
      while (this.toasts.length > 5) {
        const dropped = this.toasts.shift()
        if (dropped) this._forget(dropped.id)
      }
      this._scheduleDismiss(t, ok ? SUCCESS_MS : errorDuration(text))
    },
    /** 安排（或重置）某条 toast 的自动消失 */
    _scheduleDismiss(t: ToastItem, ms: number) {
      const old = dismissTimers[t.id]
      if (old) clearTimeout(old)
      dismissTimers[t.id] = setTimeout(() => {
        t.out = true
        setTimeout(() => {
          this.toasts = this.toasts.filter((x) => x.id !== t.id)
          this._forget(t.id)
        }, 250)
      }, ms)
    },
    _forget(id: number) {
      const timer = dismissTimers[id]
      if (timer) clearTimeout(timer)
      delete dismissTimers[id]
    },
    error(msg: string) {
      this.toast(msg, false)
    },
    dismissToast(id: number) {
      this._forget(id)
      this.toasts = this.toasts.filter((x) => x.id !== id)
    },
    /** 一键清掉当前所有提示条（错误记录保留在错误中心） */
    dismissAllToasts() {
      for (const t of this.toasts) this._forget(t.id)
      this.toasts = []
    },
    clearErrors() {
      this.errors = []
    },
  },
})
