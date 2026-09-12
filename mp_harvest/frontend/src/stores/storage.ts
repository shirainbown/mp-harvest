// 本地数据占用与清理（2026-09）。
//
// 起因：用户问「把本地缓存删了会怎样」—— 一查 data/ 有 815MB，其中 806MB 是
// 从来没清理过的历史更新包，而界面上根本看不到「什么占了多大、哪些能安全清」。
//
// 清单里 `safe=false` 的项**不可清理**（账号、凭证、模型 Key、CA、提示词、设置、
// 文章缓存）—— 它们也显示出来，是为了让人看得见「这些不归清理管」，
// 而不是清完才发现少了东西。
import { defineStore } from 'pinia'
import { call, rest } from '../api/rest'
import { useUiStore } from './ui'

export interface StorageItem {
  key: string
  label: string
  path: string
  size: number
  count: number
  /** true 才可清理（可重建）；false 的项只是展示 */
  safe: boolean
  note: string
}

/** 字节 → 人读的大小。0 显示 `0 B`（而不是「0.0 KB」那种假精度）。 */
export function fmtSize(n: number): string {
  const v = Number(n) || 0
  if (v < 1024) return `${v} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let x = v / 1024
  let i = 0
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024
    i += 1
  }
  return `${x >= 100 ? Math.round(x) : x.toFixed(1)} ${units[i]}`
}

export const useStorageStore = defineStore('storage', {
  state: () => ({
    items: [] as StorageItem[],
    total_bytes: 0,
    clearable_bytes: 0,
    loading: false,
    /** 同 logs：骨架屏只在首次加载时出现（空列表时插骨架会闪） */
    loaded: false,
  }),
  getters: {
    /** 同 logs.showSkeleton：骨架屏只在首次加载时出现 */
    showSkeleton(state): boolean {
      return state.loading && !state.loaded
    },
    clearable(state): StorageItem[] {
      return state.items.filter((i) => i.safe)
    },
  },
  actions: {
    async load() {
      this.loading = true
      try {
        const r = await call(
          rest.get<{ items: StorageItem[]; total_bytes: number; clearable_bytes: number }>(
            '/api/storage',
          ),
        )
        if (!r) return
        this.loaded = true
        this.items = r.items
        this.total_bytes = r.total_bytes
        this.clearable_bytes = r.clearable_bytes
      } finally {
        this.loading = false
      }
    },
    async clean(keys: string[]) {
      const ui = useUiStore()
      if (!keys.length) return
      const r = await call(
        rest.post<{ ok: boolean; freed: number; removed: string[]; errors: string[] }>(
          '/api/storage/clean',
          { keys },
        ),
      )
      if (!r) return
      if (r.errors?.length) ui.error(`部分未清理：${r.errors.join('；')}`)
      else ui.toast(`已清理 ${r.removed.length} 项，释放 ${fmtSize(r.freed)}`)
      await this.load()
    },
  },
})
