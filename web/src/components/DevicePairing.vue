<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'
import QRCode from 'qrcode'
import { AlertCircle, Check, Copy, Eye, EyeOff, LoaderCircle, QrCode, RefreshCw, Smartphone, Unplug, X } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { BoundDevice, PairingCode } from '../devicePairing'
import { isLoopbackAddress, secondsRemaining, serverAddressError, validPairingCode } from '../devicePairing'

const serverURL = ref(window.location.origin)
const pairing = ref<PairingCode | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const qrReady = ref(false)
const generating = ref(false)
const pairingError = ref('')
const pairingNotice = ref('')
const revealCode = ref(false)
const copyMessage = ref('')
const copying = ref(false)
const devices = ref<BoundDevice[]>([])
const devicesLoading = ref(true)
const devicesError = ref('')
const deviceActionError = ref('')
const revoking = ref('')
const now = ref(Date.now())
const addressError = computed(() => serverAddressError(serverURL.value))
const loopback = computed(() => isLoopbackAddress(serverURL.value))
const remaining = computed(() => pairing.value ? secondsRemaining(pairing.value.expires_at, now.value) : 0)
const countdown = computed(() => `${Math.floor(remaining.value / 60)}:${String(remaining.value % 60).padStart(2, '0')}`)
let generation = 0
let listRequest = 0
let disposed = false
let listing = false
let pairingController: AbortController | undefined
let clockTimer: ReturnType<typeof setInterval> | undefined
let deviceTimer: ReturnType<typeof setInterval> | undefined

function clearPairing(message = '') {
  generation++; pairingController?.abort()
  const drawing = canvas.value
  drawing?.getContext('2d')?.clearRect(0, 0, drawing.width, drawing.height)
  pairing.value = null; qrReady.value = false; revealCode.value = false; copyMessage.value = ''; copying.value = false; generating.value = false
  pairingNotice.value = message
}
function addressChanged() { pairingError.value = ''; if (pairing.value || generating.value) clearPairing('地址已更改，请重新生成绑定码。') }
function updateClock() {
  now.value = Date.now()
  if (pairing.value && !remaining.value) clearPairing('绑定码已过期，请重新生成。')
}
async function generatePairing() {
  if (generating.value) return
  if (addressError.value) { pairingError.value = addressError.value; return }
  clearPairing(); pairingError.value = ''; generating.value = true
  const id = ++generation; const controller = new AbortController(); pairingController = controller
  try {
    const result = await api<PairingCode>('/devices/pairing', {method: 'POST', body: JSON.stringify({server_url: new URL(serverURL.value.trim()).origin}), signal: controller.signal})
    if (disposed || id !== generation) return
    if (!validPairingCode(result)) throw new Error('服务返回的绑定码无效或已过期，请重新生成。')
    pairing.value = result; now.value = Date.now()
    await nextTick()
    if (disposed || id !== generation || !canvas.value) return
    await QRCode.toCanvas(canvas.value, result.qr_payload, {width: 280, margin: 4, errorCorrectionLevel: 'M', color: {dark: '#202633', light: '#ffffff'}})
    if (!disposed && id === generation) qrReady.value = true
  } catch (failure) { if (!disposed && id === generation) { clearPairing(); pairingError.value = errorMessage(failure) } }
  finally { if (id === generation) generating.value = false }
}
async function copyCode() {
  if (!pairing.value || !secondsRemaining(pairing.value.expires_at) || copying.value) return
  const id = pairing.value.id; const payload = pairing.value.qr_payload
  copying.value = true; copyMessage.value = ''
  try {
    if (!navigator.clipboard?.writeText) throw new Error('manual-copy')
    await navigator.clipboard.writeText(payload)
    if (!disposed && pairing.value?.id === id) copyMessage.value = '绑定码已复制，可粘贴到 App。'
  } catch {
    if (!disposed && pairing.value?.id === id) { revealCode.value = true; copyMessage.value = '当前浏览器不能自动复制，请在下方手动复制绑定码。' }
  } finally { copying.value = false }
}
async function loadDevices(silent = false) {
  if (listing || disposed || revoking.value) return
  const id = ++listRequest; listing = true
  if (!silent) devicesLoading.value = true
  try {
    const result = await api<{items: BoundDevice[]}>('/devices')
    if (!disposed && id === listRequest) { devices.value = result.items; devicesError.value = '' }
  } catch (failure) { if (!disposed && id === listRequest) devicesError.value = errorMessage(failure) }
  finally { listing = false; if (id === listRequest) devicesLoading.value = false }
}
async function revokeDevice(device: BoundDevice) {
  if (revoking.value || devicesLoading.value || device.status === 'revoked') return
  revoking.value = device.id; deviceActionError.value = ''; listRequest++
  let changed = false
  try {
    await api(`/devices/${device.id}`, {method: 'DELETE'})
    changed = true
    if (!disposed) devices.value = devices.value.map(entry => entry.id === device.id ? {...entry, status: 'revoked', revoked_at: new Date().toISOString()} : entry)
  } catch (failure) { if (!disposed) deviceActionError.value = errorMessage(failure) }
  finally { revoking.value = ''; if (!disposed && changed) loadDevices(true) }
}
function deviceDate(value: string | null) {
  if (!value) return '尚未使用'
  const date = new Date(value)
  return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat('zh-CN', {year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'}).format(date) : '时间未知'
}
function pageHidden() { clearPairing() }
onMounted(() => { loadDevices(); clockTimer = setInterval(updateClock, 1000); deviceTimer = setInterval(() => loadDevices(true), 10000); window.addEventListener('pagehide', pageHidden) })
onBeforeUnmount(() => { disposed = true; listRequest++; clearPairing(); clearInterval(clockTimer); clearInterval(deviceTimer); window.removeEventListener('pagehide', pageHidden) })
</script>

<template>
  <section class="panel settings-card device-pairing-card">
    <div class="settings-title"><Smartphone :size="22" /><div><h2>手机 App</h2></div></div>
    <p class="device-pairing-permission">扫码设备可查看书库、保存阅读进度，并请求服务器下载正文。</p>
    <form class="device-pairing-form" @submit.prevent="generatePairing"><label class="field">服务器地址<input v-model="serverURL" type="url" required autocomplete="off" spellcheck="false" placeholder="http://192.168.1.10:8000" @input="addressChanged" /></label><button class="button secondary" :disabled="generating || !!addressError"><LoaderCircle v-if="generating" class="spin" :size="17" /><QrCode v-else :size="17" />{{ generating ? '正在生成…' : pairing ? '重新生成绑定码' : '生成绑定二维码' }}</button></form>
    <p v-if="addressError" class="device-address-note" :class="{'device-loopback-note': loopback}"><AlertCircle :size="16" /><span>{{ addressError }}</span></p><p v-else class="device-address-note">手机需能访问此地址；绑定码有效期 5 分钟，仅可使用一次。</p>
    <div v-if="pairingError" class="alert error" role="alert"><AlertCircle :size="18" /><span>{{ pairingError }}</span></div>
    <div v-if="pairing && remaining" class="device-pairing-code"><header><strong>用栖页 App 扫描</strong><span class="status-pill queued">{{ countdown }} 后过期</span><button class="icon-button" aria-label="关闭并清除绑定码" @click="clearPairing()"><X :size="19" /></button></header><div class="device-code-content"><div class="device-qr-frame"><canvas v-show="qrReady" ref="canvas" role="img" aria-label="手机 App 一次性绑定二维码"></canvas><div v-if="!qrReady" class="inline-loading"><LoaderCircle class="spin" :size="20" />生成二维码…</div></div><div class="device-code-details"><strong>服务器</strong><span>{{ pairing.server_url }}</span><p>二维码在当前浏览器生成。重新生成后，旧的未使用绑定码失效。</p><div class="device-code-actions"><button class="button secondary small" :disabled="copying || !qrReady" @click="copyCode"><Copy :size="15" />复制绑定码</button><button class="text-button" :disabled="!qrReady" @click="revealCode = !revealCode"><EyeOff v-if="revealCode" :size="15" /><Eye v-else :size="15" />{{ revealCode ? '隐藏绑定码' : '显示绑定码' }}</button></div><p v-if="copyMessage" class="device-copy-message" role="status">{{ copyMessage }}</p></div></div><label v-if="revealCode" class="field device-code-fallback">无摄像头时，可复制并粘贴到 App<textarea :value="pairing.qr_payload" readonly rows="4" spellcheck="false" aria-label="一次性绑定码" @focus="($event.target as HTMLTextAreaElement).select()"></textarea></label></div>
    <p v-if="pairingNotice" class="device-pairing-notice" role="status">{{ pairingNotice }}</p>
    <div class="device-list-heading"><h3>已绑定设备</h3><button class="text-button" :disabled="devicesLoading || !!revoking" @click="loadDevices()"><RefreshCw :size="15" :class="{spin: devicesLoading}" />刷新</button></div>
    <div v-if="devicesLoading && !devices.length" class="inline-loading"><LoaderCircle class="spin" :size="18" />读取设备…</div>
    <div v-if="devicesError || deviceActionError" class="alert error" role="alert"><AlertCircle :size="18" /><span>{{ deviceActionError || devicesError }}</span></div>
    <ul v-if="devices.length" class="bound-device-list"><li v-for="device in devices" :key="device.id"><Smartphone :size="21" /><div class="bound-device-info"><div><strong>{{ device.device_name }}</strong><span class="status-pill" :class="device.status === 'active' ? 'success' : 'neutral'">{{ {active: '可访问', expired: '已过期', revoked: '已撤销'}[device.status] }}</span></div><p>最近活动：{{ deviceDate(device.last_used_at) }}</p><p>{{ device.status === 'revoked' ? '撤销时间' : '授权到期' }}：{{ deviceDate(device.status === 'revoked' ? device.revoked_at : device.expires_at) }}</p></div><button v-if="device.status !== 'revoked'" class="button quiet small danger-text" :disabled="!!revoking || devicesLoading" :aria-label="`撤销设备 ${device.device_name}`" @click="revokeDevice(device)"><LoaderCircle v-if="revoking === device.id" class="spin" :size="15" /><Unplug v-else :size="15" />撤销</button><Check v-else :size="17" class="muted" /></li></ul>
    <p v-else-if="!devicesLoading && !devicesError" class="device-empty">尚未绑定设备。生成二维码后，在 App 中扫码连接。</p>
    <p class="device-revoke-note">撤销后，该设备将无法继续访问书库。</p>
  </section>
</template>
