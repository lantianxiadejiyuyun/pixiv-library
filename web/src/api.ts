export class ApiError extends Error { constructor(message: string, public status: number) { super(message) } }

// Coalesce simultaneous ordinary JSON reads only. Completed responses are never
// cached, and writes/auth changes fence requests from the previous state.
const pendingReads = new Map<string, Promise<unknown>>()

export async function apiResponse(path: string, options: RequestInit = {}): Promise<Response> {
  if (!['GET', 'HEAD'].includes((options.method || 'GET').toUpperCase())) pendingReads.clear()
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  let response: Response
  try { response = await fetch(`/api${path}`, { ...options, headers, credentials: 'same-origin' }) }
  catch { throw new ApiError('暂时无法连接书库服务，请检查服务是否正在运行。', 0) }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    const detail = typeof payload.detail === 'string' ? payload.detail : Array.isArray(payload.detail)
      ? payload.detail.map((row: {msg?: string}) => row.msg || '输入有误').join('；') : `请求失败（${response.status}）`
    if (response.status === 401) { pendingReads.clear(); window.dispatchEvent(new Event('library:unauthorized')) }
    throw new ApiError(detail, response.status)
  }
  return response
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const read = async () => {
    const response = await apiResponse(path, options)
    return response.status === 204 ? undefined : response.json()
  }
  // Custom signals, headers and fetch options keep independent ownership.
  if (Object.keys(options).length) return read() as Promise<T>
  let pending = pendingReads.get(path)
  if (!pending) {
    pending = read()
    pendingReads.set(path, pending)
  }
  try {
    const value = await pending
    // Responses are JSON values; this also works on LAN HTTP browsers where
    // secure-context-only browser APIs may be unavailable.
    return (value === undefined ? undefined : JSON.parse(JSON.stringify(value))) as T
  }
  finally { if (pendingReads.get(path) === pending) pendingReads.delete(path) }
}
export const post = <T>(path: string, body: unknown = {}) => api<T>(path, {method: 'POST', body: JSON.stringify(body)})
export function errorMessage(error: unknown) { return error instanceof Error ? error.message : '操作未完成，请重试。' }
