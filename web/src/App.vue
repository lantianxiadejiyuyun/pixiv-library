<script setup lang="ts">
import { ref, reactive, computed, watch, nextTick, onMounted, onBeforeUnmount } from 'vue'
import { LibraryBig, BookOpen, PanelsTopLeft, Rss, Tags, ListTodo, Sparkles, Settings2, Plus, Search, ChevronRight, ChevronLeft, ChevronDown, ArrowUpRight, ArrowRight, Upload, FolderOpen, HardDrive, RefreshCw, Check, CheckCircle2, AlertCircle, X, Menu, LogOut, LockKeyhole, LoaderCircle, Download, Pencil, Trash2, Globe2, Plug, SlidersHorizontal, Bookmark, FileText, Image, CircleHelp, Layers3, Database, Server, Tag as TagIcon, CheckCheck } from 'lucide-vue-next'
import { api, post, errorMessage } from './api'
import type { Item, ItemPreferencePatch, Dashboard, Source, SourceAccount, Subscription, Job, AIStatus, Settings, ItemType, Rating, Tag, Collection, CharacterLookupSeed, ReadingProgress, ContentScope } from './types'
import { bytes, date, publicationDate, typeLabels, sourceLabels, remotePlatforms, ratingLabels, jobStatusLabels, jobKindLabels, contentReady, contentStateLabel, contentStateTone, contentCountLabel, contentScopeLabels } from './types'
import { pixivImportInput } from './reading'
import BookCard from './components/BookCard.vue'
import TagBrowser from './components/TagBrowser.vue'
import Reader from './components/Reader.vue'
import Modal from './components/Modal.vue'
import JobResult from './components/JobResult.vue'
import SourceLoginModal from './components/SourceLoginModal.vue'
import ProxySettings from './components/ProxySettings.vue'
import CollectionSwitcher from './components/CollectionSwitcher.vue'
import ManagedProxySettings from './components/ManagedProxySettings.vue'
import PlatformApiSettings from './components/PlatformApiSettings.vue'
import CharacterLookup from './components/CharacterLookup.vue'
import AIExecutionStatus from './components/AIExecutionStatus.vue'
import AINodes from './components/AINodes.vue'
import DownloadQueue from './components/DownloadQueue.vue'
import ContentDownloadState from './components/ContentDownloadState.vue'
import DatabaseBackup from './components/DatabaseBackup.vue'
import SeriesPanel from './components/SeriesPanel.vue'
import DevicePairing from './components/DevicePairing.vue'
import TranslationSettings from './components/TranslationSettings.vue'
import AppUpdateSettings from './components/AppUpdateSettings.vue'
import PixivAccountRoles from './components/PixivAccountRoles.vue'
import PixivCollectionSettings from './components/PixivCollectionSettings.vue'
import PixivFollowDownloadSettings from './components/PixivFollowDownloadSettings.vue'
import DownloadAccounts from './components/DownloadAccounts.vue'
import CharacterWall from './components/CharacterWall.vue'
import AuthorsPanel from './components/AuthorsPanel.vue'
import WatchlistPanel from './components/WatchlistPanel.vue'
import FavoritesPanel from './components/FavoritesPanel.vue'
import FavoriteControl from './components/FavoriteControl.vue'
import { createItemPreferenceTracker } from './item_preferences'

type View = 'library' | 'sources' | 'subscriptions' | 'tags' | 'tasks' | 'ai' | 'settings' | 'authors' | 'accounts' | 'characters' | 'favorites' | 'history' | 'watchlist'
const favoriteRevision = ref(0)
const preferenceTracker = createItemPreferenceTracker()
function favoriteChanged(patch: ItemPreferencePatch) {
  preferenceTracker.record(patch)
  if (selectedItem.value?.id === patch.id) selectedItem.value = {...selectedItem.value, ...patch}
  items.value = items.value.map(current => current.id === patch.id ? {...current, ...patch} : current)
  aiItems.value = aiItems.value.map(current => current.id === patch.id ? {...current, ...patch} : current)
  favoriteRevision.value++
}
async function fetchItem(id: string) {
  const mergePreferences = preferenceTracker.snapshot()
  return mergePreferences(await api<Item>(`/items/${encodeURIComponent(id)}`))
}
const authorSeed = ref<{key: number; target: string; query?: string}>()
type ModalType = '' | 'import' | 'detail' | 'account' | 'remote' | 'subscription'
const nav = [
  {id: 'library', label: '我的书库', icon: LibraryBig}, {id: 'sources', label: '内容来源', icon: PanelsTopLeft},
  {id: 'accounts', label: '账号管理', icon: LockKeyhole},
  {id: 'favorites', label: '我的收藏', icon: Bookmark},
  {id: 'history', label: '浏览历史', icon: BookOpen},
  {id: 'authors', label: '作者与收藏', icon: Bookmark},
  {id: 'characters', label: '人物作品墙', icon: Image},
  {id: 'subscriptions', label: '关注订阅', icon: Rss}, {id:'watchlist',label:'连载追更',icon:Layers3}, {id: 'tags', label: '标签分类', icon: Tags},
  {id: 'tasks', label: '任务中心', icon: ListTodo}, {id: 'ai', label: 'AI 整理', icon: Sparkles},
] as const
const view = ref<View>('library')
const titles: Record<View, {title: string}> = {
  library: {title: '我的书库'},
  favorites: {title: '我的收藏'},
  history: {title: '浏览历史'},
  sources: {title: '内容来源'},
  accounts: {title: '账号管理'},
  characters: {title: '人物作品墙'},
  subscriptions: {title: '关注订阅'},
  watchlist: {title:'连载追更'},
  tags: {title: '标签分类'},
  tasks: {title: '任务中心'},
  ai: {title: 'AI 整理'},
  settings: {title: '书库设置'},
  authors: {title: 'Pixiv 作者与收藏'},
}
const booting = ref(true)
const authenticated = ref(false)
const password = ref('')
const loginError = ref('')
const busy = ref(false)
const loading = ref(false)
const pageError = ref('')
const modalError = ref('')
const mobileNav = ref(false)
const externalProxyOpen = ref(false)
const managedProxyEnabled = ref(false)
const toast = ref<{message: string; error?: boolean} | null>(null)
let toastTimer: ReturnType<typeof setTimeout> | undefined
let pollingTimer: ReturnType<typeof setInterval> | undefined
let searchTimer: ReturnType<typeof setTimeout> | undefined
const dashboard = ref<Dashboard | null>(null)
const collections = ref<Collection[]>([])
const collectionsError = ref('')
const settings = ref<Settings | null>(null)
const items = ref<Item[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = 24
const query = ref('')
const searchQuery = ref('')
const typeFilter = ref('')
const sourceFilter = ref('')
const ratingFilter = ref('')
const tagFilter = ref('')
const sort = ref('latest')
const showFilters = ref(false)
const blurAdult = ref(localStorage.getItem('qiye:blur-adult') !== 'false')
const sources = ref<Source[]>([])
const pixivRoles = ref<{enabled: boolean; main_account_id: string | null; content_account_id: string | null; content_account_ids?: string[]} | null>(null)
const subscriptions = ref<Subscription[]>([])
const jobs = ref<Job[]>([])
const jobFilter = ref('')
const focusedJob = ref<Job | null>(null)
const focusedJobError = ref('')
const aiStatus = ref<AIStatus | null>(null)
const aiItems = ref<Item[]>([])
const aiSelected = ref<string[]>([])
const aiStatusRefreshing = ref(false)
const aiStatusError = ref('')
const characterLookupSeed = ref<CharacterLookupSeed | null>(null)
const analyzableItems = computed(() => aiItems.value.filter(contentReady))
const modal = ref<ModalType>('')
const selectedItem = ref<Item | null>(null)
const selectedContentJob = computed(() => jobs.value.find(job => job.id === selectedItem.value?.content_job_id))
const readerItem = ref<Item | null>(null)
const editMode = ref(false)
const confirmDelete = ref(false)
const selectedSource = ref<Source | null>(null)
const selectedSubscription = ref<Subscription | null>(null)
const edit = reactive({title: '', author: '', description: '', language: '', rating: 'unknown' as Rating, tags: ''})
const importMode = ref<'upload' | 'folder'>('upload')
const uploadFiles = ref<File[]>([])
const fileInput = ref<HTMLInputElement | null>(null)
const dragActive = ref(false)
const importForm = reactive({title: '', type: 'manga' as ItemType, path: ''})
const remoteForm = reactive({url: '', account_id: '', mode: 'work' as 'work' | 'series', kind: 'manga' as 'manga' | 'novel'})
const subscriptionForm = reactive({platform: 'pixiv', target_type: 'author', target: '', label: '', enabled: true, auto_download: false, download_works: false, download_bookmarks: false, content_scope: 'all' as ContentScope, account_id: '', interval_minutes: 360})
const subscriptionAccounts = computed(() => sources.value.find(source => source.platform === subscriptionForm.platform)?.accounts || [])
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
const activeFilters = computed(() => !!(searchQuery.value || typeFilter.value || ratingFilter.value || tagFilter.value))
const jobLane = ref('')
function jobsPath() { return `/jobs${view.value === 'tasks' && jobLane.value ? '?queue=' + jobLane.value : ''}` }
watch(jobLane, () => { if (authenticated.value) pollJobs() })
const jobLanes: Record<string, string> = {retry: '失败重试', manual_download: '手动下载', download: '自动下载', details: '详情解析', checks: '作者与订阅检查', ai: 'AI 识别', translation: 'AI 汉化', other: '其他任务'}
const filteredJobs = computed(() => jobs.value.filter(job => (!jobLane.value || job.queue === jobLane.value) && (!jobFilter.value || job.status === jobFilter.value)))
const runningCount = computed(() => jobs.value.filter(job => job.status === 'running' || job.status === 'queued').length)
const currentCollection = computed(() => collections.value.find(collection => collection.source === sourceFilter.value))
const currentLibraryName = computed(() => sourceFilter.value ? (currentCollection.value?.name || sourceLabels[sourceFilter.value]).replace(/书库$/, '') : '全库汇总')
const currentLibraryTotal = computed(() => currentCollection.value?.item_count ?? (sourceFilter.value ? dashboard.value?.source_counts[sourceFilter.value] || 0 : dashboard.value?.total_items || 0))
const currentLibraryPages = computed(() => currentCollection.value?.page_count ?? (sourceFilter.value ? 0 : dashboard.value?.total_pages || 0))
const currentLibraryBytes = computed(() => currentCollection.value?.size_bytes ?? (sourceFilter.value ? 0 : dashboard.value?.total_bytes || 0))
const currentLibrarySaved = computed(() => currentCollection.value?.saved_item_count ?? (sourceFilter.value ? 0 : collections.value.reduce((sum, collection) => sum + collection.saved_item_count, 0)))
const selectedTitle = computed(() => view.value === 'library' ? {title: sourceFilter.value ? `${currentLibraryName.value}书库` : titles.library.title} : titles[view.value])
const uploadSize = computed(() => uploadFiles.value.reduce((sum, file) => sum + file.size, 0))

function notify(message: string, error = false) {
  toast.value = {message, error}; clearTimeout(toastTimer)
  toastTimer = setTimeout(() => toast.value = null, error ? 7000 : 4500)
}
function unauthorized() { if (authenticated.value) { authenticated.value = false; modal.value = ''; readerItem.value = null; loginError.value = '登录已过期，请重新登录。' } }
async function login() {
  busy.value = true; loginError.value = ''
  try { await post('/auth/login', {password: password.value}); authenticated.value = true; password.value = ''; await initialize() }
  catch (error) { loginError.value = errorMessage(error) }
  finally { busy.value = false }
}
async function logout() {
  try { await post('/auth/logout'); authenticated.value = false; modal.value = ''; readerItem.value = null }
  catch (error) { notify(errorMessage(error), true) }
}
async function initialize() {
  booting.value = false
  const background = Promise.allSettled([
    api<Dashboard>('/dashboard').then(value => dashboard.value = value),
    loadCollections(),
    api<Settings>('/settings').then(value => settings.value = value),
    api<Source[]>('/sources').then(value => sources.value = value),
    api<{enabled: boolean; main_account_id: string | null; content_account_id: string | null}>('/sources/pixiv/account-roles').then(value => pixivRoles.value = value),
    api<{items: Job[]}>(jobsPath()).then(value => jobs.value = value.items),
  ])
  void background.then(results => {
    const failed = results.find(result => result.status === 'rejected')
    if (failed?.status === 'rejected' && authenticated.value) notify(errorMessage(failed.reason), true)
  })
  await loadView()
}
let libraryRequestId = 0
async function loadLibrary(silent = false) {
  const mergePreferences = preferenceTracker.snapshot()
  if (silent && loading.value) return
  const requestId = ++libraryRequestId
  if (!silent) loading.value = true
  pageError.value = ''
  const params = new URLSearchParams({page: String(page.value), page_size: String(pageSize), sort: sort.value})
  for (const [key, value] of Object.entries({q: searchQuery.value, type: typeFilter.value, source: sourceFilter.value, rating: ratingFilter.value, tag: tagFilter.value})) if (value) params.set(key, value)
  try {
    const result = await api<{items: Item[]; total: number}>(`/items?${params}`)
    if (requestId !== libraryRequestId) return
    total.value = result.total
    const lastPage = Math.max(1, Math.ceil(result.total / pageSize))
    if (page.value > lastPage) { page.value = lastPage; return }
    items.value = result.items.map(mergePreferences)
  } catch (error) { if (requestId === libraryRequestId) pageError.value = errorMessage(error) }
  finally { if (requestId === libraryRequestId && !silent) loading.value = false }
}
async function loadView() {
  if (!authenticated.value) return
  if (view.value === 'library') { await Promise.all([loadLibrary(), loadCollections()]); return }
  loading.value = true; pageError.value = ''
  if (view.value === 'favorites' || view.value === 'history') favoriteRevision.value++
  try {
    if (view.value === 'sources' || view.value === 'accounts') sources.value = await api<Source[]>('/sources')
    if (view.value === 'subscriptions') subscriptions.value = (await api<{items: Subscription[]}>('/subscriptions')).items
    if (view.value === 'tasks') jobs.value = (await api<{items: Job[]}>(jobsPath())).items
    if (view.value === 'settings') settings.value = await api<Settings>('/settings')
    if (view.value === 'ai') {
      const mergePreferences = preferenceTracker.snapshot()
      const result = await Promise.all([api<AIStatus>('/ai/status'), api<{items: Item[]}>('/items?page_size=50&sort=latest')])
      aiStatus.value = result[0]; aiItems.value = result[1].items.map(mergePreferences)
    }
  } catch (error) { pageError.value = errorMessage(error) }
  finally { loading.value = false }
}
function navigate(next: View) { view.value = next; mobileNav.value = false; window.history.replaceState(null, '', `#${next}${next === 'library' && sourceFilter.value ? `/${sourceFilter.value}` : ''}`) }
function clearFilters() { query.value = ''; searchQuery.value = ''; typeFilter.value = ''; ratingFilter.value = ''; tagFilter.value = '' }
function selectCollection(source: string) { clearFilters(); sourceFilter.value = source; navigate('library') }
function browseAuthor(item: Item) { closeModal(); authorSeed.value = {key: Date.now(), target: item.author_source_id || '', query: item.author || ''}; navigate('authors') }
function browseTag(tag: string) { sourceFilter.value = ''; tagFilter.value = tag; navigate('library') }
async function loadCollections() {
  try { collections.value = await api<Collection[]>('/library/collections'); collectionsError.value = '' }
  catch (error) { collectionsError.value = `分库信息暂不可用：${errorMessage(error)}` }
}
async function refreshDashboard() { await Promise.all([api<Dashboard>('/dashboard').then(value => dashboard.value = value), loadCollections()]) }
async function refreshAfterChange() { await Promise.all([loadView(), refreshDashboard()]) }
function openModal(kind: ModalType) { modalError.value = ''; modal.value = kind }
function closeModal() { if (busy.value) return; modal.value = ''; modalError.value = ''; confirmDelete.value = false }
function openImport() { uploadFiles.value = []; importForm.title = ''; importForm.path = ''; importForm.type = 'manga'; openModal('import') }
function importIntoCurrentCollection() {
  if (!sourceFilter.value || sourceFilter.value === 'local') { openImport(); return }
  const source = sources.value.find(value => value.platform === sourceFilter.value)
  if (!source) { notify('平台信息尚未加载，请在内容来源中重试。', true); navigate('sources'); return }
  openRemote(source)
}
function filesSelected(event: Event) { uploadFiles.value = Array.from((event.target as HTMLInputElement).files || []) }
function filesDropped(event: DragEvent) { dragActive.value = false; uploadFiles.value = Array.from(event.dataTransfer?.files || []) }
async function importLocal() {
  modalError.value = ''; busy.value = true
  try {
    let item: Item
    if (importMode.value === 'folder') item = await post<Item>('/imports/folder', importForm)
    else {
      if (!uploadFiles.value.length) throw new Error('请先选择要导入的文件。')
      const data = new FormData()
      uploadFiles.value.forEach(file => data.append('files', file))
      data.append('title', importForm.title); data.append('type', importForm.type)
      item = await api<Item>('/imports/upload', {method: 'POST', body: data})
    }
    modal.value = ''; notify(`「${item.title}」已加入书库`); await refreshAfterChange()
  } catch (error) { modalError.value = errorMessage(error) }
  finally { busy.value = false }
}
async function openItem(item: Item) {
  selectedItem.value = item; editMode.value = false; confirmDelete.value = false; openModal('detail'); busy.value = true
  try { selectedItem.value = await fetchItem(item.id); fillEdit(); void post(`/items/${item.id}/visit`, {}).catch(() => {}) }
  catch (error) { modalError.value = errorMessage(error) }
  finally { busy.value = false }
}
async function openResultItem(id: string) {
  try { const item = await fetchItem(id); await openItem(item) }
  catch (error) { notify(errorMessage(error), true) }
}
async function showContentTask(item: Item) {
  if (!item.content_job_id) return
  modal.value = ''; focusedJobError.value = ''; focusedJob.value = jobs.value.find(job => job.id === item.content_job_id) || null
  navigate('tasks')
  try { focusedJob.value = await api<Job>(`/jobs/${item.content_job_id}`) }
  catch (error) { focusedJobError.value = errorMessage(error) }
}
async function refreshSelectedContent() {
  const item = selectedItem.value
  if (!item || modal.value !== 'detail' || editMode.value || busy.value) return
  try {
    const updated = await fetchItem(item.id)
    if (selectedItem.value?.id === item.id && modal.value === 'detail' && !editMode.value && !busy.value) selectedItem.value = updated
  } catch (error) { if (selectedItem.value?.id === item.id) modalError.value = errorMessage(error) }
}
function lookupCharacters(query: string, mode: 'character' | 'work', item?: Item) {
  modal.value = ''
  characterLookupSeed.value = {key: Date.now(), query: query.replace(/^角色[:：]/, '').replace(/_/g, ' ').slice(0, 120), mode, item}
  navigate('ai')
}
async function lookupJobCharacter(value: {query: string; itemId: string}) {
  try {
    const item = value.itemId ? await fetchItem(value.itemId) : undefined
    lookupCharacters(value.query, 'character', item)
  } catch (error) { notify(errorMessage(error), true) }
}
function characterTagSaved(itemId: string, tag: Tag) {
  const records = [...items.value, ...aiItems.value, ...(selectedItem.value ? [selectedItem.value] : []), ...(characterLookupSeed.value?.item ? [characterLookupSeed.value.item] : [])]
  for (const item of records) if (item.id === itemId && !item.tags.some(existing => existing.id === tag.id && existing.origin === tag.origin)) item.tags.push({...tag})
  refreshDashboard().catch(error => notify(errorMessage(error), true))
}
async function refreshAIStatus() {
  if (aiStatusRefreshing.value) return
  aiStatusRefreshing.value = true; aiStatusError.value = ''
  try { aiStatus.value = await api<AIStatus>('/ai/status') }
  catch (error) { aiStatusError.value = errorMessage(error) }
  finally { aiStatusRefreshing.value = false }
}
function fillEdit() {
  if (!selectedItem.value) return
  const item = selectedItem.value
  Object.assign(edit, {title: item.title, author: item.author || '', description: item.description || '', language: item.language || '', rating: item.rating, tags: item.tags.filter(tag => tag.origin === 'manual' || !tag.origin).map(tag => tag.name).join(', ')})
}
async function saveItem() {
  if (!selectedItem.value) return
  const mergePreferences = preferenceTracker.snapshot()
  busy.value = true; modalError.value = ''
  try {
    const {rating, ...fields} = edit
    const payload = {...fields, ...(rating !== selectedItem.value.rating ? {rating} : {}), tags: [...new Set(edit.tags.split(/[,，\n]/).map(tag => tag.trim()).filter(Boolean))]}
    selectedItem.value = mergePreferences(await api<Item>(`/items/${selectedItem.value.id}`, {method: 'PATCH', body: JSON.stringify(payload)}))
    editMode.value = false; notify('作品信息已保存'); await refreshAfterChange()
  } catch (error) { modalError.value = errorMessage(error) }
  finally { busy.value = false }
}
async function removeItem() {
  if (!selectedItem.value) return
  busy.value = true; modalError.value = ''
  try { await api(`/items/${selectedItem.value.id}`, {method: 'DELETE'}); modal.value = ''; notify('作品已从书库移除，原文件保留。'); await refreshAfterChange() }
  catch (error) { modalError.value = errorMessage(error) }
  finally { busy.value = false }
}
function startReading() { if (!selectedItem.value || !contentReady(selectedItem.value) || !selectedItem.value.assets?.length) return; readerItem.value = selectedItem.value; modal.value = '' }
function updateProgress(value: ReadingProgress & {item_id: string}) {
  const {item_id, ...progress} = value
  for (const item of [...items.value, ...aiItems.value, ...(readerItem.value ? [readerItem.value] : []), ...(selectedItem.value ? [selectedItem.value] : [])]) if (item.id === item_id) item.progress = progress
}
async function openReaderSeriesItem(id: string) {
  try {
    const item = await fetchItem(id)
    if (contentReady(item) && item.assets?.length) readerItem.value = item
    else { readerItem.value = null; await nextTick(); await openItem(item) }
  } catch (error) { notify(errorMessage(error), true) }
}
function seriesJobQueued(job: Job) {
  if (!jobs.value.some(value => value.id === job.id)) jobs.value.unshift(job)
  pollJobs()
}
function isAccountVerified(account: SourceAccount) { return account.auth_status === 'verified' || !!account.verified_at }
function accountVerificationHint(account: SourceAccount) {
  if (!isAccountVerified(account)) return '仅保存会话凭据，尚未验证远端登录。'
  const verifiedAt = account.verified_at ? new Date(account.verified_at) : null
  const timestamp = verifiedAt && Number.isFinite(verifiedAt.getTime()) ? `于 ${verifiedAt.toLocaleString('zh-CN')} ` : '添加账号时'
  return `${timestamp}通过远端登录验证；此状态不代表持续验证，会话可能在平台端过期。`
}
function sourceStatus(source: Source) {
  if (['unavailable', 'unsupported', 'pending', 'not_implemented'].includes(source.status)) return '待验证'
  if (source.platform === 'kemono') return '公开访问'
  if (source.accounts?.some(isAccountVerified)) return '已登录'
  if (source.accounts?.length) return '凭据未验证'
  return '未登录'
}
function capabilityLabel(status: string) { return ({available: '可用', supported: '支持', verified: '已验证', implemented: '已实现', pending: '待验证', unavailable: '不可用', unsupported: '暂不支持', partial: '部分支持', experimental: '需实测', blocked: '不可用', unverified: '待验证'} as Record<string, string>)[status] || status }
function syncLabel(source: Source) { return source.capabilities.find(capability => capability.key === 'sync')?.label || '同步内容' }
function canSync(source: Source) { return ['available', 'verified', 'supported', 'implemented'].includes(source.capabilities.find(capability => capability.key === 'sync')?.status || '') }
function openAccount(source: Source) { if (source.platform === 'kemono') return; selectedSource.value = source; openModal('account') }
async function openPlatformAPIs() {
  navigate('settings')
  await nextTick()
  document.getElementById('platform-api-settings')?.scrollIntoView({block: 'start'})
}
function openDownloadAccount() { const source = sources.value.find(source => source.platform === 'pixiv'); if (source) openAccount(source) }
async function reloadAccounts() { try { sources.value = await api<Source[]>('/sources') } catch (error) { notify(errorMessage(error), true) } }
const deleteAccountId = ref('')
async function removeAccount(source: Source, id: string) {
  if (deleteAccountId.value !== id) { deleteAccountId.value = id; return }
  busy.value = true
  try {
    await api(`/sources/${source.platform}/accounts/${id}`, {method: 'DELETE'})
    deleteAccountId.value = ''; sources.value = await api<Source[]>('/sources')
    notify('账号已移除，关联订阅已暂停。')
  } catch (error) { notify(errorMessage(error), true) }
  finally { busy.value = false }
}
async function accountSaved(message: string) {
  modal.value = ''; notify(message)
  try { sources.value = await api<Source[]>('/sources') }
  catch (error) { notify(errorMessage(error), true) }
}
function openRemote(source: Source) { selectedSource.value = source; remoteForm.url = ''; remoteForm.mode = 'work'; remoteForm.kind = typeFilter.value === 'novel' ? 'novel' : 'manga'; remoteForm.account_id = source.platform === 'kemono' ? '' : (source.platform === 'pixiv' && pixivRoles.value?.enabled ? pixivRoles.value.main_account_id : source.accounts?.[0]?.id) || ''; openModal('remote') }
async function downloadOriginal(item: Item) {
  if (busy.value || ['queued', 'downloading'].includes(item.content_state)) return
  busy.value = true; modalError.value = ''
  try {
    const job = await post<Job>(`/items/${item.id}/download`)
    if (!jobs.value.some(existing => existing.id === job.id)) jobs.value.unshift(job)
    const updated = await fetchItem(item.id)
    if (selectedItem.value?.id === updated.id) selectedItem.value = updated
    for (const list of [items.value, aiItems.value]) { const index = list.findIndex(entry => entry.id === updated.id); if (index >= 0) list[index] = updated }
    notify('正文下载已加入队列，可在任务中心查看。')
  } catch (error) { modalError.value = errorMessage(error) }
  finally { busy.value = false }
}
async function importRemote() {
  if (!selectedSource.value) return
  busy.value = true; modalError.value = ''
  try {
    const pixiv = selectedSource.value.platform === 'pixiv'
    const input = pixiv && remoteForm.mode === 'work' ? pixivImportInput(remoteForm.url, remoteForm.kind) : remoteForm.url.trim()
    const account = selectedSource.value.platform !== 'kemono' && remoteForm.account_id ? {account_id: remoteForm.account_id} : {}
    if (pixiv && remoteForm.mode === 'series') await post('/series/import', {platform: 'pixiv', url_or_id: input, kind: remoteForm.kind, ...account})
    else await post(`/sources/${selectedSource.value.platform}/import`, {...(/^https?:\/\//i.test(input) ? {url: input} : {remote_id: input}), ...account})
    modal.value = ''; notify(pixiv && remoteForm.mode === 'series' ? '系列目录任务已创建；成员只获取封面与资料，正文需单独下载。' : '封面与资料任务已创建，正文下载将单独排队。'); await pollJobs()
  } catch (error) { modalError.value = errorMessage(error) }
  finally { busy.value = false }
}
async function syncSource(source: Source, account_id?: string) {
  busy.value = true
  try { await post(`/sources/${source.platform}/sync`, account_id ? {account_id} : {}); notify(`${syncLabel(source)}任务已创建`); await pollJobs() }
  catch (error) { notify(errorMessage(error), true) }
  finally { busy.value = false }
}
function openSubscription(subscription?: Subscription) {
  selectedSubscription.value = subscription || null
  Object.assign(subscriptionForm, subscription ? {platform: subscription.platform, target_type: subscription.target_type, target: subscription.target, label: subscription.label, enabled: subscription.enabled, auto_download: subscription.auto_download, download_works: subscription.download_works ?? subscription.auto_download, download_bookmarks: subscription.download_bookmarks ?? subscription.auto_download, content_scope: subscription.content_scope || 'images', account_id: subscription.account_id || '', interval_minutes: subscription.interval_minutes || 360} : {platform: 'pixiv', target_type: 'author', target: '', label: '', enabled: true, auto_download: false, download_works: false, download_bookmarks: false, content_scope: 'all', account_id: '', interval_minutes: 360})
  openModal('subscription')
}
function changeSubscriptionPlatform() {
  subscriptionForm.account_id = ''
  subscriptionForm.content_scope = subscriptionForm.platform === 'pixiv' && subscriptionForm.target_type === 'author' ? 'all' : 'images'
}
function openSourceSubscription(source: Source) {
  openSubscription()
  subscriptionForm.platform = source.platform
  changeSubscriptionPlatform()
}
async function saveSubscription() {
  busy.value = true; modalError.value = ''
  try {
    const payload = {...subscriptionForm, content_scope: subscriptionForm.platform === 'pixiv' ? subscriptionForm.content_scope : 'images', account_id: subscriptionForm.platform === 'kemono' ? null : subscriptionForm.account_id || null}
    if (selectedSubscription.value) await api(`/subscriptions/${selectedSubscription.value.id}`, {method: 'PATCH', body: JSON.stringify(payload)})
    else await post('/subscriptions', payload)
    modal.value = ''; notify(selectedSubscription.value ? '订阅已更新' : '订阅已创建'); await loadView()
  } catch (error) { modalError.value = errorMessage(error) }
  finally { busy.value = false }
}
const deleteSubscriptionId = ref('')
async function removeSubscription(subscription: Subscription) {
  if (deleteSubscriptionId.value !== subscription.id) { deleteSubscriptionId.value = subscription.id; return }
  busy.value = true
  try { await api(`/subscriptions/${subscription.id}`, {method: 'DELETE'}); subscriptions.value = subscriptions.value.filter(entry => entry.id !== subscription.id); deleteSubscriptionId.value = ''; notify('订阅已删除，已收藏作品不受影响。') }
  catch (error) { notify(errorMessage(error), true) }
  finally { busy.value = false }
}
async function jobAction(job: Job, action: 'retry' | 'cancel') {
  busy.value = true
  try { await post(`/jobs/${job.id}/${action}`); await pollJobs(); notify(action === 'retry' ? '任务已加入失败重试队列' : '已请求取消任务') }
  catch (error) { notify(errorMessage(error), true) }
  finally { busy.value = false }
}
let jobsPolling = false
async function pollJobs() {
  if (!authenticated.value || jobsPolling) return
  jobsPolling = true
  const requestedPath = jobsPath()
  try {
    const [jobResult, nextDashboard] = await Promise.all([api<{items: Job[]}>(jobsPath()), api<Dashboard>('/dashboard')])
    const next = jobResult.items
    if (requestedPath !== jobsPath()) return
    const changed = next.some(job => job.status === 'completed' && !jobs.value.some(old => old.id === job.id && old.status === 'completed'))
    const acquisitionKinds = ['import', 'import_content', 'source_sync', 'pixiv_bookmark', 'series_sync', 'author_profile', 'author_feed']
    const acquisitionChanged = next.some(job => acquisitionKinds.includes(job.kind) && !jobs.value.some(old => old.id === job.id && old.status === job.status && old.updated_at === job.updated_at))
    const activeAcquisition = [...next, ...jobs.value].some(job => acquisitionKinds.includes(job.kind) && ['queued', 'running'].includes(job.status))
    const aiExecutionChanged = next.some(job => job.kind === 'ai' && ['completed', 'failed', 'cancelled'].includes(job.status) && jobs.value.some(old => old.id === job.id && old.status !== job.status))
    jobs.value = next
    dashboard.value = nextDashboard
    if (view.value === 'tasks') pageError.value = ''
    if (changed) await loadCollections()
    if (view.value === 'library' && (changed || acquisitionChanged || activeAcquisition)) await loadLibrary(true)
    if (modal.value === 'detail' && (acquisitionChanged || activeAcquisition || !selectedItem.value || !contentReady(selectedItem.value))) await refreshSelectedContent()
    if (readerItem.value && next.some(job => job.kind === 'series_sync' && job.status === 'completed') && acquisitionChanged) {
      const id = readerItem.value.id
      const item = await api<Item>(`/items/${id}`)
      if (readerItem.value?.id === id) readerItem.value = {...item, progress: readerItem.value.progress}
    }
    if (view.value === 'tasks' && focusedJob.value) {
      const id = focusedJob.value.id
      try { const job = await api<Job>(`/jobs/${id}`); if (focusedJob.value?.id === id) { focusedJob.value = job; focusedJobError.value = '' } }
      catch (error) { focusedJobError.value = errorMessage(error) }
    }
    if (view.value === 'ai' && (changed || acquisitionChanged)) {
      const mergePreferences = preferenceTracker.snapshot()
      aiItems.value = (await api<{items: Item[]}>('/items?page_size=50&sort=latest')).items.map(mergePreferences)
      aiSelected.value = aiSelected.value.filter(id => aiItems.value.some(item => item.id === id && contentReady(item)))
    }
    if (aiExecutionChanged && view.value === 'ai') await refreshAIStatus()
  } catch (error) { if (view.value === 'tasks') pageError.value = errorMessage(error) }
  finally { jobsPolling = false; if (requestedPath !== jobsPath()) void pollJobs() }
}
async function analyze(ids: string[]) {
  const readyIds = ids.filter(id => [...aiItems.value, ...(selectedItem.value ? [selectedItem.value] : [])].some(item => item.id === id && contentReady(item)))
  if (!readyIds.length) { notify('请先选择正文已下载完成的作品。', true); return }
  busy.value = true
  try { const result = await post<{jobs: Job[]}>('/ai/analyze', {item_ids: readyIds}); notify(`已创建 ${result.jobs.length} 个 AI 整理任务`); aiSelected.value = []; await pollJobs() }
  catch (error) { notify(errorMessage(error), true) }
  finally { busy.value = false }
}
function toggleAIAll() { aiSelected.value = aiSelected.value.length === analyzableItems.value.length ? [] : analyzableItems.value.map(item => item.id) }
function jobProgress(job: Job) { return Math.min(100, Math.max(0, Number(job.progress) || 0)) }
function safeSourceURL(url?: string) { return url && /^https?:\/\//i.test(url) ? url : undefined }

watch(view, loadView)
watch(() => remoteForm.url, value => {
  if (selectedSource.value?.platform !== 'pixiv' || !/^https?:\/\//i.test(value.trim())) return
  try {
    const path = new URL(value.trim()).pathname
    if (/\/novel\/series\//.test(path)) { remoteForm.mode = 'series'; remoteForm.kind = 'novel' }
    else if (/\/user\/\d+\/series\//.test(path)) { remoteForm.mode = 'series'; remoteForm.kind = 'manga' }
    else if (/\/novel\/show\.php/.test(path)) { remoteForm.mode = 'work'; remoteForm.kind = 'novel' }
    else if (/\/artworks\//.test(path)) { remoteForm.mode = 'work'; remoteForm.kind = 'manga' }
  } catch { /* The backend validates incomplete or unsupported URLs. */ }
})
watch(query, value => { clearTimeout(searchTimer); searchTimer = setTimeout(() => searchQuery.value = value.trim(), 300) })
watch([searchQuery, typeFilter, sourceFilter, ratingFilter, tagFilter, sort], () => { if (page.value !== 1) page.value = 1; else if (view.value === 'library' && authenticated.value) loadLibrary() })
watch(page, () => { if (view.value === 'library' && authenticated.value) loadLibrary() })
watch(blurAdult, value => localStorage.setItem('qiye:blur-adult', String(value)))
onMounted(async () => {
  window.addEventListener('library:unauthorized', unauthorized)
  const [hash, collection] = window.location.hash.slice(1).split('/')
  if (hash && hash in titles) view.value = hash as View
  if (hash === 'library' && collection && Object.hasOwn(sourceLabels, collection)) sourceFilter.value = collection
  try { authenticated.value = (await api<{authenticated: boolean}>('/auth/status')).authenticated; if (authenticated.value) await initialize() }
  catch (error) { loginError.value = errorMessage(error) }
  finally { booting.value = false }
  pollingTimer = setInterval(() => { if (!document.hidden) void pollJobs() }, 6000)
})
onBeforeUnmount(() => { window.removeEventListener('library:unauthorized', unauthorized); clearInterval(pollingTimer); clearTimeout(searchTimer); clearTimeout(toastTimer) })
</script>

<template>
  <div v-if="booting" class="boot-screen"><div class="brand-symbol"><LibraryBig :size="31" :stroke-width="1.5" /></div><p>正在加载…</p><LoaderCircle class="spin muted" :size="22" /></div>
  <div v-else-if="!authenticated" class="login-layout">
    <div class="login-story"><div class="brand login-brand"><div class="brand-symbol"><LibraryBig :size="27" :stroke-width="1.5" /></div><div><strong>栖页</strong></div></div><div class="login-story-copy"><h1>私人图书馆</h1><div class="book-spines" aria-hidden="true"><span></span><span></span><span></span><span></span><span></span></div></div></div>
    <div class="login-panel"><form class="login-form" @submit.prevent="login"><h2>登录书库</h2><label class="field">书库密码<div class="input-with-icon"><LockKeyhole :size="18" /><input v-model="password" type="password" autocomplete="current-password" placeholder="输入管理员密码" required autofocus /></div></label><div v-if="loginError" class="alert error"><AlertCircle :size="18" /><span>{{ loginError }}</span></div><button class="button primary login-submit" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="18" /><span>{{ busy ? '正在连接' : '进入书库' }}</span><ArrowRight v-if="!busy" :size="18" /></button><div class="login-note"><Server :size="15" /><span>密码由部署时的管理员配置提供</span></div></form><div class="login-copyright">栖页 · 私人图书馆</div></div>
  </div>
  <div v-else class="app-shell">
    <div v-if="mobileNav" class="sidebar-scrim" @click="mobileNav = false"></div>
    <aside class="sidebar" :class="{'mobile-open': mobileNav}">
      <a href="#library" class="brand" @click.prevent="navigate('library')"><div class="brand-symbol"><LibraryBig :size="27" :stroke-width="1.5" /></div><div><strong>栖页<span class="brand-dot">.</span></strong></div></a>

      <nav class="main-nav" aria-label="主导航"><button v-for="entry in nav" :key="entry.id" :class="{active: view === entry.id}" @click="navigate(entry.id)"><component :is="entry.icon" :size="19" :stroke-width="1.7" /><span>{{ entry.label }}</span><span v-if="entry.id === 'tasks' && dashboard?.pending_jobs" class="nav-badge" title="所有等待或正在处理的任务">{{ dashboard.pending_jobs }}</span></button></nav>
      <div class="sidebar-bottom"><div class="storage-card"><div class="storage-heading"><HardDrive :size="16" /><span>存储</span><span class="status-dot" :class="{offline: !settings?.storage_available}"></span></div><strong>{{ bytes(dashboard?.total_bytes) }}</strong><span class="storage-caption">{{ dashboard?.total_items || 0 }} 部作品 · {{ settings?.storage_available ? '存储已连接' : '检查存储连接' }}</span><div class="storage-decoration"><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span><span></span></div></div><button class="sidebar-settings" :class="{active: view === 'settings'}" @click="navigate('settings')"><Settings2 :size="18" />书库设置</button><div class="profile-row"><span class="profile-avatar">私</span><div><strong>管理员</strong></div><button aria-label="退出登录" class="logout-button" @click="logout"><LogOut :size="17" /></button></div></div>
    </aside>
    <main class="main-content">
      <header class="topbar"><div class="breadcrumb"><button class="icon-button mobile-menu" aria-label="打开导航" @click="mobileNav = true"><Menu :size="22" /></button><span>私人图书馆</span><ChevronRight :size="14" /><strong>{{ selectedTitle.title }}</strong></div><div class="topbar-right"><span class="local-badge"><span class="status-dot" :class="{offline: !settings?.storage_available}"></span>{{ settings?.storage_available ? '本地存储已连接' : '存储待连接' }}</span><span class="topbar-divider"></span><button class="icon-button" aria-label="刷新当前页面" title="刷新" :disabled="loading" @click="loadView"><RefreshCw :size="17" :class="{spin: loading}" /></button><span class="user-circle">私</span></div></header>
      <div class="page-content">
        <section class="page-heading"><div><h1>{{ selectedTitle.title }}<span v-if="view === 'library'" class="heading-count">{{ currentLibraryTotal }}</span></h1></div><button v-if="view === 'library'" class="button primary" @click="importIntoCurrentCollection"><Plus :size="18" />{{ sourceFilter && sourceFilter !== 'local' ? `从 ${currentLibraryName} 导入` : '导入本地内容' }}</button><button v-else-if="view === 'subscriptions'" class="button primary" @click="openSubscription()"><Plus :size="18" />新建订阅</button><button v-else-if="view === 'ai'" class="button primary" :disabled="busy || !aiSelected.length" @click="analyze(aiSelected)"><Sparkles :size="17" />整理选中内容<span v-if="aiSelected.length">({{ aiSelected.length }})</span></button></section>
        <div v-if="pageError" class="alert error page-alert"><AlertCircle :size="20" /><span>{{ pageError }}</span><button class="text-button" @click="loadView">重试</button></div>

        <template v-if="view === 'library'">
          <CollectionSwitcher :collections="collections" :selected="sourceFilter" :total="dashboard?.total_items || 0" :error="collectionsError" @select="selectCollection" @retry="loadCollections" />
          <section class="stats-grid collection-scope-stats" :aria-label="`${currentLibraryName}概览`">
            <div class="stat-card"><div class="stat-icon green"><BookOpen :size="20" :stroke-width="1.7" /></div><div><span>{{ sourceFilter ? '本库收藏' : '全部收藏' }}</span><strong>{{ currentLibraryTotal.toLocaleString() }}<small>部</small></strong></div></div>
            <div class="stat-card"><div class="stat-icon amber"><Layers3 :size="20" :stroke-width="1.7" /></div><div><span>已保存页数</span><strong>{{ currentLibraryPages.toLocaleString() }}<small>页</small></strong></div></div>
            <div class="stat-card"><div class="stat-icon blue"><HardDrive :size="20" :stroke-width="1.7" /></div><div><span>内容大小</span><strong class="stat-size-value">{{ bytes(currentLibraryBytes) }}</strong></div></div>
            <div class="stat-card"><div class="stat-icon purple"><CheckCircle2 :size="20" :stroke-width="1.7" /></div><div><span>可离线阅读</span><strong>{{ currentLibrarySaved.toLocaleString() }}<small>部</small></strong></div></div>
          </section>
          <section class="library-section"><div class="collection-heading"><div class="section-title"><h2>{{ sourceFilter ? `${currentLibraryName}的收藏` : '全部收藏' }}</h2><span>{{ total }} 部作品</span></div><div class="collection-controls"><label class="sort-select"><span class="sr-only">排序方式</span><select v-model="sort"><option value="latest">最近添加</option><option value="oldest">最早添加</option><option value="title">名称排序</option></select><ChevronDown :size="14" /></label><button class="icon-button filter-toggle" :class="{selected: showFilters || activeFilters}" aria-label="筛选收藏" @click="showFilters = !showFilters"><SlidersHorizontal :size="18" /></button></div></div>
            <div class="library-toolbar"><div class="type-tabs"><button :class="{active: !typeFilter}" @click="typeFilter = ''">全部</button><button v-for="(label, key) in typeLabels" :key="key" :class="{active: typeFilter === key}" @click="typeFilter = key">{{ label }}</button></div><div class="search-field"><Search :size="17" /><input v-model="query" aria-label="搜索书库" placeholder="搜索作品、作者、标签…" /><button v-if="query" class="clear-search" aria-label="清除搜索" @click="query = ''"><X :size="14" /></button><span v-else class="search-shortcut">搜索</span></div></div>
            <div v-if="showFilters || tagFilter" class="filter-row"><label>内容分级<select v-model="ratingFilter"><option value="">全部分级</option><option v-for="(label, key) in ratingLabels" :key="key" :value="key">{{ label }}</option></select></label><label class="checkbox-label"><input v-model="blurAdult" type="checkbox" />模糊 R18 封面</label><button v-if="tagFilter" class="tag-filter" @click="tagFilter = ''"><TagIcon :size="13" />{{ tagFilter }}<X :size="13" /></button><button v-if="activeFilters" class="text-button" @click="clearFilters">重置筛选</button></div>
            <div v-if="loading && !items.length" class="book-grid skeleton-grid" aria-label="正在加载书库"><div v-for="n in 6" :key="n" class="skeleton-book"><div></div><span></span><i></i></div></div>
            <div v-else-if="items.length" class="book-grid" :class="{'is-loading': loading}"><BookCard v-for="item in items" :key="item.id" :item="item" :blur-adult="blurAdult" @open="openItem" /></div>
            <div v-else-if="!pageError" class="library-empty"><div class="empty-books"><div class="empty-book book-left"></div><div class="empty-book book-center"><BookOpen :size="32" :stroke-width="1.3" /><i></i></div><div class="empty-book book-right"></div><span class="empty-spark spark-one">✧</span><span class="empty-spark spark-two">✧</span></div><h3>{{ activeFilters ? '没有匹配的作品' : '书库暂无作品' }}</h3><p>{{ activeFilters ? '请调整关键词或筛选条件。' : (sourceFilter && sourceFilter !== 'local' ? `从 ${currentLibraryName} 导入作品到此书库。` : '导入本地文件，或切换平台书库导入作品。') }}</p><div class="empty-actions"><button v-if="activeFilters" class="button secondary" @click="clearFilters">清除筛选</button><template v-else><button class="button primary" @click="importIntoCurrentCollection"><Upload :size="17" />{{ sourceFilter && sourceFilter !== 'local' ? '导入平台作品' : '导入本地内容' }}</button><button class="button quiet" @click="navigate('sources')">连接内容来源<ArrowRight :size="16" /></button></template></div><div v-if="!activeFilters && (!sourceFilter || sourceFilter === 'local')" class="format-hints"><span>图片 / 视频</span><i></i><span>ZIP / CBZ</span><i></i><span>TXT / Markdown</span><i></i><span>本地与 NAS 文件夹</span></div></div>
            <div v-if="totalPages > 1" class="pagination"><span>共 {{ total }} 部 · 第 {{ page }} / {{ totalPages }} 页</span><div><button class="button secondary small" :disabled="page <= 1 || loading" @click="page--"><ChevronLeft :size="16" />上一页</button><button class="button secondary small" :disabled="page >= totalPages || loading" @click="page++">下一页<ChevronRight :size="16" /></button></div></div>
          </section>

        </template>

        <template v-else-if="view === 'favorites' || view === 'history'"><FavoritesPanel :key="view" :history="view === 'history'" :revision="favoriteRevision" :blur-adult="blurAdult" @open="openItem" /></template>
        <template v-else-if="view === 'authors'"><AuthorsPanel :seed="authorSeed" :blur-adult="blurAdult" @open="openResultItem" /></template>
        <template v-else-if="view === 'watchlist'"><WatchlistPanel @open="openResultItem" /></template>
        <template v-else-if="view === 'accounts'"><DownloadAccounts :accounts="sources.find(source => source.platform === 'pixiv')?.accounts || []" @login="openDownloadAccount" @change="pixivRoles = $event" @removed="reloadAccounts" /></template>
        <template v-else-if="view === 'characters'"><CharacterWall :blur-adult="blurAdult" @open="openResultItem" /></template>
        <template v-else-if="view === 'sources'">
          <div class="info-banner"><Plug :size="21" /><div><p>按平台能力导入作品或订阅作者。代理与平台 API 可在「书库设置」中配置。</p></div></div>
          <div class="source-grid"><article v-for="source in sources" :key="source.platform" class="source-card"><div class="source-card-header"><div class="source-logo" :class="source.platform">{{ ({pixiv: 'P', ehentai: 'E', picacg: '哔', jm: 'JM', kemono: 'K'} as Record<string, string>)[source.platform] || source.label[0] }}</div><div><h2>{{ source.label || sourceLabels[source.platform] }}</h2><span>{{ ({pixiv: '插画 · 漫画 · 小说', ehentai: '画廊 · 图集 · 标签', picacg: '漫画 · 章节 · 收藏', jm: '漫画 · 连载 · 收藏', kemono: '公开图文 · 作者订阅'} as Record<string, string>)[source.platform] }}</span></div><span class="status-pill" :class="{'success': source.accounts?.some(isAccountVerified), 'neutral': !source.accounts?.some(isAccountVerified)}">{{ sourceStatus(source) }}</span></div><p class="source-description">{{ source.message || '配置账号后，从作品链接或远端 ID 导入内容。' }}</p><div class="capability-list"><div v-for="capability in source.capabilities" :key="capability.key"><span>{{ capability.label }}</span><span :class="{'capability-ok': ['available', 'verified', 'supported', 'implemented'].includes(capability.status)}"><Check v-if="['available', 'verified', 'supported', 'implemented'].includes(capability.status)" :size="13" />{{ capabilityLabel(capability.status) }}</span></div></div><div class="source-accounts"><div v-for="account in source.accounts" :key="account.id" class="account-row"><span><span class="tiny-avatar">{{ account.label.slice(0, 1) }}</span>{{ account.label }}<small v-if="source.platform === 'pixiv' && pixivRoles?.enabled && [pixivRoles.main_account_id, ...(pixivRoles.content_account_ids || (pixivRoles.content_account_id ? [pixivRoles.content_account_id] : []))].includes(account.id)" class="account-auth-status">{{ account.id === pixivRoles.main_account_id ? '主账号 · 检索' : '副账号 · 图片下载' }}</small><small class="account-auth-status" :class="{verified: isAccountVerified(account)}" :title="accountVerificationHint(account)">{{ isAccountVerified(account) ? '已登录' : '凭据未验证' }}</small></span><div class="account-actions"><button v-if="deleteAccountId !== account.id" class="text-button" :disabled="busy || !canSync(source) || (source.platform === 'pixiv' && pixivRoles?.enabled && account.id !== pixivRoles.main_account_id)" @click="syncSource(source, account.id)"><RefreshCw :size="13" />{{ syncLabel(source) }}</button><button v-else class="text-button danger-text" :disabled="busy" @click="removeAccount(source, account.id)">确认移除</button><button class="icon-button danger-text" :aria-label="deleteAccountId === account.id ? '取消移除' : '移除账号（将暂停关联订阅）'" :disabled="busy" @click="deleteAccountId === account.id ? deleteAccountId = '' : removeAccount(source, account.id)"><X v-if="deleteAccountId === account.id" :size="13" /><Trash2 v-else :size="13" /></button></div></div><span v-if="!source.accounts?.length" class="muted">{{ source.platform === 'kemono' ? '公开作品无需登录，可导入作品或订阅作者。' : '登录后使用平台账号访问可用内容' }}</span></div><PixivCollectionSettings v-if="source.platform === 'pixiv'" /><PixivFollowDownloadSettings v-if="source.platform === 'pixiv'" /><PixivAccountRoles v-if="source.platform === 'pixiv'" :accounts="source.accounts" compact @manage="navigate('accounts')" @login="openAccount(source)" @change="pixivRoles = $event" /><div class="source-actions"><button v-if="source.platform === 'kemono'" class="button secondary small" @click="openSourceSubscription(source)"><Rss :size="15" />订阅作者</button><button v-else class="button secondary small" @click="openAccount(source)"><LockKeyhole :size="15" />{{ source.accounts?.length ? '登录其他账号' : '登录平台账号' }}</button><button class="button quiet small" @click="openPlatformAPIs"><Globe2 :size="14" />API 地址</button><button class="button quiet small" @click="openRemote(source)">导入作品<ArrowUpRight :size="15" /></button></div></article></div>
          <div v-if="!sources.length && !loading && !pageError" class="empty-state"><Plug :size="35" /><h3>暂无可用来源</h3><p>请确认后端平台适配器已加载。</p></div>
        </template>

        <template v-else-if="view === 'subscriptions'">
          <div class="info-banner"><Rss :size="21" /><div><p>按作者、标签或关键词检查更新。支持的关注列表可在「内容来源」中导入。</p></div></div>
          <div v-if="subscriptions.length" class="panel subscriptions-panel"><div class="panel-header"><h2>我的订阅 <span class="muted">{{ subscriptions.length }}</span></h2><span class="muted">删除订阅会保留已收藏内容</span></div><div v-for="subscription in subscriptions" :key="subscription.id" class="subscription-row"><div class="subscription-symbol"><component :is="subscription.target_type === 'author' ? Bookmark : subscription.target_type === 'tag' ? Tags : Search" :size="20" /></div><div class="subscription-info"><h3>{{ subscription.label || subscription.target }}<span class="status-pill" :class="subscription.enabled && subscription.runtime_status !== 'pending' ? 'success' : 'neutral'">{{ !subscription.enabled ? '已暂停' : subscription.runtime_status === 'pending' ? '能力待实现' : '已启用' }}</span></h3><p>{{ sourceLabels[subscription.platform] }}<template v-if="subscription.platform === 'pixiv'"><span>·</span>{{ contentScopeLabels[subscription.content_scope || 'images'] }}</template><span>·</span>{{ ({author: '作者', tag: '标签', search: '搜索'} as Record<string, string>)[subscription.target_type] || subscription.target_type }}：{{ subscription.target }}</p><p v-if="subscription.status_message" class="subscription-status-note">{{ subscription.status_message }}</p><p v-if="subscription.last_error" class="inline-error">{{ subscription.last_error }}</p></div><div class="subscription-meta"><span v-if="subscription.platform === 'pixiv' && subscription.target_type === 'author'">作品：{{subscription.download_works ? '下载' : '仅资料'}} · 收藏：{{subscription.download_bookmarks ? '下载' : '仅资料'}}</span><span v-else>{{ subscription.auto_download ? '发现后自动下载' : '仅发现内容' }}</span><small>上次检查：{{ date(subscription.last_checked_at) }}</small><small v-if="subscription.enabled && subscription.next_check_at">下次：{{ date(subscription.next_check_at) }}</small></div><div class="row-actions"><button class="icon-button" aria-label="编辑订阅" @click="openSubscription(subscription)"><Pencil :size="16" /></button><button class="icon-button danger-text" :aria-label="deleteSubscriptionId === subscription.id ? '确认删除订阅' : '删除订阅'" :disabled="busy" @click="removeSubscription(subscription)"><Check v-if="deleteSubscriptionId === subscription.id" :size="17" /><Trash2 v-else :size="17" /></button><button v-if="deleteSubscriptionId === subscription.id" class="icon-button" aria-label="取消删除" @click="deleteSubscriptionId = ''"><X :size="16" /></button></div></div></div>
          <div v-else-if="!loading && !pageError" class="empty-state large-empty"><div class="empty-icon"><Rss :size="32" :stroke-width="1.4" /></div><h3>暂无订阅</h3><p>添加作者、标签或关键词订阅。</p><button class="button primary" @click="openSubscription()"><Plus :size="17" />新建订阅</button></div>
        </template>

        <template v-else-if="view === 'tags'">
          <TagBrowser @browse="browseTag" />
        </template>

        <template v-else-if="view === 'tasks'">
          <DownloadQueue @changed="pollJobs" @lane="jobLane = $event" />
          <div class="task-tabs"><button :class="{active: !jobLane}" @click="jobLane = ''">全部队列</button><button v-for="(label, id) in jobLanes" :key="id" :class="{active: jobLane === id}" @click="jobLane = id">{{ label }}</button></div>
          <section v-if="focusedJob || focusedJobError" class="panel focused-download-job"><header><h2>当前作品下载任务</h2><button class="icon-button" aria-label="收起当前下载任务" @click="focusedJob = null; focusedJobError = ''"><X :size="18" /></button></header><div v-if="focusedJob" class="focused-job-state"><strong>{{ focusedJob.label || jobKindLabels[focusedJob.kind] || focusedJob.kind }}</strong><span class="status-pill" :class="focusedJob.status">{{ jobStatusLabels[focusedJob.status] || focusedJob.status }}</span><code>{{ focusedJob.id.slice(0, 8) }}</code><span v-if="['running', 'queued'].includes(focusedJob.status)">{{ jobProgress(focusedJob) }}%</span><button v-if="['failed', 'cancelled'].includes(focusedJob.status)" class="button secondary small" :disabled="busy" @click="jobAction(focusedJob, 'retry')"><RefreshCw :size="15" />重试</button><button v-if="['queued', 'running'].includes(focusedJob.status)" class="button quiet small" :disabled="busy" @click="jobAction(focusedJob, 'cancel')"><X :size="15" />取消</button><button v-if="focusedJob.item_id" class="text-button" @click="openResultItem(focusedJob.item_id)">查看作品<ArrowUpRight :size="14" /></button></div><p v-if="focusedJob?.error || focusedJobError" class="inline-error">{{ focusedJobError || focusedJob?.error }}</p><p v-if="focusedJob?.retry_last_error && focusedJob.retry_last_error !== focusedJob.error" class="field-hint">上次失败：{{ focusedJob.retry_last_error }}</p></section>
          <div class="task-summary"><div><span class="pulse-dot"></span><strong>最近 {{ jobs.length }} 条任务中，{{ runningCount }} 条等待或处理中</strong><span>每 6 秒自动更新</span></div><button class="text-button" @click="pollJobs"><RefreshCw :size="14" />立即刷新</button></div><div class="panel"><div class="task-tabs"><button v-for="entry in [{value: '', label: '最近任务'}, {value: 'running', label: '处理中'}, {value: 'queued', label: '排队中'}, {value: 'completed', label: '已完成'}, {value: 'failed', label: '失败'}, {value: 'cancelled', label: '已取消'}]" :key="entry.value" :class="{active: jobFilter === entry.value}" @click="jobFilter = entry.value">{{ entry.label }}<span v-if="entry.value === 'failed' && jobs.some(job => job.status === 'failed')">{{ jobs.filter(job => job.status === 'failed').length }}</span></button></div><div v-for="job in filteredJobs" :key="job.id" class="job-row"><div class="job-icon" :class="job.status"><Sparkles v-if="job.kind === 'ai'" :size="21" /><RefreshCw v-else-if="job.kind === 'source_sync'" :size="21" /><Image v-else-if="job.kind === 'import'" :size="21" /><Download v-else :size="21" /></div><div class="job-info"><h3>{{ job.label || jobKindLabels[job.kind] || job.kind }}<span class="status-pill" :class="job.status">{{ jobStatusLabels[job.status] || job.status }}</span></h3><div class="job-meta"><code>{{ job.id.slice(0, 8) }}</code><span>创建于 {{ date(job.created_at) }}</span><span v-if="job.queue">{{ jobLanes[job.queue] || job.queue }}</span><span v-if="job.queue === 'retry' && job.original_queue">原队列：{{ jobLanes[job.original_queue] || job.original_queue }}</span><button v-if="job.item_id" class="text-button" @click="openResultItem(job.item_id)">查看作品<ArrowUpRight :size="13" /></button></div><div v-if="['running', 'queued'].includes(job.status)" class="job-progress"><div><span :style="{width: `${jobProgress(job)}%`}"></span></div><small>{{ jobProgress(job) }}%</small></div><p v-if="job.error" class="inline-error">{{ job.error }}</p><p v-if="job.retry_last_error && job.retry_last_error !== job.error" class="field-hint">上次失败：{{ job.retry_last_error }}</p><JobResult v-if="job.result && Object.keys(job.result).length" :result="job.result" @open="openResultItem" @lookup="lookupJobCharacter" /></div><div class="row-actions"><button v-if="['failed', 'cancelled'].includes(job.status)" class="button secondary small" :disabled="busy" @click="jobAction(job, 'retry')"><RefreshCw :size="14" />重试</button><button v-if="['queued', 'running'].includes(job.status)" class="button quiet small" :disabled="busy" @click="jobAction(job, 'cancel')"><X :size="14" />取消</button><CheckCircle2 v-if="job.status === 'completed'" class="success-text" :size="20" /></div></div><div v-if="!filteredJobs.length && !loading && !pageError" class="empty-state"><div class="empty-icon"><ListTodo :size="30" :stroke-width="1.4" /></div><h3>{{ jobFilter ? '没有此类任务' : '暂无任务' }}</h3><p>导入、平台同步和 AI 整理的后台任务会出现在这里。</p></div></div>
        </template>

        <template v-else-if="view === 'ai'">
          <AINodes @change="refreshAIStatus" />
          <section class="ai-hero"><div class="ai-hero-symbol"><Sparkles :size="34" :stroke-width="1.3" /></div><div><h2>本地 AI 分类</h2><p>识别标签、角色及内容分级；人工标签不会被覆盖。</p></div><span class="ai-local-seal"><LockKeyhole :size="14" />本地推理</span></section>
          <div v-if="aiStatus?.message" class="alert" :class="aiStatus.available ? 'info' : 'warning'"><component :is="aiStatus.available ? CheckCircle2 : CircleHelp" :size="19" /><span>{{ aiStatus.message }}</span></div>
          <AIExecutionStatus v-if="aiStatus" :status="aiStatus" :refreshing="aiStatusRefreshing" :error="aiStatusError" @refresh="refreshAIStatus" />
          <div class="model-grid"><div v-for="model in aiStatus?.models || []" :key="model.id" class="model-card"><div class="model-heading"><div class="model-icon"><component :is="model.id.includes('ocr') ? FileText : model.id.includes('text') ? Tags : Image" :size="20" /></div><span class="status-pill" :class="model.available ? 'success' : 'neutral'">{{ model.available ? '已就绪' : '尚未就绪' }}</span></div><h3>{{ model.label }}</h3><p>{{ model.message || (model.available ? '本地模型已加载，可以创建整理任务。' : '请安装所需的本地模型文件。') }}</p></div></div>
          <CharacterLookup :items="aiItems" :seed="characterLookupSeed" @saved="characterTagSaved" />
          <div class="panel"><div class="panel-header"><div><h2>选择要整理的收藏</h2><p class="muted">最近 {{ aiItems.length }} 部作品 · 识别结果写入作品详情</p></div><button v-if="analyzableItems.length" class="text-button" @click="toggleAIAll"><CheckCheck :size="16" />{{ aiSelected.length === analyzableItems.length ? '取消全选' : '全选' }}</button></div><label v-for="item in aiItems" :key="item.id" class="ai-item-row"><input v-model="aiSelected" type="checkbox" :value="item.id" :disabled="!contentReady(item)" :aria-label="`选择 ${item.title}`" /><div class="mini-cover"><img v-if="item.cover_url" :src="item.cover_url" alt="" :class="{'blurred-thumbnail': blurAdult && item.rating === 'adult'}" /><BookOpen v-else :size="20" /></div><div><strong>{{ item.title }}</strong><span>{{ typeLabels[item.type] }} · {{ contentReady(item) ? `${contentCountLabel(item)} · 已离线` : contentStateLabel(item) }} · {{ item.author || '作者未填写' }}</span></div><span class="status-pill neutral">{{ ratingLabels[item.rating] }}</span><span class="ai-tag-count">{{ item.tags.length }} 个标签</span></label><div v-if="!aiItems.length && !loading" class="empty-state"><div class="empty-icon"><Sparkles :size="29" :stroke-width="1.4" /></div><h3>暂无可整理作品</h3><p>请先导入内容。</p><button class="button secondary" @click="openImport"><Upload :size="16" />导入内容</button></div></div>
        </template>

        <template v-else-if="view === 'settings'"><PlatformApiSettings /><ManagedProxySettings @change="managedProxyEnabled = $event" /><details class="external-proxy-advanced" @toggle="externalProxyOpen = ($event.target as HTMLDetailsElement).open"><summary><Settings2 :size="18" /><div><strong>高级：使用外部代理 / 外部 Clash</strong><span>已有独立代理服务时使用；内置代理开启时优先使用内置出口。</span></div><ChevronDown :size="17" /></summary><div class="external-proxy-content"><ProxySettings v-if="externalProxyOpen" :managed-enabled="managedProxyEnabled" /></div></details>
          <DevicePairing />
          <TranslationSettings />
          <AppUpdateSettings />
          <div class="settings-grid"><section class="panel settings-card"><div class="settings-title"><HardDrive :size="21" /><div><h2>文件与存储</h2></div><span class="status-pill" :class="settings?.storage_available ? 'success' : 'failed'">{{ settings?.storage_available ? '已连接' : '不可用' }}</span></div><dl class="setting-details"><div><dt>内容存储目录</dt><dd><code>{{ settings?.storage_root || '—' }}</code></dd></div><div><dt>文件夹导入根目录</dt><dd><code>{{ settings?.import_root || '未配置' }}</code></dd></div><div><dt>已保存内容</dt><dd>{{ bytes(dashboard?.total_bytes) }}<span class="muted">{{ dashboard?.total_items || 0 }} 部作品</span></dd></div></dl><div class="setting-note"><FolderOpen :size="16" /><p>NAS 目录需先挂载。文件夹导入仅限配置的根目录。</p></div></section><section class="panel settings-card"><div class="settings-title"><Database :size="21" /><div><h2>服务与数据库</h2></div></div><dl class="setting-details"><div><dt>数据库</dt><dd>{{ settings?.database_kind?.toUpperCase() || '—' }}</dd></div><div><dt>AI 运行方式</dt><dd>{{ settings?.ai_mode || '—' }}</dd></div></dl><div class="setting-note"><Server :size="16" /><p>连接参数与模型路径在服务端配置，修改后需重启服务。</p></div></section><DatabaseBackup :database-kind="settings?.database_kind" /><section class="panel settings-card"><div class="settings-title"><SlidersHorizontal :size="21" /><div><h2>阅读与展示</h2><p>这些偏好保存在当前浏览器</p></div></div><div class="preference-row"><div><strong>模糊 R18 封面</strong><span>书库缩略图隐藏细节，打开作品后仍可阅读</span></div><label class="toggle"><input v-model="blurAdult" type="checkbox" aria-label="模糊 R18 封面" /><span></span></label></div></section><section class="panel settings-card about-card"><h2>当前账号</h2><p>管理员</p><button class="button secondary small" @click="logout"><LogOut :size="15" />退出当前登录</button></section></div>
        </template>
        <div v-if="loading && view !== 'library'" class="inline-loading"><LoaderCircle :size="19" class="spin" />正在读取…</div>
      </div>
    </main>
  </div>

  <Modal v-if="modal === 'import'" title="导入本地内容" subtitle="保存到「本地书库」。" :busy="busy" @close="closeModal"><form @submit.prevent="importLocal"><div class="segmented-control"><button type="button" :class="{active: importMode === 'upload'}" @click="importMode = 'upload'"><Upload :size="16" />上传文件</button><button type="button" :class="{active: importMode === 'folder'}" @click="importMode = 'folder'"><FolderOpen :size="16" />服务器文件夹</button></div><div v-if="importMode === 'upload'"><input ref="fileInput" class="sr-only" type="file" accept="image/*,video/mp4,video/webm,video/quicktime,.mp4,.webm,.m4v,.mov,.cbz,.zip,.txt,.md" multiple @change="filesSelected" /><button type="button" class="upload-dropzone" :class="{dragging: dragActive}" @click="fileInput?.click()" @dragover.prevent="dragActive = true" @dragleave.prevent="dragActive = false" @drop.prevent="filesDropped"><div class="upload-symbol"><Upload :size="25" :stroke-width="1.5" /></div><strong>{{ uploadFiles.length ? `已选择 ${uploadFiles.length} 个文件` : '拖放文件到这里，或点击选择' }}</strong><span>{{ uploadFiles.length ? `${bytes(uploadSize)} · 点击重新选择` : '图片、MP4 / WebM / MOV 视频、ZIP / CBZ 漫画、TXT / Markdown 文章' }}</span></button><div v-if="uploadFiles.length" class="selected-files"><span v-for="(file, index) in uploadFiles.slice(0, 5)" :key="`${file.name}-${index}`"><FileText :size="13" />{{ file.name }}</span><span v-if="uploadFiles.length > 5" class="muted">另有 {{ uploadFiles.length - 5 }} 个文件</span></div><p class="field-hint">本次选择的文件将组成一部作品；多张图片按文件名排序。</p></div><div v-else><label class="field">文件夹路径<input v-model="importForm.path" placeholder="导入根目录下的文件夹路径" required /></label><p class="field-hint">指运行书库服务的设备上的目录。允许的根目录：<code>{{ settings?.import_root || '请先在服务端配置 LIBRARY_IMPORT_ROOT' }}</code></p></div><label class="field">作品名称 <span class="optional">选填</span><input v-model="importForm.title" placeholder="留空时根据文件名生成" maxlength="500" /></label><label class="field">内容类型<select v-model="importForm.type"><option v-for="(label, key) in typeLabels" :key="key" :value="key">{{ label }}</option></select></label><div v-if="modalError" class="alert error"><AlertCircle :size="18" /><span>{{ modalError }}</span></div><div v-if="busy" class="alert info"><LoaderCircle class="spin" :size="18" /><span>正在上传、校验和入库，请保持页面打开。</span></div><div class="modal-footer"><button type="button" class="button secondary" :disabled="busy" @click="closeModal">取消</button><button class="button primary" :disabled="busy || (importMode === 'upload' && !uploadFiles.length)"><LoaderCircle v-if="busy" class="spin" :size="17" /><Plus v-else :size="17" />{{ busy ? '正在导入' : '加入书库' }}</button></div></form></Modal>

  <Modal v-if="modal === 'detail' && selectedItem" :title="editMode ? '编辑作品信息' : '作品详情'" :wide="true" :busy="busy" @close="closeModal"><div v-if="busy && !selectedItem.assets" class="inline-loading"><LoaderCircle class="spin" :size="19" />正在读取作品…</div><div v-if="modalError" class="alert error"><AlertCircle :size="18" /><span>{{ modalError }}</span></div><form v-if="editMode" @submit.prevent="saveItem"><label class="field">作品名称<input v-model="edit.title" required maxlength="500" /></label><div class="form-grid"><label class="field">作者<input v-model="edit.author" placeholder="作者名称" /></label><label class="field">语言<input v-model="edit.language" placeholder="例如：中文 / 日本語" /></label></div><label class="field">内容分级<select v-model="edit.rating"><option v-for="(label, key) in ratingLabels" :key="key" :value="key">{{ label }}</option></select></label><label class="field">人工标签<textarea v-model="edit.tags" rows="2" placeholder="用逗号或换行分隔标签" /></label><p class="field-hint">仅修改人工标签；来源标签和 AI 标签会分别保留。</p><label class="field">简介<textarea v-model="edit.description" rows="4" placeholder="写一点关于这部作品的介绍…" /></label><div class="modal-footer"><button type="button" class="button secondary" :disabled="busy" @click="editMode = false">取消编辑</button><button class="button primary" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="17" /><Check v-else :size="17" />保存信息</button></div></form><template v-else><div class="detail-top"><div class="detail-cover"><img v-if="selectedItem.cover_url" :src="selectedItem.cover_url" :alt="selectedItem.title" /><BookOpen v-else :size="45" :stroke-width="1.2" /></div><div class="detail-main"><div class="detail-labels"><span class="status-pill neutral">{{ typeLabels[selectedItem.type] }}</span><span class="status-pill" :class="selectedItem.rating === 'adult' ? 'failed' : 'neutral'">{{ ratingLabels[selectedItem.rating] }}</span><span class="status-pill" :class="contentStateTone(selectedItem)">{{ contentStateLabel(selectedItem) }}</span></div><h2>{{ selectedItem.title_zh || selectedItem.title }}</h2><p v-if="selectedItem.title_zh" class="muted">原题：{{ selectedItem.title }}</p><p class="detail-author">{{ selectedItem.author || '尚未填写作者' }}</p><div class="detail-meta"><span>{{ contentCountLabel(selectedItem) }}</span><i></i><span>{{ bytes(selectedItem.size_bytes) }}</span><i></i><span>{{ selectedItem.language || '语言未标注' }}</span></div><p class="detail-source">{{ sourceLabels[selectedItem.source] || selectedItem.source }} · {{ date(selectedItem.created_at) }} 加入</p><p class="detail-source">原作者发布时间：{{ publicationDate(selectedItem.source_published_at) }}</p><div class="detail-actions"><button class="button primary" :disabled="!contentReady(selectedItem) || !selectedItem.assets?.length || busy" @click="startReading"><BookOpen :size="17" />{{ selectedItem.progress ? (['novel', 'article'].includes(selectedItem.type) ? `继续阅读 · 第 ${(selectedItem.progress.section || 0) + 1} 节` : `继续阅读 · 第 ${selectedItem.progress.page + 1} P`) : '开始阅读' }}</button><a v-if="contentReady(selectedItem)" class="button secondary" :href="`/api/items/${selectedItem.id}/export`"><Download :size="16" />离线打包</a></div></div></div><FavoriteControl :key="selectedItem.id" :item="selectedItem" @change="favoriteChanged" /><ContentDownloadState v-if="!contentReady(selectedItem)" :item="selectedItem" :job="selectedContentJob" :busy="busy" @download="downloadOriginal(selectedItem)" @tasks="showContentTask(selectedItem)" @import="openImport" /><SeriesPanel :key="selectedItem.id" :item="selectedItem" @open="openResultItem" @changed="refreshSelectedContent" @queued="seriesJobQueued" /><section class="detail-section"><div class="detail-section-title"><h3>内容标签</h3><button class="text-button" @click="fillEdit(); editMode = true"><Pencil :size="13" />编辑</button></div><div v-if="selectedItem.tags.length" class="detail-tags"><button v-for="tag in selectedItem.tags" :key="`${tag.id}-${tag.origin}`" class="detail-tag" :class="tag.origin" :title="`${tag.origin === 'ai' ? 'AI 建议' : tag.origin === 'source' ? '来源标签' : '人工标签'}${tag.score != null ? ` · ${Math.round(tag.score * 100)}%` : ''}`" @click="closeModal(); browseTag(tag.name)"><Sparkles v-if="tag.origin === 'ai'" :size="12" /><span v-else>#</span>{{ tag.name_zh || tag.name }}</button></div><p v-else class="muted">还没有标签。添加标签，或使用 AI 辅助整理。</p></section><section class="detail-section character-detail-section"><div class="detail-section-title"><h3>角色资料查证</h3><button class="text-button" @click="lookupCharacters(selectedItem.title, 'work', selectedItem)"><Globe2 :size="14" />按作品名查找</button></div><div v-if="selectedItem.tags.some(tag => /^角色[:：]/.test(tag.name))" class="character-detail-actions"><button v-for="tag in selectedItem.tags.filter(tag => /^角色[:：]/.test(tag.name))" :key="`${tag.id}-${tag.origin}`" class="button secondary small" @click="lookupCharacters(tag.name, 'character', selectedItem)">{{ tag.name.replace(/^角色[:：]/, '') }}<Globe2 :size="14" />联网查证</button></div><p class="field-hint">先确认搜索词再查询；联网结果需要人工确认后才能添加角色标签。</p></section><section v-if="selectedItem.description" class="detail-section"><h3>关于作品</h3><p class="detail-description">{{ selectedItem.description }}</p></section><section v-if="confirmDelete" class="alert error delete-confirm"><AlertCircle :size="20" /><div><strong>确认从书库移除这部作品？</strong><p>作品记录和阅读进度将被移除，存储中的原文件保留。</p><div><button class="button danger small" :disabled="busy" @click="removeItem">确认移除</button><button class="button quiet small" @click="confirmDelete = false">取消</button></div></div></section><div class="detail-bottom"><div><button v-if="selectedItem.source === 'pixiv'" class="button quiet small" @click="browseAuthor(selectedItem)">Pixiv 作者主页</button><button class="button quiet small" :disabled="busy" @click="fillEdit(); editMode = true"><Pencil :size="15" />编辑信息</button><button class="button quiet small" :disabled="busy || !contentReady(selectedItem)" @click="analyze([selectedItem.id])"><Sparkles :size="15" />AI 整理</button><a v-if="safeSourceURL(selectedItem.source_url)" class="button quiet small" :href="safeSourceURL(selectedItem.source_url)" target="_blank" rel="noopener noreferrer"><Globe2 :size="15" />原始页面</a></div><button class="icon-button danger-text" aria-label="移除作品" :disabled="busy" @click="confirmDelete = true"><Trash2 :size="17" /></button></div></template></Modal>

  <SourceLoginModal v-if="modal === 'account' && selectedSource" :source="selectedSource" @close="closeModal" @saved="accountSaved" />

  <Modal v-if="modal === 'remote' && selectedSource" :title="`从 ${selectedSource.label} 导入`" :subtitle="`输入作品链接或 ID，内容将保存到「${selectedSource.label}书库」。`" :busy="busy" @close="closeModal"><form @submit.prevent="importRemote"><div v-if="selectedSource.platform === 'pixiv'" class="form-grid"><label class="field">导入范围<select v-model="remoteForm.mode"><option value="work">单篇作品</option><option value="series">官方系列目录</option></select></label><label class="field">内容类型<select v-model="remoteForm.kind"><option value="manga">插画 / 漫画</option><option value="novel">小说</option></select></label></div><label class="field">{{ remoteForm.mode === 'series' && selectedSource.platform === 'pixiv' ? '系列链接 / ID' : '作品链接 / ID' }}<input v-model="remoteForm.url" :placeholder="selectedSource.platform === 'kemono' ? 'https://kemono.cr/service/user/id/post/id 或 service:user:post' : selectedSource.platform === 'pixiv' && remoteForm.kind === 'novel' ? 'Pixiv 小说链接或数字 ID' : 'https://… 或数字 ID'" required /></label><p v-if="selectedSource.platform === 'pixiv'" class="field-hint">链接自动识别；填写数字 ID 时，请选择正确的内容类型。</p><p v-if="selectedSource.platform === 'kemono'" class="field-hint">填写完整作品链接，或 service:user:post；service 是作品原来源平台，如 patreon、fanbox。</p><div v-if="selectedSource.platform === 'pixiv' && remoteForm.mode === 'series'" class="alert info"><Layers3 :size="18" /><span>获取官方目录及成员封面、资料；正文需要逐篇下载。</span></div><label v-if="selectedSource.platform !== 'kemono'" class="field">使用账号<select v-model="remoteForm.account_id" :disabled="selectedSource.platform === 'pixiv' && pixivRoles?.enabled"><option value="">默认 / 匿名访问（若平台允许）</option><option v-for="account in selectedSource.accounts" :key="account.id" :value="account.id">{{ account.label }}</option></select></label><p v-if="selectedSource.platform === 'pixiv' && pixivRoles?.enabled" class="field-hint">检索使用主账号；图片、作品详情／小说正文按账号管理中设置的功能分配。</p><div v-if="selectedSource.message" class="alert info"><CircleHelp :size="18" /><span>{{ selectedSource.message }}</span></div><div v-if="modalError" class="alert error"><AlertCircle :size="18" /><span>{{ modalError }}</span></div><div class="modal-footer"><button type="button" class="button secondary" :disabled="busy" @click="closeModal">取消</button><button class="button primary" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="17" /><Download v-else :size="17" />创建导入任务</button></div></form></Modal>

  <Modal v-if="modal === 'subscription'" :title="selectedSubscription ? '编辑订阅' : '新建订阅'" subtitle="按平台支持的能力检查更新。" :busy="busy" @close="closeModal"><form @submit.prevent="saveSubscription"><label class="field">订阅名称<input v-model="subscriptionForm.label" placeholder="为这条订阅起一个容易辨认的名字" required /></label><div class="form-grid"><label class="field">内容平台<select v-model="subscriptionForm.platform" :disabled="!!selectedSubscription" @change="changeSubscriptionPlatform"><option v-for="platform in remotePlatforms" :key="platform" :value="platform">{{ sourceLabels[platform] }}</option></select></label><label class="field">订阅类型<select v-model="subscriptionForm.target_type" :disabled="!!selectedSubscription" @change="subscriptionForm.content_scope = subscriptionForm.platform === 'pixiv' && subscriptionForm.target_type === 'author' ? 'all' : 'images'"><option value="author">作者</option><option value="tag">标签</option><option value="search">关键词搜索</option></select></label></div><label class="field">{{ subscriptionForm.target_type === 'author' ? '作者 ID' : subscriptionForm.target_type === 'tag' ? '标签名称' : '搜索关键词' }}<input v-model="subscriptionForm.target" :minlength="subscriptionForm.platform === 'kemono' && subscriptionForm.target_type === 'search' ? 3 : 1" :maxlength="subscriptionForm.platform === 'kemono' ? 200 : undefined" :placeholder="subscriptionForm.platform === 'kemono' && subscriptionForm.target_type === 'author' ? 'Kemono 作者链接或 service:user' : subscriptionForm.platform === 'kemono' && subscriptionForm.target_type === 'search' ? '至少 3 个字符' : subscriptionForm.target_type === 'author' ? '填写平台中的作者 ID' : '填写要关注的内容'" required /></label><div class="form-grid"><label v-if="subscriptionForm.platform !== 'kemono'" class="field">使用账号<select v-model="subscriptionForm.account_id"><option value="">使用平台默认账号</option><option v-for="account in subscriptionAccounts" :key="account.id" :value="account.id">{{ account.label }}</option></select></label><label class="field">检查间隔（分钟）<input v-model.number="subscriptionForm.interval_minutes" type="number" min="30" max="10080" required /></label></div><label v-if="subscriptionForm.platform === 'pixiv'" class="field">订阅内容<select v-model="subscriptionForm.content_scope"><option v-for="(label, scope) in contentScopeLabels" :key="scope" :value="scope">{{ label }}</option></select></label><div class="subscription-options"><label class="checkbox-label"><input v-model="subscriptionForm.enabled" type="checkbox" />启用这条订阅</label><template v-if="subscriptionForm.platform === 'pixiv' && subscriptionForm.target_type === 'author'"><label class="checkbox-label"><input v-model="subscriptionForm.download_works" type="checkbox" />下载作者发布的作品</label><label class="checkbox-label"><input v-model="subscriptionForm.download_bookmarks" type="checkbox" />下载作者公开收藏</label></template><label v-else class="checkbox-label"><input v-model="subscriptionForm.auto_download" type="checkbox" />发现更新后自动下载</label></div><div class="alert info"><CircleHelp :size="18" /><span>需要登录的平台，请先在「内容来源」中添加账号。尚不支持的订阅类型会显示检查原因。</span></div><div v-if="modalError" class="alert error"><AlertCircle :size="18" /><span>{{ modalError }}</span></div><div class="modal-footer"><button type="button" class="button secondary" :disabled="busy" @click="closeModal">取消</button><button class="button primary" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="17" /><Rss v-else :size="17" />{{ selectedSubscription ? '保存订阅' : '创建订阅' }}</button></div></form></Modal>

  <Reader v-if="readerItem" :key="readerItem.id" :item="readerItem" @close="readerItem = null" @progress="updateProgress" @open="openReaderSeriesItem" @queued="seriesJobQueued" />
  <Transition name="toast"><div v-if="toast" class="toast-message" :class="{error: toast.error}" role="status"><AlertCircle v-if="toast.error" :size="19" /><CheckCircle2 v-else :size="19" /><span>{{ toast.message }}</span><button class="icon-button" aria-label="关闭提示" @click="toast = null"><X :size="16" /></button></div></Transition>
</template>
