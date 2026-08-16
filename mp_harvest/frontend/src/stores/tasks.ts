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

const pendingTimers: Record<string, ReturnType<typeof setTimeout>> = {}

type PendingEvent =
  | { kind: 'progress'; percent: number; message: string }
  | { kind: 'done'; result?: unknown }
  | { kind: 'error'; error: string }

export const useTasksStore = defineStore('tasks', {
  state: () => ({
    tasks: {} as Record<string, TaskInfo>,
    callbacks: {} as Record<string, TaskCallbacks>,
    // 任务完成太快时，task.done 可能先于 track() 注册到达。这里缓存
    // 未匹配到 task 的事件，track() 注册后再重放，避免事件被丢弃导致
    // aiTaskId 永远不释放、按钮“死掉”。
    pendingEvents: {} as Record<string, PendingEvent>,
  }),
  actions: {
    /** 创建任务后登记回调（进度/完成/失败） */
    track(task_id: string, kind: string, cb: TaskCallbacks = {}) {
      this.tasks[task_id] = { task_id, kind, percent: 0, message: '', status: 'running' }
      this.callbacks[task_id] = cb

      const pending = this.pendingEvents[task_id]
      if (pending) {
        delete this.pendingEvents[task_id]
        const timer = pendingTimers[task_id]
        if (timer) {
          clearTimeout(timer)
          delete pendingTimers[task_id]
        }
        if (pending.kind === 'done') this.onDone(task_id, pending.result)
        else if (pending.kind === 'error') this.onError(task_id, pending.error)
        else this.onProgress(task_id, pending.percent, pending.message)
      }
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
      if (!t) {
        this._buffer(task_id, { kind: 'progress', percent, message })
        return
      }
      if (t.status !== 'running') return
      t.percent = percent
      t.message = message
      this.callbacks[task_id]?.onProgress?.(t)
    },
    onDone(task_id: string, result?: unknown) {
      const t = this.tasks[task_id]
      if (!t) {
        this._buffer(task_id, { kind: 'done', result })
        return
      }
      if (t.status !== 'running') return
      t.percent = 100
      t.status = 'done'
      t.result = result
      this.callbacks[task_id]?.onDone?.(t)
      this.cleanup(task_id)
    },
    onError(task_id: string, error: string) {
      const t = this.tasks[task_id]
      if (!t) {
        this._buffer(task_id, { kind: 'error', error })
        return
      }
      if (t) {
        t.status = 'error'
        t.error = error
        this.callbacks[task_id]?.onError?.(t)
      }
      useUiStore().error(error)
      this.cleanup(task_id)
    },
    _buffer(task_id: string, event: PendingEvent) {
      this.pendingEvents[task_id] = event
      const old = pendingTimers[task_id]
      if (old) clearTimeout(old)
      pendingTimers[task_id] = setTimeout(() => {
        delete this.pendingEvents[task_id]
        delete pendingTimers[task_id]
      }, 15000)
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
