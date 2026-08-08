import { defineStore } from 'pinia'

export type ViewId = 'credentials' | 'history' | 'ai' | 'network'

export interface ToastItem {
  id: number
  msg: string
  ok: boolean
  out: boolean
}

let seq = 0

export const useUiStore = defineStore('ui', {
  state: () => ({
    view: 'credentials' as ViewId,
    toasts: [] as ToastItem[],
    updateOpen: false,
  }),
  actions: {
    go(v: ViewId) {
      this.view = v
    },
    /** 成功 1.2s / 错误 2.5s，最多叠 3 条（2026-08-09 缩短：用户反馈弹窗停留过久） */
    toast(msg: string, ok = true) {
      const t: ToastItem = { id: ++seq, msg, ok, out: false }
      this.toasts.push(t)
      // 最多 3 条：超出移除最旧
      while (this.toasts.length > 3) this.toasts.shift()
      setTimeout(() => {
        t.out = true
        setTimeout(() => {
          this.toasts = this.toasts.filter((x) => x.id !== t.id)
        }, 250)
      }, ok ? 1200 : 2500)
    },
    error(msg: string) {
      this.toast(msg, false)
    },
  },
})
