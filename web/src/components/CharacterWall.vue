<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowLeft, ArrowUpRight, BookOpen, LoaderCircle, Plus, Search, UserRound } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { CharacterCandidate, CharacterSearchResult, Item, WallCharacter } from '../types'
import { sourceLabels } from '../types'
import BookCard from './BookCard.vue'
import Modal from './Modal.vue'

defineProps<{blurAdult: boolean}>()
const emit = defineEmits<{open: [id: string]}>()
const characters = ref<WallCharacter[]>([]), selected = ref<WallCharacter | null>(null), works = ref<Item[]>([])
const query = ref(''), source = ref(''), downloaded = ref(false), sort = ref('count')
const page = ref(1), total = ref(0), loading = ref(true), error = ref(''), failedMore = ref(false)
const lookup = ref(false), lookupQuery = ref(''), lookupMode = ref<'character' | 'work'>('character')
const candidates = ref<CharacterCandidate[]>([]), researching = ref(false), saving = ref(false), lookupError = ref(''), searched = ref(false)
const more = computed(() => (selected.value ? works.value.length : characters.value.length) < total.value)
let request = 0, lookupRequest = 0, disposed = false, debounce: ReturnType<typeof setTimeout> | undefined
function sourceURL(url: string) { return /^https:\/\/anilist\.co\/(character|anime|manga)\/[1-9]\d*$/.test(url) ? url : undefined }
async function load(append = false) {
  if (disposed || (append && (loading.value || !more.value))) return
  const current = ++request, target = selected.value?.id, nextPage = append ? page.value + 1 : 1
  loading.value = true; error.value = ''; failedMore.value = false
  if (!append) { page.value = 1; total.value = 0; if (target) works.value = []; else characters.value = [] }
  const filters = {source: source.value, downloaded: String(downloaded.value)}
  const params = new URLSearchParams({...filters, page: String(nextPage), page_size: '24', ...(target ? {tag: target} : {q: query.value, sort: sort.value})})
  try {
    const [data, detail] = await Promise.all([
      api<{items: Item[] | WallCharacter[]; total: number}>(`${target ? '/items' : '/characters'}?${params}`),
      target && !append ? api<WallCharacter>(`/characters/${encodeURIComponent(target)}?${new URLSearchParams(filters)}`) : Promise.resolve(null),
    ])
    if (disposed || current !== request) return
    if (target) { works.value = [...new Map([...(append ? works.value : []), ...(data.items as Item[])].map(item => [item.id, item])).values()]; if (detail) selected.value = detail }
    else characters.value = [...new Map([...(append ? characters.value : []), ...(data.items as WallCharacter[])].map(item => [item.id, item])).values()]
    total.value = data.total; page.value = nextPage
  } catch (e) { if (!disposed && current === request) { error.value = errorMessage(e); failedMore.value = append } }
  finally { if (current === request) loading.value = false }
}
function open(character: WallCharacter) { selected.value = character; load() }
function back() { selected.value = null; load() }
function openLookup() {
  ++lookupRequest; researching.value = false; lookup.value = true; lookupError.value = ''; candidates.value = []; searched.value = false
  lookupQuery.value = selected.value?.name.replaceAll('_', ' ') || ''; lookupMode.value = 'character'
}
function closeLookup() { if (saving.value) return; lookup.value = false; ++lookupRequest; researching.value = false }
async function search() {
  if (researching.value || saving.value || !lookupQuery.value.trim()) return
  const current = ++lookupRequest; researching.value = true; candidates.value = []; lookupError.value = ''; searched.value = false
  try {
    const result = await api<CharacterSearchResult>('/characters/search', {method: 'POST', body: JSON.stringify({query: lookupQuery.value.trim(), mode: lookupMode.value, limit: 6})})
    if (!disposed && current === lookupRequest) { candidates.value = result.candidates; searched.value = true }
  } catch (e) { if (!disposed && current === lookupRequest) lookupError.value = errorMessage(e) }
  finally { if (current === lookupRequest) researching.value = false }
}
async function confirm(candidate: CharacterCandidate) {
  if (saving.value || researching.value) return
  const current = ++lookupRequest, target = selected.value?.id
  saving.value = true; lookupError.value = ''
  try {
    const result = await api<WallCharacter>(target ? `/characters/${encodeURIComponent(target)}/profile` : '/characters', {method: 'POST', body: JSON.stringify({candidate_id: Number(candidate.id)})})
    if (disposed || current !== lookupRequest) return
    lookup.value = false; selected.value = result; await load()
  } catch (e) { if (!disposed && current === lookupRequest) lookupError.value = errorMessage(e) }
  finally { saving.value = false }
}
watch([source, downloaded, sort], () => load())
watch(query, () => { ++request; clearTimeout(debounce); debounce = setTimeout(() => { if (!selected.value) load() }, 300) })
onMounted(() => load())
onBeforeUnmount(() => { disposed = true; ++request; ++lookupRequest; clearTimeout(debounce) })
</script>

<template>
  <section class="character-wall">
    <div class="wall-toolbar">
      <button v-if="selected" class="button secondary small" @click="back"><ArrowLeft :size="16" />全部人物</button>
      <label v-else class="search-field"><Search :size="17" /><input v-model="query" aria-label="搜索人物" placeholder="人物名、中文名或别名…" /></label>
      <label class="wall-filter">书库<select v-model="source"><option value="">全部</option><option v-for="(label, key) in sourceLabels" :key="key" :value="key">{{ label }}</option></select></label>
      <label class="checkbox-label"><input v-model="downloaded" type="checkbox" />仅已下载</label>
      <label v-if="!selected" class="wall-filter">排序<select v-model="sort"><option value="count">作品最多</option><option value="name">人物名称</option></select></label>
      <button class="button primary small wall-add" @click="openLookup"><Plus :size="16" />{{ selected ? (selected.profile ? '更新人物资料' : '补全人物资料') : '联网添加人物' }}</button>
    </div>
    <article v-if="selected" class="panel wall-profile">
      <div class="wall-person-icon"><UserRound :size="32" /></div><div class="wall-profile-copy"><h2>{{ selected.display_name }}</h2><p v-if="selected.profile?.name && selected.profile.name !== selected.display_name" class="muted">{{ selected.profile.name }}</p><p v-if="selected.profile?.aliases.length" class="wall-aliases">{{ selected.profile.aliases.join(' / ') }}</p><p class="wall-description">{{ selected.profile?.description || (selected.profile ? '资料来源尚未提供人物介绍。' : '尚未保存人物介绍，可联网搜索并确认对应人物。') }}</p><div v-if="selected.profile?.works.length" class="wall-originals"><span>原作</span><a v-for="work in selected.profile.works" :key="work.source_url" :href="sourceURL(work.source_url)" target="_blank" rel="noopener noreferrer">{{ work.title }}<ArrowUpRight :size="13" /></a></div><a v-if="selected.profile" class="text-button" :href="sourceURL(selected.profile.source_url)" target="_blank" rel="noopener noreferrer">资料来源 · {{ selected.profile.provider }}<ArrowUpRight :size="13" /></a></div>
    </article>
    <p class="wall-count">{{ selected ? `${total} 部本地作品` : `${total} 位人物` }}</p>
    <div v-if="!selected && characters.length" class="wall-grid">
      <button v-for="character in characters" :key="character.id" class="wall-card panel" @click="open(character)"><div class="wall-collage" :class="{'one-cover': character.covers.length < 2}"><template v-for="cover in character.covers" :key="cover.id"><img v-if="cover.cover_url" :src="cover.cover_url" alt="相关作品封面" loading="lazy" :class="{'wall-adult-blur': blurAdult && cover.rating === 'adult'}" /><div v-else class="wall-cover-placeholder"><BookOpen :size="26" /></div></template><div v-if="!character.covers.length" class="wall-cover-placeholder"><UserRound :size="48" /></div></div><div class="wall-card-copy"><h2>{{ character.display_name }}</h2><p>{{ character.count }} 部作品 · {{ character.profile ? '已保存人物资料' : '待补全人物资料' }}</p><p v-if="character.profile?.description" class="wall-card-intro">{{ character.profile.description }}</p></div></button>
    </div>
    <div v-if="selected && works.length" class="book-grid"><BookCard v-for="item in works" :key="item.id" :item="item" :blur-adult="blurAdult" @open="emit('open', item.id)" /></div>
    <div v-if="loading" class="inline-loading"><LoaderCircle class="spin" :size="20" />正在加载…</div>
    <div v-if="error" class="alert error" role="alert">{{ error }}<button class="text-button" @click="load(failedMore)">重试</button></div>
    <div v-if="!loading && !error && !(selected ? works.length : characters.length)" class="empty-state"><UserRound :size="40" /><h3>{{ selected ? '此筛选下暂无本地作品' : '暂无人物' }}</h3><p>{{ selected ? '给对应作品添加此人物的角色标签后，会自动汇集到这里。' : 'AI 识别和人工确认的角色标签会自动展示；也可以先联网添加人物资料。' }}</p></div>
    <button v-if="more && !error" class="button secondary wall-more" :disabled="loading" @click="load(true)">加载更多{{ selected ? '作品' : '人物' }}</button>
    <Modal v-if="lookup" title="联网获取人物资料" :busy="saving" @close="closeLookup"><p class="field-hint">发送人物名或作品名到 AniList，不上传书页。确认同名人物后，保存简介、别名和原作信息；保存后查看不再请求外网。</p><form class="wall-lookup-form" @submit.prevent="search"><label class="field">查找方式<select v-model="lookupMode" :disabled="saving || researching"><option value="character">人物名</option><option value="work">作品名找人物</option></select></label><label class="field">搜索词<input v-model="lookupQuery" required maxlength="120" :disabled="saving || researching" /></label><button class="button primary small" :disabled="saving || researching"><LoaderCircle v-if="researching" class="spin" :size="16" /><Search v-else :size="16" />搜索</button></form><div v-if="lookupError" class="alert error" role="alert">{{ lookupError }}</div><p v-if="searched && !candidates.length" class="field-hint">没有找到匹配人物，请尝试其他名字或作品名。</p><article v-for="candidate in candidates" :key="candidate.id" class="wall-candidate"><h3>{{ candidate.native_name || candidate.name }}</h3><p>{{ candidate.name }}<template v-if="candidate.aliases.length"> · {{ candidate.aliases.join(' / ') }}</template></p><p>{{ candidate.works.map(work => work.title).join(' · ') }}</p><p class="wall-card-intro">{{ candidate.description || '暂无人物介绍' }}</p><div><a class="text-button" :href="sourceURL(candidate.source_url)" target="_blank" rel="noopener noreferrer">查看来源<ArrowUpRight :size="13" /></a><button class="button secondary small" :disabled="saving || researching" @click="confirm(candidate)">{{ saving ? '正在保存…' : '确认此人物并保存' }}</button></div></article></Modal>
  </section>
</template>

<style scoped>
.wall-toolbar {display:flex;align-items:center;gap:14px;flex-wrap:wrap;margin-bottom:22px;}.wall-toolbar .search-field {flex:1;min-width:220px;}.wall-toolbar input {width:100%;}.wall-filter {display:flex;align-items:center;gap:8px;font-size:13px;}.wall-filter select {max-width:150px;padding:7px;border:1px solid var(--line);border-radius:6px;background:var(--paper);}.wall-toolbar .checkbox-label input {width:15px;}.wall-add {margin-left:auto;}.wall-count {font-size:14px;color:var(--text-secondary);margin-bottom:16px;}.wall-grid {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:22px;}.wall-card {padding:0;overflow:hidden;text-align:left;min-width:0;transition:box-shadow .2s;}.wall-card:hover {box-shadow:0 5px 20px #17203315;}.wall-collage {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));height:210px;overflow:hidden;gap:3px;background:var(--surface-muted);}.wall-collage.one-cover {grid-template-columns:1fr;}.wall-collage img {width:100%;height:100%;object-fit:cover;min-width:0;}.wall-cover-placeholder {display:grid;place-items:center;min-width:0;color:var(--text-secondary);background:var(--surface-muted);}.wall-adult-blur {filter:blur(16px);transform:scale(1.1);}.wall-card-copy {padding:18px;}.wall-card h2 {font-size:19px;overflow-wrap:anywhere;}.wall-card p {font-size:13px;line-height:1.8;color:var(--text-secondary);margin-top:9px;}.wall-card-intro {display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;line-height:1.8;white-space:pre-line;}.wall-profile {display:flex;gap:20px;padding:24px;margin-bottom:24px;}.wall-person-icon {width:64px;height:64px;flex-shrink:0;display:grid;place-items:center;border-radius:16px;color:var(--accent);background:var(--accent-soft);}.wall-profile-copy {min-width:0;}.wall-profile h2 {font-size:24px;}.wall-aliases {font-size:13px;color:var(--text-secondary);margin-top:8px;overflow-wrap:anywhere;}.wall-description {font-size:15px;line-height:1.9;white-space:pre-line;overflow-wrap:anywhere;margin:15px 0;}.wall-originals {display:flex;flex-wrap:wrap;gap:10px 15px;align-items:center;margin:14px 0;font-size:13px;}.wall-originals a,.wall-candidate a {display:inline-flex;align-items:center;gap:4px;color:var(--accent);}.wall-more {display:block;margin:25px auto;}.wall-lookup-form {display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap;margin:20px 0;}.wall-lookup-form .field {flex:1;min-width:150px;}.wall-candidate {padding:18px 0;border-top:1px solid var(--line);}.wall-candidate h3 {font-size:17px;}.wall-candidate p {font-size:13px;color:var(--text-secondary);line-height:1.8;margin-top:8px;}.wall-candidate>div {display:flex;justify-content:space-between;align-items:center;gap:12px;margin-top:16px;}
@media(max-width:1100px) {.wall-grid {grid-template-columns:repeat(2,minmax(0,1fr));}}@media(max-width:600px) {.wall-grid {grid-template-columns:1fr;}.wall-toolbar .search-field {flex-basis:100%;}.wall-profile {padding:18px;gap:12px;}.wall-person-icon {width:40px;height:40px;}.wall-profile h2 {font-size:21px;}.wall-collage {height:230px;}}
</style>
