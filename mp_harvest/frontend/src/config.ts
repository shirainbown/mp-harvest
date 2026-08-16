// 运行时配置：全部从 URL query 读取（设计稿 §3.5 token 注入 / 开发期 ?api= 指向后端）
// 示例：
//   http://127.0.0.1:8765/?token=xxx                     生产（shell 注入）
//   http://localhost:5173/?token=xxx                     开发（走 vite proxy）
//   http://localhost:5173/?api=http://127.0.0.1:8765&token=xxx   开发（直连后端，绕过 proxy）
//   http://localhost:5173/?mock=1                        无后端演示（stores 用假数据）

const q = new URLSearchParams(location.search)

export const TOKEN = q.get('token') || ''
export const API_BASE = (q.get('api') || '').replace(/\/$/, '')
export const MOCK = q.get('mock') === '1'
export const APP_VERSION = 'v2.1.10'

export function apiUrl(path: string): string {
  const sep = path.includes('?') ? '&' : '?'
  return `${API_BASE}${path}${TOKEN ? `${sep}token=${encodeURIComponent(TOKEN)}` : ''}`
}

export function wsUrl(): string {
  const base = API_BASE || location.origin
  return `${base.replace(/^http/, 'ws')}/ws?token=${encodeURIComponent(TOKEN)}`
}
