// REST 封装（§7.1）：自动带 token、超时控制、统一错误 toast；mock 模式下转发给 mock 后端
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

/** 默认超时 30s；导出/拉取类长请求用 120s（rest.xxx(path, body, { timeout })） */
export const DEFAULT_TIMEOUT = 30_000
export const LONG_TIMEOUT = 120_000

export interface RestOptions {
  timeout?: number
}

/**
 * FastAPI 的错误体有两种形态：业务错误是字符串 `detail`，请求校验失败
 * (422) 是**对象数组** `[{loc, msg, type}]`。直接把数组塞进 Error.message
 * 会渲染成「[object Object]」（2026-09 修复），这里统一转成可读文本。
 */
function formatApiDetail(detail: unknown, error: unknown, fallback: string): string {
  for (const candidate of [detail, error]) {
    if (typeof candidate === 'string' && candidate.trim()) return candidate
  }
  if (Array.isArray(detail)) {
    const parts = detail
      .map((d) => {
        if (typeof d === 'string') return d
        const item = d as { loc?: unknown; msg?: unknown }
        const loc = Array.isArray(item?.loc) ? item.loc.slice(1).join('.') : ''
        const msg = typeof item?.msg === 'string' ? item.msg : ''
        return loc ? `${loc}: ${msg}` : msg
      })
      .filter(Boolean)
    if (parts.length) return parts.join('；')
  }
  return fallback
}

async function request<T>(method: string, path: string, body?: unknown, opts: RestOptions = {}): Promise<T> {
  if (MOCK) return mockHandle<T>(method, path, body)
  const ctrl = new AbortController()
  const timer = setTimeout(() => ctrl.abort(), opts.timeout ?? DEFAULT_TIMEOUT)
  let res: Response
  try {
    res = await fetch(apiUrl(path), {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctrl.signal,
    })
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw new ApiError(-2, '请求超时，请重试')
    throw new ApiError(-1, '无法连接后端服务')
  } finally {
    clearTimeout(timer)
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`
    try {
      const data = await res.json()
      msg = formatApiDetail(data.detail, data.error, msg)
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
  get: <T>(path: string, opts?: RestOptions) => request<T>('GET', path, undefined, opts),
  post: <T>(path: string, body?: unknown, opts?: RestOptions) => request<T>('POST', path, body, opts),
  put: <T>(path: string, body?: unknown, opts?: RestOptions) => request<T>('PUT', path, body, opts),
  del: <T>(path: string, opts?: RestOptions) => request<T>('DELETE', path, undefined, opts),
}
