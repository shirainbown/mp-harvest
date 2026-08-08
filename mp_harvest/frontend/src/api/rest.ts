// REST 封装（§7.1）：自动带 token、统一错误 toast；mock 模式下转发给 mock 后端
import { apiUrl, MOCK } from '../config'
import { useUiStore } from '../stores/ui'
import { mockHandle } from '../mock'

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  if (MOCK) return mockHandle<T>(method, path, body)
  let res: Response
  try {
    res = await fetch(apiUrl(path), {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new ApiError(-1, '无法连接后端服务')
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const data = await res.json()
      msg = data.detail || data.error || msg
    } catch {
      /* keep default */
    }
    throw new ApiError(res.status, msg)
  }
  if (res.status === 204) return undefined as T
  const ct = res.headers.get('content-type') || ''
  return (ct.includes('json') ? res.json() : res.text()) as Promise<T>
}

/** 统一错误处理：toast 并返回 null（调用方关心错误可自己 catch） */
export async function call<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p
  } catch (e) {
    useUiStore().error(e instanceof Error ? e.message : String(e))
    return null
  }
}

export const rest = {
  get: <T>(path: string) => request<T>('GET', path),
  post: <T>(path: string, body?: unknown) => request<T>('POST', path, body),
  put: <T>(path: string, body?: unknown) => request<T>('PUT', path, body),
  del: <T>(path: string) => request<T>('DELETE', path),
}
