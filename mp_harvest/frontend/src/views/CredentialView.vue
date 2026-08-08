<script setup lang="ts">
// 页面一：凭证管理（§5.4）
import { ref } from 'vue'
import SButton from '../components/SButton.vue'
import SInput from '../components/SInput.vue'
import SPopover from '../components/SPopover.vue'
import STooltip from '../components/STooltip.vue'
import EmptyState from '../components/EmptyState.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import ImportDrawer from './ImportDrawer.vue'
import { useAccountsStore } from '../stores/accounts'
import { useUiStore } from '../stores/ui'
import { useTicker, fmtCountdown } from '../composables/useTicker'
import type { Account } from '../types'

const accounts = useAccountsStore()
const ui = useUiStore()
const now = useTicker()

// ---- 添加表单：回车提交、loading 等待抓包、90s 超时 ----
const name = ref('')
const url = ref('')
const formError = ref('')
const adding = ref(false)
let addTimer: ReturnType<typeof setTimeout> | null = null

async function submit() {
  if (adding.value) return
  formError.value = ''
  if (!/^https?:\/\/mp\.weixin\.qq\.com\//.test(url.value.trim())) {
    formError.value = '请填写有效的公众号文章链接'
    return
  }
  adding.value = true
  const acct = await accounts.add(name.value.trim(), url.value.trim())
  if (!acct) {
    adding.value = false
    return
  }
  name.value = ''
  url.value = ''
  // 等待 WS credential.captured 或 90s 超时
  const stop = accounts.$subscribe((_m, s) => {
    const cur = s.list.find((a) => a.id === acct.id)
    if (cur && !cur.pending && cur.expires_at) {
      done()
      stop()
    }
  })
  function done() {
    adding.value = false
    if (addTimer) clearTimeout(addTimer)
    addTimer = null
  }
  addTimer = setTimeout(() => {
    stop()
    done()
    ui.error('未捕获到凭证，请在微信内刷新该公众号文章')
  }, 90_000)
}

// ---- 倒计时（全局 ticker 驱动） ----
function remain(a: Account) {
  return a.expires_at ? a.expires_at - now.value : 0
}
function countdown(a: Account) {
  if (a.pending) return { text: '等待抓包…', cls: 'warn' }
  return fmtCountdown(remain(a))
}
function dotCls(a: Account) {
  if (a.pending) return 'yellow'
  const r = remain(a)
  if (r <= 0) return 'gray'
  return r < 300 ? 'yellow' : 'green'
}

// ---- 行内操作 ----
function openLink(a: Account) {
  window.open(a.url, '_blank', 'noopener')
}

const importOpen = ref(false)
</script>

<template>
  <section class="view-root">
  <header class="page-header">
    <h1>凭证管理</h1>
    <SButton size="sm" :disabled="!accounts.list.length" @click="accounts.renewAll()">⏻ 一键续约全部</SButton>
  </header>
  <div class="page-body">
    <!-- MITM 面板 -->
    <div class="panel mitm-panel" :class="{ warn: !accounts.ca.trusted }">
      <div class="panel-title">MITM 代理</div>
      <div class="mitm-row">
        <span style="display:inline-flex;align-items:center;gap:6px">
          <span class="dot" :class="accounts.mitm.running ? 'green' : 'gray'"></span>
          <span v-if="accounts.mitm.running">运行中 <span class="mono muted">127.0.0.1:{{ accounts.mitm.port }}</span></span>
          <span v-else>已停止</span>
        </span>
        <span class="muted">
          CA：<span v-if="accounts.ca.trusted" class="status-ok">✓ 已信任</span><span v-else class="status-fail">未信任</span>
        </span>
        <span style="flex:1"></span>
        <SButton size="sm" @click="accounts.toggleMitm()">{{ accounts.mitm.running ? '停止代理' : '启动代理' }}</SButton>
        <SButton v-if="!accounts.ca.trusted" size="sm" class="pulse" @click="accounts.installCa()">安装 CA 证书</SButton>
        <SButton size="sm" variant="ghost" @click="accounts.openCaFolder()">打开证书文件</SButton>
        <SPopover>
          <template #anchor>
            <span class="tertiary" style="cursor:help;border-bottom:1px dashed var(--text-tertiary)">抓包指引 ⓘ</span>
          </template>
          <div style="line-height:1.7">
            首次使用三步：<br />
            1. 点「安装 CA 证书」，输入管理员密码完成信任（仅此一次）；<br />
            2. 点「启动代理」；<br />
            3. 添加公众号后，在微信桌面内刷新该公众号已打开的文章，即可自动捕获凭证（30 分钟有效）。
          </div>
        </SPopover>
      </div>
    </div>

    <!-- 添加表单 -->
    <div class="panel">
      <div class="panel-title">添加公众号</div>
      <div class="mitm-row">
        <span class="form-label">名称</span>
        <SInput v-model="name" placeholder="可留空（默认未命名公众号）" width="200px" :disabled="adding" @enter="submit" />
        <span class="form-label">文章链接</span>
        <SInput
          v-model="url"
          placeholder="https://mp.weixin.qq.com/s/…"
          :error="formError"
          :disabled="adding"
          style="flex:1;min-width:220px"
          @enter="submit"
        />
        <SButton variant="primary" :loading="adding" @click="submit">{{ adding ? '等待抓包…' : '添加并抓包' }}</SButton>
        <SButton @click="importOpen = true">批量导入 ▸</SButton>
      </div>
    </div>

    <!-- 凭证表格 -->
    <div class="toolbar" style="padding:0 2px">
      <span class="muted">已添加 <b style="color:var(--text-primary)">{{ accounts.list.length }}</b></span>
    </div>
    <div class="acct-table">
      <div class="acct-head"><span>状态</span><span>名称</span><span>__biz</span><span>链接</span><span></span></div>
      <SkeletonRows v-if="accounts.loading && !accounts.loaded" :rows="4" />
      <EmptyState v-else-if="!accounts.list.length" text="先添加公众号并抓包" />
      <div v-for="a in accounts.list" :key="a.id" class="acct-row">
        <span style="display:flex;align-items:center;gap:6px">
          <span class="dot" :class="dotCls(a)"></span>
          <span class="countdown" :class="countdown(a).cls">{{ countdown(a).text }}</span>
        </span>
        <STooltip :text="a.name" style="min-width:0"><span class="acct-name">{{ a.name }}</span></STooltip>
        <span class="acct-biz mono">{{ a.__biz ? a.__biz.slice(0, 6) + '…' : '—' }}</span>
        <STooltip :text="a.url" style="min-width:0">
          <span class="acct-biz mono">{{ a.url.replace(/^https?:\/\//, '').slice(0, 22) }}…</span>
        </STooltip>
        <span class="row-actions">
          <SButton size="sm" variant="ghost" @click="accounts.copyCredential(a)">复制</SButton>
          <SButton size="sm" variant="ghost" :disabled="a.pending" @click="accounts.renew(a)">续约</SButton>
          <SButton size="sm" variant="ghost" @click="openLink(a)">打开</SButton>
          <SPopover>
            <template #anchor><SButton size="sm" variant="danger">删除</SButton></template>
            <template #default="{ close }">
              <div style="margin-bottom:8px">确认删除「{{ a.name }}」？<br /><span class="tertiary">凭证与历史配置将一并移除。</span></div>
              <div style="display:flex;justify-content:flex-end;gap:8px">
                <SButton size="sm" @click="close()">取消</SButton>
                <SButton size="sm" variant="danger" @click="close(); accounts.remove(a)">删除</SButton>
              </div>
            </template>
          </SPopover>
        </span>
      </div>
    </div>
  </div>

  <ImportDrawer v-model:open="importOpen" />
  </section>
</template>
