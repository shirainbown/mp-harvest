// 任务注册表镜像（§3.2）：REST 创建 Task 返回 task_id，WS 推 progress/done/error
import { defineStore } from 'pinia'
import type { TaskInfo } from '../types'
import { call, rest } from '../api/rest'
import { useUiStore } from './ui'

interface TaskCallbacks {
  onProgress?: (t: TaskInfo) => void
  onDone?: (t: TaskInfo) => void
  onError?: (t: TaskInfo) => void
}

export const useTasksStore = defineStore('tasks', {
  state: () => ({
    tasks: {} as Record<string, TaskInfo>,
    callbacks: {} as Record<string, TaskCallbacks>,
  }),
  actions: {
    /** 创建任务后登记回调（进度/完成/失败） */
    track(task_id: string, kind: string, cb: TaskCallbacks = {}) {
      this.tasks[task_id] = { task_id, kind, percent: 0, message: '', status: 'running' }
      this.callbacks[task_id] = cb
    },
    async cancel(task_id: string) {
      const t = this.tasks[task_id]
      await call(rest.post(`/api/tasks/${task_id}/cancel`))
      if (t) {
        t.status = 'cancelled'
        this.callbacks[task_id]?.onError?.(t)
      }
    },
    // ---- WS 事件 ----
    onProgress(task_id: string, percent: number, message: string) {
      const t = this.tasks[task_id]
      if (!t || t.status !== 'running') return
      t.percent = percent
      t.message = message
      this.callbacks[task_id]?.onProgress?.(t)
    },
    onDone(task_id: string, result?: unknown) {
      const t = this.tasks[task_id]
      if (!t || t.status !== 'running') return
      t.percent = 100
      t.status = 'done'
      t.result = result
      this.callbacks[task_id]?.onDone?.(t)
      this.cleanup(task_id)
    },
    onError(task_id: string, error: string) {
      const t = this.tasks[task_id]
      if (t) {
        t.status = 'error'
        t.error = error
        this.callbacks[task_id]?.onError?.(t)
      }
      useUiStore().error(error)
      this.cleanup(task_id)
    },
    cleanup(task_id: string) {
      delete this.callbacks[task_id]
      // 完成的任务保留 10s 供 UI 展示后清除
      setTimeout(() => {
        if (this.tasks[task_id]?.status !== 'running') delete this.tasks[task_id]
      }, 10000)
    },
  },
})
