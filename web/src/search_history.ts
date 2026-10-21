import type { Tag } from './types'

export type TagMode = 'all' | 'any'
export interface SearchEntry { query: string; tags: Tag[]; mode: TagMode }
const key = 'qiye:search-history:v1'

export function searchKey(entry: SearchEntry): string {
  return JSON.stringify([entry.query.trim(), entry.tags.map(tag => tag.id).sort(), entry.mode])
}
export function searchLabel(entry: SearchEntry): string {
  return [entry.query, ...entry.tags.map(tag => `#${tag.name_zh || tag.name}`)].filter(Boolean).join(' · ')
}
export function readSearchHistory(): SearchEntry[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) || '[]')
    if (!Array.isArray(value)) return []
    return value.filter((row): row is SearchEntry => row && typeof row.query === 'string' && row.query.length <= 512 &&
      ['all', 'any'].includes(row.mode) && Array.isArray(row.tags) && row.tags.length <= 20 &&
      row.tags.every((tag: Tag) => tag && typeof tag.id === 'string' && tag.id.length <= 36 && typeof tag.name === 'string' &&
        (tag.name_zh == null || typeof tag.name_zh === 'string')) && (row.query.trim() || row.tags.length)).slice(0, 20)
  } catch { return [] }
}
export function writeSearchHistory(entries: SearchEntry[]): void {
  try { localStorage.setItem(key, JSON.stringify(entries.slice(0, 20))) } catch { /* Search works when browser storage is unavailable. */ }
}
export function rememberSearch(entries: SearchEntry[], entry: SearchEntry): SearchEntry[] {
  const clean = { query: entry.query.trim().slice(0, 512), tags: entry.tags.slice(0, 20).map(tag => ({ id: tag.id, name: tag.name, name_zh: tag.name_zh })), mode: entry.mode }
  if (!clean.query && !clean.tags.length) return entries
  const result = [clean, ...entries.filter(old => searchKey(old) !== searchKey(clean))].slice(0, 20)
  writeSearchHistory(result)
  return result
}
