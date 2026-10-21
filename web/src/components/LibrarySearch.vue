<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { Search, X, History } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { Tag } from '../types'
import { readSearchHistory, rememberSearch, writeSearchHistory, searchKey, searchLabel, type SearchEntry, type TagMode } from '../search_history'

const props = defineProps<{ modelValue: string; tags: Tag[]; mode: TagMode; source: string }>()
const emit = defineEmits<{
  'update:modelValue': [value: string]; 'update:tags': [tags: Tag[]]; 'update:mode': [mode: TagMode]; submit: []; browse: []
}>()
const root = ref<HTMLElement>(), open = ref(false), suggestions = ref<Tag[]>([]), loading = ref(false), error = ref('')
const history = ref(readSearchHistory()), active = ref(-1)
let revision = 0, disposed = false, timer: ReturnType<typeof setTimeout> | undefined
let composing = false
function input(event: Event) { if (!composing) emit('update:modelValue', (event.target as HTMLInputElement).value) }
function compositionEnd(event: CompositionEvent) { composing = false; input(event) }
function remember(entry: SearchEntry = { query: props.modelValue, tags: props.tags, mode: props.mode }) { history.value = rememberSearch(history.value, entry) }
watch(() => props.mode, () => { if (props.tags.length) remember() })
onMounted(() => { if (props.tags.length) remember() })
async function suggest() {
  const request = ++revision, q = props.modelValue.trim()
  if (!q || !open.value) { loading.value = false; suggestions.value = []; return }
  loading.value = true; error.value = ''
  try {
    const params = new URLSearchParams({ q: q.slice(0, 191), limit: '12', suggest: 'true' })
    if (props.source) params.set('source', props.source)
    const result = await api<Tag[]>(`/tags?${params}`)
    if (!disposed && request === revision) suggestions.value = result.filter(tag => !props.tags.some(selected => selected.id === tag.id)).slice(0, 8)
  } catch (failure) { if (!disposed && request === revision) error.value = errorMessage(failure) }
  finally { if (!disposed && request === revision) loading.value = false }
}
watch(() => [props.modelValue, props.source, props.tags, open.value], () => {
  revision++; clearTimeout(timer); suggestions.value = []; active.value = -1; error.value = ''; loading.value = !!props.modelValue.trim() && open.value
  timer = setTimeout(suggest, 250)
})
function choose(tag: Tag) {
  if (props.tags.some(selected => selected.id === tag.id)) return
  if (props.tags.length >= 20) { error.value = '最多选择 20 个标签'; return }
  const tags = [...props.tags, tag]
  emit('update:tags', tags); emit('update:modelValue', ''); emit('submit')
  remember({ query: '', tags, mode: props.mode }); suggestions.value = []; active.value = -1
}
function restore(entry: SearchEntry) {
  emit('update:modelValue', entry.query); emit('update:tags', [...entry.tags]); emit('update:mode', entry.mode); emit('submit')
  remember(entry); open.value = false
}
function removeHistory(entry?: SearchEntry) { history.value = entry ? history.value.filter(old => searchKey(old) !== searchKey(entry)) : []; writeSearchHistory(history.value) }
function submit() { remember(); emit('submit'); open.value = false }
function keydown(event: KeyboardEvent) {
  if (event.isComposing || composing) return
  if (event.key === 'Escape') { open.value = false; return }
  if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
    event.preventDefault(); open.value = true
    active.value = Math.max(-1, Math.min(suggestions.value.length - 1, active.value + (event.key === 'ArrowDown' ? 1 : -1)))
  } else if (event.key === 'Enter') {
    event.preventDefault()
    if (open.value && active.value >= 0 && suggestions.value[active.value]) choose(suggestions.value[active.value]!)
    else submit()
  }
}
function focusOut(event: FocusEvent) { if (!root.value?.contains(event.relatedTarget as Node | null)) { if (props.modelValue.trim()) remember(); open.value = false } }
onBeforeUnmount(() => { disposed = true; revision++; clearTimeout(timer) })
</script>
<template>
  <div ref="root" class="library-search" @focusout="focusOut">
    <div class="search-field">
      <Search :size="17" />
      <input :value="modelValue" aria-label="搜索书库" placeholder="搜索作品、作者、标签…" maxlength="512" role="combobox" aria-autocomplete="list" aria-controls="library-tag-suggestions" :aria-expanded="open" :aria-activedescendant="active >= 0 ? `tag-suggestion-${active}` : undefined" @input="input" @compositionstart="composing = true" @compositionend="compositionEnd" @focus="open = true" @keydown="keydown" />
      <button v-if="modelValue" type="button" class="clear-search" aria-label="清除搜索" @click="emit('update:modelValue', '')"><X :size="14" /></button>
      <button type="button" class="text-button" @click="submit">搜索</button>
    </div>
    <div v-if="open" class="search-popover">
      <template v-if="modelValue.trim()">
        <div class="search-section-title">标签联想 <button class="text-button" @click="emit('browse')">全部标签</button></div>
        <p v-if="loading" class="muted" role="status">查找标签中…</p>
        <p v-else-if="error" class="inline-error" role="alert">{{error}} <button class="text-button" @click="suggest">重试</button></p>
        <p v-else-if="!suggestions.length" class="muted">没有匹配标签，可直接搜索关键词</p>
        <div id="library-tag-suggestions" role="listbox" aria-label="标签联想">
          <button v-for="(tag, index) in suggestions" :id="`tag-suggestion-${index}`" :key="tag.id" role="option" :aria-selected="active === index" class="suggestion-row" :class="{active: active === index}" @click="choose(tag)"><span># {{tag.name_zh || tag.name}}<small v-if="tag.name_zh">{{tag.name}}</small></span><small>{{tag.count || 0}} 部</small></button>
        </div>
      </template>
      <template v-else>
        <div class="search-section-title"><span><History :size="14" /> 搜索历史</span><button v-if="history.length" class="text-button" @click="removeHistory()">清空</button></div>
        <p v-if="!history.length" class="muted">暂无搜索历史</p>
        <div v-for="entry in history.slice(0, 8)" :key="searchKey(entry)" class="history-row"><button class="suggestion-row" @click="restore(entry)"><span>{{searchLabel(entry)}}<small v-if="entry.tags.length > 1">{{entry.mode === 'all' ? '全部匹配' : '匹配任一'}}</small></span></button><button class="icon-button" :aria-label="`删除搜索记录 ${searchLabel(entry)}`" @click="removeHistory(entry)"><X :size="14" /></button></div>
        <button class="text-button" @click="emit('browse')">选择多个标签</button>
      </template>
    </div>
  </div>
</template>
<style scoped>
.library-search{position:relative;flex:1;max-width:460px;min-width:220px}.library-search .search-field{max-width:none;width:100%}.search-popover{position:absolute;z-index:30;top:calc(100% + 8px);left:0;right:0;background:var(--surface,#fff);border:1px solid var(--line);border-radius:12px;padding:14px;box-shadow:0 12px 35px #0002;max-height:420px;overflow:auto}.search-section-title,.history-row{display:flex;align-items:center;justify-content:space-between;gap:8px}.search-section-title{margin-bottom:8px;font-weight:600}.search-section-title>span{display:flex;align-items:center;gap:6px}.suggestion-row{display:flex;justify-content:space-between;align-items:center;gap:12px;width:100%;text-align:left;padding:10px 8px;background:transparent;border:0;border-radius:6px;color:var(--text);font:inherit;cursor:pointer}.suggestion-row:hover,.suggestion-row.active{background:var(--accent-soft)}.suggestion-row>span{min-width:0;overflow-wrap:anywhere}.suggestion-row small{display:block;color:var(--muted);font-size:12px}.history-row>.suggestion-row{flex:1}.search-popover .muted{padding:8px 0}@media(max-width:600px){.library-search{max-width:none;min-width:0;width:100%}}
</style>
