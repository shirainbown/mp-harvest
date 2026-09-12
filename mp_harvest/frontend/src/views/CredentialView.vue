<script setup lang="ts">
// 页面一：凭证管理（§5.4）
import { ref } from 'vue'
import SButton from '../components/SButton.vue'
import SIcon from '../components/SIcon.vue'
import SInput from '../components/SInput.vue'
import SPopover from '../components/SPopover.vue'
import STooltip from '../components/STooltip.vue'
import EmptyState from '../components/EmptyState.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import SModal from '../components/SModal.vue'
import ImportDrawer from './ImportDrawer.vue'
import { openExternal } from '../api/desktop'
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
let addStop: (() => void) | null = null

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
  addStop = stop
  function done() {
    adding.value = false
    addStop = null
    if (addTimer) clearTimeout(addTimer)
    addTimer = null
  }
  addTimer = setTimeout(async () => {
    stop()
    done()
    // 超时复位：renew 对 pending 账号可用，重置为等待抓包并提示重试
    await accounts.renew(acct, true)
    ui.error('未捕获到凭证，已重置为等待抓包：请在微信内刷新文章后重试')
  }, 90_000)
}

/** 等待抓包期间可取消：解除输入禁用（账号保留在列表中，可续约或删除） */
function cancelAdd() {
  if (addStop) addStop()
  addStop = null
  if (addTimer) clearTimeout(addTimer)
  addTimer = null
  adding.value = false
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
async function openLink(a: Account) {
  // 必须走 shell 的 open_external：window.open(_blank) 在 pywebview 里是静默空操作
  if (!(await openExternal(a.url))) ui.error('打开失败：该公众号没有可用的文章链接')
}

const importOpen = ref(false)

// ---- 重复公众号合并（2026-09）----
//
// 同一个 __biz 被加了两行（批量导入按名称去重、不按公众号；短链里没有 __biz
// 可判）。后果是同一篇文章在两个账号下各存一份：列表里成对出现、筛选跑两遍、
// 正文拉两遍。合并**保留哪一行由用户选** —— 名字是用户可见的，哪个名字才对
// 只有他知道（后端的 article_count 只用来排序给个默认）。
const mergeOpen = ref(false)
/** 每组选中的保留行；没选过的用该组第一个（文章最多的那行） */
const mergeKeep = ref<Record<string, string>>({})
const merging = ref(false)

function keepIdOf(group: { biz: string; accounts: { id: string }[] }): string {
  return mergeKeep.value[group.biz] || group.accounts[0]?.id || ''
}

function openMerge() {
  mergeKeep.value = {}
  mergeOpen.value = true
}

async function doMerge() {
  const groups = accounts.duplicates
  if (!groups.length || merging.value) return
  merging.value = true
  let mergedCount = 0
  try {
    for (const g of groups) {
      const keepId = keepIdOf(g)
      const dropIds = g.accounts.map((a) => a.id).filter((id) => id !== keepId)
      if (!dropIds.length) continue
      if (!(await accounts.mergeDuplicates(keepId, dropIds))) {
        ui.error('合并失败，已停止（前面的分组可能已经合并）')
        return
      }
      mergedCount += dropIds.length
    }
    ui.toast(`已合并 ${mergedCount} 个重复的公众号行`)
    mergeOpen.value = false
  } finally {
    merging.value = false
  }
}
</script>

<template>
  <section class="view-root">
  <header class="page-header">
    <h1>凭证管理</h1>
    <SButton size="sm" :disabled="!accounts.list.length" @click="accounts.renewAll()"><SIcon name="refresh" /> 一键续约全部</SButton>
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
          CA：<span v-if="accounts.ca.trusted" class="status-ok"><SIcon name="check" :size="12" /> 已信任</span><span v-else class="status-fail">未信任</span>
        </span>
        <span style="flex:1"></span>
        <SButton size="sm" @click="accounts.toggleMitm()">{{ accounts.mitm.running ? '停止代理' : '启动代理' }}</SButton>
        <SButton v-if="!accounts.ca.trusted" size="sm" class="pulse" @click="accounts.installCa()">安装 CA 证书</SButton>
        <SButton size="sm" variant="ghost" @click="accounts.openCaFolder()">打开证书文件</SButton>
        <SPopover>
          <template #anchor>
            <span class="tertiary" style="cursor:help;border-bottom:1px dashed var(--text-tertiary)">抓包指引 <SIcon name="info" :size="12" /></span>
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
        <SButton v-if="adding" @click="cancelAdd">取消</SButton>
        <SButton @click="importOpen = true">批量导入 <SIcon name="chevron-right" :size="12" /></SButton>
      </div>
    </div>

    <!-- 凭证表格 -->
    <div class="toolbar" style="padding:0 2px">
      <span class="muted">已添加 <b style="color:var(--text-primary)">{{ accounts.list.length }}</b></span>
      <!-- 只在真有重复时出现。平时不显示，免得让人以为列表有问题 -->
      <span v-if="accounts.duplicates.length" class="spacer"></span>
      <SButton v-if="accounts.duplicates.length" size="sm" variant="ghost" @click="openMerge">
        <SIcon name="info" :size="12" /> {{ accounts.duplicates.length }} 组重复公众号
      </SButton>
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
          <SButton size="sm" variant="ghost" @click="accounts.renew(a)">续约</SButton>
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

  <!-- 合并重复公众号：每组选一个「保留哪个名字」，其余行的文章并过来后删掉。
       ⚠️ 提醒写清楚「删掉的行连同它的判定一起没了」——文章是并集不会丢，
       但那一行的名字和状态会消失。 -->
  <SModal :open="mergeOpen" @close="mergeOpen = false">
    <template #head>合并重复公众号</template>
    <div style="display:flex;flex-direction:column;gap:var(--sp-2)">
      <span class="muted" style="font-size:var(--fs-sm)">
        下面这些分组里，每一组其实是<b>同一个公众号</b>（<span class="mono">__biz</span> 相同）
        被添加了两次。合并后文章按篇去重取并集，不会丢；被合并掉的那一行会消失，
        请选你要保留的名字。
      </span>
      <div v-for="g in accounts.duplicates" :key="g.biz"
           style="border:1px solid var(--border);border-radius:var(--radius-md);padding:var(--sp-2)">
        <div class="muted mono" style="font-size:var(--fs-xs);margin-bottom:4px">{{ g.biz }}</div>
        <div v-for="a in g.accounts" :key="a.id" class="radio-row" @click="mergeKeep[g.biz] = a.id">
          <span class="radio" :class="{ on: keepIdOf(g) === a.id }"></span>
          <span>{{ a.name }}</span>
          <span class="tertiary" style="font-size:var(--fs-xs)">
            {{ a.article_count }} 篇{{ keepIdOf(g) === a.id ? ' · 保留' : ' · 并入后删除' }}
          </span>
        </div>
      </div>
    </div>
    <template #foot>
      <SButton variant="ghost" @click="mergeOpen = false">取消</SButton>
      <SButton variant="primary" :loading="merging" @click="doMerge">合并</SButton>
    </template>
  </SModal>
  </section>
</template>
