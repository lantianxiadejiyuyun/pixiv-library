<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { AlertCircle, ArrowUpRight, BookOpen, Check, FileImage, Globe2, LoaderCircle, Search, ShieldCheck, UserRound, X } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { CharacterCandidate, CharacterLookupSeed, CharacterSearchResult, Item, Tag } from '../types'
import CharacterImageSearch from './CharacterImageSearch.vue'

const props = defineProps<{items: Item[]; seed?: CharacterLookupSeed | null}>()
const emit = defineEmits<{saved: [itemId: string, tag: Tag]}>()
const root = ref<HTMLElement | null>(null)
const searchInput = ref<HTMLInputElement | null>(null)
const query = ref('')
const activeTab = ref<'text' | 'image'>('text')
const imageOpened = ref(false)
const mode = ref<'character' | 'work'>('character')
const loading = ref(false)
const error = ref('')
const result = ref<CharacterSearchResult | null>(null)
const selected = ref<CharacterCandidate | null>(null)
const targetId = ref('')
const confirmedName = ref('')
const contextItem = ref<Item | undefined>()
const saving = ref(false)
const saveError = ref('')
const savedMessage = ref('')
let request = 0
let controller: AbortController | null = null
const targets = computed(() => {
  const values = [...props.items]
  if (contextItem.value && !values.some(item => item.id === contextItem.value?.id)) values.unshift(contextItem.value)
  return values
})
function imageLookup(value: {query: string; mode: 'character' | 'work'; item?: Item}) {
  activeTab.value = 'text'; query.value = value.query.slice(0, 120); mode.value = value.mode
  contextItem.value = value.item
  targetId.value = value.item?.id || ''
  result.value = null; selected.value = null; error.value = ''; savedMessage.value = ''; saveError.value = ''
  nextTick(() => { root.value?.scrollIntoView({behavior: 'smooth', block: 'start'}); searchInput.value?.focus({preventScroll: true}) })
}
function externalURL(value: string) {
  try { const url = new URL(value); return url.protocol === 'https:' ? url.href : undefined } catch { return undefined }
}
async function search() {
  const text = query.value.trim()
  if (!text || loading.value) return
  const current = ++request
  controller?.abort(); controller = new AbortController()
  loading.value = true; error.value = ''; result.value = null; selected.value = null; savedMessage.value = ''; saveError.value = ''
  try {
    const response = await api<CharacterSearchResult>('/ai/characters/search', {method: 'POST', body: JSON.stringify({query: text, mode: mode.value, limit: 6}), signal: controller.signal})
    if (current === request) result.value = response
  } catch (failure) { if (current === request) error.value = errorMessage(failure) }
  finally { if (current === request) loading.value = false }
}
function choose(candidate: CharacterCandidate) {
  selected.value = candidate
  confirmedName.value = candidate.native_name || candidate.name
  savedMessage.value = ''; saveError.value = ''
}
async function confirmCharacter() {
  if (!selected.value || !targetId.value || !confirmedName.value.trim() || saving.value) return
  saving.value = true; saveError.value = ''; savedMessage.value = ''
  const name = confirmedName.value.trim(), itemId = targetId.value
  try {
    const response = await api<{item_id: string; tag: Tag}>('/ai/characters/confirm', {method: 'POST', body: JSON.stringify({item_id: itemId, name})})
    savedMessage.value = `已为「${targets.value.find(item => item.id === itemId)?.title || '所选作品'}」添加 ${response.tag.name}`
    emit('saved', response.item_id, response.tag)
  } catch (failure) { saveError.value = errorMessage(failure) }
  finally { saving.value = false }
}
watch(() => props.seed?.key, async () => {
  if (!props.seed) return
  activeTab.value = 'text'
  request++; controller?.abort(); loading.value = false
  query.value = props.seed.query; mode.value = props.seed.mode
  contextItem.value = props.seed.item
  targetId.value = props.seed.item?.id || ''
  result.value = null; selected.value = null; error.value = ''; savedMessage.value = ''; saveError.value = ''
  await nextTick()
  root.value?.scrollIntoView({behavior: 'smooth', block: 'start'}); searchInput.value?.focus({preventScroll: true})
}, {immediate: true})
onBeforeUnmount(() => { request++; controller?.abort() })
</script>

<template>
  <section id="character-lookup" ref="root" class="panel character-lookup">
    <header class="panel-header"><div><h2><Globe2 :size="20" />联网查证角色</h2><p>{{ activeTab === 'text' ? '文字搜索仅发送确认的搜索词，不上传书页。' : '以图搜索会在你确认后，将一张图片发送至第三方。' }}结果需要人工确认。</p></div><span class="lookup-source">{{ activeTab === 'text' ? 'AniList' : 'SauceNAO' }}</span></header>
    <div class="character-lookup-body">
      <div class="segmented-control character-search-tabs"><button type="button" :class="{active: activeTab === 'text'}" :aria-pressed="activeTab === 'text'" :disabled="loading || saving" @click="activeTab = 'text'"><Search :size="16" />文字搜索</button><button type="button" :class="{active: activeTab === 'image'}" :aria-pressed="activeTab === 'image'" :disabled="loading || saving" @click="activeTab = 'image'; imageOpened = true"><FileImage :size="16" />以图搜索</button></div>
      <CharacterImageSearch v-if="imageOpened" v-show="activeTab === 'image'" :items="targets" :initial-item="contextItem" @lookup="imageLookup" />
      <div v-show="activeTab === 'text'">
      <form class="character-search-form" @submit.prevent="search">
        <label class="field character-mode">查找方式<select v-model="mode" :disabled="loading || saving"><option value="character">按角色名</option><option value="work">按作品名找角色</option></select></label>
        <label class="field character-search-word">搜索词<input ref="searchInput" v-model="query" :disabled="loading || saving" required maxlength="120" :placeholder="mode === 'character' ? '输入角色名称或别名' : '输入漫画名称'" /></label>
        <button class="button primary" :disabled="loading || saving || !query.trim()"><LoaderCircle v-if="loading" :size="17" class="spin" /><Search v-else :size="17" />{{ loading ? '正在查找' : '确认并搜索' }}</button>
      </form>
      <p v-if="contextItem" class="lookup-context"><BookOpen :size="15" />来自作品：{{ contextItem.title }}<span>可修改搜索词后再发送。</span></p>
      <div v-if="error" class="alert error" role="alert"><AlertCircle :size="18" /><span>{{ error }}</span></div>
      <div v-if="loading" class="inline-loading" role="status"><LoaderCircle :size="18" class="spin" />正在查询公开角色资料…</div>
      <template v-if="result">
        <div class="lookup-result-heading"><strong>“{{ result.query }}” · {{ result.candidates.length }} 个候选</strong><span>来源：{{ result.provider }}</span></div>
        <p v-if="result.notice" class="lookup-notice">{{ result.notice }}</p>
        <div v-if="!result.candidates.length" class="lookup-empty"><UserRound :size="25" /><div><strong>没有找到匹配角色</strong><p>尝试原文名称、别名，或切换为按作品名查找。</p></div></div>
        <div v-else class="character-candidates">
          <article v-for="candidate in result.candidates" :key="candidate.id" class="character-candidate" :class="{selected: selected?.id === candidate.id}">
            <header><div class="character-avatar"><UserRound :size="22" /></div><div><h3>{{ candidate.native_name || candidate.name }}</h3><p v-if="candidate.native_name && candidate.name !== candidate.native_name">{{ candidate.name }}</p></div><a v-if="externalURL(candidate.source_url)" class="text-button" :href="externalURL(candidate.source_url)" target="_blank" rel="noopener noreferrer">资料来源<ArrowUpRight :size="15" /></a></header>
            <p v-if="candidate.aliases.length" class="character-aliases"><span>别名</span>{{ candidate.aliases.join(' / ') }}</p>
            <div v-if="candidate.works.length" class="character-works"><span>相关作品</span><a v-for="work in candidate.works" :key="work.source_url + work.title" :href="externalURL(work.source_url)" target="_blank" rel="noopener noreferrer"><BookOpen :size="13" /><span>{{ work.title }}</span><small>{{ work.type === 'manga' ? '漫画' : '动画' }}</small><ArrowUpRight v-if="externalURL(work.source_url)" :size="13" /></a></div>
            <details v-if="candidate.description" class="character-description"><summary>查看简介</summary><p>{{ candidate.description }}</p></details>
            <div class="character-card-action"><button class="button secondary small" type="button" :disabled="saving" @click="choose(candidate)"><Check v-if="selected?.id === candidate.id" :size="16" /><UserRound v-else :size="16" />{{ selected?.id === candidate.id ? '已选择此角色' : '选择此角色' }}</button></div>
          </article>
        </div>
      </template>
      <div v-if="selected" class="character-confirmation">
        <div class="character-confirm-heading"><ShieldCheck :size="20" /><div><h3>人工确认角色标签</h3><p>确认角色确实出现在作品中后再保存。</p></div><button type="button" class="icon-button" :disabled="saving" aria-label="取消角色选择" @click="selected = null"><X :size="18" /></button></div>
        <div class="form-grid"><label class="field">目标作品<select v-model="targetId" :disabled="saving"><option value="">选择要添加标签的作品</option><option v-for="item in targets" :key="item.id" :value="item.id">{{ item.title }}</option></select></label><label class="field">角色名称<input v-model="confirmedName" :disabled="saving" maxlength="188" /></label></div>
        <p class="field-hint">列表显示最近的作品；其他作品可从详情页进入「联网查证」。</p>
        <div v-if="saveError" class="alert error" role="alert"><AlertCircle :size="18" /><span>{{ saveError }}</span></div>
        <div v-if="savedMessage" class="alert info" role="status"><Check :size="18" /><span>{{ savedMessage }}</span></div>
        <button type="button" class="button primary" :disabled="saving || !targetId || !confirmedName.trim()" @click="confirmCharacter"><LoaderCircle v-if="saving" :size="17" class="spin" /><Check v-else :size="17" />{{ saving ? '正在保存' : '确认并添加角色标签' }}</button>
      </div>
      </div>
    </div>
  </section>
</template>
