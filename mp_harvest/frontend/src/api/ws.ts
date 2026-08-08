// WebSocket 封装（§7.2）：/ws?token=，自动重连（1s→2s→…→10s 退避），事件分派到 stores
import { MOCK, wsUrl } from '../config'
import type { WsEvent } from '../types'
import { useAccountsStore } from '../stores/accounts'
import { useTasksStore } from '../stores/tasks'
import { useUiStore } from '../stores/ui'

let ws: WebSocket | null = null
let retry = 0
let closed = false

/** WS 事件分派（真实 ws 与 mock 共用同一入口） */
export function dispatchWsEvent(evt: WsEvent) {
  const accounts = useAccountsStore()
  const tasks = useTasksStore()
  const ui = useUiStore()
  switch (evt.type) {
    case 'task.progress':
      tasks.onProgress(evt.task_id, evt.percent, evt.message)
      break
    case 'task.done':
      tasks.onDone(evt.task_id, evt.result)
      break
    case 'task.error':
      tasks.onError(evt.task_id, evt.error)
      break
    case 'ai.batch':
      accounts.onAiBatch(evt.account_id, evt.articles)
      break
    case 'credential.captured':
      accounts.onCaptured(evt.account_id, evt.expires_at)
      break
    case 'credential.expired':
      accounts.onExpired(evt.account_id)
      break
    case 'mitm.status':
      accounts.mitm = { running: evt.running, port: evt.port }
      break
    case 'clipboard.credential':
      ui.toast(`剪贴板目击凭证链接：${evt.name || evt.url}，可在凭证管理页入库`)
      break
  }
}

export function connectWs() {
  if (MOCK || closed) return
  try {
    ws = new WebSocket(wsUrl())
  } catch {
    scheduleReconnect()
    return
  }
  ws.onopen = () => {
    retry = 0
    // 重连成功后刷新全量状态，避免断线期间丢事件
    useAccountsStore().load()
  }
  ws.onmessage = (e) => {
    try {
      dispatchWsEvent(JSON.parse(e.data) as WsEvent)
    } catch {
      /* 忽略无法解析的帧 */
    }
  }
  ws.onclose = () => scheduleReconnect()
  ws.onerror = () => ws?.close()
}

function scheduleReconnect() {
  if (closed) return
  retry++
  const delay = Math.min(1000 * 2 ** (retry - 1), 10000)
  setTimeout(connectWs, delay)
}

export function closeWs() {
  closed = true
  ws?.close()
}
