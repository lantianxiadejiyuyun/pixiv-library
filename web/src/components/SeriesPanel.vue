<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { AlertCircle, ArrowDown, ArrowUp, ArrowLeft, ArrowRight, BookOpen, Check, Layers3, LoaderCircle, Pencil, Plus, RefreshCw, Search, X } from 'lucide-vue-next'
import { api, errorMessage, post } from '../api'
import type { Item, Job, SeriesDetail, SeriesEntry } from '../types'
import { sourceLabels, typeLabels } from '../types'

const props = withDefaults(defineProps<{item: Item; editable?: boolean}>(), {editable: true})
const emit = defineEmits<{open: [id: string]; changed: []; queued: [job: Job]}>()
const selectedId = ref('')
const series = ref<SeriesDetail | null>(null)
const loading = ref(false)
const busy = ref(false)
const error = ref('')
const feedback = ref('')
const editing = ref(false)
const editId = ref('')
const editRevision = ref(0)
const title = ref('')
const kind = ref<'novel' | 'manga'>('manga')
type DraftEntry = {item_id: string; title: string; label: string}
const entries = ref<DraftEntry[]>([])
const query = ref('')
const results = ref<Item[]>([])
const searching = ref(false)
const searchError = ref('')
let searchTimer: ReturnType<typeof setTimeout> | undefined
let request = 0
let searchRequest = 0
let disposed = false
const orderedEntries = computed(() => [...(series.value?.entries || [])].sort((a, b) => a.position - b.position))
const currentPosition = computed(() => orderedEntries.value.findIndex(entry => entry.item_id === props.item.id))
const previous = computed(() => currentPosition.value > 0 ? orderedEntries.value[currentPosition.value - 1] : null)
const next = computed(() => currentPosition.value >= 0 ? orderedEntries.value[currentPosition.value + 1] : null)
function entryStatus(entry: SeriesEntry) { return ({metadata: '待下载', queued: '等待下载', downloading: '下载中', ready: entry.page_count > 0 ? '已离线' : '待下载', failed: '下载失败'})[entry.content_state] || '状态待确认' }
async function loadSeries() {
  const id = ++request
  if (!selectedId.value) { series.value = null; loading.value = false; return }
  loading.value = true; error.value = ''
  try { const result = await api<SeriesDetail>(`/series/${selectedId.value}`); if (!disposed && id === request) series.value = result }
  catch (failure) { if (!disposed && id === request) { error.value = errorMessage(failure); series.value = null } }
  finally { if (id === request) loading.value = false }
}
function chooseSeries() { editing.value = false; feedback.value = ''; loadSeries() }
async function fetchOfficial() {
  if (busy.value || !props.item.source_series) return
  busy.value = true; error.value = ''; feedback.value = ''
  try {
    const job = await post<Job>('/series/import', {platform: 'pixiv', url_or_id: props.item.source_series.remote_id, kind: props.item.source_series.kind})
    feedback.value = '已创建目录任务；成员只获取封面与资料，正文需单独下载。'; emit('queued', job)
  } catch (failure) { error.value = errorMessage(failure) }
  finally { busy.value = false }
}
function beginEdit(existing = false) {
  const current = existing ? series.value : null
  if (current && current.origin !== 'manual') return
  editId.value = current?.id || ''; editRevision.value = current?.revision || 0
  title.value = current?.title || ''; kind.value = current?.kind || (props.item.type === 'novel' ? 'novel' : 'manga')
  entries.value = current ? orderedEntries.value.map(entry => ({item_id: entry.item_id, title: entry.title, label: entry.label || ''})) : [{item_id: props.item.id, title: props.item.title, label: ''}]
  editing.value = true; query.value = ''; results.value = []; searchError.value = ''; error.value = ''; feedback.value = ''; searchItems()
}
async function searchItems() {
  if (!editing.value) return
  const id = ++searchRequest; searching.value = true; searchError.value = ''
  try {
    const result = await api<{items: Item[]}>(`/items?${new URLSearchParams({q: query.value.trim(), page_size: '50', sort: 'latest'})}`)
    if (!disposed && id === searchRequest) results.value = result.items
  } catch (failure) { if (!disposed && id === searchRequest) searchError.value = errorMessage(failure) }
  finally { if (id === searchRequest) searching.value = false }
}
function addItem(item: Item) { if (!entries.value.some(entry => entry.item_id === item.id)) entries.value.push({item_id: item.id, title: item.title, label: ''}) }
function move(index: number, direction: number) {
  const destination = index + direction
  if (destination < 0 || destination >= entries.value.length) return
  const entry = entries.value.splice(index, 1)[0]!
  entries.value.splice(destination, 0, entry)
}
async function saveSeries() {
  if (busy.value || !title.value.trim() || (!editId.value && !entries.value.length)) return
  busy.value = true; error.value = ''
  try {
    const payload = {title: title.value.trim(), entries: entries.value.map(({item_id, label}) => ({item_id, label: label.trim()}))}
    const result = editId.value
      ? await api<SeriesDetail>(`/series/${editId.value}`, {method: 'PUT', body: JSON.stringify({...payload, revision: editRevision.value})})
      : await post<SeriesDetail>('/series', {...payload, kind: kind.value})
    selectedId.value = result.id; series.value = result; editing.value = false; feedback.value = '系列顺序已保存。'; emit('changed')
  } catch (failure) { error.value = errorMessage(failure) }
  finally { busy.value = false }
}
watch(query, () => { clearTimeout(searchTimer); searchTimer = setTimeout(searchItems, 300) })
watch(() => `${props.item.id}:${(props.item.series || []).map(entry => `${entry.id}/${entry.revision}`).join(',')}`, () => {
  const summaries = props.item.series || []
  if (!summaries.some(entry => entry.id === selectedId.value)) selectedId.value = summaries[0]?.id || ''
  if (!editing.value) loadSeries()
}, {immediate: true})
onBeforeUnmount(() => { disposed = true; request++; searchRequest++; clearTimeout(searchTimer) })
</script>

<template>
  <section class="series-panel">
    <div class="series-heading"><h3><Layers3 :size="18" />系列与上下集</h3><button v-if="editable && !editing" class="text-button" :disabled="busy" @click="beginEdit()"><Plus :size="15" />手动关联</button></div>
    <div v-if="item.source_series" class="series-official"><div><span>Pixiv 官方系列</span><strong>{{ item.source_series.title }}</strong></div><button class="button secondary small" :disabled="busy || editing" @click="fetchOfficial"><LoaderCircle v-if="busy && !editing" :size="15" class="spin" /><RefreshCw v-else :size="15" />{{ item.series?.some(entry => entry.origin === 'source') ? '更新目录' : '获取目录' }}</button></div>
    <template v-if="!editing">
      <div v-if="item.series?.length" class="series-selection"><label class="field"><span class="sr-only">选择系列</span><select v-model="selectedId" :disabled="loading || busy" @change="chooseSeries"><option v-for="entry in item.series" :key="entry.id" :value="entry.id">{{ entry.title }} · {{ entry.origin === 'manual' ? '手动' : '官方' }}</option></select></label><button class="icon-button" :disabled="loading || busy" aria-label="刷新系列目录" @click="loadSeries"><RefreshCw :size="16" :class="{spin: loading}" /></button><button v-if="editable && series?.origin === 'manual'" class="text-button" :disabled="loading || busy" @click="beginEdit(true)"><Pencil :size="14" />编辑</button></div>
      <div v-if="loading" class="inline-loading"><LoaderCircle :size="17" class="spin" />读取系列目录…</div>
      <template v-else-if="series">
        <div class="series-neighbors"><button class="button secondary small" :disabled="!previous" @click="previous && emit('open', previous.item_id)"><ArrowLeft :size="16" />上一篇</button><span>{{ currentPosition >= 0 ? `当前第 ${currentPosition + 1} / ${orderedEntries.length} 篇` : `共 ${orderedEntries.length} 篇` }}</span><button class="button secondary small" :disabled="!next" @click="next && emit('open', next.item_id)">下一篇<ArrowRight :size="16" /></button></div>
        <p v-if="!series.complete" class="series-notice">目录不完整，可能达到单次获取上限。</p>
        <ol class="series-entry-list"><li v-for="entry in orderedEntries" :key="entry.item_id"><button :class="{current: entry.item_id === item.id}" :aria-current="entry.item_id === item.id ? 'true' : undefined" @click="emit('open', entry.item_id)"><span class="series-entry-number">{{ entry.position + 1 }}</span><div><strong>{{ entry.label || entry.title }}</strong><span v-if="entry.label">{{ entry.title }}</span></div><span class="status-pill" :class="entry.content_state === 'ready' && entry.page_count > 0 ? 'success' : entry.content_state === 'failed' ? 'failed' : 'neutral'">{{ entryStatus(entry) }}</span></button></li></ol>
        <p class="series-notice">未下载的作品会打开详情，可在那里下载正文。</p>
      </template>
      <p v-else-if="!loading && !error && !item.source_series" class="series-notice">尚未关联系列。可手动选择已有作品并设置上下集顺序。</p>
    </template>
    <form v-else class="series-editor" @submit.prevent="saveSeries">
      <div class="form-grid"><label class="field">系列名称<input v-model="title" required maxlength="500" :disabled="busy" /></label><label class="field">系列类型<select v-model="kind" :disabled="busy || !!editId"><option value="manga">漫画 / 图集</option><option value="novel">小说</option></select></label></div>
      <ol class="series-draft-list"><li v-for="(entry, index) in entries" :key="entry.item_id"><span class="series-entry-number">{{ index + 1 }}</span><div><strong>{{ entry.title }}</strong><input v-model="entry.label" :disabled="busy" :aria-label="`第 ${index + 1} 篇名称`" placeholder="集名选填，例如：上篇、下篇" maxlength="200" /></div><div class="series-order-actions"><button type="button" class="icon-button" :disabled="busy || index === 0" :aria-label="`上移 ${entry.title}`" @click="move(index, -1)"><ArrowUp :size="16" /></button><button type="button" class="icon-button" :disabled="busy || index === entries.length - 1" :aria-label="`下移 ${entry.title}`" @click="move(index, 1)"><ArrowDown :size="16" /></button><button type="button" class="icon-button danger-text" :disabled="busy" :aria-label="`从系列移除 ${entry.title}`" @click="entries.splice(index, 1)"><X :size="16" /></button></div></li></ol>
      <p class="series-notice">移除关联不会删除作品。官方目录不受手动系列影响。</p>
      <label class="field series-search"><span>添加书库作品</span><span class="search-field"><Search :size="16" /><input v-model="query" :disabled="busy" placeholder="搜索标题、作者或标签" /></span></label>
      <div v-if="searching" class="inline-loading"><LoaderCircle :size="16" class="spin" />搜索作品…</div>
      <div v-else class="series-search-results"><button v-for="item in results" :key="item.id" type="button" :disabled="busy || entries.some(entry => entry.item_id === item.id)" @click="addItem(item)"><BookOpen :size="17" /><div><strong>{{ item.title }}</strong><span>{{ sourceLabels[item.source] || item.source }} · {{ typeLabels[item.type] }}</span></div><Check v-if="entries.some(entry => entry.item_id === item.id)" :size="17" /><Plus v-else :size="17" /></button><p v-if="!results.length && !searchError">未找到作品，请尝试其他搜索词。</p></div>
      <p v-if="searchError" class="series-error">{{ searchError }}</p><p class="series-notice">最多显示 50 条搜索结果，可输入更具体的名称。</p>
      <div class="series-editor-actions"><button type="button" class="button secondary small" :disabled="busy" @click="editing = false">取消</button><button class="button primary small" :disabled="busy || (!editId && !entries.length) || !title.trim()"><LoaderCircle v-if="busy" :size="16" class="spin" /><Check v-else :size="16" />保存系列</button></div>
    </form>
    <div v-if="error" class="alert error" role="alert"><AlertCircle :size="17" /><span>{{ error }}</span></div>
    <p v-if="feedback" class="series-feedback" role="status">{{ feedback }}</p>
  </section>
</template>
