<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { AlertCircle, ArrowUpRight, BookOpen, Check, ChevronDown, FileImage, KeyRound, LoaderCircle, Search, Settings2, Upload } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { Asset, Item } from '../types'
import { contentReady } from '../types'

type ImageSettings = {provider: string; has_api_key: boolean; max_file_bytes: number; notice: string}
type ImageMatch = {similarity: number; index_name: string; title: string; creator: string; characters: string[]; material: string; source_urls: string[]}
type ImageResult = {provider: string; matches: ImageMatch[]; notice: string; uploaded_size: number}
const props = defineProps<{items: Item[]; initialItem?: Item}>()
defineEmits<{lookup: [value: {query: string; mode: 'character' | 'work'; item?: Item}]}>()
const settings = ref<ImageSettings | null>(null)
const settingsError = ref('')
const settingsOpen = ref(false)
const apiKey = ref('')
const savingKey = ref(false)
const keyMessage = ref('')
const inputKind = ref<'upload' | 'library'>('upload')
const fileInput = ref<HTMLInputElement | null>(null)
const file = ref<File | null>(null)
const fileURL = ref('')
const itemId = ref('')
const assets = ref<Asset[]>([])
const assetId = ref('')
const loadingPages = ref(false)
const pagesError = ref('')
const consent = ref(false)
const searching = ref(false)
const error = ref('')
const result = ref<ImageResult | null>(null)
let pageRequest = 0
let searchRequest = 0
let controller: AbortController | null = null
const maxBytes = computed(() => settings.value?.max_file_bytes || 8388608)
const selectedAsset = computed(() => assets.value.find(asset => asset.id === assetId.value))
const selectedItem = computed(() => libraryItems.value.find(item => item.id === itemId.value))
const preview = computed(() => inputKind.value === 'upload' ? fileURL.value : selectedAsset.value?.url || '')
const selectionLabel = computed(() => inputKind.value === 'upload' ? file.value?.name || '' : selectedAsset.value ? `第 ${selectedAsset.value.position + 1} 页 · ${selectedAsset.value.original_name}` : '')
const canSearch = computed(() => consent.value && !searching.value && !loadingPages.value && !!(inputKind.value === 'upload' ? file.value : selectedAsset.value))
const libraryItems = computed(() => {
  const list = props.items.filter(contentReady)
  if (props.initialItem && contentReady(props.initialItem) && !list.some(item => item.id === props.initialItem?.id)) list.unshift(props.initialItem)
  return list
})
function externalURL(value: string) { try { const url = new URL(value); return ['https:', 'http:'].includes(url.protocol) ? url.href : undefined } catch { return undefined } }
async function readSettings() {
  settingsError.value = ''
  try { settings.value = await api<ImageSettings>('/ai/characters/image/settings') }
  catch (failure) { settingsError.value = errorMessage(failure) }
}
async function saveKey(clear = false) {
  if (savingKey.value || (!clear && !apiKey.value.trim())) return
  savingKey.value = true; settingsError.value = ''; keyMessage.value = ''
  try {
    settings.value = await api<ImageSettings>('/ai/characters/image/settings', {method: 'PUT', body: JSON.stringify({api_key: clear ? '' : apiKey.value.trim()})})
    apiKey.value = ''; keyMessage.value = clear ? '密钥已移除，将使用匿名查询。' : '密钥已保存；可用性以实际查询结果为准。'
  } catch (failure) { settingsError.value = errorMessage(failure) }
  finally { savingKey.value = false }
}
function chooseFile(event: Event) {
  const next = (event.target as HTMLInputElement).files?.[0]
  error.value = ''; result.value = null; consent.value = false
  if (fileURL.value) URL.revokeObjectURL(fileURL.value)
  fileURL.value = ''; file.value = null
  if (!next) return
  if (!next.type.startsWith('image/')) { error.value = '请选择图片文件。'; return }
  if (next.size > maxBytes.value) { error.value = `图片不能超过 ${Math.round(maxBytes.value / 1024 / 1024)} MB，可先裁剪或压缩。`; return }
  file.value = next; fileURL.value = URL.createObjectURL(next)
}
async function loadPages() {
  const current = ++pageRequest
  assets.value = []; assetId.value = ''; pagesError.value = ''; consent.value = false; loadingPages.value = false
  if (!itemId.value) return
  loadingPages.value = true
  try {
    const item = await api<Item>(`/items/${itemId.value}`)
    if (current !== pageRequest) return
    if (!contentReady(item)) { pagesError.value = '正文尚未下载完成，暂不能选择书库页面。'; return }
    assets.value = (item.assets || []).filter(asset => asset.mime_type.startsWith('image/'))
    assetId.value = assets.value[0]?.id || ''
  } catch (failure) { if (current === pageRequest) pagesError.value = errorMessage(failure) }
  finally { if (current === pageRequest) loadingPages.value = false }
}
async function searchImage() {
  if (!canSearch.value) return
  const current = ++searchRequest
  const payload = new FormData()
  if (inputKind.value === 'upload' && file.value) payload.append('file', file.value)
  else if (selectedAsset.value) payload.append('asset_id', selectedAsset.value.id)
  else return
  payload.append('consent', 'true')
  searching.value = true; error.value = ''; result.value = null
  controller?.abort(); controller = new AbortController()
  try {
    const response = await api<ImageResult>('/ai/characters/image-search', {method: 'POST', body: payload, signal: controller.signal})
    if (current === searchRequest) result.value = response
  } catch (failure) { if (current === searchRequest) error.value = errorMessage(failure) }
  finally { if (current === searchRequest) { searching.value = false; consent.value = false } }
}
watch(itemId, loadPages)
watch([inputKind, assetId], () => { consent.value = false; result.value = null; error.value = '' })
onMounted(() => { readSettings(); if (props.initialItem && contentReady(props.initialItem)) { inputKind.value = 'library'; itemId.value = props.initialItem.id } })
onBeforeUnmount(() => { searchRequest++; pageRequest++; controller?.abort(); if (fileURL.value) URL.revokeObjectURL(fileURL.value); apiKey.value = '' })
</script>

<template>
  <section class="character-image-search">
    <div class="image-search-intro"><p>将选中的一张图片发给 SauceNAO 查找相似来源，可再查询匹配记录中的人物资料。相似图片不代表角色身份已确认。</p><button type="button" class="text-button" @click="settingsOpen = !settingsOpen"><Settings2 :size="15" />图搜设置<ChevronDown :size="14" /></button></div>
    <p v-if="settings && !settings.has_api_key" class="lookup-notice">当前使用限额匿名查询，可在「图搜设置」中保存 API 密钥。</p>
    <a class="text-button image-official-link" href="https://saucenao.com/" target="_blank" rel="noopener noreferrer">在 SauceNAO 官网搜索<ArrowUpRight :size="14" /></a><span class="image-official-note">仅打开官网，不会自动上传图片。</span>
    <div v-if="settingsOpen" class="image-provider-settings">
      <div class="image-provider-heading"><KeyRound :size="17" /><strong>SauceNAO API 密钥</strong><span class="status-pill" :class="settings?.has_api_key ? 'neutral' : 'queued'">{{ settings?.has_api_key ? '已保存密钥' : '匿名查询' }}</span></div>
      <p class="field-hint">未配置密钥时可尝试匿名查询，额度受限。密钥在此保存，之后不会回显。</p>
      <label class="field">{{ settings?.has_api_key ? '替换密钥' : 'API 密钥' }}<input v-model="apiKey" type="password" autocomplete="off" spellcheck="false" :disabled="savingKey" placeholder="输入 SauceNAO API 密钥" /></label>
      <div class="image-key-actions"><button type="button" class="button secondary small" :disabled="savingKey || !apiKey.trim()" @click="saveKey()"><LoaderCircle v-if="savingKey" class="spin" :size="16" /><Check v-else :size="16" />保存密钥</button><button v-if="settings?.has_api_key" type="button" class="text-button danger-text" :disabled="savingKey" @click="saveKey(true)">移除密钥</button><a class="text-button" href="https://saucenao.com/user.php?page=search-api" target="_blank" rel="noopener noreferrer">获取密钥<ArrowUpRight :size="14" /></a></div>
      <p v-if="keyMessage" class="field-hint" role="status">{{ keyMessage }}</p>
    </div>
    <div v-if="settingsError" class="alert error" role="alert"><AlertCircle :size="17" /><span>{{ settingsError }}</span><button class="text-button" @click="readSettings">重试</button></div>
    <div class="image-input-options"><label class="checkbox-label"><input v-model="inputKind" type="radio" value="upload" :disabled="searching" />上传截图</label><label class="checkbox-label"><input v-model="inputKind" type="radio" value="library" :disabled="searching" />选择书库页面</label></div>
    <div v-if="inputKind === 'upload'" class="image-upload-input"><input ref="fileInput" class="sr-only" type="file" accept="image/png,image/jpeg,image/webp,image/gif" :disabled="searching" @change="chooseFile" /><button type="button" class="button secondary" :disabled="searching" @click="fileInput?.click()"><Upload :size="17" />{{ file ? '重新选择图片' : '选择图片' }}</button><span>可上传裁剪后的截图，最大 {{ Math.round(maxBytes / 1024 / 1024) }} MB。</span></div>
    <div v-else class="form-grid image-library-fields"><label class="field">作品<select v-model="itemId" :disabled="searching"><option value="">选择已下载的作品</option><option v-for="item in libraryItems" :key="item.id" :value="item.id">{{ item.title }}</option></select></label><label class="field">页面<select v-model="assetId" :disabled="searching || loadingPages || !assets.length"><option v-if="!assets.length" value="">{{ loadingPages ? '正在读取页面…' : '暂无图片页面' }}</option><option v-for="asset in assets" :key="asset.id" :value="asset.id">第 {{ asset.position + 1 }} 页 · {{ asset.original_name }}</option></select></label></div>
    <div v-if="pagesError" class="alert error" role="alert"><AlertCircle :size="17" /><span>{{ pagesError }}</span></div>
    <div v-if="preview" class="image-search-preview"><img :src="preview" alt="本次图搜将发送的图片预览" /><div><strong>本次发送的图片</strong><p>{{ selectionLabel }}</p><span>仅发送当前这一张图片。</span></div></div>
    <p v-if="settings?.notice" class="lookup-notice">{{ settings.notice }}</p>
    <label class="checkbox-label image-search-consent"><input v-model="consent" type="checkbox" :disabled="searching || !preview" /><span>我同意将上方选中的一张图片发送至第三方 SauceNAO。</span></label>
    <button type="button" class="button primary" :disabled="!canSearch" @click="searchImage"><LoaderCircle v-if="searching" class="spin" :size="17" /><Search v-else :size="17" />{{ searching ? '正在发送并查找' : '发送图片并搜索' }}</button>
    <div v-if="error" class="alert error image-search-error" role="alert"><AlertCircle :size="18" /><span>{{ error }}</span></div>
    <template v-if="result">
      <div class="lookup-result-heading"><strong>{{ result.matches.length }} 个相似来源</strong><span>{{ result.provider }} · 已发送 {{ Math.max(1, Math.round(result.uploaded_size / 1024)) }} KB</span></div>
      <p v-if="result.notice" class="lookup-notice">{{ result.notice }}</p>
      <div v-if="!result.matches.length" class="lookup-empty"><FileImage :size="25" /><div><strong>未找到相似来源</strong><p>可尝试清晰的单个人物截图，或改用文字搜索。</p></div></div>
      <div v-else class="image-match-list"><article v-for="(match, index) in result.matches" :key="`${match.index_name}-${index}`" class="image-match-card"><header><div><h3>{{ match.title || match.material || '未提供标题' }}</h3><p>{{ match.creator || '作者未提供' }} · {{ match.index_name }}</p></div><span class="status-pill neutral">相似度 {{ Number.isFinite(match.similarity) ? match.similarity.toFixed(1) : '—' }}%</span></header><p v-if="match.material && match.material !== match.title" class="image-match-material"><BookOpen :size="14" />{{ match.material }}</p><div v-if="match.characters.length" class="image-match-characters"><button v-for="character in match.characters" :key="character" class="button secondary small" @click="$emit('lookup', {query: character, mode: 'character', item: inputKind === 'library' ? selectedItem : undefined})">{{ character }}<Search :size="14" />查人物资料</button></div><p v-else class="field-hint">该匹配未提供角色名称。可查看来源，或使用作品名继续查找。</p><div class="image-match-links"><a v-for="url in match.source_urls.filter(value => externalURL(value))" :key="url" class="text-button" :href="externalURL(url)" target="_blank" rel="noopener noreferrer">查看来源<ArrowUpRight :size="14" /></a><button v-if="match.material" class="text-button" @click="$emit('lookup', {query: match.material, mode: 'work', item: inputKind === 'library' ? selectedItem : undefined})">以此作品名继续查找<Search :size="14" /></button></div></article></div>
    </template>
  </section>
</template>
