import type { ReadingProgress, TextSection } from './types'

export function boundedIndex(value: number | undefined, length: number) {
  return Math.max(0, Math.min(Number.isFinite(value) ? Math.trunc(value!) : 0, Math.max(0, length - 1)))
}
export function boundedOffset(value: number | undefined) { return Math.max(0, Math.min(Number.isFinite(value) ? value! : 0, 1)) }
export function restoreTextPosition(progress: ReadingProgress | null | undefined, sections: TextSection[]) {
  return {section: boundedIndex(progress?.section, sections.length), offset: boundedOffset(progress?.offset)}
}
export function textReadingPercent(section: number, count: number, offset: number) {
  return count ? Math.round((boundedIndex(section, count) + boundedOffset(offset)) / count * 100) : 0
}
export function pixivImportInput(value: string, kind: 'manga' | 'novel') {
  const input = value.trim()
  return /^\d+$/.test(input) && kind === 'novel' ? `novel:${input}` : input
}
