<script setup lang="ts">
import { ref, computed, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { ArrowLeft, ChevronLeft, ChevronRight, Download, Maximize2, Minimize2, BookOpen, AlertCircle, LoaderCircle, List, Layers3, X } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { Item, Job, ReadingProgress, TextSection } from '../types'
import { contentReady } from '../types'
import { boundedIndex, boundedOffset, restoreTextPosition, textReadingPercent } from '../reading'
import SeriesPanel from './SeriesPanel.vue'
const props = defineProps<{item: Item}>()
const emit = defineEmits<{close: []; progress: [value: ReadingProgress & {item_id: string}]; open: [id: string]; queued: [job: Job]}>()
const assets = computed(() => contentReady(props.item) ? [...(props.item.assets || [])].sort((a, b) => a.position - b.position) : [])
const current = ref(boundedIndex(props.item.progress?.page, assets.value.length))
const currentAsset = computed(() => assets.value[current.value])
const isText = computed(() => currentAsset.value?.mime_type.startsWith('text/') || false)
const sections = ref<TextSection[]>([])
const section = ref(0)
const offset = ref(0)
const currentSection = computed(() => sections.value[section.value])
const loading = ref(false)
const error = ref('')
const saveError = ref('')
const fit = ref(true)
const pageInput = ref(current.value + 1)
const textSize = ref(19)
const content = ref<HTMLElement | null>(null)
const outline = ref<HTMLElement | null>(null)
const outlineTab = ref<'pages' | 'series'>('pages')
const outlineOpen = ref(false)
const percent = computed(() => isText.value ? textReadingPercent(section.value, sections.value.length, offset.value) : assets.value.length ? Math.round((current.value + 1) / assets.value.length * 100) : 0)
const hasPrevious = computed(() => isText.value ? section.value > 0 : current.value > 0)
const hasNext = computed(() => isText.value ? section.value < sections.value.length - 1 : current.value < assets.value.length - 1)
let loadController: AbortController | undefined
let progressTimer: ReturnType<typeof setTimeout> | undefined
let savingProgress = false
let queuedProgress: ReadingProgress | null = null
let restoring = false
let disposed = false
let loadId = 0
function snapshot(): ReadingProgress | null {
  if (!assets.value.length || error.value || (isText.value && (loading.value || !sections.value.length))) return null
  return {page: current.value, section: isText.value ? section.value : 0, offset: isText.value ? offset.value : 0}
}
async function saveProgress() {
  const position = snapshot()
  if (!position) return
  queuedProgress = position
  if (savingProgress) return
  savingProgress = true
  try {
    while (queuedProgress) {
      const saved: ReadingProgress = queuedProgress; queuedProgress = null
      try {
        const response = await api<ReadingProgress>(`/items/${props.item.id}/progress`, {method: 'PUT', body: JSON.stringify(saved)})
        saveError.value = ''; emit('progress', {...response, item_id: props.item.id})
      } catch { saveError.value = '阅读位置尚未保存，请在连接恢复后滚动或翻页重试。' }
    }
  } finally { savingProgress = false }
}
function scheduleSave() { clearTimeout(progressTimer); progressTimer = setTimeout(saveProgress, 600) }
async function restoreScroll(value: number) {
  restoring = true; offset.value = boundedOffset(value)
  await nextTick()
  if (disposed || !content.value) return
  content.value.scrollTop = Math.max(0, content.value.scrollHeight - content.value.clientHeight) * offset.value
  requestAnimationFrame(() => { restoring = false })
}
async function displayAsset(saved?: ReadingProgress | null) {
  loadController?.abort(); const id = ++loadId
  sections.value = []; error.value = ''; loading.value = false; section.value = 0; offset.value = 0
  pageInput.value = current.value + 1
  if (!isText.value || !currentAsset.value) { await restoreScroll(0); scheduleSave(); return }
  const controller = new AbortController(); loadController = controller; loading.value = true
  try {
    const result = await api<{sections: TextSection[]}>(`/assets/${currentAsset.value.id}/text-sections`, {signal: controller.signal})
    if (disposed || id !== loadId) return
    if (!result.sections.length) throw new Error('此正文没有可阅读的章节。')
    sections.value = [...result.sections].sort((a, b) => a.index - b.index)
    const position = restoreTextPosition(saved, sections.value)
    section.value = position.section; loading.value = false
    await restoreScroll(position.offset)
  } catch (failure) { if (!controller.signal.aborted && id === loadId) error.value = errorMessage(failure) }
  finally { if (id === loadId) loading.value = false }
}
async function goPage(page: number) {
  const next = boundedIndex(page - 1, assets.value.length)
  if (!assets.value.length || !Number.isFinite(page)) return
  if (next === current.value) { pageInput.value = current.value + 1; outlineOpen.value = false; return }
  void saveProgress(); clearTimeout(progressTimer)
  current.value = next; outlineOpen.value = false; await displayAsset()
}
async function goSection(index: number) {
  if (loading.value || !sections.value.length) return
  const next = boundedIndex(index, sections.value.length)
  if (next === section.value) { outlineOpen.value = false; return }
  void saveProgress(); clearTimeout(progressTimer)
  section.value = next; outlineOpen.value = false; await restoreScroll(0); scheduleSave()
}
function previous() { if (isText.value) goSection(section.value - 1); else goPage(current.value) }
function next() { if (isText.value) goSection(section.value + 1); else goPage(current.value + 2) }
function scrolled() {
  if (!isText.value || restoring || loading.value || !content.value) return
  const distance = content.value.scrollHeight - content.value.clientHeight
  if (distance > 0) offset.value = boundedOffset(content.value.scrollTop / distance)
  scheduleSave()
}
async function resizeText(change: number) { textSize.value = Math.min(32, Math.max(14, textSize.value + change)); await restoreScroll(offset.value) }
async function markFinished() { if (!isText.value || loading.value || hasNext.value) return; await restoreScroll(1); await saveProgress() }
async function showOutline(tab: 'pages' | 'series') { outlineTab.value = tab; outlineOpen.value = true; await nextTick(); outline.value?.querySelector<HTMLButtonElement>('button')?.focus() }
function openWork(id: string) { if (id === props.item.id) { outlineOpen.value = false; return }; void saveProgress(); emit('open', id) }
function close() { void saveProgress(); emit('close') }
function keydown(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); if (outlineOpen.value) outlineOpen.value = false; else close(); return }
  if (outlineOpen.value) {
    if (event.key === 'Tab') {
      const elements = outline.value?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled])')
      const first = elements?.[0]; const last = elements?.[elements.length - 1]
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
    }
    return
  }
  if ((event.target as HTMLElement).matches('input, textarea, select')) return
  if (event.key === 'ArrowLeft') { event.preventDefault(); previous() }
  if (event.key === 'ArrowRight') { event.preventDefault(); next() }
}
onMounted(() => { document.addEventListener('keydown', keydown); displayAsset(props.item.progress); document.body.style.overflow = 'hidden' })
onBeforeUnmount(() => { void saveProgress(); disposed = true; loadId++; loadController?.abort(); clearTimeout(progressTimer); document.removeEventListener('keydown', keydown); document.body.style.overflow = '' })
</script>
<template>
  <section class="reader" role="dialog" aria-modal="true" :aria-label="`阅读：${item.title}`">
    <header class="reader-header"><button class="reader-back" aria-label="返回书库" @click="close"><ArrowLeft :size="20" /><span>返回书库</span></button><div class="reader-title"><BookOpen :size="17" /><strong>{{ item.title }}</strong></div><div class="reader-actions"><button class="icon-button" :aria-label="isText ? '打开章节目录' : '打开分 P 页面目录'" :title="isText ? '章节目录' : '页面目录'" @click="showOutline('pages')"><List :size="20" /></button><button class="icon-button" aria-label="打开系列与上下集" title="系列与上下集" @click="showOutline('series')"><Layers3 :size="19" /></button><template v-if="isText"><button class="icon-button reader-font-button" aria-label="减小字号" @click="resizeText(-1)">A−</button><button class="icon-button reader-font-button" aria-label="增大字号" @click="resizeText(1)">A＋</button></template><button v-else class="icon-button" :aria-label="fit ? '原始宽度' : '适应屏幕'" @click="fit = !fit"><component :is="fit ? Maximize2 : Minimize2" :size="18" /></button><a v-if="contentReady(item) && assets.length" class="icon-button reader-export" :href="`/api/items/${item.id}/export`" aria-label="下载离线存档"><Download :size="18" /></a></div></header>
    <div ref="content" class="reader-content" :class="{'text-reader': isText}" @scroll.passive="scrolled"><div v-if="loading" class="reader-message"><LoaderCircle class="spin" />正在读取正文</div><div v-else-if="error" class="reader-message"><AlertCircle />{{ error }}<button class="button secondary small" @click="displayAsset({page: current, section, offset})">重试</button></div><article v-else-if="isText && currentSection" class="reader-chapter"><h1>{{ currentSection.title || `第 ${section + 1} 节` }}</h1><pre class="reader-text" :style="{fontSize: `${textSize}px`}">{{ currentSection.text }}</pre><div class="reader-chapter-navigation"><button class="button secondary" :disabled="!hasPrevious" @click="previous"><ChevronLeft :size="18" />上一节</button><button v-if="hasNext" class="button secondary" @click="next">下一节<ChevronRight :size="18" /></button><button v-else class="button secondary" :disabled="offset === 1" @click="markFinished">{{ offset === 1 ? '已读完' : '标记读完' }}</button></div></article><img v-else-if="currentAsset && currentAsset.mime_type.startsWith('image/')" :key="currentAsset.id" :src="currentAsset.url" :alt="`第 ${current + 1} / ${assets.length} P`" class="reader-image" :class="{'fit-width': fit}" @error="error = '本页图片读取失败，请检查存储连接。'" /><div v-else class="reader-message"><BookOpen />暂无可阅读的正文</div></div>
    <p v-if="saveError" class="reader-save-error" role="status">{{ saveError }}</p>
    <footer class="reader-footer"><span class="reader-hint">{{ isText ? '← → 切换章节 · Esc 返回' : '← → 翻页 · Esc 返回' }}</span><div class="page-navigation"><button class="icon-button" :disabled="!hasPrevious || loading" :aria-label="isText ? '上一节' : '上一 P'" @click="previous"><ChevronLeft :size="22" /></button><button v-if="isText" class="reader-position-button" :disabled="loading || !sections.length" @click="showOutline('pages')">第 {{ sections.length ? section + 1 : 0 }} / {{ sections.length }} 节</button><form v-else @submit.prevent="goPage(Number(pageInput))"><input v-model.number="pageInput" aria-label="分 P 页码" type="number" min="1" :max="assets.length" /><span>/ {{ assets.length }} P</span></form><button class="icon-button" :disabled="!hasNext || loading" :aria-label="isText ? '下一节' : '下一 P'" @click="next"><ChevronRight :size="22" /></button></div><span class="reader-percent" :title="isText ? '当前正文的阅读位置' : '当前分 P 位置'">{{ percent }}%</span></footer>
    <div v-if="outlineOpen" class="reader-outline-backdrop" @click.self="outlineOpen = false"><aside ref="outline" class="reader-outline" role="dialog" aria-modal="true" aria-label="阅读目录"><header><h2>阅读目录</h2><button class="icon-button" aria-label="关闭阅读目录" @click="outlineOpen = false"><X :size="20" /></button></header><div class="reader-outline-tabs"><button :class="{active: outlineTab === 'pages'}" @click="outlineTab = 'pages'">{{ isText ? '章节' : '分 P 页面' }}</button><button :class="{active: outlineTab === 'series'}" @click="outlineTab = 'series'">系列 / 上下集</button></div><div class="reader-outline-body"><SeriesPanel v-if="outlineTab === 'series'" :item="item" :editable="false" @open="openWork" @queued="emit('queued', $event)" /><template v-else><label v-if="assets.length > 1 && isText" class="field">正文文件<select :value="current" @change="goPage(Number(($event.target as HTMLSelectElement).value) + 1)"><option v-for="(asset, index) in assets" :key="asset.id" :value="index">{{ asset.original_name || `正文 ${index + 1}` }}</option></select></label><div v-if="loading" class="inline-loading"><LoaderCircle class="spin" :size="17" />读取章节…</div><ol v-else-if="isText" class="reader-section-list"><li v-for="(entry, index) in sections" :key="entry.index"><button :class="{current: index === section}" :aria-current="index === section ? 'true' : undefined" @click="goSection(index)"><span>{{ index + 1 }}</span>{{ entry.title || `第 ${index + 1} 节` }}</button></li></ol><div v-else class="reader-page-grid"><button v-for="(asset, index) in assets" :key="asset.id" :class="{current: index === current}" :aria-label="`跳转至第 ${index + 1} P`" :aria-current="index === current ? 'true' : undefined" @click="goPage(index + 1)"><img v-if="asset.mime_type.startsWith('image/')" :src="asset.url" alt="" loading="lazy" /><BookOpen v-else :size="23" /><span>{{ index + 1 }} P</span></button></div></template></div></aside></div>
  </section>
</template>
