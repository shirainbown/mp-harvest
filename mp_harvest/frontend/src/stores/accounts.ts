import { defineStore } from 'pinia'
import type { Account, CaStatus, DuplicateGroup, ImportItem, MitmStatus } from '../types'
import { call, rest, LONG_TIMEOUT } from '../api/rest'
import { copyText } from '../api/desktop'
import { useUiStore } from './ui'
import { MOCK } from '../config'

export const useAccountsStore = defineStore('accounts', {
  state: () => ({
    list: [] as Account[],
    mitm: { running: false, port: 8080 } as MitmStatus,
    ca: { trusted: false } as CaStatus,
    loading: false,
    loaded: false,
    /** 同一个公众号（__biz）被添加了多次的分组；空 = 没有重复（2026-09） */
    duplicates: [] as DuplicateGroup[],
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
      // 顺带看看有没有重复的公众号（同一个 __biz 两行）。**放在 load 里**：
      // 视图是 v-show 常驻的，onMounted 只跑一次 —— 删掉重复行之后切回本页
      // 不重新查的话，那个提示会一直挂在那儿（2026-09 踩过同一类坑）。
      await this.loadDuplicates()
    },
    /** 查重复公众号分组（同一个 __biz 被添加多次）。失败静默：这只是个提示。 */
    async loadDuplicates() {
      const r = await call(rest.get<{ groups: DuplicateGroup[] }>('/api/accounts/duplicates'))
      if (r) this.duplicates = r.groups || []
    },
    /** 合并重复行：保留 keepId，把 dropIds 的文章并进去再删掉它们。
     *  返回是否成功；调用方负责提示与刷新。 */
    async mergeDuplicates(keepId: string, dropIds: string[]): Promise<boolean> {
      const r = await call(
        rest.post<{ added_articles: number; total: number }>('/api/accounts/merge-duplicates', {
          keep_id: keepId,
          drop_ids: dropIds,
        }),
      )
      if (r === null) return false
      await this.load()
      return true
    },
    /** 添加公众号并抓包；返回 account（调用方负责 90s 等待逻辑）。
     *  后端 best-effort 启动 MITM 失败时会在 mitm_message 给出原因（账号仍已添加） */
    async add(name: string, url: string) {
      const acct = await call(rest.post<Account>('/api/accounts', { name, url }))
      if (acct) {
        acct.pending = true
        this.list.push(acct)
        if (acct.mitm_message) {
          useUiStore().error(`${acct.mitm_message}`) // 原因由后端文案给出（可能是 CA 未信任、端口占用等），不再硬编码
        }
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
    /** 续约：标记等待抓包，用户在微信内刷新文章后 WS 回写（§5.4）。
     *  pending 账号同样可续约（后端对 pending 保持可用），silent=true 时不弹成功提示 */
    async renew(a: Account, silent = false) {
      a.pending = true
      const r = await call(rest.post(`/api/accounts/${a.id}/renew`))
      if (r === null && !MOCK) a.pending = false
      else if (!silent) useUiStore().toast('已切换到等待抓包，请在微信内刷新文章')
    },
    async renewAll() {
      // 只挑「需要续约」的：等待抓包的、以及已过期的。
      // 不能把健康的 active 账号也一起置为 awaiting（2026-09 修复）：后端 renew
      // 会把 status 改成 awaiting，而导出的凭证校验要求 status=active —— 于是
      // 点一下「一键续约全部」，所有本来好好的账号都会导出失败，而界面上的
      // 有效期还没刷新，看起来像凭空的错。
      const now = Date.now()
      const targets = this.list.filter(
        (a) => a.pending || !a.expires_at || a.expires_at * 1000 <= now,
      )
      if (!targets.length) {
        useUiStore().toast('所有账号凭证都有效，无需续约')
        return
      }
      for (const a of targets) a.pending = true
      await Promise.all(targets.map((a) => call(rest.post(`/api/accounts/${a.id}/renew`))))
      useUiStore().toast(`已进入批量续约（${targets.length} 个）：请在微信内依次刷新各公众号文章`)
    },
    async copyCredential(a: Account) {
      if (!a.expires_at || a.expires_at * 1000 <= Date.now()) {
        useUiStore().error('凭证已过期，请先续约')
        return
      }
      const data = await call(rest.get<unknown>(`/api/accounts/${a.id}/credential`))
      if (data !== null) {
        // 看真实结果：剪贴板被拒时不能再弹「已复制」
        if (await copyText(JSON.stringify(data, null, 2))) {
          useUiStore().toast('凭证 JSON 已复制')
        } else {
          useUiStore().error('复制失败，请手动复制凭证')
        }
      }
    },
    async toggleMitm() {
      const next = !this.mitm.running
      const r = await call(
        rest.post<{ ok?: boolean; message?: string; mitm_message?: string; running?: boolean; port?: number }>(
          next ? '/api/mitm/start' : '/api/mitm/stop',
        ),
      )
      if (r) {
        if (r.running !== undefined) this.mitm = { running: r.running, port: r.port ?? this.mitm.port }
        const failMsg = r.mitm_message || (r.ok === false ? r.message || '代理操作失败' : '')
        if (failMsg) {
          // 系统代理被拒等原因：不自动消失，并指引修复路径
          useUiStore().error(`${failMsg}`) // 原因由后端文案给出（可能是 CA 未信任、端口占用等），不再硬编码
        } else {
          useUiStore().toast(next ? `MITM 代理已启动（127.0.0.1:${this.mitm.port}）` : 'MITM 代理已停止')
        }
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
    /** 用系统默认程序打开 CA 证书**文件本身**（唤起钥匙串/证书导入向导）。
     *  不是「打开所在目录」—— 那条路会暴露同目录下的 CA 私钥与凭证文件。 */
    async openCaFolder() {
      const r = await call(rest.post<{ ok: boolean; path?: string }>('/api/ca/open'))
      if (r) useUiStore().toast(`已打开证书文件：${r.path || '…'}`)
    },
    /** 批量导入两段式（§7.1）：预览 → 确认 */
    async importPreview(text: string): Promise<ImportItem[]> {
      const r = await call(rest.post<{ items: ImportItem[] }>('/api/accounts/import', { text }))
      return r?.items ?? []
    },
    async importConfirm(items: ImportItem[]) {
      const r = await call(
        rest.post<{ imported: number; skipped: number; mitm_message?: string }>(
          '/api/accounts/import',
          { stage: 'confirm', items },
          { timeout: LONG_TIMEOUT },
        ),
      )
      if (r) {
        if (r.mitm_message) {
          useUiStore().error(`${r.mitm_message}`) // 原因由后端文案给出（可能是 CA 未信任、端口占用等），不再硬编码
        }
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
