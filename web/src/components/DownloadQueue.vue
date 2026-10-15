<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { AlertCircle, Check, Download, Image, LoaderCircle, Pause, Play, RefreshCw, Save } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { Job } from '../types'
import { jobKindLabels, sourceLabels } from '../types'

interface QueueSettings {paused: boolean; rate_limit_enabled: boolean; min_interval_seconds: number; max_interval_seconds: number; concurrency: number}
interface Lane {id: string; label: string; paused: boolean; blocked_by_master: boolean; concurrency: number; counts: {queued: number; running: number; failed: number; completed: number}}
interface Worker {lane: string; pid: number | null; state: string; cpus: number[]; requested_cpus: number[]; affinity_applied: boolean; active: number; restarts: number}
interface QueueState {runtime?: {mode: string; api_cpus: number[]; shared_cpus: boolean; affinity_applied: boolean; workers: Worker[]}; lanes?: Lane[]; settings: QueueSettings; counts: {queued: number; running: number; failed: number}; active: Job | null; active_jobs?: Job[]}
const emit = defineEmits<{changed: []; lane: [id: string]}>()
const laneLimits = ref<Record<string, number>>({})
const dirtyLanes = new Set<string>()
async function saveLane(lane: Lane, pause = false) {
  if (busy.value) return
  action.value = lane.id; error.value = ''; feedback.value = ''; ++requestId
  try {
    const result = await api<{paused: boolean; concurrency: number}>(`/queue/lanes/${lane.id}`, {method: 'PUT', body: JSON.stringify(pause ? {paused: !lane.paused} : {concurrency: laneLimits.value[lane.id]})})
    if (disposed) return
    Object.assign(lane, result); if (!pause) { laneLimits.value[lane.id] = result.concurrency; dirtyLanes.delete(lane.id) }; emit('changed')
  } catch (e) { if (!disposed) error.value = errorMessage(e) }
  finally { action.value = ''; if (!disposed) load(true) }
}
const state = ref<QueueState | null>(null)
const loading = ref(true)
const action = ref('')
const error = ref('')
const feedback = ref('')
const minInterval = ref(2)
const maxInterval = ref(5)
const intervalDirty = ref(false)
const concurrency = ref(1)
const concurrencyDirty = ref(false)
const coverSource = ref('')
const coverResult = ref<{queued: number; skipped: number; has_more: boolean; message: string} | null>(null)
let timer: ReturnType<typeof setInterval> | undefined
let requestId = 0
let polling = false
let disposed = false
const busy = computed(() => loading.value || !!action.value)
const activeJobs = computed(() => state.value?.active_jobs ?? (state.value?.active ? [state.value.active] : []))
const workers = computed(() => Object.fromEntries((state.value?.runtime?.workers ?? []).map(worker => [worker.lane, worker])))
const runningWorkers = computed(() => state.value?.runtime?.workers.filter(worker => worker.state === 'running').length ?? 0)
function workerLabel(id: string) {
  const worker = workers.value[id]
  if (!worker) return ''
  const placement = worker.affinity_applied ? `CPU ${worker.cpus.join(', ')}` : '未绑定 CPU'
  return `${worker.state === 'running' ? '工作进程' : worker.state === 'restarting' ? '进程重启中' : worker.state === 'unresponsive' ? '进程心跳超时' : '进程启动中'} ${worker.pid ?? ''} · ${placement}`
}
function progress(job: Job) { return Math.max(0, Math.min(100, Number(job.progress) || 0)) }
async function load(silent = false) {
  if (disposed || polling || action.value) return
  const id = ++requestId; polling = true
  if (!silent) loading.value = true
  try {
    const next = await api<QueueState>('/queue/status')
    if (disposed || id !== requestId) return
    state.value = next; error.value = ''
    for (const lane of next.lanes ?? []) if (!dirtyLanes.has(lane.id)) laneLimits.value[lane.id] = lane.concurrency
    if (!intervalDirty.value) { minInterval.value = next.settings.min_interval_seconds; maxInterval.value = next.settings.max_interval_seconds }
    if (!concurrencyDirty.value) concurrency.value = next.settings.concurrency
  } catch (failure) { if (!disposed && id === requestId) error.value = errorMessage(failure) }
  finally { polling = false; if (id === requestId) loading.value = false }
}
async function save(change: 'paused' | 'interval' | 'rate_limit' | 'concurrency') {
  if (!state.value || busy.value) return
  if (change === 'interval' && (!Number.isFinite(minInterval.value) || !Number.isFinite(maxInterval.value) || minInterval.value < 0 || maxInterval.value > 60 || minInterval.value > maxInterval.value)) { error.value = '请求间隔需满足：0 ≤ 最短 ≤ 最长 ≤ 60 秒。'; return }
  if (change === 'concurrency' && (!Number.isInteger(concurrency.value) || concurrency.value < 1 || concurrency.value > 8)) { error.value = '采集队列统一并发需为 1–8 的整数。'; return }
  const current = ++requestId
  action.value = change; error.value = ''; feedback.value = ''
  try {
    const payload = change === 'paused' ? {paused: !state.value.settings.paused} : change === 'rate_limit' ? {rate_limit_enabled: !state.value.settings.rate_limit_enabled} : change === 'concurrency' ? {concurrency: concurrency.value} : {min_interval_seconds: minInterval.value, max_interval_seconds: maxInterval.value}
    const next = await api<QueueSettings>('/queue/settings', {method: 'PUT', body: JSON.stringify(payload)})
    if (disposed || current !== requestId) return
    state.value.settings = next
    if (change === 'interval') { minInterval.value = next.min_interval_seconds; maxInterval.value = next.max_interval_seconds; intervalDirty.value = false; feedback.value = `请求间隔已设为 ${next.min_interval_seconds}–${next.max_interval_seconds} 秒。` }
    else if (change === 'concurrency') { concurrency.value = next.concurrency; concurrencyDirty.value = false; feedback.value = `采集队列统一并发已设为 ${next.concurrency}；正在执行的任务会继续完成。` }
    else if (change === 'rate_limit') feedback.value = next.rate_limit_enabled ? '请求限速已开启，继续使用保存的间隔。' : '请求限速已关闭，不再随机等待；平台 429 等待仍保留。'
    else feedback.value = next.paused ? '采集已暂停，当前请求结束后生效；其他队列按各自开关运行。' : '平台采集队列已恢复。'
    emit('changed')
  } catch (failure) { if (!disposed && current === requestId) error.value = errorMessage(failure) }
  finally { action.value = ''; if (!disposed) load(true) }
}
async function queueMissingCovers() {
  if (busy.value) return
  action.value = 'covers'; error.value = ''; feedback.value = ''; coverResult.value = null; requestId++
  try {
    coverResult.value = await api('/queue/covers', {method: 'POST', body: JSON.stringify(coverSource.value ? {source: coverSource.value} : {})})
    emit('changed')
  } catch (failure) { error.value = errorMessage(failure) }
  finally { action.value = ''; load(true) }
}
onMounted(() => { load(); timer = setInterval(() => load(true), 6000) })
onBeforeUnmount(() => { disposed = true; requestId++; clearInterval(timer) })
</script>

<template>
  <section class="panel download-queue-panel">
    <header><div><Download :size="21" /><h2>队列管理</h2><span v-if="state" class="status-pill" :class="state.settings.paused ? 'queued' : 'running'">{{ state.settings.paused ? '已暂停' : '运行中' }}</span></div><div class="download-queue-actions"><button class="icon-button" aria-label="刷新采集队列" :disabled="busy" @click="load()"><RefreshCw :size="16" :class="{spin: loading}" /></button><button v-if="state" class="button secondary small" :disabled="busy" @click="save('paused')"><LoaderCircle v-if="action === 'paused'" class="spin" :size="16" /><Play v-else-if="state.settings.paused" :size="16" /><Pause v-else :size="16" />{{ state.settings.paused ? '继续采集' : '暂停采集' }}</button></div></header>
    <div v-if="loading && !state" class="inline-loading"><LoaderCircle class="spin" :size="18" />正在读取采集队列…</div>
    <template v-if="state">
      <div v-if="state.runtime?.mode === 'process'" class="queue-runtime" role="status"><strong>独立工作进程 {{ runningWorkers }}/{{ state.runtime.workers.length }}</strong><span>Web {{ state.runtime.affinity_applied ? `CPU ${state.runtime.api_cpus.join(', ')}` : '未绑定 CPU' }}</span><span v-if="state.runtime.shared_cpus">可用 CPU 较少或配置重叠，部分队列共用</span><span v-else-if="state.runtime.affinity_applied">各队列分配独立的逻辑 CPU</span></div>
      <div class="queue-lanes"><article v-for="lane in state.lanes" :key="lane.id"><button type="button" class="text-button" @click="emit('lane', lane.id)"><strong>{{ lane.label }}</strong></button><span>{{ lane.counts.running }} 运行 · {{ lane.counts.queued }} 等待 · {{ lane.counts.failed }} 失败</span><small v-if="workers[lane.id]" class="queue-worker-info">{{ workerLabel(lane.id) }}</small><small>{{ lane.paused ? '本队列已暂停' : lane.blocked_by_master ? '采集总开关已暂停' : '可执行' }}</small><form @submit.prevent="saveLane(lane)"><label>并发<input v-model.number="laneLimits[lane.id]" type="number" min="1" max="8" required :aria-label="`${lane.label}并发`" :disabled="busy" @input="dirtyLanes.add(lane.id)" /></label><button class="button secondary small" :disabled="busy">保存</button><button type="button" class="text-button" :disabled="busy" @click="saveLane(lane, true)">{{ lane.paused ? '恢复' : '暂停' }}</button></form></article></div><p v-if="state.lanes" class="field-hint">点击队列名称查看任务。队列独立运行；降低并发后，已执行的任务继续完成。采集总开关控制下载、详情和检查。</p><div class="download-queue-counts"><span><strong>{{ state.counts.queued }}</strong>等待</span><span><strong>{{ state.counts.running }}</strong>处理中</span><span :class="{'danger-text': state.counts.failed > 0}"><strong>{{ state.counts.failed }}</strong>失败</span></div>
      <div class="request-limit-row"><div><strong>启用请求限速</strong><p class="field-hint">关闭后取消随机等待；平台返回的 429 等待仍保留。</p></div><button type="button" class="request-limit-switch" :class="{enabled: state.settings.rate_limit_enabled}" role="switch" aria-label="启用请求限速" :aria-checked="state.settings.rate_limit_enabled" :disabled="busy" @click="save('rate_limit')"><span /></button></div>
      <form class="download-queue-settings" @submit.prevent="save('interval')"><label class="field">最短间隔（秒）<input v-model.number="minInterval" type="number" min="0" :max="maxInterval" step="0.1" required :disabled="busy || !state.settings.rate_limit_enabled" @input="intervalDirty = true" /></label><label class="field">最长间隔（秒）<input v-model.number="maxInterval" type="number" :min="minInterval" max="60" step="0.1" required :disabled="busy || !state.settings.rate_limit_enabled" @input="intervalDirty = true" /></label><button class="button secondary small" :disabled="busy || !intervalDirty || !state.settings.rate_limit_enabled"><LoaderCircle v-if="action === 'interval'" class="spin" :size="16" /><Save v-else :size="16" />保存间隔</button><p>开启限速后，所有并发任务共用上述请求间隔。</p></form>
      <form class="download-queue-settings" @submit.prevent="save('concurrency')"><label class="field">采集队列统一并发<input v-model.number="concurrency" type="number" min="1" max="8" step="1" required :disabled="busy" @input="concurrencyDirty = true" /></label><button class="button secondary small" :disabled="busy || !concurrencyDirty"><LoaderCircle v-if="action === 'concurrency'" class="spin" :size="16" /><Save v-else :size="16" />保存并发数</button><p>同时设置下载、详情、检查三个队列各自的并发上限（1–8）。也可用上方卡片分别设置；AI 和汉化各自独立。</p></form>
      <div v-for="job in activeJobs" :key="job.id" class="download-queue-active"><div><strong>{{ job.label || jobKindLabels[job.kind] || job.kind }}</strong><code>{{ job.id.slice(0, 8) }}</code><span>{{ progress(job) }}%</span></div><div class="download-queue-progress" role="progressbar" :aria-label="`采集任务 ${job.id.slice(0, 8)} 进度`" :aria-valuenow="progress(job)" aria-valuemin="0" aria-valuemax="100"><span :style="{width: `${progress(job)}%`}"></span></div><p v-if="state.settings.paused">当前请求结束后任务重新排队；继续采集时会重新请求正文。</p></div>
      <p v-if="!activeJobs.length" class="download-queue-note">{{ state.settings.paused ? '任务等待继续采集；暂停的正文任务会重新请求，不从原页码续传。' : state.counts.queued ? '等待后台开始下一项任务。' : '当前没有采集任务。' }}</p>
      <div class="queue-cover-backfill"><div><h3>补全缺失封面</h3><p>为已有但缺少封面和正文的作品获取基本资料，每次最多 500 部；不下载正文、不修改订阅设置。</p></div><div class="queue-cover-controls"><label class="field"><span class="sr-only">补全封面的平台</span><select v-model="coverSource" :disabled="busy"><option value="">全部平台</option><option v-for="source in ['pixiv', 'ehentai', 'picacg', 'jm']" :key="source" :value="source">{{ sourceLabels[source] }}</option></select></label><button class="button secondary small" :disabled="busy" @click="queueMissingCovers"><LoaderCircle v-if="action === 'covers'" class="spin" :size="16" /><Image v-else :size="16" />补全封面</button></div><div v-if="coverResult" class="queue-cover-result" role="status"><strong>{{ coverResult.queued }} 部已入队 · {{ coverResult.skipped }} 部已跳过</strong><p>{{ coverResult.message }}</p><p v-if="coverResult.has_more">仍有待处理作品，可再次点击补全封面。</p></div></div>
    </template>
    <div v-if="error" class="alert error" role="alert"><AlertCircle :size="18" /><span>{{ error }}</span><button v-if="!state" class="text-button" :disabled="busy" @click="load()">重试</button></div>
    <p v-if="feedback" class="download-queue-feedback" role="status"><Check :size="15" />{{ feedback }}</p>
  </section>
</template>

<style scoped>
.queue-runtime {display:flex;gap:14px;flex-wrap:wrap;padding:14px 0;font-size:13px;color:var(--text-secondary);}.queue-runtime strong {color:var(--text-primary);}.queue-worker-info {overflow-wrap:anywhere;}

.queue-lanes {display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:12px;margin:20px 0;}.queue-lanes article {border:1px solid var(--line);border-radius:10px;padding:16px;display:flex;flex-direction:column;gap:10px;}.queue-lanes article>span,.queue-lanes small {font-size:13px;color:var(--text-secondary);}.queue-lanes form {display:flex;gap:10px;align-items:center;flex-wrap:wrap;}.queue-lanes label {display:flex;align-items:center;gap:8px;font-size:13px;}.queue-lanes input {width:65px;padding:6px;}.queue-lanes .text-button {justify-content:flex-start;}

.request-limit-row {display:flex;align-items:center;justify-content:space-between;gap:16px;margin:16px 0;}
.request-limit-row strong {font-size:14px;}.request-limit-row .field-hint {margin:5px 0 0;}
.request-limit-switch {width:44px;height:26px;flex:0 0 44px;border:0;border-radius:20px;background:#8b909c;padding:3px;cursor:pointer;}
.request-limit-switch span {display:block;width:20px;height:20px;border-radius:50%;background:white;transition:transform .15s;}
.request-limit-switch.enabled {background:#5967aa;}.request-limit-switch.enabled span {transform:translateX(18px);}
.request-limit-switch:focus-visible {outline:2px solid #5967aa;outline-offset:3px;}.request-limit-switch:disabled {opacity:.5;cursor:wait;}
</style>
