<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { FolderHeart, Plus, Pencil, Trash2, Search, ChevronLeft, ChevronRight } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { Item } from '../types'
import { sourceLabels, typeLabels } from '../types'
import BookCard from './BookCard.vue'
import Modal from './Modal.vue'
const props = defineProps<{blurAdult: boolean; revision?: number; history?: boolean}>()
defineEmits<{open: [item: Item]}>()
const groups = ref<{id: string; name: string; count: number}[]>([]), all = ref(0), ungrouped = ref(0)
const selected = ref(''), query = ref(''), source = ref(''), type = ref(''), downloaded = ref(false)
const items = ref<Item[]>([]), total = ref(0), page = ref(1), loading = ref(true), busy = ref(false), error = ref('')
const edit = ref<'create' | 'rename' | ''>(''), name = ref(''), editError = ref(''), confirmDelete = ref(false)
let generation = 0, disposed = false, timer: ReturnType<typeof setTimeout> | undefined
async function loadGroups() {
  if (props.history) return
  const result = await api<{items: typeof groups.value; total_favorites: number; ungrouped_count: number}>('/favorite-groups')
  if (disposed) return
  groups.value = result.items; all.value = result.total_favorites; ungrouped.value = result.ungrouped_count
}
async function load() {
  const current = ++generation; loading.value = true; error.value = ''
  const params = new URLSearchParams({page: String(page.value), page_size: '24', ...(props.history ? {history: 'true'} : {favorite: 'true'})})
  if (!props.history && selected.value) params.set('favorite_group', selected.value)
  if (query.value.trim()) params.set('q', query.value.trim())
  if (source.value) params.set('source', source.value)
  if (type.value) params.set('type', type.value)
  if (downloaded.value) params.set('downloaded', 'true')
  try {
    const result = await api<{items: Item[]; total: number}>(`/items?${params}`)
    if (disposed || current !== generation) return
    const last = Math.max(1, Math.ceil(result.total / 24))
    if (page.value > last) {page.value = last; return}
    items.value = result.items; total.value = result.total
  } catch (e) { if (!disposed && current === generation) error.value = errorMessage(e) }
  finally { if (current === generation) loading.value = false }
}
async function refresh() {try {await loadGroups(); await load()} catch (e) {error.value = errorMessage(e); loading.value = false}}
function begin(mode: 'create' | 'rename') {editError.value = ''; edit.value = mode; name.value = mode === 'rename' ? groups.value.find(g => g.id === selected.value)?.name || '' : ''}
async function saveGroup() {
  if (busy.value) return
  if (!name.value.trim()) {editError.value = '请输入分组名称'; return}
  busy.value = true; editError.value = ''
  try {
    const group = await api<{id: string}>(edit.value === 'create' ? '/favorite-groups' : `/favorite-groups/${selected.value}`, {method: edit.value === 'create' ? 'POST' : 'PUT', body: JSON.stringify({name: name.value.trim()})})
    edit.value = ''; await loadGroups(); selected.value = group.id
  } catch (e) {editError.value = errorMessage(e)} finally {busy.value = false}
}
async function remove() {
  if (busy.value) return
  busy.value = true; error.value = ''
  try {
    await api(props.history ? '/history' : `/favorite-groups/${selected.value}`, {method: 'DELETE'})
    confirmDelete.value = false; selected.value = ''; page.value = 1; await refresh()
  } catch (e) {error.value = errorMessage(e)} finally {busy.value = false}
}
function resetPage() {if (page.value !== 1) page.value = 1; else load()}
watch([selected, source, type, downloaded], () => {confirmDelete.value = false; resetPage()})
watch(query, () => {clearTimeout(timer); timer = setTimeout(resetPage, 300)})
watch(page, () => load())
watch(() => props.revision, () => refresh())
onMounted(refresh)
onBeforeUnmount(() => {disposed = true; ++generation; clearTimeout(timer)})
</script>
<template>
  <section class="favorites-panel">
    <aside v-if="!history" class="panel favorite-folders"><header><h2>收藏分组</h2><button class="icon-button" aria-label="新建收藏分组" @click="begin('create')"><Plus :size="19" /></button></header><button class="folder-choice" :class="{selected: !selected}" @click="selected = ''"><span>全部收藏</span><strong>{{ all }}</strong></button><button class="folder-choice" :class="{selected: selected === 'ungrouped'}" @click="selected = 'ungrouped'"><span>未分组</span><strong>{{ ungrouped }}</strong></button><button v-for="group in groups" :key="group.id" class="folder-choice" :class="{selected: selected === group.id}" @click="selected = group.id"><span><FolderHeart :size="16" />{{ group.name }}</span><strong>{{ group.count }}</strong></button></aside>
    <div class="favorite-results">
      <div class="panel favorite-toolbar"><div class="search-field"><Search :size="17" /><input v-model="query" :aria-label="history ? '搜索浏览历史' : '搜索我的收藏'" placeholder="搜索作品、作者、标签…" /></div><select v-model="source" aria-label="筛选平台"><option value="">全部平台</option><option v-for="(label, id) in sourceLabels" :key="id" :value="id">{{ label }}</option></select><select v-model="type" aria-label="筛选类型"><option value="">全部类型</option><option v-for="(label, id) in typeLabels" :key="id" :value="id">{{ label }}</option></select><label><input v-model="downloaded" type="checkbox" />仅已下载</label></div>
      <div class="favorite-heading"><h2>{{ history ? '最近浏览' : !selected ? '全部收藏' : selected === 'ungrouped' ? '未分组' : groups.find(g => g.id === selected)?.name }}</h2><span>{{ total }} 部作品</span><div v-if="selected && selected !== 'ungrouped' && !history"><button class="text-button" @click="begin('rename')"><Pencil :size="14" />重命名</button><button class="text-button danger-text" @click="confirmDelete = true"><Trash2 :size="14" />删除分组</button></div><button v-if="history" class="text-button danger-text" :disabled="!total || busy" @click="confirmDelete = true">清空浏览历史</button></div>
      <div v-if="confirmDelete" class="alert error"><span>{{ history ? '清空浏览历史？作品、收藏和阅读进度保留。' : '删除此分组？作品仍保留在我的收藏中。' }}</span><button class="button danger small" :disabled="busy" @click="remove">确认{{ history ? '清空' : '删除' }}</button><button class="text-button" :disabled="busy" @click="confirmDelete = false">取消</button></div>
      <div v-if="error" class="alert error" role="alert">{{ error }}<button class="text-button" @click="refresh">重试</button></div>
      <p v-if="loading && !items.length" class="field-hint">正在加载…</p><div v-else-if="items.length" class="book-grid" :class="{'is-loading': loading}"><BookCard v-for="item in items" :key="item.id" :item="item" :blur-adult="blurAdult" @open="$emit('open', $event)" /></div><div v-else-if="!error" class="panel favorite-empty"><FolderHeart :size="32" /><h3>{{ history ? '暂无浏览记录' : '暂无收藏作品' }}</h3><p>{{ history ? '打开作品详情后会记录在这里。' : '在作品详情点击「收藏作品」，选择分组并评分。' }}</p></div>
      <div v-if="total > 24" class="pagination"><span>第 {{ page }} / {{ Math.ceil(total / 24) }} 页</span><button class="button secondary small" :disabled="loading || page <= 1" @click="page--"><ChevronLeft :size="16" />上一页</button><button class="button secondary small" :disabled="loading || page >= Math.ceil(total / 24)" @click="page++">下一页<ChevronRight :size="16" /></button></div>
    </div>
    <Modal v-if="edit" :title="edit === 'create' ? '新建收藏分组' : '重命名分组'" :busy="busy" @close="edit = ''"><form @submit.prevent="saveGroup"><label class="field">分组名称<input v-model="name" maxlength="80" required :disabled="busy" /></label><p v-if="editError" class="alert error" role="alert">{{ editError }}</p><div class="modal-footer"><button type="button" class="button secondary" :disabled="busy" @click="edit = ''">取消</button><button class="button primary" :disabled="busy">保存分组</button></div></form></Modal>
  </section>
</template>
<style scoped>
.favorites-panel {display:grid;grid-template-columns:220px minmax(0,1fr);gap:24px;align-items:start;}.favorites-panel:has(>.favorite-results:first-child) {grid-template-columns:1fr;}.favorite-folders {padding:14px;}.favorite-folders header,.favorite-heading {display:flex;align-items:center;gap:12px;}.favorite-folders header {justify-content:space-between;padding:4px 8px 12px;}.favorite-folders h2,.favorite-heading h2 {font-size:17px;}.folder-choice {display:flex;justify-content:space-between;align-items:center;width:100%;padding:12px 10px;border:0;border-radius:8px;background:transparent;color:var(--text-secondary);font:inherit;cursor:pointer;text-align:left;gap:10px;}.folder-choice.selected {background:var(--accent-soft);color:var(--accent);}.folder-choice span {display:flex;gap:8px;align-items:center;overflow-wrap:anywhere;min-width:0;}.folder-choice strong {font-size:13px;font-variant-numeric:tabular-nums;}.favorite-results {min-width:0;}.favorite-toolbar {display:flex;flex-wrap:wrap;align-items:center;gap:12px;padding:16px;margin-bottom:20px;}.favorite-toolbar .search-field {flex:1;min-width:180px;}.favorite-toolbar select {max-width:160px;padding:9px;}.favorite-toolbar label {display:flex;align-items:center;gap:7px;font-size:13px;}.favorite-heading {flex-wrap:wrap;margin-bottom:18px;}.favorite-heading>span {color:var(--text-secondary);font-size:13px;margin-right:auto;}.favorite-heading>div {display:flex;gap:12px;}.favorite-empty {text-align:center;padding:60px 16px;color:var(--text-secondary);}.favorite-empty h3 {margin:12px 0 8px;}.alert {margin:12px 0;flex-wrap:wrap;}
@media(max-width:1000px) {.favorites-panel {grid-template-columns:180px minmax(0,1fr);gap:16px;}}
@media(max-width:700px) {.favorites-panel {grid-template-columns:1fr;}.favorite-folders {display:flex;flex-wrap:wrap;gap:6px;}.favorite-folders header {width:100%;}.folder-choice {width:auto;max-width:100%;padding:8px 12px;}.favorite-toolbar .search-field {flex-basis:100%;}.favorite-toolbar select {max-width:100%;flex:1;}.favorite-heading h2 {overflow-wrap:anywhere;}}
</style>
