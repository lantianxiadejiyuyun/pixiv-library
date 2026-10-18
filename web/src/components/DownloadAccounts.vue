<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { LoaderCircle, Plus, RefreshCw, Save, Search, Trash2 } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { SourceAccount } from '../types'

type Account = {id: string; label: string | null; status: 'verified' | 'unverified' | 'missing' | 'unconfigured'; verified_at?: string | null; in_pool?: boolean; running: number; completed: number; failed: number}
type Config = {enabled: boolean; main_account_id: string | null; content_account_id: string | null; content_account_ids: string[]; manual_account_ids?: string[]; retry_account_ids?: string[]; detail_account_ids?: string[]; check_account_ids?: string[]; account_limits?: Record<string, number>; download_accounts: Account[]; missing_account_ids?: string[]}
const props = defineProps<{accounts: SourceAccount[]}>()
const emit = defineEmits<{change: [config: Config]; login: []; removed: []}>()
const state = ref<Config | null>(null)
const form = reactive({enabled: false, main_account_id: '', content_account_ids: [] as string[], manual_account_ids: [] as string[], retry_account_ids: [] as string[], detail_account_ids: [] as string[], check_account_ids: [] as string[], account_limits: {} as Record<string, number>})
const loading = ref(true), busy = ref(false), error = ref(''), message = ref(''), saved = ref('')
const query = ref(''), confirmRemove = ref('')
type FunctionFilter = 'all' | 'download' | 'manual' | 'retry' | 'details' | 'checks'
const functionFilter = ref<FunctionFilter>('all')
const functionOptions: {id: FunctionFilter; label: string}[] = [{id: 'all', label: '全部账号'}, {id: 'download', label: '自动图片下载'}, {id: 'manual', label: '手动下载'}, {id: 'retry', label: '失败重试'}, {id: 'details', label: '作品详情 / 正文'}, {id: 'checks', label: '作者资料'}]
const dirty = computed(() => !!saved.value && JSON.stringify(form) !== saved.value)
const accounts = computed<Account[]>(() => (state.value?.download_accounts ?? props.accounts.map(account => ({...account, status: account.verified_at ? 'verified' : 'unverified', running: 0, completed: 0, failed: 0}))).filter(account => account.status !== 'missing'))
const roleCounts = computed(() => ({all: accounts.value.length, download: form.content_account_ids.length, manual: form.manual_account_ids.length, retry: form.retry_account_ids.length, details: form.detail_account_ids.length, checks: form.check_account_ids.length}))
const filtered = computed(() => accounts.value.filter(account => {
  if (!`${account.label || ''} ${account.id}`.toLowerCase().includes(query.value.trim().toLowerCase())) return false
  return functionFilter.value === 'all' || (functionFilter.value === 'download' ? form.content_account_ids : functionFilter.value === 'manual' ? form.manual_account_ids : functionFilter.value === 'retry' ? form.retry_account_ids : functionFilter.value === 'details' ? form.detail_account_ids : form.check_account_ids).includes(account.id)
}))
const names = {verified: '已登录', unverified: '凭据未验证', missing: '账号已移除', unconfigured: '未配置'}
let request = 0, disposed = false, polling = false
let timer: ReturnType<typeof setInterval> | undefined
function apply(config: Config, preserve = false) {
  const keep = preserve && dirty.value
  state.value = config
  if (!keep) {
    Object.assign(form, {enabled: config.enabled, main_account_id: config.main_account_id || '', content_account_ids: [...(config.content_account_ids ?? (config.content_account_id ? [config.content_account_id] : []))], manual_account_ids: [...(config.manual_account_ids ?? [])], retry_account_ids: [...(config.retry_account_ids ?? [])], detail_account_ids: [...(config.detail_account_ids ?? [])], check_account_ids: [...(config.check_account_ids ?? [])], account_limits: Object.fromEntries(config.download_accounts.map(account => [account.id, config.account_limits?.[account.id] ?? 2]))})
    saved.value = JSON.stringify(form)
  }
  // Drop deleted identities from both server selections and unsaved edits.
  // The server retains old references to make already assigned jobs fail closed.
  const available = new Set(accounts.value.map(account => account.id))
  if (!available.has(form.main_account_id)) form.main_account_id = ''
  for (const key of ['content_account_ids', 'manual_account_ids', 'retry_account_ids', 'detail_account_ids', 'check_account_ids'] as const) form[key] = form[key].filter(id => available.has(id))
  form.account_limits = Object.fromEntries(Object.entries(form.account_limits).filter(([id]) => available.has(id)))
  emit('change', config)
}
async function load(silent = false) {
  if (disposed || busy.value || (silent && polling)) return
  const current = ++request; polling = true
  if (!silent) loading.value = true
  try { const config = await api<Config>('/sources/pixiv/account-roles'); if (!disposed && current === request) { apply(config, true); error.value = '' } }
  catch (e) { if (!disposed && current === request) error.value = errorMessage(e) }
  finally { if (current === request) { loading.value = false; polling = false } }
}
async function save() {
  if (busy.value || loading.value || disposed) return
  if (form.enabled && (!form.main_account_id || !(form.content_account_ids.length || form.manual_account_ids.length || form.detail_account_ids.length || form.check_account_ids.length))) { error.value = '请选择主账号，并为至少一种功能分配账号。'; return }
  const current = ++request; busy.value = true; error.value = ''; message.value = ''
  try {
    const config = await api<Config>('/sources/pixiv/account-roles', {method: 'PUT', body: JSON.stringify({enabled: form.enabled, main_account_id: form.main_account_id || null, content_account_ids: [...form.content_account_ids], manual_account_ids: [...form.manual_account_ids], retry_account_ids: [...form.retry_account_ids], detail_account_ids: [...form.detail_account_ids], check_account_ids: [...form.check_account_ids], account_limits: {...form.account_limits}})})
    if (disposed || current !== request) return
    apply(config)
    message.value = config.enabled ? '已保存账号功能与并发设置。' : '已保存，主副账号分工未开启。'
  } catch (e) { if (!disposed && current === request) error.value = errorMessage(e) }
  finally { busy.value = false; polling = false }
}
function selectVisible(selected: boolean) {
  const visible = filtered.value.filter(account => account.id !== form.main_account_id && account.status !== 'missing').map(account => account.id)
  form.content_account_ids = selected ? [...new Set([...form.content_account_ids, ...visible])] : form.content_account_ids.filter(id => !visible.includes(id))
}
async function remove(id: string) {
  if (busy.value || loading.value) return
  if (confirmRemove.value !== id) { confirmRemove.value = id; return }
  const current = ++request; busy.value = true; error.value = ''; message.value = ''
  try {
    await api(`/sources/pixiv/accounts/${encodeURIComponent(id)}`, {method: 'DELETE'})
    if (disposed || current !== request) return
    confirmRemove.value = ''; emit('removed')
    // Remove immediately, even when the subsequent refresh is slow or fails.
    if (state.value) {
      apply({...state.value, download_accounts: state.value.download_accounts.filter(account => account.id !== id), missing_account_ids: [...new Set([...(state.value.missing_account_ids ?? []), id])]}, true)
    }
  } catch (e) { if (!disposed && current === request) error.value = errorMessage(e) }
  finally { busy.value = false; polling = false }
  if (!disposed) await load(true)
}
watch(() => form.main_account_id, id => { form.content_account_ids = form.content_account_ids.filter(value => value !== id) })
watch(() => props.accounts, () => load(true), {deep: true})
onMounted(() => { load(); timer = setInterval(() => { if (!document.hidden) void load(true) }, 6000) })
onBeforeUnmount(() => { disposed = true; ++request; clearInterval(timer) })
</script>

<template>
  <section class="panel download-accounts">
    <header class="accounts-header"><div><h2>Pixiv 账号分工</h2><p class="field-hint">每个账号可承担多种任务；同一账号的任务共用并发额度。账号数量不设上限。</p></div><div class="accounts-actions"><button class="icon-button" aria-label="刷新账号" :disabled="busy || loading" @click="load()"><RefreshCw :size="17" :class="{spin: loading}" /></button><button class="button primary small" :disabled="busy" @click="emit('login')"><Plus :size="16" />添加账号</button></div></header>
    <p v-if="loading && !state" class="field-hint">正在读取账号…</p>
    <form v-else @submit.prevent="save">
      <p v-if="state?.missing_account_ids?.length || state?.download_accounts.some(account => account.status === 'missing')" class="alert error" role="status">账号分工中有已移除的账号，请重新选择并保存。已分配给旧账号的任务不会自动换号。</p>
      <div class="accounts-config"><label class="field">检索主账号<select v-model="form.main_account_id" :disabled="busy" :required="form.enabled"><option value="">请选择主账号</option><option v-for="account in accounts" :key="account.id" :value="account.id" :disabled="account.status === 'missing'">{{ account.label || '已移除账号' }}</option></select></label><label class="accounts-enabled"><input v-model="form.enabled" type="checkbox" :disabled="busy" />启用账号分工</label></div>
      <p class="field-hint accounts-role-hint">主账号检索作品、同步关注及作者／收藏／系列目录；其他请求按下方勾选的功能分配。</p>
      <div class="accounts-overview" aria-label="账号功能统计">
        <button v-for="option in functionOptions" :key="option.id" type="button" :aria-pressed="functionFilter === option.id" :class="{selected: functionFilter === option.id}" @click="functionFilter = option.id"><span>{{ option.label }}</span><strong>{{ roleCounts[option.id] }}</strong></button>
      </div>
      <div class="accounts-toolbar"><div class="search-field"><Search :size="17" /><input v-model="query" aria-label="搜索账号" placeholder="搜索账号名称或 ID…" /></div><span class="result-count">{{ filtered.length }} 个结果</span><button type="button" class="text-button" :disabled="busy || !filtered.length" @click="selectVisible(true)">全选自动下载</button><button type="button" class="text-button" :disabled="busy || !filtered.length" @click="selectVisible(false)">取消自动下载</button></div>
      <div class="accounts-list">
        <article v-for="account in filtered" :key="account.id" class="download-account-row">
          <div class="account-identity"><span class="account-avatar" aria-hidden="true">{{ (account.label || 'P').slice(0, 1) }}</span><div><strong>{{ account.label || '未命名账号' }}</strong><div class="account-badges"><span v-if="account.id === form.main_account_id" class="main-badge">检索主账号</span><span class="status-pill" :class="account.status === 'verified' ? 'success' : 'neutral'" :title="account.status === 'verified' ? '添加时已通过验证；会话是否仍有效以实际请求为准。' : ''">{{ names[account.status] }}</span></div></div></div>
          <div class="account-metrics" aria-label="账号任务统计"><span :class="{active: account.running > 0}">运行 <strong>{{ account.running }}</strong></span><span>完成 <strong>{{ account.completed }}</strong></span><span :class="{'danger-text': account.failed > 0}">失败 <strong>{{ account.failed }}</strong></span></div>
          <div class="account-controls"><div class="account-capabilities" :aria-label="`${account.label} 的功能`"><label :class="{checked: form.content_account_ids.includes(account.id), unavailable: account.id === form.main_account_id}"><input v-model="form.content_account_ids" type="checkbox" :value="account.id" :disabled="busy || account.id === form.main_account_id" :aria-label="`图片下载 ${account.label}`" />自动图片下载</label><label :class="{checked: form.manual_account_ids.includes(account.id)}"><input v-model="form.manual_account_ids" type="checkbox" :value="account.id" :disabled="busy" :aria-label="`手动下载 ${account.label}`" />手动下载</label><label :class="{checked: form.retry_account_ids.includes(account.id)}"><input v-model="form.retry_account_ids" type="checkbox" :value="account.id" :disabled="busy" :aria-label="`失败重试 ${account.label}`" />失败重试</label><label :class="{checked: form.detail_account_ids.includes(account.id)}"><input v-model="form.detail_account_ids" type="checkbox" :value="account.id" :disabled="busy" :aria-label="`作品详情与小说正文 ${account.label}`" />作品详情 / 正文</label><label :class="{checked: form.check_account_ids.includes(account.id)}"><input v-model="form.check_account_ids" type="checkbox" :value="account.id" :disabled="busy" :aria-label="`作者主页与账号资料 ${account.label}`" />作者资料</label></div><label class="account-concurrency">并发<input v-model.number="form.account_limits[account.id]" type="number" min="1" max="8" required :disabled="busy" :aria-label="`账号并发 ${account.label}`" /></label></div>
          <div class="account-remove"><button v-if="account.status !== 'missing'" type="button" class="text-button danger-text" :disabled="busy" @click="remove(account.id)"><Trash2 :size="14" />{{ confirmRemove === account.id ? '确认移除' : '移除' }}</button><button v-if="confirmRemove === account.id" type="button" class="text-button" :disabled="busy" @click="confirmRemove = ''">取消</button></div>
        </article>
        <div v-if="!filtered.length" class="accounts-empty"><Search :size="28" /><p>{{ accounts.length ? '没有匹配的账号' : '尚未添加 Pixiv 账号' }}</p><button v-if="accounts.length" type="button" class="text-button" @click="query = ''; functionFilter = 'all'">查看全部账号</button><button v-else type="button" class="button secondary small" @click="emit('login')">添加账号</button></div>
      </div>
      <div class="accounts-footer"><p class="field-hint">手动下载权限需单独勾选。失败重试需同时勾选重试权限与原任务对应功能；可与其他功能多选。并发 1–8，同时遵守各队列上限。作品详情／小说正文和作者资料需选择对应账号，留空不会改用主账号；主账号可手动勾选这两项。</p><div class="accounts-save"><span v-if="dirty" role="status">有未保存的修改</span><button class="button primary small" :disabled="busy || (!dirty && !!state)"><LoaderCircle v-if="busy" class="spin" :size="16" /><Save v-else :size="16" />保存配置</button></div></div>
    </form>
    <div v-if="error" class="alert error" role="alert">{{ error }}</div><div v-if="message" class="alert info" role="status">{{ message }}</div>
  </section>
</template>

<style scoped>
.download-accounts {padding:24px;}.accounts-header,.accounts-actions,.accounts-toolbar,.accounts-footer {display:flex;align-items:center;gap:14px;}.accounts-header,.accounts-footer {justify-content:space-between;}.accounts-header {align-items:flex-start;margin-bottom:24px;}.accounts-header h2 {font-size:18px;}.accounts-header p {margin:8px 0 0;max-width:650px;}.accounts-actions {flex-shrink:0;}.accounts-config {display:grid;grid-template-columns:minmax(200px,360px) 1fr;gap:24px;align-items:center;margin-bottom:24px;}.accounts-enabled {display:flex;gap:9px;align-items:center;font-size:14px;}.accounts-toolbar {flex-wrap:wrap;margin-bottom:14px;font-size:13px;color:var(--text-secondary);}.accounts-toolbar .search-field {flex:1;min-width:180px;}.accounts-list {border-top:1px solid var(--line);}.download-account-row {display:grid;grid-template-columns:minmax(170px,1fr) auto 100px;gap:20px;align-items:center;padding:18px 0;border-bottom:1px solid var(--line);}.account-select {display:flex;align-items:center;gap:12px;min-width:0;}.account-select>span {display:flex;flex-direction:column;gap:7px;min-width:0;}.account-select strong {font-size:15px;overflow-wrap:anywhere;}.account-select small {font-size:13px;color:var(--text-secondary);}.account-capabilities {display:flex;gap:14px;flex-wrap:wrap;grid-column:1/-1;grid-row:2;}.account-capabilities label {display:flex;align-items:center;gap:7px;font-size:13px;}.account-capabilities input[type=number] {width:70px;padding:6px;} .account-metrics {display:flex;gap:16px;align-items:center;flex-wrap:wrap;font-size:13px;color:var(--text-secondary);}.account-remove {display:flex;flex-direction:column;align-items:flex-end;gap:8px;}.accounts-footer {margin-top:20px;align-items:flex-start;}.accounts-footer p {margin:0;max-width:700px;}.accounts-footer button {flex-shrink:0;}.alert {margin-top:16px;}
.accounts-role-hint {margin:0 0 20px;}
.accounts-overview {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:0 0 20px;}
.accounts-overview button {display:flex;flex-direction:column;align-items:flex-start;gap:8px;padding:16px 18px;border:1px solid var(--line);border-radius:12px;background:var(--surface);color:var(--text-secondary);cursor:pointer;text-align:left;font:inherit;}
.accounts-overview button.selected {border-color:var(--accent);background:var(--accent-soft);color:var(--accent);}
.accounts-overview span {font-size:13px;}.accounts-overview strong {font-size:26px;font-weight:650;line-height:1.2;font-variant-numeric:tabular-nums;}
.accounts-list {display:flex;flex-direction:column;gap:12px;border:0;}
.download-account-row {grid-template-columns:minmax(0,1fr) auto auto;padding:18px 20px;border:1px solid var(--line);border-radius:12px;gap:18px 24px;}
.account-identity {display:flex;align-items:center;gap:12px;min-width:0;}.account-identity>div {min-width:0;}.account-identity strong {font-size:15px;overflow-wrap:anywhere;}
.account-avatar {display:grid;place-items:center;flex-shrink:0;width:40px;height:40px;border-radius:12px;background:var(--accent-soft);color:var(--accent);font-size:17px;font-weight:600;}
.account-badges {display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:7px;}.main-badge {font-size:12px;color:var(--accent);font-weight:600;}
.account-metrics {gap:18px;}.account-metrics span {display:flex;gap:5px;align-items:center;white-space:nowrap;}.account-metrics strong {font-variant-numeric:tabular-nums;}.account-metrics .active {color:var(--accent);}
.account-controls {grid-column:1/-1;grid-row:2;display:flex;justify-content:space-between;align-items:center;gap:14px;padding-top:14px;border-top:1px solid var(--line);}
.account-capabilities {gap:8px;}.account-capabilities label {border:1px solid var(--line);border-radius:8px;padding:8px 12px;cursor:pointer;min-height:36px;}.account-capabilities label.checked {border-color:var(--accent);color:var(--accent);background:var(--accent-soft);}.account-capabilities label.unavailable {color:var(--text-secondary);cursor:default;}
.account-concurrency {display:flex;align-items:center;gap:10px;white-space:nowrap;font-size:13px;color:var(--text-secondary);}.account-concurrency input {width:64px;padding:7px 8px;text-align:center;}
.account-remove {grid-column:3;grid-row:1;min-width:64px;}.accounts-save {display:flex;gap:12px;align-items:center;flex-shrink:0;}.accounts-save>span {font-size:13px;color:var(--accent);}.accounts-empty {padding:40px 16px;display:flex;flex-direction:column;gap:12px;align-items:center;color:var(--text-secondary);}.accounts-empty p {margin:0;}
.result-count {white-space:nowrap;}
@media(max-width:1000px) {.account-metrics {grid-column:1;grid-row:2;}.account-controls {grid-row:3;}.account-remove {grid-column:2;}.download-account-row {grid-template-columns:minmax(0,1fr) auto;}.accounts-save {flex-direction:column;align-items:flex-end;}}
@media(max-width:800px) {.accounts-config {grid-template-columns:1fr;gap:16px;}.accounts-overview {gap:8px;}.accounts-overview button {padding:12px;}}
@media(max-width:520px) {.download-accounts {padding:16px;}.accounts-header,.accounts-footer {flex-direction:column;}.accounts-actions {align-self:flex-end;}.accounts-toolbar {gap:12px;}.accounts-toolbar .search-field {flex-basis:100%;}.account-metrics {gap:16px;}.accounts-overview {grid-template-columns:repeat(2,minmax(0,1fr));}.accounts-overview button {flex-direction:row;justify-content:space-between;align-items:center;}.accounts-overview strong {font-size:22px;}.download-account-row {padding:14px;gap:14px;}.account-controls {align-items:flex-start;flex-wrap:wrap;}.account-capabilities {gap:6px;}.account-capabilities label {padding:8px;}.accounts-save {flex-direction:row;justify-content:space-between;align-items:center;width:100%;}.accounts-save button {margin-left:auto;}}
</style>
