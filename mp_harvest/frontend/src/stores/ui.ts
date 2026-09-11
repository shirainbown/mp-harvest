import { defineStore } from 'pinia'

export type ViewId = 'credentials' | 'history' | 'ai' | 'network' | 'settings'

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
    /** 成功 2.5s 自动消失；错误不自动消失（手动 ✕ 关闭）；最多叠 5 条 */
    toast(msg: string, ok = true) {
      const t: ToastItem = { id: ++seq, msg, ok, out: false, sticky: !ok }
      this.toasts.push(t)
      if (!ok) {
        this.errors.push({ id: t.id, time: Date.now(), msg })
        while (this.errors.length > 50) this.errors.shift()
      }
      while (this.toasts.length > 5) this.toasts.shift()
      if (ok) {
        setTimeout(() => {
          t.out = true
          setTimeout(() => {
            this.toasts = this.toasts.filter((x) => x.id !== t.id)
          }, 250)
        }, 2500)
      }
    },
    error(msg: string) {
      this.toast(msg, false)
    },
    dismissToast(id: number) {
      this.toasts = this.toasts.filter((x) => x.id !== id)
    },
    clearErrors() {
      this.errors = []
    },
  },
})
