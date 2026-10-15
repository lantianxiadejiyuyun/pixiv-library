import { ApiError, apiResponse } from './api'

export function databaseExportFilename(disposition: string | null, now = new Date()) {
  const extended = disposition?.match(/filename\*\s*=\s*UTF-8''([^;]+)/i)?.[1]
  const regular = disposition?.match(/filename\s*=\s*(?:"([^"]+)"|([^;\s]+))/i)
  let filename = regular?.[1] || regular?.[2] || ''
  if (extended) { try { filename = decodeURIComponent(extended.trim()) } catch { /* Use the plain filename when encoding is invalid. */ } }
  if (/^qiye-database-\d{8}-\d{6}\.sql$/.test(filename)) return filename
  const timestamp = now.toISOString().replace(/[-:]/g, '').replace('T', '-').slice(0, 15)
  return `qiye-database-${timestamp}.sql`
}

export async function requestDatabaseExport(signal?: AbortSignal) {
  const response = await apiResponse('/database/export', {method: 'POST', cache: 'no-store', signal})
  if (response.headers.get('Content-Type')?.split(';')[0]?.trim().toLowerCase() !== 'application/sql') {
    throw new ApiError('服务未返回 SQL 备份文件，请重试。', response.status)
  }
  let file: Blob
  try { file = await response.blob() }
  catch { throw new ApiError('导出文件接收未完成，请重试。', 0) }
  if (!file.size) throw new ApiError('导出文件为空，请重试。', response.status)
  return {file, filename: databaseExportFilename(response.headers.get('Content-Disposition'))}
}
