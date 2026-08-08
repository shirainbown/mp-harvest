// 全局单一 1s interval 驱动的时钟（§5.10.6）：所有倒计时行共享，不每行一个 timer
import { ref } from 'vue'

const now = ref(Math.floor(Date.now() / 1000))
let started = false

export function useTicker() {
  if (!started) {
    started = true
    setInterval(() => {
      now.value = Math.floor(Date.now() / 1000)
    }, 1000)
  }
  return now
}

/** 剩余秒 → mm:ss / 已过期；<5min 为 warn */
export function fmtCountdown(remain: number): { text: string; cls: string } {
  if (remain <= 0) return { text: '已过期', cls: 'expired' }
  const m = String(Math.floor(remain / 60)).padStart(2, '0')
  const s = String(remain % 60).padStart(2, '0')
  return { text: `${m}:${s}`, cls: remain < 300 ? 'warn' : '' }
}
