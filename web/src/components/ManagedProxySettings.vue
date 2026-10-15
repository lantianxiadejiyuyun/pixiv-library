<script setup lang="ts">
import { ref, reactive, computed, onMounted, onBeforeUnmount } from 'vue'
import { AlertCircle, Check, CheckCircle2, ChevronDown, CircleHelp, Cpu, Download, Eye, EyeOff, Globe2, Link2, LoaderCircle, Pencil, Play, Plus, Power, RadioTower, RefreshCw, Rss, Save, Search, ShieldCheck, Timer, Trash2, Waypoints, X } from 'lucide-vue-next'
import { api, post, errorMessage, ApiError } from '../api'
import { sourceLabels, date } from '../types'
import Modal from './Modal.vue'

interface ManagedSubscription {id: string; label: string; url_host: string; node_count: number; auto_update: boolean; interval_hours: number; fetch_via_proxy: boolean; last_updated_at: string | null; last_error: string | null; next_update_at: string | null}
interface ManagedNode {id: string; name: string; type: string; subscription_id: string; subscription_label: string; selected: boolean; delay: number | null; delay_checked_at?: string | null}
interface ManagedState {enabled: boolean; core: {installed: boolean; running: boolean; version: string; error: string | null}; subscriptions: ManagedSubscription[]; nodes: ManagedNode[]; selected_node_id: string | null; message: string}
interface ProbeResult {ok: boolean; reachable: boolean; status_code: number | null; elapsed_ms: number; message: string; via_proxy: boolean}
interface NodeMeasurement {node_id: string; status: 'pending' | 'running' | 'success' | 'failed' | 'skipped'; delay: number | null; ok: boolean | null; message: string | null; checked_at?: string | null}
interface DelayBatch {batch_id: string; status: 'running' | 'completed' | 'cancelled'; total: number; completed: number; succeeded: number; failed: number; started_at: string; finished_at: string | null; message: string; results: NodeMeasurement[]}
const emit = defineEmits<{change: [enabled: boolean]}>()
const state = ref<ManagedState | null>(null)
const loading = ref(true)
const action = ref('')
const error = ref('')
const success = ref('')
const pollError = ref('')
const showForm = ref(false)
const editingId = ref('')
const formError = ref('')
const showSubscriptionURL = ref(false)
const form = reactive({label: '', url: '', auto_update: true, interval_hours: 24, fetch_via_proxy: false})
const deleteCandidate = ref<ManagedSubscription | null>(null)
const nodeQuery = ref('')
const subscriptionFilter = ref('')
const selectedNodeId = ref('')
const testPlatform = ref('pixiv')
const testedPlatform = ref('pixiv')
const probe = ref<ProbeResult | null>(null)
const showTest = ref(false)
const delayBatch = ref<DelayBatch | null>(null)
const batchStarting = ref(false)
const batchChecking = ref(false)
const batchError = ref('')
const nodeMeasurements = reactive<Record<string, NodeMeasurement>>({})
const nodeActivities = reactive<Record<string, {result: NodeMeasurement; started_at: string}>>({})
const nodeNotices = reactive<Record<string, NodeMeasurement>>({})
const batchRunning = computed(() => delayBatch.value?.status === 'running')
const batchPercent = computed(() => delayBatch.value?.total ? Math.min(100, Math.round(delayBatch.value.completed / delayBatch.value.total * 100)) : 0)
const busy = computed(() => loading.value || !!action.value || batchStarting.value || batchRunning.value)
const currentNode = computed(() => state.value?.nodes.find(node => node.id === state.value?.selected_node_id))
const filteredNodes = computed(() => (state.value?.nodes || []).filter(node => (!subscriptionFilter.value || node.subscription_id === subscriptionFilter.value) && `${node.name} ${node.type} ${node.subscription_label}`.toLowerCase().includes(nodeQuery.value.toLowerCase())))
const coreLabel = computed(() => !state.value?.core.installed ? '内核未就绪' : state.value.core.running ? '内核运行中' : '内核已停止')
let timer: ReturnType<typeof setInterval> | undefined
let requestId = 0
let batchTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false

function setState(value: ManagedState) {
  for (const node of value.nodes) {
    const previous = state.value?.nodes.find(entry => entry.id === node.id)
    if (previous?.delay_checked_at && node.delay_checked_at && measurementTimestamp(previous.delay_checked_at) > measurementTimestamp(node.delay_checked_at)) {
      node.delay = previous.delay; node.delay_checked_at = previous.delay_checked_at
    }
  }
  state.value = value; pollError.value = ''
  if (!value.nodes.some(node => node.id === selectedNodeId.value)) selectedNodeId.value = value.selected_node_id || ''
  if (subscriptionFilter.value && !value.subscriptions.some(subscription => subscription.id === subscriptionFilter.value)) subscriptionFilter.value = ''
  emit('change', value.enabled)
}
async function load(silent = false) {
  if (silent && (action.value || loading.value)) return
  const id = ++requestId
  if (!silent) { loading.value = true; error.value = '' }
  try { const value = await api<ManagedState>('/network/managed'); if (id === requestId) setState(value) }
  catch (err) { if (id === requestId) { if (silent) pollError.value = errorMessage(err); else error.value = errorMessage(err) } }
  finally { if (id === requestId && !silent) loading.value = false }
}
function feedbackReset() { error.value = ''; success.value = ''; formError.value = ''; probe.value = null }
function openSubscription(subscription?: ManagedSubscription) {
  editingId.value = subscription?.id || ''; formError.value = ''; showSubscriptionURL.value = false
  Object.assign(form, {label: subscription?.label || '', url: '', auto_update: subscription?.auto_update ?? true, interval_hours: subscription?.interval_hours || 24, fetch_via_proxy: subscription?.fetch_via_proxy ?? false})
  showForm.value = true
}
function closeForm() { if (action.value) return; showForm.value = false; form.url = ''; formError.value = '' }
async function saveSubscription() {
  action.value = 'subscription-save'; feedbackReset(); ++requestId
  try {
    const path = editingId.value ? `/network/managed/subscriptions/${editingId.value}` : '/network/managed/subscriptions'
    const payload = {label: form.label.trim(), auto_update: form.auto_update, interval_hours: form.interval_hours, fetch_via_proxy: form.fetch_via_proxy, ...(!editingId.value || form.url.trim() ? {url: form.url.trim()} : {})}
    const next = await api<ManagedState>(path, {method: editingId.value ? 'PATCH' : 'POST', body: JSON.stringify(payload)})
    setState(next); showForm.value = false; form.url = ''
    success.value = editingId.value ? '订阅设置已保存。' : '订阅已导入，请选择节点后开启内置代理。'
  } catch (err) { formError.value = errorMessage(err) }
  finally { action.value = '' }
}
async function refreshSubscription(subscription: ManagedSubscription) {
  action.value = `refresh:${subscription.id}`; feedbackReset(); ++requestId
  try { setState(await post<ManagedState>(`/network/managed/subscriptions/${subscription.id}/refresh`)); success.value = `「${subscription.label}」已更新。` }
  catch (err) { error.value = errorMessage(err) }
  finally { action.value = '' }
}
async function removeSubscription() {
  if (!deleteCandidate.value) return
  action.value = 'subscription-delete'; feedbackReset(); ++requestId
  try {
    setState(await api<ManagedState>(`/network/managed/subscriptions/${deleteCandidate.value.id}`, {method: 'DELETE'}))
    deleteCandidate.value = null; success.value = '订阅及对应节点已移除。'
  } catch (err) { error.value = errorMessage(err); deleteCandidate.value = null }
  finally { action.value = '' }
}
async function selectNode() {
  if (!selectedNodeId.value) return
  action.value = 'select'; feedbackReset(); ++requestId
  try {
    setState(await post<ManagedState>('/network/managed/select', {node_id: selectedNodeId.value}))
    success.value = state.value?.enabled ? `内置出口已切换至「${currentNode.value?.name || '所选节点'}」。` : '节点已选定，开启内置代理后生效。'
  } catch (err) { error.value = errorMessage(err) }
  finally { action.value = '' }
}
async function toggleEnabled() {
  if (!state.value) return
  action.value = 'enable'; feedbackReset(); ++requestId
  try {
    setState(await post<ManagedState>('/network/managed/enable', {enabled: !state.value.enabled}))
    success.value = state.value.enabled ? '内置代理已开启，平台请求将使用选定节点。' : '内置代理已关闭；后续请求遵循外部代理设置，未配置则直连。'
  } catch (err) { error.value = errorMessage(err) }
  finally { action.value = '' }
}
async function restartCore() {
  action.value = 'restart'; feedbackReset(); ++requestId
  try { setState(await post<ManagedState>('/network/managed/restart')); success.value = state.value?.core.running ? '内置内核已启动。' : '已完成启动请求，请查看内核状态。' }
  catch (err) { error.value = errorMessage(err) }
  finally { action.value = '' }
}
async function measureDelay(node: ManagedNode) {
  if (busy.value) return
  action.value = `delay:${node.id}`; error.value = ''; success.value = ''; ++requestId
  try {
    const result = await post<NodeMeasurement>('/network/managed/delay', {node_id: node.id})
    acceptMeasurement(result)
    const saved = state.value?.nodes.find(entry => entry.id === result.node_id)
    if (saved && ['success', 'failed'].includes(result.status) && result.checked_at && measurementTimestamp(result.checked_at) >= measurementTimestamp(saved.delay_checked_at)) {
      saved.delay = result.delay; saved.delay_checked_at = result.checked_at
    }
    if (!result.ok) error.value = result.message || '本次节点测速未完成。'
    else success.value = `${node.name}：${result.delay ?? '—'} ms。${result.message || ''}`
  } catch (err) { error.value = errorMessage(err) }
  finally { action.value = '' }
}
function measurementTimestamp(value?: string | null) {
  if (!value) return ''
  const time = new Date(value)
  if (!Number.isFinite(time.getTime())) return ''
  const fraction = value.match(/\.(\d+)/)?.[1] || ''
  return `${time.toISOString().slice(0, 19)}.${fraction.padEnd(6, '0').slice(0, 6)}Z`
}
function latestMeasurement(node: ManagedNode) {
  const sample = nodeMeasurements[node.id]
  if ((node.delay_checked_at || node.delay != null) && (!sample || measurementTimestamp(node.delay_checked_at) > measurementTimestamp(sample.checked_at))) {
    return {node_id: node.id, status: node.delay != null ? 'success' : 'failed', delay: node.delay, ok: node.delay != null, message: node.delay == null ? '上次节点测速失败或超时。' : null, checked_at: node.delay_checked_at} as NodeMeasurement
  }
  return sample
}
function acceptMeasurement(result: NodeMeasurement, batchStartedAt?: string) {
  if (['pending', 'running'].includes(result.status)) {
    if (batchStartedAt) nodeActivities[result.node_id] = {result, started_at: batchStartedAt}
    return
  }
  delete nodeActivities[result.node_id]
  const saved = state.value?.nodes.find(node => node.id === result.node_id)
  const previous = saved ? latestMeasurement(saved) : nodeMeasurements[result.node_id]
  if (result.status === 'skipped') {
    const stamp = result.checked_at || batchStartedAt
    if (measurementTimestamp(stamp) >= measurementTimestamp(nodeNotices[result.node_id]?.checked_at)) nodeNotices[result.node_id] = {...result, checked_at: stamp}
    return
  }
  if (!result.checked_at || measurementTimestamp(result.checked_at) < measurementTimestamp(previous?.checked_at)) return
  nodeMeasurements[result.node_id] = result
  if (measurementTimestamp(result.checked_at) >= measurementTimestamp(nodeNotices[result.node_id]?.checked_at)) delete nodeNotices[result.node_id]
}
function displayedMeasurement(node: ManagedNode) {
  const sample = latestMeasurement(node)
  if (action.value === `delay:${node.id}`) return {node_id: node.id, status: 'running', delay: null, ok: null, message: null} as NodeMeasurement
  const activity = nodeActivities[node.id]
  if (batchRunning.value && activity && measurementTimestamp(activity.started_at) >= measurementTimestamp(sample?.checked_at)) return activity.result
  return sample || nodeNotices[node.id]
}
function measurementMessage(node: ManagedNode) {
  const sample = latestMeasurement(node), notice = nodeNotices[node.id]
  if (notice?.message && measurementTimestamp(notice.checked_at) >= measurementTimestamp(sample?.checked_at)) return notice.message
  return sample?.status === 'failed' ? sample.message : ''
}
function measurementLabel(node: ManagedNode) {
  const measurement = displayedMeasurement(node)
  if (!measurement) return node.delay != null ? `${node.delay} ms` : '尚未测速'
  if (measurement.status === 'pending') return batchRunning.value ? '等待测速' : '未完成测速'
  if (measurement.status === 'running') return '测速中'
  if (measurement.status === 'failed') return '连接失败'
  if (measurement.status === 'skipped') return '已跳过'
  return measurement.delay != null ? `${measurement.delay} ms` : '已完成，未返回延迟'
}
function setBatch(value: DelayBatch) {
  if (delayBatch.value && measurementTimestamp(value.started_at) < measurementTimestamp(delayBatch.value.started_at)) return
  const changed = delayBatch.value?.batch_id !== value.batch_id
  const wasRunning = batchRunning.value
  delayBatch.value = value; batchError.value = ''
  if (changed) for (const id of Object.keys(nodeActivities)) delete nodeActivities[id]
  if (changed || wasRunning || value.status === 'running') for (const result of value.results) acceptMeasurement(result, value.started_at)
  clearTimeout(batchTimer)
  if (value.status === 'running' && !disposed) batchTimer = setTimeout(() => checkBatch(value.batch_id), 1000)
  else if ((wasRunning || changed) && !disposed) load(true)
}
async function checkBatch(batchId?: string) {
  if (batchChecking.value || disposed) return
  batchChecking.value = true
  try {
    const value = await api<DelayBatch | null>(`/network/managed/delay-batch${batchId ? `/${batchId}` : ''}`)
    if (disposed) return
    if (value) setBatch(value)
    else if (batchId) delayBatch.value = null
  } catch (err) {
    if (disposed) return
    if (err instanceof ApiError && err.status === 404 && batchId) {
      delayBatch.value = null
      for (const id of Object.keys(nodeActivities)) delete nodeActivities[id]
      batchError.value = '测速记录已失效，服务可能已重启。可重新开始测速。'
    } else {
      batchError.value = `测速状态暂时无法更新：${errorMessage(err)}`
      if (batchRunning.value) batchTimer = setTimeout(() => checkBatch(delayBatch.value?.batch_id), 3000)
    }
  } finally { batchChecking.value = false }
}
async function measureAllNodes() {
  if (busy.value || batchChecking.value || !state.value?.nodes.length || !state.value.core.running) return
  batchStarting.value = true; batchError.value = ''; error.value = ''; success.value = ''
  try {
    const value = await post<DelayBatch>('/network/managed/delay-batch')
    if (!disposed) setBatch(value)
  } catch (err) {
    if (!disposed) { const message = errorMessage(err); await checkBatch(); if (!batchRunning.value) batchError.value = message }
  } finally { batchStarting.value = false }
}
async function testConnection() {
  action.value = 'test'; feedbackReset(); testedPlatform.value = testPlatform.value
  try { probe.value = await post<ProbeResult>('/network/proxy/test', {platform: testPlatform.value}) }
  catch (err) { error.value = errorMessage(err) }
  finally { action.value = '' }
}
onMounted(() => { load(); checkBatch(); timer = setInterval(() => { load(true); if (!batchRunning.value && !batchStarting.value && !action.value) checkBatch() }, 15000) })
onBeforeUnmount(() => { disposed = true; clearInterval(timer); clearTimeout(batchTimer); form.url = ''; ++requestId })
</script>

<template>
  <section class="panel managed-proxy-settings" aria-labelledby="managed-network-heading">
    <header class="managed-network-header"><div class="managed-network-symbol"><Waypoints :size="27" :stroke-width="1.5" /></div><div><h2 id="managed-network-heading">内置代理</h2></div><span class="managed-local-badge"><Cpu :size="13" />内置 Mihomo</span><button class="icon-button" aria-label="刷新内置代理状态" :disabled="busy" @click="load()"><RefreshCw :size="16" :class="{spin: loading}" /></button></header>
    <div v-if="loading && !state" class="inline-loading"><LoaderCircle :size="20" class="spin" />正在读取内置连接状态…</div>
    <template v-if="state">
      <div class="managed-route-state"><div class="managed-route-main"><div class="managed-route-dot" :class="{active: state.enabled && !!currentNode && state.core.running, waiting: state.enabled && (!currentNode || !state.core.running)}"><Power :size="19" /></div><div><strong>{{ state.enabled ? currentNode ? state.core.running ? '正在使用内置代理' : '内置代理开启，内核待恢复' : '内置代理开启，等待选择节点' : '内置代理尚未开启' }}</strong><p>{{ state.enabled ? currentNode ? state.core.running ? currentNode.name : '内核恢复前，请求不会回退到直接连接。' : '选择节点前，请求不会回退到直接连接。' : currentNode ? `已选定：${currentNode.name}，开启后用于平台访问。` : '请导入订阅并选择节点。' }}</p></div></div><button class="button" :class="state.enabled ? 'secondary' : 'primary'" :disabled="busy || (!state.enabled && (!currentNode || !state.core.installed))" @click="toggleEnabled"><LoaderCircle v-if="action === 'enable'" :size="16" class="spin" /><Power v-else :size="16" />{{ state.enabled ? '关闭内置代理' : '开启内置代理' }}</button></div>
      <div class="managed-core-line"><span class="status-pill" :class="state.core.running ? 'success' : 'neutral'"><span class="status-dot" :class="{offline: !state.core.running}"></span>{{ coreLabel }}</span><span v-if="state.core.version" class="core-version">{{ state.core.version }}</span><button class="text-button" :disabled="busy || !state.core.installed" @click="restartCore"><LoaderCircle v-if="action === 'restart'" :size="12" class="spin" /><RefreshCw v-else-if="state.core.running" :size="12" /><Play v-else :size="12" />{{ state.core.running ? '重启内核' : '启动内核' }}</button></div>
      <div v-if="state.core.error" class="alert error managed-inline-feedback"><AlertCircle :size="17" /><span>{{ state.core.error }}</span></div><p v-if="state.message" class="managed-state-message">{{ state.message }}</p>

      <section class="managed-subscriptions"><div class="network-subheading"><div><h3><Rss :size="17" />节点订阅</h3></div><button class="button primary small" :disabled="busy" @click="openSubscription()"><Plus :size="15" />导入订阅</button></div><div v-if="!state.subscriptions.length" class="managed-subscription-empty"><div class="managed-empty-orbit"><Link2 :size="30" :stroke-width="1.4" /></div><h3>暂无节点订阅</h3><p>导入 HTTPS 订阅后，选择节点并开启代理。</p><button class="button secondary small" :disabled="busy" @click="openSubscription()"><Plus :size="15" />导入订阅</button></div><div v-else class="managed-subscription-list"><article v-for="subscription in state.subscriptions" :key="subscription.id" class="managed-subscription-row"><div class="managed-subscription-icon"><Rss :size="18" /></div><div class="managed-subscription-info"><h4>{{ subscription.label }}<span>{{ subscription.node_count }} 个节点</span></h4><p><span>{{ subscription.url_host }}</span><span>·</span><span>{{ subscription.auto_update ? `每 ${subscription.interval_hours} 小时更新` : '手动更新' }}</span><span v-if="subscription.fetch_via_proxy">· 经当前代理更新</span></p><p class="subscription-update-time">上次更新：{{ subscription.last_updated_at ? date(subscription.last_updated_at) : '尚未成功更新' }}<span v-if="subscription.auto_update && subscription.next_update_at"> · 下次：{{ date(subscription.next_update_at) }}</span></p><p v-if="subscription.last_error" class="inline-error">{{ subscription.last_error }}</p></div><div class="managed-subscription-actions"><button class="icon-button" :aria-label="`更新 ${subscription.label}`" title="立即更新订阅" :disabled="busy" @click="refreshSubscription(subscription)"><LoaderCircle v-if="action === `refresh:${subscription.id}`" class="spin" :size="16" /><RefreshCw v-else :size="16" /></button><button class="icon-button" :aria-label="`编辑 ${subscription.label}`" title="订阅设置" :disabled="busy" @click="openSubscription(subscription)"><Pencil :size="15" /></button><button class="icon-button danger-text" :aria-label="`移除 ${subscription.label}`" title="移除订阅" :disabled="busy" @click="deleteCandidate = subscription"><Trash2 :size="15" /></button></div></article></div></section>

      <section v-if="state.nodes.length" class="managed-node-section"><div class="network-subheading"><div><h3><Waypoints :size="17" />节点</h3><p>{{ state.nodes.length }} 个节点 · 测速不会切换节点</p></div><div class="managed-node-heading-actions"><span class="managed-node-total">{{ currentNode ? '已选定节点' : '尚未选择' }}</span><button class="button secondary small" :disabled="busy || batchChecking || !state.core.running" @click="measureAllNodes"><LoaderCircle v-if="batchStarting || batchRunning" class="spin" :size="16" /><RadioTower v-else :size="16" />{{ batchStarting ? '正在启动测速' : batchRunning ? `测速中 ${delayBatch?.completed || 0}/${delayBatch?.total || 0}` : '一键测速' }}</button></div></div><div v-if="delayBatch" class="managed-batch-status" :class="{running: batchRunning}"><div class="managed-batch-summary"><strong>{{ batchRunning ? '正在测试全部节点' : delayBatch.status === 'cancelled' ? '测速已中止' : '测速完成' }}</strong><span>{{ delayBatch.completed }} / {{ delayBatch.total }} 个已完成</span><span class="success-text">{{ delayBatch.succeeded }} 个可连接</span><span :class="{'danger-text': delayBatch.failed > 0}">{{ delayBatch.failed }} 个失败</span></div><div class="managed-batch-progress" role="progressbar" :aria-valuenow="delayBatch.completed" :aria-valuemax="delayBatch.total" aria-valuemin="0" aria-label="节点测速完成进度"><span :style="{width: `${batchPercent}%`}"></span></div><p>{{ batchRunning ? '任务在后台继续，离开页面后可回来查看结果。' : delayBatch.finished_at ? `结束于 ${date(delayBatch.finished_at)}` : '未完成的节点可以单独重试。' }}<span> 测试连接延迟，不代表下载速度。</span></p><div v-if="delayBatch.message" class="managed-batch-notice" role="status"><CircleHelp :size="17" /><span>{{ delayBatch.message }}</span></div></div><div v-if="batchError" class="alert error managed-batch-error" role="alert"><AlertCircle :size="17" /><span>{{ batchError }}</span><button class="text-button" :disabled="batchChecking || batchStarting" @click="checkBatch(delayBatch?.batch_id)">刷新测速状态</button></div><p v-if="!state.core.running" class="managed-batch-hint">启动内核后可测速；测速不会自动开启代理或切换节点。</p><div class="managed-node-filters"><div class="search-field"><Search :size="15" /><input v-model="nodeQuery" placeholder="搜索节点名称…" aria-label="搜索内置节点" /></div><label v-if="state.subscriptions.length > 1" class="managed-filter-select"><span class="sr-only">按订阅筛选节点</span><select v-model="subscriptionFilter"><option value="">全部订阅</option><option v-for="subscription in state.subscriptions" :key="subscription.id" :value="subscription.id">{{ subscription.label }}</option></select></label></div><div class="managed-node-grid"><article v-for="node in filteredNodes" :key="node.id" class="managed-node-card" :class="{selected: selectedNodeId === node.id, current: state.selected_node_id === node.id}"><label><input v-model="selectedNodeId" type="radio" name="managed-node" :value="node.id" :disabled="busy" /><span class="managed-node-name">{{ node.name }}<small>{{ node.type }} · {{ node.subscription_label }}</small></span><span v-if="state.selected_node_id === node.id" class="node-current-badge"><Check :size="11" />已选定</span></label><div class="managed-node-speed"><span :class="{'danger-text': displayedMeasurement(node)?.status === 'failed', 'success-text': displayedMeasurement(node)?.status === 'success'}"><LoaderCircle v-if="displayedMeasurement(node)?.status === 'running'" class="spin" :size="13" /><Timer v-else :size="13" />{{ measurementLabel(node) }}</span><button class="text-button" :disabled="busy || !state.core.running" @click="measureDelay(node)"><LoaderCircle v-if="action === `delay:${node.id}`" class="spin" :size="12" /><RadioTower v-else :size="12" />{{ action === `delay:${node.id}` ? '测速中' : '测速' }}</button></div><p v-if="measurementMessage(node)" class="managed-node-test-error">{{ measurementMessage(node) }}</p></article></div><div v-if="!filteredNodes.length" class="network-inline-note"><Search :size="16" />没有匹配的节点，请调整搜索条件。</div><div class="managed-node-apply"><span v-if="!state.enabled">选定节点后需开启内置代理。</span><button class="button primary small" :disabled="busy || !selectedNodeId || selectedNodeId === state.selected_node_id" @click="selectNode"><LoaderCircle v-if="action === 'select'" class="spin" :size="15" /><Check v-else :size="15" />{{ state.enabled ? '应用所选节点' : '选定此节点' }}</button></div></section>

      <section class="managed-test-section"><button class="advanced-toggle" :aria-expanded="showTest" @click="showTest = !showTest"><RadioTower :size="16" /><span>检查平台连通性</span><ChevronDown :size="15" :class="{expanded: showTest}" /></button><div v-if="showTest" class="managed-test-content"><p>测试当前出口，内置代理优先。</p><div class="network-test-controls"><label class="field"><span class="sr-only">测试平台</span><select v-model="testPlatform" :disabled="busy"><option v-for="platform in ['pixiv', 'ehentai', 'picacg', 'jm']" :key="platform" :value="platform">{{ sourceLabels[platform] }}</option></select></label><button class="button secondary small" :disabled="busy" @click="testConnection"><LoaderCircle v-if="action === 'test'" class="spin" :size="15" /><Globe2 v-else :size="15" />{{ action === 'test' ? '正在测试' : '测试连接' }}</button></div><div v-if="probe" class="network-test-result" :class="probe.ok ? 'test-success' : probe.reachable ? 'test-warning' : 'test-failed'" role="status"><component :is="probe.ok ? CheckCircle2 : AlertCircle" :size="19" /><div><strong>{{ sourceLabels[testedPlatform] }} · {{ probe.ok ? '连接正常' : probe.reachable ? '平台返回异常' : '连接失败' }}</strong><p>{{ probe.message }}</p><div><span>{{ probe.via_proxy ? '通过代理' : '直接连接' }}</span><span>{{ Math.round(probe.elapsed_ms) }} ms</span><span v-if="probe.status_code != null">HTTP {{ probe.status_code }}</span></div></div></div></div></section>
      <div class="managed-bottom-note"><ShieldCheck :size="14" /><p>订阅链接加密保存。代理仅影响书库服务；Pixiv 官方登录页使用浏览器网络。</p></div>
    </template>
    <div v-if="error || pollError" class="alert error managed-feedback" role="alert"><AlertCircle :size="18" /><span>{{ error || `状态更新失败：${pollError}` }}</span><button v-if="!state" class="text-button" @click="load()">重试</button><button class="icon-button" aria-label="关闭错误" @click="error = ''; pollError = ''"><X :size="14" /></button></div><div v-if="success" class="alert info managed-feedback" role="status"><CheckCircle2 :size="18" /><span>{{ success }}</span><button class="icon-button" aria-label="关闭提示" @click="success = ''"><X :size="14" /></button></div>
  </section>

  <Modal v-if="showForm" :title="editingId ? '编辑节点订阅' : '导入节点订阅'" :busy="!!action" @close="closeForm"><form @submit.prevent="saveSubscription"><label class="field">订阅名称<input v-model="form.label" placeholder="例如：我的常用订阅" maxlength="100" required :disabled="!!action" /></label><label class="field">HTTPS 订阅链接 <span v-if="editingId" class="optional">留空保留原链接</span><div class="input-with-icon login-password-field"><Link2 :size="16" /><input v-model="form.url" :type="showSubscriptionURL ? 'text' : 'password'" :placeholder="editingId ? '输入新链接以替换；原链接不会回显' : '粘贴 https://… 订阅链接'" autocomplete="new-password" spellcheck="false" :required="!editingId" :disabled="!!action" /><button class="icon-button" type="button" :aria-label="showSubscriptionURL ? '隐藏订阅链接' : '显示订阅链接'" @click="showSubscriptionURL = !showSubscriptionURL"><component :is="showSubscriptionURL ? EyeOff : Eye" :size="16" /></button></div></label><div class="managed-update-form"><label class="checkbox-label"><input v-model="form.auto_update" type="checkbox" :disabled="!!action" />自动更新订阅</label><label class="field">更新间隔（小时）<input v-model.number="form.interval_hours" type="number" min="1" max="168" :disabled="!!action || !form.auto_update" required /></label></div><label class="checkbox-label managed-fetch-proxy"><input v-model="form.fetch_via_proxy" type="checkbox" :disabled="!!action" />通过当前代理获取订阅</label><p class="field-hint managed-fetch-hint">首次配置建议直连；通过代理更新需有可用出口。</p><div class="alert info"><CircleHelp :size="16" /><span>仅导入节点，不会自动开启代理。</span></div><div v-if="formError" class="alert error" role="alert"><AlertCircle :size="17" /><span>{{ formError }}</span></div><div class="modal-footer"><button type="button" class="button secondary" :disabled="!!action" @click="closeForm">取消</button><button class="button primary" :disabled="!!action"><LoaderCircle v-if="action === 'subscription-save'" class="spin" :size="16" /><Save v-else-if="editingId" :size="16" /><Download v-else :size="16" />{{ action === 'subscription-save' ? '正在处理订阅' : editingId ? '保存设置' : '导入订阅' }}</button></div></form></Modal>
  <Modal v-if="deleteCandidate" title="移除这个节点订阅？" :subtitle="deleteCandidate.label" :busy="!!action" @close="!action && (deleteCandidate = null)"><p class="managed-delete-copy">将移除此订阅及其节点，保留书库内容。若当前节点被移除，代理仍保持开启且不回退直连；重新选择节点后才能恢复访问。</p><div class="modal-footer"><button class="button secondary" :disabled="!!action" @click="deleteCandidate = null">保留订阅</button><button class="button danger" :disabled="!!action" @click="removeSubscription"><LoaderCircle v-if="action === 'subscription-delete'" class="spin" :size="16" /><Trash2 v-else :size="16" />确认移除</button></div></Modal>
</template>
