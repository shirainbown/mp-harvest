// Mock 后端：URL 加 ?mock=1 时接管全部 REST，并用 setTimeout 模拟 §7.2 WS 事件。
// 仅用于后端未就绪时的页面演示，真实模式不加载任何 mock 逻辑。
import type {
  Account,
  AiModel,
  Article,
  CaStatus,
  ImportItem,
  MitmStatus,
  NetworkSettings,
  PlatformInfo,
  UpdateCheckResult,
} from '../types'
import { dispatchWsEvent } from '../api/ws'

const now = () => Math.floor(Date.now() / 1000)

// ---------- 假数据 ----------
const accounts: Account[] = [
  { id: 'a1', name: '互联网周刊', url: 'https://mp.weixin.qq.com/s/8fK2demo', __biz: 'aXJvc2RlbW8x', expires_at: now() + 1781 },
  { id: 'a2', name: '芯片那些事', url: 'https://mp.weixin.qq.com/s/Q7d1demo', __biz: 'bWl0bXhkZW1v', expires_at: now() + 723 },
  { id: 'a3', name: '半导体行业观察', url: 'https://mp.weixin.qq.com/s/Zm3wdemo', __biz: 'c2VtaWRlbW8z', expires_at: now() + 214 },
  { id: 'a4', name: '前端早读课', url: 'https://mp.weixin.qq.com/s/1pN8demo', __biz: 'd2luZG9kZW1v', expires_at: null },
  { id: 'a5', name: '机器之心', url: 'https://mp.weixin.qq.com/s/T5rRdemo', __biz: 'bWFjaGluZGVtbw', expires_at: now() + 1599 },
  { id: 'a6', name: '晚点 LatePost', url: 'https://mp.weixin.qq.com/s/W9qQdemo', __biz: 'bGF0ZXBvc3RkZW1v', expires_at: null },
]

const SAMPLE: Array<[string, string, Article['source'], 'keep' | 'drop']> = [
  ['国产 EDA 的突围之路：从点工具到全流程', '行业深度分析，信息增量高', 'M', 'keep'],
  ['周三例行产品发布会会议纪要', '例行纪要，信息密度低', 'G', 'drop'],
  ['RISC-V 生态 2026 半年报解读', '技术趋势长文，数据详实', 'M', 'keep'],
  ['【招聘】芯片验证工程师（base 上海）', '招聘启事，予以过滤', 'G', 'drop'],
  ['先进封装：Chiplet 时代的必争之地', '技术深度好文', 'G', 'keep'],
  ['限时优惠！这门课改变了我的人生', '软文标题党，正文信息量低', 'G', 'drop'],
  ['台积电 2nm 良率爬坡的幕后', '独家供应链信息', 'M', 'keep'],
  ['大模型推理成本一年下降 90% 之后', '趋势判断有数据支撑', 'G', 'keep'],
  ['公司团建精彩瞬间回顾', '与领域无关', 'G', 'drop'],
  ['从 MITM 到凭证管理：公众号采集的工程实践', '工程方法论，可复用', '补', 'keep'],
  ['AI 芯片国产化替代的三种路径', '行业格局分析清晰', 'G', 'keep'],
  ['本周值得关注的新品发布会预告', '活动预告，予以过滤', 'G', 'drop'],
  ['存算一体架构的十年演进', '技术综述，引用扎实', 'G', 'keep'],
  ['深度｜HBM4 供应链格局重构', '深度报道，信息增量高', 'M', 'keep'],
  ['某明星同款穿搭盘点', '泛娱乐内容，予以过滤', 'G', 'drop'],
]

const articles: Article[] = []
{
  // 生成 620 条以演示 >500 虚拟滚动
  const day = 24 * 3600
  for (let i = 0; i < 620; i++) {
    const [title, reason, source, verdict] = SAMPLE[i % SAMPLE.length]
    const d = new Date((now() - Math.floor(i / 8) * day - 3600) * 1000)
    articles.push({
      id: `art${i + 1}`,
      account_id: 'a1',
      title: i < SAMPLE.length ? title : `${title}（第 ${Math.floor(i / SAMPLE.length) + 1} 期）`,
      url: `https://mp.weixin.qq.com/s/demo${i}`,
      date: d.toISOString(),
      source,
      verdict,
      reason,
    })
  }
}

let mitm: MitmStatus = { running: true, port: 8080 }
const ca: CaStatus = { trusted: true }
const models: AiModel[] = [
  { id: 'm1', enabled: true, base_url: 'https://api.openai.com', api_key: 'sk-proj-abc123def456', format: 'openai', model: 'gpt-5-mini' },
  { id: 'm2', enabled: true, base_url: 'https://api.anthropic.com', api_key: 'sk-ant-xyz789', format: 'anthropic', model: 'claude-sonnet-4.5' },
]
const DEFAULT_PRINCIPLES = `请判断以下公众号文章标题是否值得阅读。

值得保留（keep=true）的标准：
- 行业深度分析、技术趋势长文
- 有信息增量的独家报道
- 与半导体 / AI / 互联网产品相关的硬核内容

应当过滤（keep=false）的标准：
- 例行会议纪要、活动预告、招聘启事
- 标题党但正文信息量低的软文
- 与上述领域无关的泛娱乐内容`
let principles = DEFAULT_PRINCIPLES
const settings: NetworkSettings = { mode: 'direct', proxy_url: '' }
const platform: PlatformInfo = {
  os: 'mac',
  os_version: 'macOS 15',
  ca_needs_admin: true,
  proxy_needs_admin: true,
  data_dir: '~/Library/Application Support/MP Harvest/data/',
  engine: 'WKWebView',
  version: 'v2.0.0',
}

// ---------- 工具 ----------
const delay = (ms: number) => new Promise((r) => setTimeout(r, ms))
let taskSeq = 0

/** 模拟一个后台任务：持续推 progress，最后推 done */
function simulateTask(kind: string, steps: string[], stepMs = 450, result?: unknown): string {
  const task_id = `mock-task-${++taskSeq}`
  let i = 0
  const timer = setInterval(() => {
    i++
    if (i >= steps.length) {
      clearInterval(timer)
      dispatchWsEvent({ type: 'task.progress', task_id, percent: 100, message: steps[steps.length - 1] })
      dispatchWsEvent({ type: 'task.done', task_id, result })
    } else {
      dispatchWsEvent({ type: 'task.progress', task_id, percent: Math.round((i / steps.length) * 100), message: steps[i - 1] })
    }
  }, stepMs)
  void kind
  return task_id
}

function parseImport(text: string): ImportItem[] {
  const seen = new Set<string>()
  const items: ImportItem[] = []
  for (const line of text.split(/\r?\n/)) {
    const m = line.trim().match(/^(.*?)[\s,，]+(https?:\/\/\S+)$/) || line.trim().match(/^(https?:\/\/\S+)$/)
    if (!m) continue
    const url = (m.length === 3 ? m[2] : m[1]).trim()
    const name = (m.length === 3 ? m[1].trim() : '') || url
    const dup = seen.has(url) || accounts.some((a) => a.url === url)
    seen.add(url)
    items.push({ name, url, dup })
  }
  return items
}

// ---------- REST mock 路由 ----------
export async function mockHandle<T>(method: string, path: string, body?: unknown): Promise<T> {
  await delay(120) // 模拟网络延迟
  const p = path.split('?')[0]
  const b = (body ?? {}) as Record<string, unknown>

  // accounts
  if (p === '/api/accounts' && method === 'GET') return accounts as T
  if (p === '/api/accounts' && method === 'POST') {
    const acct: Account = {
      id: `a${Date.now()}`,
      name: String(b.name || '未命名'),
      url: String(b.url || ''),
      __biz: undefined,
      expires_at: null,
      pending: true,
    }
    accounts.push(acct)
    // 2.2s 后模拟抓到凭证
    setTimeout(() => {
      acct.__biz = 'bW9ja2JpeGRlbW8'
      dispatchWsEvent({ type: 'credential.captured', account_id: acct.id, expires_at: now() + 1800 })
    }, 2200)
    return acct as T
  }
  if (p === '/api/accounts/import' && method === 'POST') {
    if (b.stage === 'confirm') {
      const items = (b.items as ImportItem[]) || []
      let n = 0
      for (const it of items) {
        if (it.dup) continue
        accounts.push({ id: `a${Date.now()}${n}`, name: it.name, url: it.url, expires_at: null })
        n++
      }
      return { imported: n, skipped: items.length - n } as T
    }
    return { items: parseImport(String(b.text || '')) } as T
  }
  const renewMatch = p.match(/^\/api\/accounts\/([^/]+)\/renew$/)
  if (renewMatch && method === 'POST') {
    const a = accounts.find((x) => x.id === renewMatch[1])
    if (a) {
      a.pending = true
      setTimeout(() => {
        dispatchWsEvent({ type: 'credential.captured', account_id: a.id, expires_at: now() + 1800 })
      }, 2500)
    }
    return { ok: true } as T
  }
  const acctMatch = p.match(/^\/api\/accounts\/([^/]+)(\/credential)?$/)
  if (acctMatch && method === 'DELETE') {
    const i = accounts.findIndex((a) => a.id === acctMatch[1])
    if (i >= 0) accounts.splice(i, 1)
    return { ok: true } as T
  }
  if (acctMatch && acctMatch[2] && method === 'GET') {
    const a = accounts.find((x) => x.id === acctMatch[1])
    return { __biz: a?.__biz, url: a?.url, expires_at: a?.expires_at, credential: { mock: true } } as T
  }

  // mitm / ca
  if (p === '/api/mitm/start' && method === 'POST') {
    mitm = { running: true, port: 8080 }
    dispatchWsEvent({ type: 'mitm.status', ...mitm })
    return mitm as T
  }
  if (p === '/api/mitm/stop' && method === 'POST') {
    mitm = { running: false, port: 8080 }
    dispatchWsEvent({ type: 'mitm.status', ...mitm })
    return mitm as T
  }
  if (p === '/api/mitm/status' && method === 'GET') return mitm as T
  if (p === '/api/ca/status' && method === 'GET') return ca as T
  if (p === '/api/ca/install' && method === 'POST') return { needs_admin: true, ok: true } as T

  // history / articles
  if (p === '/api/history/fetch' && method === 'POST') {
    const task_id = simulateTask('history', ['第 1 页 / 已获 24 篇', '第 2 页 / 已获 51 篇', '第 3 页 / 已获 87 篇', '第 4 页 / 已获 112 篇'], 600, { added: 12, total: articles.length })
    return { task_id } as T
  }
  if (p === '/api/articles' && method === 'GET') return articles as T
  if (p === '/api/articles/supplement' && method === 'POST') {
    const art: Article = {
      id: `art${Date.now()}`,
      account_id: String(b.account_id || 'a1'),
      title: `补录文章 ${new URL(String(b.url || 'https://mp.weixin.qq.com/s/x')).pathname.slice(-4)}`,
      url: String(b.url || ''),
      date: new Date().toISOString(),
      source: '补',
      verdict: null,
      reason: '',
    }
    articles.unshift(art)
    return art as T
  }
  if (p === '/api/articles/export-list' && method === 'GET') return '# 文章列表（mock 导出内容）\n' as T
  if (p === '/api/articles/export-html' && method === 'POST') {
    const ids = (b.ids as string[]) || []
    const task_id = simulateTask('export', ids.length ? ids.map((_, i) => `${i + 1}/${ids.length}`) : ['12/87', '45/87', '87/87'], 250, { dir: 'exports/互联网周刊/', count: ids.length || 87 })
    return { task_id } as T
  }

  // ai
  if (p === '/api/ai/filter' && method === 'POST') {
    const task_id = simulateTask('ai', ['模型 A 34/620', '模型 A 128/620 · 模型 B 96/620', '模型 A 300/620 · 模型 B 260/620', '模型 A 620/620 ✓ · 模型 B 620/620 ✓'], 700, { keep: 280, drop: 340, cached: 203 })
    return { task_id } as T
  }
  if (p === '/api/ai/models' && method === 'GET') return { models } as T
  if (p === '/api/ai/models' && method === 'PUT') {
    const list = (Array.isArray(b) ? b : (b as { models?: AiModel[] })?.models) || []
    models.splice(0, models.length, ...list)
    return { ok: true } as T
  }
  if (p === '/api/ai/models/test' && method === 'POST') {
    await delay(1000)
    if (String(b.base_url || '').includes('anthropic')) return { ok: false, error: 'HTTP 401：authentication_error · invalid x-api-key' } as T
    return { ok: true, latency_ms: 212 } as T
  }
  if (p === '/api/ai/models/fetch' && method === 'POST') {
    await delay(700)
    if (String(b.base_url || '').includes('bad')) return { ok: false, message: 'HTTP 401：API Key 无效', models: [] } as T
    if (String(b.format || '') === 'anthropic') return { ok: false, message: 'Anthropic 接口不支持拉取模型列表，请手动填写模型名', models: [] } as T
    return { ok: true, models: ['deepseek-chat', 'deepseek-reasoner', 'gpt-4o'], message: '' } as T
  }
  if (p === '/api/ai/principles' && method === 'GET') return { text: principles, default: DEFAULT_PRINCIPLES } as T
  if (p === '/api/ai/principles' && method === 'PUT') {
    principles = String(b.text ?? principles)
    return { ok: true } as T
  }

  // settings / platform / update / tasks
  if (p === '/api/settings' && method === 'GET') return { settings } as T
  if (p === '/api/settings' && method === 'PUT') {
    Object.assign(settings, b)
    return { ok: true } as T
  }
  if (p === '/api/settings/test-proxy' && method === 'POST') {
    await delay(800)
    return { ok: true, latency_ms: 189 } as T
  }
  if (p === '/api/platform' && method === 'GET') return platform as T
  if (p === '/api/update/check' && method === 'GET') {
    await delay(900)
    return {
      ok: true,
      available: true,
      version: 'v2.1.0',
      current_version: '2.0.0',
      zip_url: 'https://github.com/shirainbown/mp-harvest/releases/download/v2.1.0/mp-harvest-v2.1.0.zip',
      notes: '#### 更新内容\n\n- 新增 macOS 支持（签名公证版 DMG）\n- 正文导出收敛为 HTML，支持图片本地化\n- 文章列表虚拟滚动，500+ 条流畅不卡\n- 修复 AI 筛选缓存在跨公众号时偶发串键',
    } as UpdateCheckResult as T
  }
  if (p === '/api/update/download' && method === 'POST') {
    const task_id = simulateTask('update', ['12%', '37%', '62%', '88%'], 500, { ready: true })
    return { task_id } as T
  }
  if (p === '/api/update/apply' && method === 'POST') return { ok: true } as T
  if (p.startsWith('/api/tasks/') && p.endsWith('/cancel') && method === 'POST') return { ok: true } as T

  throw new Error(`mock: 未实现的端点 ${method} ${p}`)
}
