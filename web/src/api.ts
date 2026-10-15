export class ApiError extends Error { constructor(message: string, public status: number) { super(message) } }

export async function apiResponse(path: string, options: RequestInit = {}): Promise<Response> {
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  let response: Response
  try { response = await fetch(`/api${path}`, { ...options, headers, credentials: 'same-origin' }) }
  catch { throw new ApiError('暂时无法连接书库服务，请检查服务是否正在运行。', 0) }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    const detail = typeof payload.detail === 'string' ? payload.detail : Array.isArray(payload.detail)
      ? payload.detail.map((row: {msg?: string}) => row.msg || '输入有误').join('；') : `请求失败（${response.status}）`
    if (response.status === 401) window.dispatchEvent(new Event('library:unauthorized'))
    throw new ApiError(detail, response.status)
  }
  return response
}
export async function api<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await apiResponse(path, options)
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}
export const post = <T>(path: string, body: unknown = {}) => api<T>(path, {method: 'POST', body: JSON.stringify(body)})
export function errorMessage(error: unknown) { return error instanceof Error ? error.message : '操作未完成，请重试。' }
