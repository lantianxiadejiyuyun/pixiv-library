<script setup lang="ts">
import { ref, computed, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { ArrowLeft, ChevronLeft, ChevronRight, Download, Maximize2, Minimize2, BookOpen, Film, AlertCircle, LoaderCircle, List, Layers3, X } from 'lucide-vue-next'
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
const isVideo = computed(() => currentAsset.value?.mime_type.startsWith('video/') || false)
const hasMixedMedia = computed(() => assets.value.some(asset => asset.mime_type.startsWith('video/')) || (assets.value.some(asset => asset.mime_type.startsWith('text/')) && assets.value.some(asset => !asset.mime_type.startsWith('text/'))))
const video = ref<HTMLVideoElement | null>(null)
const videoReady = ref(false)
const videoRevision = ref(0)
let videoRestorePending = false
const positions = new Map<string, ReadingProgress>()
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
const percent = computed(() => isVideo.value ? Math.round(offset.value * 100) : isText.value ? textReadingPercent(section.value, sections.value.length, offset.value) : assets.value.length ? Math.round((current.value + 1) / assets.value.length * 100) : 0)
const hasPrevious = computed(() => current.value > 0 || (isText.value && section.value > 0))
const hasNext = computed(() => current.value < assets.value.length - 1 || (isText.value && section.value < sections.value.length - 1))
const previousLabel = computed(() => isText.value && section.value > 0 ? '上一节' : hasMixedMedia.value || isText.value ? '上一文件' : '上一 P')
const nextLabel = computed(() => isText.value && section.value < sections.value.length - 1 ? '下一节' : hasMixedMedia.value || isText.value ? '下一文件' : '下一 P')
let loadController: AbortController | undefined
let progressTimer: ReturnType<typeof setTimeout> | undefined
let savingProgress = false
let queuedProgress: ReadingProgress | null = null
let restoring = false
let disposed = false
let loadId = 0
function snapshot(): ReadingProgress | null {
  if (!assets.value.length || error.value || (isText.value && (loading.value || !sections.value.length)) || (isVideo.value && !videoReady.value)) return null
  if (isVideo.value && !videoRestorePending && video.value && Number.isFinite(video.value.duration) && video.value.duration > 0) offset.value = boundedOffset(video.value.currentTime / video.value.duration)
  return {page: current.value, section: isText.value ? section.value : 0, offset: isText.value || isVideo.value ? offset.value : 0}
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
function scheduleSave() { clearTimeout(progressTimer); progressTimer = setTimeout(() => { progressTimer = undefined; void saveProgress() }, 600) }
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
  videoReady.value = false; videoRestorePending = false; videoRevision.value++
  pageInput.value = current.value + 1
  if (!isText.value || !currentAsset.value) {
    offset.value = isVideo.value ? boundedOffset(saved?.offset) : 0
    await nextTick()
    if (disposed || id !== loadId) return
    if (content.value) content.value.scrollTop = 0
    if (!isVideo.value) scheduleSave()
    return
  }
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
async function goPage(page: number, fromEnd = false) {
  const next = boundedIndex(page - 1, assets.value.length)
  if (!assets.value.length || !Number.isFinite(page)) return
  if (next === current.value) { pageInput.value = current.value + 1; outlineOpen.value = false; return }
  const position = snapshot()
  if (position && currentAsset.value) positions.set(currentAsset.value.id, position)
  void saveProgress(); clearTimeout(progressTimer); progressTimer = undefined
  video.value?.pause()
  current.value = next; outlineOpen.value = false
  const remembered = positions.get(assets.value[next]!.id)
  await displayAsset(remembered || (fromEnd && isText.value ? {page: next, section: Number.MAX_SAFE_INTEGER, offset: 0} : undefined))
}
async function goSection(index: number) {
  if (loading.value || !sections.value.length) return
  const next = boundedIndex(index, sections.value.length)
  if (next === section.value) { outlineOpen.value = false; return }
  void saveProgress(); clearTimeout(progressTimer)
  section.value = next; outlineOpen.value = false; await restoreScroll(0); scheduleSave()
}
function previous() { if (isText.value && section.value > 0) return goSection(section.value - 1); return goPage(current.value, true) }
function next() { if (isText.value && section.value < sections.value.length - 1) return goSection(section.value + 1); return goPage(current.value + 2) }
function activeVideo(event: Event): HTMLVideoElement | null {
  const target = event.currentTarget as HTMLVideoElement | null
  return !disposed && isVideo.value && target === video.value && target?.dataset.assetId === currentAsset.value?.id ? target : null
}
function videoLoaded(event: Event) {
  const target = activeVideo(event)
  if (!target || !Number.isFinite(target.duration) || target.duration <= 0) return
  const time = target.duration * boundedOffset(offset.value)
  videoRestorePending = Math.abs(target.currentTime - time) > 0.05
  if (videoRestorePending) {
    try { target.currentTime = time }
    catch { videoRestorePending = false; error.value = '无法恢复视频位置，请重试当前视频或下载原文件播放。'; return }
  }
  videoReady.value = true
  scheduleSave()
}
function videoProgress(event: Event, saveNow = false, seeked = false) {
  const target = activeVideo(event)
  if (!target || !videoReady.value || !Number.isFinite(target.duration) || target.duration <= 0) return
  if (seeked) videoRestorePending = false
  if (videoRestorePending) return
  offset.value = boundedOffset(target.currentTime / target.duration)
  if (saveNow) { clearTimeout(progressTimer); progressTimer = undefined; void saveProgress() }
  // timeupdate can fire several times a second; throttle without starving saves.
  else if (!progressTimer) progressTimer = setTimeout(() => { progressTimer = undefined; void saveProgress() }, 5000)
}
function videoFailed(event: Event) {
  if (!activeVideo(event)) return
  clearTimeout(progressTimer); progressTimer = undefined; videoReady.value = false
  error.value = '视频读取失败或浏览器不支持此编码。可重试当前视频，或下载原文件播放。'
}
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
  if (document.fullscreenElement) return
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
  if ((event.target as HTMLElement).matches('input, textarea, select, video') || isVideo.value) return
  if (event.key === 'ArrowLeft') { event.preventDefault(); previous() }
  if (event.key === 'ArrowRight') { event.preventDefault(); next() }
}
onMounted(() => { document.addEventListener('keydown', keydown); displayAsset(props.item.progress); document.body.style.overflow = 'hidden' })
onBeforeUnmount(() => { void saveProgress(); disposed = true; loadId++; loadController?.abort(); clearTimeout(progressTimer); document.removeEventListener('keydown', keydown); document.body.style.overflow = '' })
</script>
<template>
  <section class="reader" role="dialog" aria-modal="true" :aria-label="`阅读：${item.title}`">
    <header class="reader-header"><button class="reader-back" aria-label="返回书库" @click="close"><ArrowLeft :size="20" /><span>返回书库</span></button><div class="reader-title"><BookOpen :size="17" /><strong>{{ item.title }}</strong></div><div class="reader-actions"><button class="icon-button" :aria-label="isText ? '打开章节目录' : '打开文件目录'" :title="isText ? '章节目录' : '文件目录'" @click="showOutline('pages')"><List :size="20" /></button><button class="icon-button" aria-label="打开系列与上下集" title="系列与上下集" @click="showOutline('series')"><Layers3 :size="19" /></button><template v-if="isText"><button class="icon-button reader-font-button" aria-label="减小字号" @click="resizeText(-1)">A−</button><button class="icon-button reader-font-button" aria-label="增大字号" @click="resizeText(1)">A＋</button></template><button v-else-if="!isVideo" class="icon-button" :aria-label="fit ? '原始宽度' : '适应屏幕'" @click="fit = !fit"><component :is="fit ? Maximize2 : Minimize2" :size="18" /></button><a v-if="isVideo && currentAsset" class="icon-button" :href="currentAsset.url" :download="currentAsset.original_name" aria-label="下载当前视频" title="下载当前视频"><Download :size="18" /></a><a v-if="contentReady(item) && assets.length" class="icon-button reader-export" :href="`/api/items/${item.id}/export`" aria-label="下载离线存档"><Download :size="18" /></a></div></header>
    <div ref="content" class="reader-content" :class="{'text-reader': isText, 'video-reader': isVideo}" @scroll.passive="scrolled"><div v-if="loading" class="reader-message"><LoaderCircle class="spin" />正在读取正文</div><div v-else-if="error" class="reader-message"><AlertCircle />{{ error }}<button class="button secondary small" @click="displayAsset({page: current, section, offset})">重试当前{{ isVideo ? '视频' : isText ? '正文' : '图片' }}</button><a v-if="isVideo && currentAsset" class="button secondary small" :href="currentAsset.url" :download="currentAsset.original_name">下载原视频</a></div><article v-else-if="isText && currentSection" class="reader-chapter"><h1>{{ currentSection.title || `第 ${section + 1} 节` }}</h1><pre class="reader-text" :style="{fontSize: `${textSize}px`}">{{ currentSection.text }}</pre><div class="reader-chapter-navigation"><button class="button secondary" :disabled="!hasPrevious" @click="previous"><ChevronLeft :size="18" />{{ previousLabel }}</button><button v-if="hasNext" class="button secondary" @click="next">{{ nextLabel }}<ChevronRight :size="18" /></button><button v-else class="button secondary" :disabled="offset === 1" @click="markFinished">{{ offset === 1 ? '已读完' : '标记读完' }}</button></div></article><div v-else-if="isVideo && currentAsset" class="reader-video-wrap"><video ref="video" :key="`${currentAsset.id}:${videoRevision}`" :data-asset-id="currentAsset.id" :src="currentAsset.url" class="reader-video" controls playsinline preload="metadata" @loadedmetadata="videoLoaded" @timeupdate="videoProgress($event)" @seeked="videoProgress($event, true, true)" @pause="videoProgress($event, true)" @ended="videoProgress($event, true)" @error="videoFailed">浏览器不支持视频播放。</video><p class="reader-video-name">{{ currentAsset.original_name }} · 使用播放器控制播放、拖动进度或全屏</p></div><img v-else-if="currentAsset && currentAsset.mime_type.startsWith('image/')" :key="currentAsset.id" :src="currentAsset.url" :alt="`第 ${current + 1} / ${assets.length} P`" class="reader-image" :class="{'fit-width': fit}" @error="error = '本页图片读取失败，请检查存储连接。'" /><div v-else class="reader-message"><BookOpen />暂无可阅读的正文</div></div>
    <p v-if="saveError" class="reader-save-error" role="status">{{ saveError }}</p>
    <footer class="reader-footer"><span class="reader-hint">{{ isVideo ? '播放器内拖动进度 · Esc 返回' : isText ? '← → 切换章节／文件 · Esc 返回' : '← → 翻页 · Esc 返回' }}</span><div class="page-navigation"><button class="icon-button" :disabled="!hasPrevious || loading" :aria-label="previousLabel" @click="previous"><ChevronLeft :size="22" /></button><button v-if="isText" class="reader-position-button" :disabled="loading || !sections.length" @click="showOutline('pages')">第 {{ sections.length ? section + 1 : 0 }} / {{ sections.length }} 节</button><form v-else @submit.prevent="goPage(Number(pageInput))"><input v-model.number="pageInput" :aria-label="hasMixedMedia ? '文件序号' : '分 P 页码'" type="number" min="1" :max="assets.length" /><span>/ {{ assets.length }} {{ hasMixedMedia ? '项' : 'P' }}</span></form><button class="icon-button" :disabled="!hasNext || loading" :aria-label="nextLabel" @click="next"><ChevronRight :size="22" /></button></div><span class="reader-percent" :title="isVideo ? '当前视频播放进度' : isText ? '当前正文的阅读位置' : '当前分 P 位置'">{{ percent }}%</span></footer>
    <div v-if="outlineOpen" class="reader-outline-backdrop" @click.self="outlineOpen = false"><aside ref="outline" class="reader-outline" role="dialog" aria-modal="true" aria-label="阅读目录"><header><h2>阅读目录</h2><button class="icon-button" aria-label="关闭阅读目录" @click="outlineOpen = false"><X :size="20" /></button></header><div class="reader-outline-tabs"><button :class="{active: outlineTab === 'pages'}" @click="outlineTab = 'pages'">{{ isText ? '章节与文件' : hasMixedMedia ? '文件' : '分 P 页面' }}</button><button :class="{active: outlineTab === 'series'}" @click="outlineTab = 'series'">系列 / 上下集</button></div><div class="reader-outline-body"><SeriesPanel v-if="outlineTab === 'series'" :item="item" :editable="false" @open="openWork" @queued="emit('queued', $event)" /><template v-else><label v-if="assets.length > 1 && isText" class="field">作品文件<select :value="current" @change="goPage(Number(($event.target as HTMLSelectElement).value) + 1)"><option v-for="(asset, index) in assets" :key="asset.id" :value="index">{{ asset.original_name || `正文 ${index + 1}` }}</option></select></label><div v-if="loading" class="inline-loading"><LoaderCircle class="spin" :size="17" />读取章节…</div><ol v-else-if="isText" class="reader-section-list"><li v-for="(entry, index) in sections" :key="entry.index"><button :class="{current: index === section}" :aria-current="index === section ? 'true' : undefined" @click="goSection(index)"><span>{{ index + 1 }}</span>{{ entry.title || `第 ${index + 1} 节` }}</button></li></ol><div v-else class="reader-page-grid"><button v-for="(asset, index) in assets" :key="asset.id" :class="{current: index === current}" :aria-label="`跳转至${asset.mime_type.startsWith('video/') ? '视频' : asset.mime_type.startsWith('text/') ? '正文' : '图片'} ${index + 1}`" :aria-current="index === current ? 'true' : undefined" @click="goPage(index + 1)"><img v-if="asset.mime_type.startsWith('image/')" :src="asset.url" alt="" loading="lazy" /><Film v-else-if="asset.mime_type.startsWith('video/')" :size="23" /><BookOpen v-else :size="23" /><span>{{ index + 1 }} {{ asset.mime_type.startsWith('video/') ? '视频' : asset.mime_type.startsWith('text/') ? '正文' : 'P' }}</span></button></div></template></div></aside></div>
  </section>
</template>

<style scoped>
.video-reader {display:flex;align-items:center;justify-content:center;}.reader-video-wrap {width:min(100%,1100px);margin:auto;padding:20px;}.reader-video {display:block;width:100%;max-height:calc(100dvh - 210px);background:#111;border-radius:6px;}.reader-video-name {margin:12px 0 0;color:#cbd5e1;font-size:13px;line-height:1.8;overflow-wrap:anywhere;text-align:center;}@media(max-width:600px){.reader-video-wrap{padding:8px}.reader-video{max-height:calc(100dvh - 190px)}}
</style>
