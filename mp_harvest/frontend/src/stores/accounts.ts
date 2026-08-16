import { defineStore } from 'pinia'
import type { Account, CaStatus, ImportItem, MitmStatus } from '../types'
import { call, rest } from '../api/rest'
import { useUiStore } from './ui'
import { MOCK } from '../config'

export const useAccountsStore = defineStore('accounts', {
  state: () => ({
    list: [] as Account[],
    mitm: { running: false, port: 8080 } as MitmStatus,
    ca: { trusted: false } as CaStatus,
    loading: false,
    loaded: false,
  }),
  getters: {
    valid: (s) => s.list.filter((a) => a.expires_at && a.expires_at * 1000 > Date.now()),
  },
  actions: {
    async load() {
      this.loading = true
      const [accounts, mitm, ca] = await Promise.all([
        call(rest.get<Account[]>('/api/accounts')),
        call(rest.get<MitmStatus>('/api/mitm/status')),
        call(rest.get<CaStatus>('/api/ca/status')),
      ])
      if (accounts) this.list = accounts
      if (mitm) this.mitm = mitm
      if (ca) this.ca = ca
      this.loading = false
      this.loaded = true
    },
    /** 添加公众号并抓包；返回 account（调用方负责 90s 等待逻辑） */
    async add(name: string, url: string) {
      const acct = await call(rest.post<Account>('/api/accounts', { name, url }))
      if (acct) {
        acct.pending = true
        this.list.push(acct)
      }
      return acct
    },
    async remove(a: Account) {
      const ok = await call(rest.del(`/api/accounts/${a.id}`))
      if (ok !== null) {
        this.list = this.list.filter((x) => x.id !== a.id)
        useUiStore().toast(`已删除「${a.name}」`)
      }
    },
    /** 续约：标记等待抓包，用户在微信内刷新文章后 WS 回写（§5.4） */
    async renew(a: Account) {
      a.pending = true
      const r = await call(rest.post(`/api/accounts/${a.id}/renew`))
      if (r === null && !MOCK) a.pending = false
      else useUiStore().toast('已切换到等待抓包，请在微信内刷新文章')
    },
    async renewAll() {
      const targets = this.list.filter((a) => !a.pending)
      for (const a of targets) a.pending = true
      await Promise.all(targets.map((a) => call(rest.post(`/api/accounts/${a.id}/renew`))))
      useUiStore().toast('已进入批量续约：请在微信内依次刷新各公众号文章')
    },
    async copyCredential(a: Account) {
      if (!a.expires_at || a.expires_at * 1000 <= Date.now()) {
        useUiStore().error('凭证已过期，请先续约')
        return
      }
      const data = await call(rest.get<unknown>(`/api/accounts/${a.id}/credential`))
      if (data !== null) {
        await navigator.clipboard.writeText(JSON.stringify(data, null, 2))
        useUiStore().toast('凭证 JSON 已复制')
      }
    },
    async toggleMitm() {
      const next = !this.mitm.running
      const r = await call(rest.post<MitmStatus>(next ? '/api/mitm/start' : '/api/mitm/stop'))
      if (r) {
        this.mitm = r
        useUiStore().toast(next ? `MITM 代理已启动（127.0.0.1:${r.port}）` : 'MITM 代理已停止')
      }
    },
    async installCa() {
      const r = await call(rest.post<{ ok: boolean; message: string; needs_admin: boolean }>('/api/ca/install'))
      if (r) {
        if (r.ok) {
          useUiStore().toast(r.message || 'CA 证书已安装并信任')
        } else {
          useUiStore().error(r.message || 'CA 证书安装失败')
        }
        const ca = await call(rest.get<CaStatus>('/api/ca/status'))
        if (ca) this.ca = ca
      }
    },
    /** 在 Finder 中打开 CA 证书所在目录（2026-08-09 补后端端点） */
    async openCaFolder() {
      const r = await call(rest.post<{ ok: boolean; path?: string }>('/api/ca/open'))
      if (r) useUiStore().toast(`已打开证书目录：${r.path || '…'}`)
    },
    /** 批量导入两段式（§7.1）：预览 → 确认 */
    async importPreview(text: string): Promise<ImportItem[]> {
      const r = await call(rest.post<{ items: ImportItem[] }>('/api/accounts/import', { text }))
      return r?.items ?? []
    },
    async importConfirm(items: ImportItem[]) {
      const r = await call(rest.post<{ imported: number; skipped: number }>('/api/accounts/import', { stage: 'confirm', items }))
      if (r) {
        useUiStore().toast(`已导入 ${r.imported} 条${r.skipped ? `（跳过 ${r.skipped} 条重复）` : ''}`)
        await this.load()
      }
      return r
    },
    // ---- WS 事件 ----
    onCaptured(account_id: string, expires_at: number) {
      const a = this.list.find((x) => x.id === account_id)
      if (a) {
        a.expires_at = expires_at
        a.pending = false
        useUiStore().toast(`已捕获「${a.name}」凭证并绑定（有效期 30 分钟）`)
      }
    },
    onExpired(account_id: string) {
      const a = this.list.find((x) => x.id === account_id)
      if (a) a.expires_at = null
    },
  },
})
