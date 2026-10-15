<script setup lang="ts">
import { ref, reactive, computed, onBeforeUnmount } from 'vue'
import { AlertCircle, ArrowUpRight, CheckCircle2, ChevronDown, Clock3, Code2, Eye, EyeOff, ExternalLink, KeyRound, LoaderCircle, LockKeyhole, LogIn, RefreshCw, ShieldCheck } from 'lucide-vue-next'
import Modal from './Modal.vue'
import { api, post, errorMessage } from '../api'
import type { Source } from '../types'

const props = defineProps<{source: Source}>()
const emit = defineEmits<{close: []; saved: [message: string]}>()
const form = reactive({label: '', username: '', password: '', credentials: '', code: ''})
const busy = ref(false)
const error = ref('')
const advanced = ref(false)
const showPassword = ref(false)
const oauth = ref<{session_id: string; authorization_url: string; expires_in: number} | null>(null)
const expiresAt = ref(0)
const now = ref(Date.now())
const secondsLeft = computed(() => Math.max(0, Math.ceil((expiresAt.value - now.value) / 1000)))
const isPixiv = computed(() => props.source.platform === 'pixiv')
const usernameLabel = computed(() => props.source.platform === 'picacg' ? '登录邮箱' : '用户名')
const credentialExamples: Record<string, string> = {
  pixiv: '{\n  "refresh_token": "你的 refresh_token"\n}',
  ehentai: '{\n  "ipb_member_id": "你的 member_id",\n  "ipb_pass_hash": "你的 pass_hash"\n}',
  picacg: '{\n  "token": "你的登录令牌"\n}',
  jm: '{\n  "AVS": "你的会话 Cookie"\n}',
}
const officialAuthURL = computed(() => {
  if (!oauth.value) return ''
  try {
    const url = new URL(oauth.value.authorization_url)
    return url.protocol === 'https:' && (url.hostname === 'pixiv.net' || url.hostname.endsWith('.pixiv.net')) ? url.toString() : ''
  } catch { return '' }
})
const expiryLabel = computed(() => `${Math.floor(secondsLeft.value / 60)} 分 ${String(secondsLeft.value % 60).padStart(2, '0')} 秒`)
const timer = setInterval(() => now.value = Date.now(), 1000)
onBeforeUnmount(() => { clearInterval(timer); form.password = ''; form.credentials = ''; form.code = '' })
function close() { if (!busy.value) emit('close') }
function label() { return form.label.trim() || `${props.source.label} 账号` }

async function login() {
  busy.value = true; error.value = ''
  try {
    await post(`/sources/${props.source.platform}/login`, {label: label(), username: form.username.trim(), password: form.password})
    emit('saved', `${props.source.label} 已登录，账号已保存。`)
  } catch (err) { error.value = errorMessage(err) }
  finally { form.password = ''; busy.value = false }
}
async function startOAuth() {
  busy.value = true; error.value = ''; form.code = ''
  try {
    const result = await post<{session_id: string; authorization_url: string; expires_in: number}>('/sources/pixiv/oauth/start')
    oauth.value = result; now.value = Date.now(); expiresAt.value = now.value + result.expires_in * 1000
    if (!officialAuthURL.value) { oauth.value = null; throw new Error('服务返回了无法识别的授权地址，请重试。') }
  } catch (err) { error.value = errorMessage(err) }
  finally { busy.value = false }
}
async function finishOAuth() {
  if (!oauth.value || !secondsLeft.value) { error.value = '本次授权已过期，请重新创建授权。'; return }
  busy.value = true; error.value = ''
  try {
    await post('/sources/pixiv/oauth/finish', {session_id: oauth.value.session_id, code: form.code.trim(), label: label()})
    emit('saved', 'Pixiv 授权成功，账号已登录。')
  } catch (err) { error.value = errorMessage(err) }
  finally { form.code = ''; busy.value = false }
}
async function saveAdvanced() {
  busy.value = true; error.value = ''
  try {
    let credentials: unknown
    try { credentials = JSON.parse(form.credentials) } catch { throw new Error('凭据不是有效的 JSON，请检查双引号和逗号。') }
    if (!credentials || typeof credentials !== 'object' || Array.isArray(credentials)) throw new Error('凭据应为 JSON 对象。')
    await api(`/sources/${props.source.platform}/accounts`, {method: 'POST', body: JSON.stringify({label: label(), credentials})})
    form.credentials = ''; emit('saved', '凭据已保存，尚未验证登录状态。')
  } catch (err) { error.value = errorMessage(err) }
  finally { busy.value = false }
}
</script>

<template>
  <Modal :title="`登录 ${source.label}`" :busy="busy" @close="close">
    <div class="source-login-heading"><div class="source-logo" :class="source.platform">{{ ({pixiv: 'P', ehentai: 'E', picacg: '哔', jm: 'JM'} as Record<string, string>)[source.platform] || source.label[0] }}</div><div><strong>{{ isPixiv ? '通过 Pixiv 官方页面授权' : `使用 ${source.label} 账号登录` }}</strong><p>{{ isPixiv ? '登录密码只在 Pixiv 官方页面输入。' : '由书库服务验证登录，成功后保存会话。' }}</p></div><ShieldCheck :size="20" /></div>
    <label class="field">在书库中显示的名称 <span class="optional">选填</span><input v-model="form.label" placeholder="例如：我的主账号" maxlength="100" :disabled="busy" /></label>

    <template v-if="isPixiv">
      <div v-if="!oauth" class="pixiv-authorization-intro"><div class="authorization-step"><span>1</span><div><strong>打开官方页面登录</strong></div></div><div class="authorization-step"><span>2</span><div><strong>粘贴授权码</strong><p>从电脑浏览器网络记录复制回调链接。</p></div></div><button class="button primary" type="button" :disabled="busy" @click="startOAuth"><LoaderCircle v-if="busy" :size="17" class="spin" /><KeyRound v-else :size="17" />{{ busy ? '正在创建授权' : '开始 Pixiv 授权' }}</button></div>
      <form v-else @submit.prevent="finishOAuth"><div class="oauth-session"><div class="oauth-session-heading"><span class="status-pill" :class="secondsLeft ? 'success' : 'neutral'"><Clock3 :size="12" />{{ secondsLeft ? `本站会话 ${expiryLabel}` : '授权已过期' }}</span><button class="text-button" type="button" :disabled="busy" @click="startOAuth"><RefreshCw :size="12" />重新创建</button></div><a v-if="secondsLeft" class="button secondary" :href="officialAuthURL" target="_blank" rel="noopener noreferrer"><ExternalLink :size="16" />打开 Pixiv 官方登录页<ArrowUpRight :size="15" /></a><ol class="oauth-instructions"><li>建议使用电脑。打开官方页面后，先按 <kbd>F12</kbd> 打开开发者工具，切换到 <strong>Network（网络）</strong>，勾选 <strong>Preserve log（保留日志）</strong>。</li><li>在官方页面完成登录，在网络请求中过滤 <code>callback</code> 或 <code>pixiv://account/login</code>，复制完整请求链接或其中的 <code>code</code> 参数。</li><li>立即返回这里粘贴并完成授权。Pixiv 授权码通常几十秒即失效；上方倒计时仅为本站授权会话有效期。</li></ol><p>本页不会自动接收原生应用回调。官方登录页使用浏览器自身网络，书库的 Clash 配置只代理服务端会话交换与内容请求。</p></div><label class="field">授权码 / 完整回调链接<textarea v-model="form.code" rows="3" placeholder="粘贴 code 或 pixiv://… 回调链接" autocomplete="off" spellcheck="false" :disabled="busy || !secondsLeft" required /></label><button class="button primary source-login-submit" :disabled="busy || !secondsLeft || !form.code.trim()"><LoaderCircle v-if="busy" class="spin" :size="17" /><CheckCircle2 v-else :size="17" />{{ busy ? '正在完成授权' : '完成授权并保存' }}</button></form>
    </template>
    <form v-else @submit.prevent="login"><label class="field">{{ usernameLabel }}<input v-model="form.username" :type="source.platform === 'picacg' ? 'email' : 'text'" autocomplete="username" :placeholder="source.platform === 'picacg' ? '请输入哔咔账号邮箱' : '请输入平台用户名'" :disabled="busy" required /></label><label class="field">登录密码<div class="input-with-icon login-password-field"><LockKeyhole :size="16" /><input v-model="form.password" :type="showPassword ? 'text' : 'password'" autocomplete="current-password" placeholder="请输入平台密码" :disabled="busy" required /><button type="button" class="icon-button" :aria-label="showPassword ? '隐藏密码' : '显示密码'" @click="showPassword = !showPassword"><component :is="showPassword ? EyeOff : Eye" :size="16" /></button></div></label><button class="button primary source-login-submit" :disabled="busy || !form.username.trim() || !form.password"><LoaderCircle v-if="busy" class="spin" :size="17" /><LogIn v-else :size="17" />{{ busy ? '正在验证登录' : '登录并保存账号' }}</button></form>

    <div v-if="error" class="alert error" role="alert"><AlertCircle :size="18" /><span>{{ error }}</span></div>
    <div class="login-privacy-note"><LockKeyhole :size="13" /><span>登录成功后仅保存加密会话凭据，密码与令牌不会回显。</span></div>
    <div class="advanced-credentials"><button type="button" class="advanced-toggle" :aria-expanded="advanced" :disabled="busy" @click="advanced = !advanced"><Code2 :size="15" /><span>高级方式：手动填写会话凭据</span><ChevronDown :size="15" :class="{expanded: advanced}" /></button><form v-if="advanced" @submit.prevent="saveAdvanced"><div class="alert warning"><AlertCircle :size="16" /><span>此方式只保存凭据，不会验证远端登录。来源页会显示「凭据未验证」。</span></div><label class="field">会话凭据 <span class="optional">JSON</span><textarea v-model="form.credentials" class="code-input" rows="6" :placeholder="credentialExamples[source.platform] || '{}'" required autocomplete="off" spellcheck="false" :disabled="busy" /></label><button class="button secondary" :disabled="busy || !form.credentials.trim()"><LoaderCircle v-if="busy" :size="15" class="spin" /><LockKeyhole v-else :size="15" />保存未验证凭据</button></form></div>
  </Modal>
</template>
