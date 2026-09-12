<script setup lang="ts">
// 页面：周报生成 —— 选区间与来源 → AI 打分选题 → 按模板渲染 → 归档原文。
//
// 设计要点（2026-09）：
// - **模板是唯一真相**：界面上的「预览」用后端渲染，改模板立刻能看效果，不调用 AI。
// - **提示词可人工编辑**，但输出 JSON 格式由后端固定拼接，改坏标准不会改崩解析。
//   改了哪段，哪段的缓存自动失效（界面会明确提示）。
import { computed, onMounted, ref } from 'vue'
import SButton from '../components/SButton.vue'
import SInput from '../components/SInput.vue'
import SIcon from '../components/SIcon.vue'
import SkeletonRows from '../components/SkeletonRows.vue'
import EmptyState from '../components/EmptyState.vue'
import ProgressInline from '../components/ProgressInline.vue'
import SegmentedControl from '../components/SegmentedControl.vue'
import { useWeeklyStore } from '../stores/weekly'
import { useAccountsStore } from '../stores/accounts'
import { useExternalStore } from '../stores/external'
import { useTasksStore } from '../stores/tasks'
import { useUiStore } from '../stores/ui'
import { chooseDirectory, chooseFile, openLocalPath } from '../api/desktop'

const weekly = useWeeklyStore()
const accounts = useAccountsStore()
const ext = useExternalStore()
const tasks = useTasksStore()
const ui = useUiStore()

onMounted(async () => {
  if (!accounts.loaded) await accounts.load()
  if (!ext.sources.length) await ext.loadAll()
  if (!weekly.preview) await weekly.loadAll()
})

// ---- 来源勾选（不勾 = 全部）----
const allAccounts = computed(() => weekly.accountIds.size === 0)
const allSources = computed(() => weekly.sourceIds.size === 0)

function toggleAccount(id: string, on: boolean) {
  if (on) weekly.accountIds.add(id)
  else weekly.accountIds.delete(id)
  void weekly.loadPreview()
}
function toggleSource(id: string, on: boolean) {
  if (on) weekly.sourceIds.add(id)
  else weekly.sourceIds.delete(id)
  void weekly.loadPreview()
}
function selectAllAccounts() {
  weekly.accountIds.clear()
  void weekly.loadPreview()
}
function selectAllSources() {
  weekly.sourceIds.clear()
  void weekly.loadPreview()
}

// ---- 目录 / 模板文件 ----
async function pickOutDir() {
  const { path, reason } = await chooseDirectory()
  if (reason) {
    ui.error(reason)
    return
  }
  if (path) weekly.outDir = path
}
async function pickTemplate() {
  const { path, reason } = await chooseFile('html')
  if (reason) {
    ui.error(reason)
    return
  }
  if (path) weekly.templatePath = path
}
function useBuiltinTemplate() {
  weekly.templatePath = ''
}

// ---- 提示词展开状态 ----
const openPrompt = ref<string>('')
function togglePrompt(k: string) {
  openPrompt.value = openPrompt.value === k ? '' : k
}
const promptKeys = computed(() => Object.keys(weekly.prompts))

const genTask = computed(() => (weekly.genTaskId ? tasks.tasks[weekly.genTaskId] : null))
const renderTask = computed(() => (weekly.renderTaskId ? tasks.tasks[weekly.renderTaskId] : null))
const busy = computed(() => !!weekly.genTaskId || !!weekly.renderTaskId)

/** 打开本地文件/目录（往期报告、期目录、上次输出）—— 走后端 shell_open，
 *  不能走 openExternal('file://')：shell 侧只放行 http(s)，file:// 会被静默挡下。 */
async function openPath(p: string) {
  if (!p) {
    ui.error('没有可打开的路径')
    return
  }
  const { ok, reason } = await openLocalPath(p)
  if (!ok) ui.error(reason || '打开失败')
}
</script>

<template>
  <section class="view-root">
    <header class="page-header">
      <h1>周报生成</h1>
      <span class="muted" style="font-size:var(--fs-sm)">
        选日期区间与来源 → AI 打分选题 → 按模板渲染 → 连同原文一起归档
      </span>
    </header>
    <div class="page-body">
      <SkeletonRows v-if="!weekly.preview && weekly.loadingPreview" :rows="6" />

      <template v-else>
        <!-- 本期设置 -->
        <div class="panel">
          <div class="panel-title">本期设置</div>
          <div class="mitm-row">
            <span class="form-label">期号</span>
            <input v-model.number="weekly.issueNum" type="number" min="1" class="input" style="width:76px" />
            <span class="tertiary" style="font-size:var(--fs-xs)">第 N 期（按输出目录里的往期自动推算，可改）</span>
          </div>
          <div class="mitm-row" style="margin-top:var(--sp-2)">
            <span class="form-label">日期区间</span>
            <input v-model="weekly.fromDate" type="date" class="input" style="width:140px"
                   @change="weekly.loadPreview()" />
            <span class="tertiary">至</span>
            <input v-model="weekly.toDate" type="date" class="input" style="width:140px"
                   @change="weekly.loadPreview()" />
            <span class="form-label">精选篇数</span>
            <input v-model.number="weekly.selectedCount" type="number" min="1" max="100"
                   class="input" style="width:70px" />
          </div>
          <div class="mitm-row" style="margin-top:var(--sp-2)">
            <span class="form-label">输出目录</span>
            <SInput v-model="weekly.outDir" mono placeholder="留空 = 设置里的默认周报目录"
                    style="flex:1;min-width:240px" />
            <SButton size="sm" @click="pickOutDir">选择目录…</SButton>
          </div>
          <div class="mitm-row" style="margin-top:var(--sp-2)">
            <span class="form-label">报告标题</span>
            <SInput v-model="weekly.reportTitle" placeholder="留空用设置里的默认标题"
                    style="flex:1;min-width:240px" />
          </div>
        </div>

        <!-- 候选与来源 -->
        <div class="panel">
          <div class="panel-title">
            候选来源
            <span class="badge" style="margin-left:8px">
              共 {{ weekly.preview?.total ?? 0 }} 篇候选
            </span>
            <span class="tertiary" style="font-weight:400;margin-left:8px">
              公众号 {{ weekly.preview?.wechat ?? 0 }} · 论文 {{ weekly.preview?.arxiv ?? 0 }}
            </span>
          </div>
          <div class="mitm-row" style="align-items:flex-start">
            <div style="min-width:240px;flex:1">
              <div class="muted" style="font-size:var(--fs-sm);margin-bottom:6px">
                公众号
                <a href="#" style="margin-left:6px" @click.prevent="selectAllAccounts">
                  {{ allAccounts ? '（全部）' : '全部' }}
                </a>
              </div>
              <EmptyState v-if="!accounts.list.length" text="还没有添加公众号" />
              <label v-for="a in accounts.list" :key="a.id" class="ck-row">
                <input type="checkbox" class="cb" :checked="weekly.accountIds.has(a.id)"
                       @change="toggleAccount(a.id, ($event.target as HTMLInputElement).checked)" />
                <span class="acct-name">{{ a.name }}</span>
              </label>
            </div>
            <div style="min-width:240px;flex:1">
              <div class="muted" style="font-size:var(--fs-sm);margin-bottom:6px">
                其他来源目录
                <a href="#" style="margin-left:6px" @click.prevent="selectAllSources">
                  {{ allSources ? '（全部）' : '全部' }}
                </a>
              </div>
              <EmptyState v-if="!ext.sources.length" text="还没有登记外部来源目录" />
              <label v-for="s in ext.sources" :key="s.id" class="ck-row">
                <input type="checkbox" class="cb" :checked="weekly.sourceIds.has(s.id)"
                       @change="toggleSource(s.id, ($event.target as HTMLInputElement).checked)" />
                <span class="acct-name">{{ s.name || s.path }}</span>
                <span class="muted mono" style="font-size:var(--fs-xs)">{{ s.item_count }}</span>
              </label>
            </div>
          </div>
          <div v-if="!weekly.preview?.total" class="muted" style="font-size:var(--fs-sm);margin-top:var(--sp-2)">
            当前条件下没有候选文章 —— 检查日期区间是否覆盖了已拉取/已扫描的内容。
          </div>
        </div>

        <!-- 模板 -->
        <div class="panel">
          <div class="panel-title">模板</div>
          <div class="mitm-row">
            <span class="form-label">模板文件</span>
            <SInput v-model="weekly.templatePath" mono
                    placeholder="留空 = 使用内置模板" style="flex:1;min-width:240px" />
            <SButton size="sm" @click="pickTemplate">选择文件…</SButton>
            <SButton size="sm" variant="ghost" @click="useBuiltinTemplate">用内置模板</SButton>
          </div>
          <div class="muted" style="font-size:var(--fs-xs);margin-top:var(--sp-2);line-height:1.7">
            内置模板：<span class="mono">{{ weekly.preview?.template_path || '（未知）' }}</span>
            <template v-if="weekly.preview && !weekly.preview.template_exists">
              <br /><span class="status-fail">内置模板缺失，生成会失败</span>
            </template>
            <br />
            <b>模板是唯一真相</b>：里面的循环、条件和字段就是最终输出。改完点下面任意一期的
            「按当前模板重新渲染」即可看效果 —— <b>不调用 AI，不花钱</b>；
            写错变量名会明确告诉你哪个名字有问题，不会静默变空白。
          </div>
        </div>

        <!-- 生成 / 重渲染 -->
        <div class="panel">
          <div class="panel-title">生成</div>
          <div class="mitm-row">
            <SButton variant="primary" :disabled="busy || !weekly.preview?.total"
                     @click="weekly.generate()">
              生成第 {{ weekly.issueNum }} 期
            </SButton>
            <SButton size="sm" variant="ghost" :disabled="weekly.loadingPreview"
                     @click="weekly.loadPreview()">重新统计候选</SButton>
            <ProgressInline v-if="genTask" :text="weekly.stageMsg || genTask.message || '生成中…'"
                            cancellable @cancel="weekly.cancelGenerate()" />
            <ProgressInline v-else-if="renderTask"
                            :text="weekly.stageMsg || renderTask.message || '渲染中…'" />
          </div>
          <div v-if="weekly.lastResult" class="muted" style="font-size:var(--fs-xs);margin-top:var(--sp-2)">
            <template v-if="weekly.lastResult.issue_dir">
              上次输出：<a href="#" class="mono" @click.prevent="openPath(weekly.lastResult!.issue_dir!)">
                {{ weekly.lastResult.issue_dir }}
              </a>
            </template>
            <template v-if="weekly.lastResult.missing_vars?.length">
              <br /><span class="status-fail">
                模板引用了未知变量：{{ weekly.lastResult.missing_vars.join('、') }}
              </span>
            </template>
          </div>
        </div>

        <!-- 提示词 -->
        <div class="panel">
          <div class="panel-title">
            提示词
            <span class="tertiary" style="font-weight:400;margin-left:8px">
              — 输出 JSON 格式由软件固定，这里编辑的是判定标准
            </span>
          </div>
          <div class="muted" style="font-size:var(--fs-xs);margin-bottom:var(--sp-2);line-height:1.7">
            改了哪一段，<b>该段的缓存就会失效</b>，下次生成按新标准重算（其他段仍命中缓存，不重复花钱）；
            改回原文还能命中旧缓存。
          </div>
          <div v-for="k in promptKeys" :key="k" style="border-top:1px solid var(--border)">
            <div class="toolbar" style="padding:var(--sp-2) 0;cursor:pointer" @click="togglePrompt(k)">
              <SIcon :name="openPrompt === k ? 'chevron-down' : 'chevron-right'" :size="12" />
              <span>{{ weekly.prompts[k].label }}</span>
              <span v-if="weekly.promptDirty(k)" class="badge bu">未保存</span>
            </div>
            <div v-if="openPrompt === k" style="padding-bottom:var(--sp-2)">
              <textarea v-model="weekly.prompts[k].text" class="principles" spellcheck="false"></textarea>
              <div class="toolbar" style="margin-top:var(--sp-2)">
                <span class="spacer"></span>
                <SButton size="sm" variant="ghost" @click="weekly.restorePrompt(k)">恢复默认</SButton>
                <SButton size="sm" variant="primary" :disabled="!weekly.promptDirty(k)"
                         @click="weekly.savePrompt(k)">保存</SButton>
              </div>
            </div>
          </div>
        </div>

        <!-- 往期 -->
        <div class="panel">
          <div class="panel-title">往期</div>
          <EmptyState v-if="!weekly.issues.length" text="还没有生成过周报" />
          <div v-else class="ext-table">
            <div class="ext-head" style="grid-template-columns:70px 110px 1fr auto">
              <span>期号</span><span>日期</span><span>目录</span><span></span>
            </div>
            <div v-for="it in weekly.issues" :key="it.dir" class="ext-row"
                 style="grid-template-columns:70px 110px 1fr auto">
              <span class="mono">第{{ it.issue_num }}期</span>
              <span class="mono muted">{{ it.date }}</span>
              <span class="ext-path">{{ it.name }}</span>
              <span class="row-actions">
                <SButton v-if="it.report" size="sm" variant="ghost" @click="openPath(it.report)">打开报告</SButton>
                <SButton size="sm" variant="ghost" @click="openPath(it.dir)">打开目录</SButton>
                <SButton v-if="it.has_snapshot" size="sm" variant="ghost"
                         :disabled="busy" @click="weekly.rerender(it.dir)">
                  按当前模板重新渲染
                </SButton>
              </span>
            </div>
          </div>
        </div>
      </template>
    </div>
  </section>
</template>

<style scoped>
.ck-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 0;
  font-size: var(--fs-sm);
  cursor: pointer;
}
</style>
