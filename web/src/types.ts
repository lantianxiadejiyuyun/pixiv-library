export type ItemType = 'manga' | 'illustration' | 'novel' | 'article'
export type Rating = 'unknown' | 'general' | 'sensitive' | 'adult'
export type ContentState = 'metadata' | 'queued' | 'downloading' | 'ready' | 'failed'
export type ContentScope = 'images' | 'novels' | 'all'
export interface ReadingProgress {page: number; section?: number; offset?: number}
export interface TextSection {index: number; title: string; text: string}
export interface SeriesSummary {id: string; title: string; origin: 'source' | 'manual'; source: string; source_id: string | null; kind: 'novel' | 'manga'; complete: boolean; revision: number}
export interface SeriesEntry {item_id: string; title: string; position: number; label: string; content_state: ContentState; page_count: number}
export interface SeriesDetail extends SeriesSummary {entries: SeriesEntry[]}
export interface Tag { id: string; name: string; name_zh?: string | null; display_name?: string; origin?: string; score?: number; count?: number }
export interface Asset { id: string; position: number; original_name: string; mime_type: string; size_bytes: number; url: string }
export interface Item {
  id: string; title: string; type: ItemType; source: string; source_url?: string; description?: string;
  title_zh?: string | null; display_title?: string;
  author_source_id?: string | null;
  is_favorite?: boolean; favorite_group_ids?: string[]; user_score?: number; source_bookmark_count?: number | null;
  author?: string; language?: string; rating: Rating; tags: Tag[]; page_count: number; size_bytes: number;
  cover_url?: string | null; created_at: string; updated_at: string;
  content_state: ContentState; content_error?: string | null; content_job_id?: string | null;
  progress?: ReadingProgress | null; assets?: Asset[];
  remote_page_count?: number | null; source_series?: {remote_id: string; title: string; kind: 'novel' | 'manga'} | null; series?: SeriesSummary[];
}
export interface Dashboard {total_items: number; total_pages: number; total_bytes: number; pending_jobs: number; untagged_items: number; recent_items: Item[]; source_counts: Record<string, number>; rating_counts: Record<string, number>}
export interface Job {queue?: string; id: string; kind: string; label?: string | null; status: string; progress: number; error?: string; item_id?: string | null; created_at: string; updated_at: string; result?: Record<string, unknown>}
export interface SourceAccount {id: string; label: string; auth_status?: string; verified_at?: string | null}
export interface Source {platform: string; label: string; status: string; capabilities: {key: string; label: string; status: string}[]; accounts: SourceAccount[]; message?: string}
export interface Subscription {id: string; platform: string; target_type: string; target: string; label: string; enabled: boolean; auto_download: boolean; content_scope?: ContentScope; last_checked_at?: string; last_error?: string; account_id?: string; interval_minutes?: number; next_check_at?: string; runtime_status?: string; status_message?: string}
export interface AIStatus {
  available: boolean; models: {id: string; label: string; available: boolean; message?: string}[]; message: string;
  requested_provider?: 'auto' | 'cpu' | 'cuda' | 'intel'; actual_provider?: string | null;
  device?: string | null; fallback_reason?: string | null;
  execution_state?: 'not_loaded' | 'ready' | 'fallback' | 'error';
}
export interface CharacterCandidate {
  id: string; name: string; native_name: string | null; aliases: string[];
  works: {title: string; type: 'manga' | 'anime'; source_url: string}[];
  description: string; source_url: string; provider: string;
}
export interface CharacterSearchResult {provider: string; query: string; mode: 'character' | 'work'; candidates: CharacterCandidate[]; notice: string}
export interface CharacterLookupSeed {key: number; query: string; mode: 'character' | 'work'; item?: Item}
export interface WallCharacter {id: string; tag_name: string; name: string; display_name: string; count: number; covers: {id: string; title: string; cover_url: string | null; rating: Rating}[]; profile: (CharacterCandidate & {updated_at: string}) | null}
export interface Settings {storage_root: string; import_root: string; database_kind: string; storage_available: boolean; ai_mode: string}
export interface Collection {source: string; name: string; storage_directory: string; item_count: number; page_count: number; size_bytes: number; saved_item_count: number; metadata_item_count: number; legacy_asset_count: number}

export const typeLabels: Record<string, string> = {manga: '漫画', illustration: '画集', novel: '小说', article: '文章'}
export const sourceLabels: Record<string, string> = {local: '本地导入', pixiv: 'Pixiv', ehentai: 'E-Hentai', picacg: '哔咔 PicACG', jm: 'JM 漫画'}
export const ratingLabels: Record<string, string> = {unknown: '待分级', general: '普通', sensitive: '敏感', adult: 'R18'}
export const jobStatusLabels: Record<string, string> = {queued: '排队中', running: '处理中', completed: '已完成', failed: '失败', cancelled: '已取消'}
export const jobKindLabels: Record<string, string> = {import: '封面与基本信息', import_content: '正文下载', source_sync: '同步关注内容', series_sync: '获取系列目录', ai: 'AI 整理', translate: '标题与标签汉化', author_profile: '作者主页资料', author_feed: '作者作品／收藏目录'}
export const contentScopeLabels: Record<ContentScope, string> = {images: '插画 / 漫画', novels: '小说', all: '插画、漫画与小说'}
export function contentCountLabel(item: Item) {
  if (item.type === 'novel' || item.type === 'article') return item.page_count ? `${item.page_count} 份正文` : '正文未下载'
  const unit = item.source === 'pixiv' ? 'P' : '页'
  if (contentReady(item)) return `${item.page_count} ${unit}`
  return item.remote_page_count ? `共 ${item.remote_page_count} ${unit} · 未完整下载` : item.page_count ? `已存 ${item.page_count} ${unit} · 未完成` : '正文未下载'
}
export function contentReady(item: Item) { return item.content_state === 'ready' && item.page_count > 0 && (item.assets === undefined || item.assets.length > 0) }
export function contentStateLabel(item: Item) {
  return ({metadata: item.cover_url ? '仅封面与资料' : '仅基本资料', queued: '等待下载', downloading: '正文下载中', ready: contentReady(item) ? '已离线' : '正文未就绪', failed: '下载失败'} as Record<string, string>)[item.content_state] || '状态待确认'
}
export function contentStateTone(item: Item) { return contentReady(item) ? 'success' : item.content_state === 'failed' ? 'failed' : item.content_state === 'downloading' ? 'running' : item.content_state === 'queued' ? 'queued' : 'neutral' }
export function bytes(value: number = 0) {
  if (!value) return '0 B'
  const i = Math.min(Math.floor(Math.log(value) / Math.log(1024)), 4)
  return `${(value / 1024 ** i).toFixed(i === 0 ? 0 : 1)} ${['B', 'KB', 'MB', 'GB', 'TB'][i]}`
}
export function date(value?: string) {
  if (!value) return '尚未同步'
  return new Intl.DateTimeFormat('zh-CN', {month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}).format(new Date(value))
}
