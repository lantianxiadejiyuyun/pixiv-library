<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, post, errorMessage } from '../api'
import type { Item } from '../types'
import BookCard from './BookCard.vue'

type Task = {id: string; status: string; error?: string; author_id?: string; refresh_paused?: boolean}
type Profile = {remote_id: string; name: string; description: string; avatar_url?: string; can_read_private: boolean; synced_at?: string; download_settings?: {download_works:boolean;download_bookmarks:boolean;subscribed:boolean}}
type Feed = {items: Item[]; total: number; complete: boolean; has_more: boolean; revision: number; unavailable: number; job: Task | null; local_only?: boolean; refresh_paused?: boolean}
type Home = {remote_id: string; section: string; category: string; restrict: string; job: Task | null; profile?: Profile | null; feed?: Feed | null; refresh_paused?: boolean}
const props = defineProps<{seed?: {key: number; target: string; query?: string}; blurAdult: boolean}>()
const emit = defineEmits<{open: [id: string]}>()
const query = ref(''), input = ref(''), error = ref(''), notice = ref('')
const authors = ref<{remote_id: string; name: string}[]>([])
const authorPage = ref(1), authorTotal = ref(0), page = ref(1)
const profile = ref<Profile | null>(null), authorId = ref(''), feed = ref<Feed | null>(null)
const section = ref('works'), category = ref('illustration'), privacy = ref('public')
const pending = ref(false), opening = ref(false)
const refreshPaused = ref(false)
const savingDownloads = ref(false)
async function saveDownloads(key:'download_works'|'download_bookmarks') {
  const current=profile.value,ident=authorId.value,version=profileRequest
  if(savingDownloads.value||!current?.download_settings)return
  savingDownloads.value=true;error.value=''
  try {
    const data=await api<NonNullable<Profile['download_settings']>>(`/pixiv/authors/${ident}/download-settings`,{method:'PUT',body:JSON.stringify({...current.download_settings,[key]:!current.download_settings[key],subscribed:undefined})})
    if(alive&&version===profileRequest&&ident===authorId.value&&profile.value)profile.value.download_settings=data
  }catch(failure){if(alive&&version===profileRequest)error.value=errorMessage(failure)}
  finally{if(alive)savingDownloads.value=false}
}
let request = 0, profileRequest = 0, alive = true, listRequest = 0
let profileTimer: ReturnType<typeof setTimeout> | undefined, feedTimer: ReturnType<typeof setTimeout> | undefined
const filters = () => ({section: section.value, category: category.value, restrict: privacy.value})
async function list(reset = false) {
  if (reset) authorPage.value = 1
  const version = ++listRequest
  try {
    const data = await api<{items: typeof authors.value; total: number}>(`/pixiv/authors?${new URLSearchParams({q: query.value, page: String(authorPage.value)})}`)
    if (!alive || version !== listRequest) return
    authors.value = data.items; authorTotal.value = data.total
  } catch (failure) { if (alive && version === listRequest) error.value = errorMessage(failure) }
}
async function readProfile() {
  const ident = authorId.value, version = profileRequest
  const data = await api<Profile>(`/pixiv/authors/${ident}`)
  if (alive && ident === authorId.value && version === profileRequest) profile.value = data
}
async function watchProfile(id: string, version: number) {
  try {
    const task = await api<Task>(`/pixiv/tasks/${id}`)
    if (!alive || version !== profileRequest) return
    if (['queued', 'running'].includes(task.status)) {
      if (task.refresh_paused) { refreshPaused.value = true; opening.value = false; notice.value = '联网更新已暂停，当前显示已收录内容。'; return }
      notice.value = '正在服务器队列中读取作者资料；采集暂停时，请在任务页恢复。'
      profileTimer = setTimeout(() => watchProfile(id, version), 3000)
    } else if (task.status === 'completed' && task.author_id) {
      authorId.value = task.author_id; opening.value = false; notice.value = ''
      await Promise.all([readProfile(), readFeed(true)])
    } else { opening.value = false; error.value = task.error || '作者资料任务未完成' }
  } catch (failure) { if (alive && version === profileRequest) { opening.value = false; error.value = errorMessage(failure) } }
}
async function refreshProfile(target = authorId.value || 'me') {
  const version = profileRequest
  clearTimeout(profileTimer); error.value = ''; opening.value = true
  try {
    const result = await post<Home>('/pixiv/authors/open', {url_or_id: target})
    if (!alive || version !== profileRequest) return
    refreshPaused.value = !!result.refresh_paused
    if (result.refresh_paused) notice.value = '联网更新已暂停，当前显示已收录内容。'
    else if (result.job) { notice.value = '正在后台更新作者资料。'; void watchProfile(result.job.id, version) }
  } catch (failure) { if (alive && version === profileRequest) error.value = errorMessage(failure) }
  finally { if (alive && version === profileRequest) opening.value = false }
}
async function open(target: string, initialSection?: string) {
  if (!target.trim()) return
  const version = ++profileRequest
  request++; clearTimeout(profileTimer); clearTimeout(feedTimer)
  error.value = ''; notice.value = ''; profile.value = null; feed.value = null; authorId.value = ''; pending.value = false; opening.value = true; page.value = 1
  try {
    const result = await post<Home>('/pixiv/authors/open', {url_or_id: target.trim(), prefer_cached: true, section: initialSection})
    if (!alive || version !== profileRequest) return
    section.value = initialSection || result.section; category.value = result.category; privacy.value = result.restrict
    refreshPaused.value = !!result.refresh_paused
    if (result.profile && result.feed) {
      authorId.value = result.remote_id; profile.value = result.profile; feed.value = result.feed
      opening.value = false
      pending.value = !refreshPaused.value && !!result.feed.job && ['queued', 'running'].includes(result.feed.job.status)
      if (refreshPaused.value) notice.value = '联网更新已暂停，当前显示已收录内容。'
      else {
        if (!result.profile.synced_at) void refreshProfile()
        if (pending.value && result.feed.job) void watchFeed(result.feed.job.id, request)
        else if (!result.feed.revision && !result.feed.job) void queue()
      }
      return
    }
    if (result.remote_id !== 'me') { authorId.value = result.remote_id; await Promise.all([readProfile(), readFeed(privacy.value === 'public')]) }
    if (!alive || version !== profileRequest) return
    if (refreshPaused.value) notice.value = '联网更新已暂停，当前显示已收录内容。'
    else if (result.job) void watchProfile(result.job.id, version)
    else void refreshProfile(target.trim())
  } catch (failure) { if (alive && version === profileRequest) { opening.value = false; error.value = errorMessage(failure) } }
  finally { if (alive && version === profileRequest) opening.value = false }
}
async function readFeed(auto = false) {
  if (!authorId.value) return
  const version = ++request
  const data = await api<Feed>(`/pixiv/authors/${authorId.value}/feed?${new URLSearchParams({...filters(), page: String(page.value)})}`)
  if (!alive || version !== request) return
  feed.value = data
  refreshPaused.value = data.refresh_paused ?? refreshPaused.value
  pending.value = !refreshPaused.value && !!data.job && ['queued', 'running'].includes(data.job.status)
  if (refreshPaused.value) { notice.value = '联网更新已暂停，当前显示已收录内容。'; return }
  if (pending.value && data.job) { clearTimeout(feedTimer); watchFeed(data.job.id, version) }
  else if (auto && data.revision === 0 && !data.job) await queue()
  else if (data.job?.status === 'failed') error.value = data.job.error || '目录读取失败'
}
async function watchFeed(id: string, version: number, nextPage?: number) {
  try {
    const task = await api<Task>(`/pixiv/tasks/${id}`)
    if (!alive || version !== request) return
    if (['queued', 'running'].includes(task.status)) {
      if (task.refresh_paused) { refreshPaused.value = true; pending.value = false; notice.value = '联网更新已暂停，当前显示已收录内容。'; return }
      pending.value = true
      feedTimer = setTimeout(() => watchFeed(id, version, nextPage), 3000)
    } else {
      pending.value = false
      if (task.status === 'completed' && nextPage) page.value = nextPage
      await readFeed()
      if (task.status === 'failed') error.value = task.error || '目录读取失败'
    }
  } catch (failure) { if (alive && version === request) { pending.value = false; error.value = errorMessage(failure) } }
}
async function queue(more = false) {
  if (!authorId.value || pending.value) return
  const version = ++request; error.value = ''; pending.value = true
  try {
    const result = await post<{job: Task | null}>(`/pixiv/authors/${authorId.value}/feed`, {...filters(), more})
    if (!alive || version !== request) return
    if (refreshPaused.value) { pending.value = false; notice.value = '已加入更新队列，联网读取目前暂停。'; return }
    if (result.job) await watchFeed(result.job.id, version, more ? Math.floor((feed.value?.total || 0) / 24) + 1 : 1)
    else { pending.value = false; await readFeed() }
  } catch (failure) { if (alive && version === request) { pending.value = false; error.value = errorMessage(failure) } }
}
async function change() {
  request++; clearTimeout(feedTimer); feed.value = null; pending.value = false; error.value = ''; page.value = 1
  if (section.value === 'works') privacy.value = 'public'
  try { await readFeed(true) } catch (failure) { error.value = errorMessage(failure) }
}
async function changePage(value: number) { page.value = value; try { await readFeed() } catch (failure) { error.value = errorMessage(failure) } }
function seed() { if (!props.seed) return; query.value = props.seed.query || ''; list(true); if (props.seed.target) open(props.seed.target) }
watch(() => props.seed?.key, seed)
onMounted(() => { if (props.seed) seed(); else list() })
onBeforeUnmount(() => { alive = false; request++; profileRequest++; clearTimeout(profileTimer); clearTimeout(feedTimer) })
</script>

<template>
  <section class="panel author-controls">
    <form class="author-input" @submit.prevent="open(input)"><input v-model="input" aria-label="作者或收藏链接" placeholder="Pixiv 作者 ID、主页或收藏链接" required maxlength="2048" /><button class="button primary small" :disabled="opening">打开主页</button></form>
    <div class="author-actions"><button class="button secondary small" @click="open('me')">我的主页</button><button class="button secondary small" @click="open('me', 'bookmarks')">我的收藏</button></div>
  </section>
  <div v-if="error" class="alert error" role="alert">{{ error }}</div>
  <div v-if="notice" class="alert info" role="status">{{ notice }}</div>
  <section v-if="profile || authorId" class="panel author-controls">
    <div v-if="profile" class="author-header"><img v-if="profile.avatar_url" :src="profile.avatar_url" alt="作者头像" /><div><h2>{{ profile.name }}</h2><p class="muted">Pixiv ID {{ profile.remote_id }}</p></div></div>
    <p v-if="profile?.description" class="author-description">{{ profile.description }}</p>
    <div v-if="profile?.download_settings" class="author-actions"><label><input type="checkbox" :checked="profile.download_settings.download_works" :disabled="savingDownloads" @change="saveDownloads('download_works')" />下载作者发布的作品</label><label><input type="checkbox" :checked="profile.download_settings.download_bookmarks" :disabled="savingDownloads" @change="saveDownloads('download_bookmarks')" />下载作者公开收藏</label><small class="muted">关闭时仍获取封面和资料；已排队任务不自动取消。</small></div>
    <div class="author-actions"><button class="button small" :class="section === 'works' ? 'primary' : 'secondary'" @click="section = 'works'; change()">作者作品</button><button class="button small" :class="section === 'bookmarks' ? 'primary' : 'secondary'" @click="section = 'bookmarks'; change()">收藏</button><button class="button secondary small" :disabled="opening" @click="refreshProfile()">刷新作者资料</button></div>
    <div class="author-actions"><button v-for="(label, key) in {illustration: '插画', manga: '漫画', novel: '小说'}" :key="key" class="button small" :class="category === key ? 'primary' : 'secondary'" @click="category = key; change()">{{ label }}</button>
      <select v-if="section === 'bookmarks' && profile?.can_read_private" v-model="privacy" aria-label="收藏可见性" @change="change"><option value="public">公开收藏</option><option value="private">本人私密收藏</option></select>
      <button class="button secondary small" :disabled="pending" @click="queue()">重新读取目录</button></div>
    <p class="field-hint">{{ feed?.local_only ? '本地已收录' : '已读取' }} {{ feed?.total || 0 }} 部 · {{ feed?.complete ? '目录已完整读取' : '远端目录尚未完整读取' }}</p>
    <p v-if="pending" class="field-hint">正在服务器队列中读取；采集暂停时，请在任务页恢复。</p>
    <p v-if="feed?.unavailable" class="field-hint">含不可访问或暂不支持的条目，旧目录会保留。</p>
    <div v-if="feed?.items.length" class="book-grid"><BookCard v-for="item in feed.items" :key="item.id" :item="item" :blur-adult="blurAdult" @open="emit('open', item.id)" /></div>
    <p v-else class="muted">目录尚无可显示内容。</p>
    <div class="author-actions"><button class="button secondary small" :disabled="page <= 1" @click="changePage(page - 1)">上一页</button><span>{{ page }} 页</span><button v-if="page * 24 < (feed?.total || 0)" class="button secondary small" @click="changePage(page + 1)">下一页</button><button v-else class="button secondary small" :disabled="pending || !feed?.has_more" @click="queue(true)">获取下一批</button></div>
  </section>
  <section class="panel author-controls">
    <form class="author-input" @submit.prevent="list(true)"><input v-model="query" aria-label="搜索已关注作者" placeholder="搜索已关注或已保存的作者" /><button class="button secondary small">搜索作者</button></form>
    <div class="author-directory"><button v-for="author in authors" :key="author.remote_id" class="button secondary" @click="open(author.remote_id)"><span>{{ author.name || 'Pixiv 作者' }}</span><small>{{ author.remote_id }}</small></button></div>
    <div class="author-actions"><button class="button secondary small" :disabled="authorPage <= 1" @click="authorPage--; list()">上一页作者</button><span>{{ authorPage }} 页 · {{ authorTotal }} 位</span><button class="button secondary small" :disabled="authorPage * 30 >= authorTotal" @click="authorPage++; list()">下一页作者</button></div>
  </section>
</template>

<style scoped>
.author-controls {padding:20px;margin-bottom:18px;}
.author-input,.author-actions {display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin:12px 0;}
.author-input input {flex:1;min-width:180px;}
.author-header {display:flex;align-items:center;gap:14px;}
.author-header img {width:64px;height:64px;border-radius:50%;object-fit:cover;}
.author-description {white-space:pre-wrap;max-height:160px;overflow:auto;line-height:1.7;}
.author-directory {display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin:16px 0;}
.author-directory button {justify-content:space-between;overflow:hidden;}
.author-directory span {overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.author-directory small {color:var(--text-muted);}
</style>
