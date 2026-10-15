export interface PairingCode {id: string; qr_payload: string; expires_at: string; server_url: string}
export interface BoundDevice {id: string; device_name: string; created_at: string; last_used_at: string | null; expires_at: string; revoked_at: string | null; status: 'active' | 'expired' | 'revoked'}

export function isLoopbackAddress(value: string) {
  try {
    const host = new URL(value).hostname.toLowerCase().replace(/^\[|\]$/g, '')
    return host === 'localhost' || host.endsWith('.localhost') || host === '::1' || /^127\./.test(host)
  } catch { return false }
}
function privateHost(hostname: string) {
  const host = hostname.toLowerCase().replace(/^\[|\]$/g, '')
  const ipv4 = /^(\d+)\.(\d+)\.(\d+)\.(\d+)$/.exec(host)
  if (ipv4) { const [a, b] = ipv4.slice(1).map(Number); return a === 10 || a === 127 || (a === 172 && b! >= 16 && b! <= 31) || (a === 192 && b === 168) }
  if (host.includes(':')) return host === '::1' || /^f[cd][0-9a-f]{2}:/.test(host) || /^fe[89ab][0-9a-f]:/.test(host)
  return !host.includes('.') || host.endsWith('.local') || host.endsWith('.lan') || host.endsWith('.home.arpa')
}
export function serverAddressError(value: string) {
  if (!value.trim()) return '请填写手机可访问的书库地址。'
  try {
    const url = new URL(value.trim())
    if (!['http:', 'https:'].includes(url.protocol)) return '书库地址需以 http:// 或 https:// 开头。'
    if (!url.hostname || url.username || url.password || url.search || url.hash || !['', '/'].includes(url.pathname)) return '请填写服务器根地址，不含账号、查询参数或页面路径。'
    if (url.hostname === '0.0.0.0' || url.hostname === '[::]') return '请使用设备的实际 IP 地址，不能使用监听地址。'
    if (isLoopbackAddress(url.origin)) return '手机无法访问此本机地址，请填写电脑或 NAS 的局域网地址。'
    if (url.protocol === 'http:' && !privateHost(url.hostname)) return '公网地址必须使用 HTTPS；HTTP 仅用于局域网地址。'
    return ''
  } catch { return '地址格式不正确，例如 http://192.168.1.10:8000。' }
}
export function secondsRemaining(expiresAt: string, now = Date.now()) {
  const expires = Date.parse(expiresAt)
  return Number.isFinite(expires) ? Math.max(0, Math.ceil((expires - now) / 1000)) : 0
}
export function validPairingCode(value: PairingCode) {
  if (!value.id || !secondsRemaining(value.expires_at)) return false
  try {
    const payload = JSON.parse(value.qr_payload)
    return payload.type === 'qiye-library' && payload.version === 1 && typeof payload.pairing_token === 'string' && payload.pairing_token.length > 0 && payload.server_url === value.server_url
  } catch { return false }
}
