<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Globe2, LoaderCircle, RotateCcw, Save } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { PlatformEndpoint } from '../types'

type Draft = {enabled: boolean; custom_url: string}
const items = ref<PlatformEndpoint[]>([])
const drafts = reactive<Record<string, Draft>>({})
const loading = ref(true), busy = ref(''), error = ref(''), message = ref('')
let request = 0, disposed = false

function dirty(item: PlatformEndpoint) {
  const draft = drafts[item.platform]
  return !!draft && (draft.enabled !== item.enabled || draft.custom_url !== (item.custom_url || ''))
}
function apply(item: PlatformEndpoint) {
  const index = items.value.findIndex(row => row.platform === item.platform)
  if (index < 0) items.value.push(item)
  else items.value[index] = item
  drafts[item.platform] = {enabled: item.enabled, custom_url: item.custom_url || ''}
}
async function load() {
  if (disposed || busy.value) return
  const current = ++request
  loading.value = true; error.value = ''
  try {
    const result = await api<{items: PlatformEndpoint[]}>('/platform-endpoints')
    if (disposed || current !== request) return
    items.value = []
    for (const item of result.items) apply(item)
  } catch (failure) { if (!disposed && current === request) error.value = errorMessage(failure) }
  finally { if (!disposed && current === request) loading.value = false }
}
function restoreDefault(item: PlatformEndpoint) {
  if (busy.value || loading.value) return
  drafts[item.platform] = {enabled: false, custom_url: ''}
  error.value = ''; message.value = `${item.label} 已恢复默认，保存后生效。`
}
async function save(item: PlatformEndpoint) {
  if (disposed || busy.value || loading.value || !dirty(item)) return
  const draft = drafts[item.platform]
  if (draft.enabled && !draft.custom_url.trim()) {
    error.value = `请填写 ${item.label} 的自定义 API 地址。`; return
  }
  const current = ++request
  busy.value = item.platform; error.value = ''; message.value = ''
  try {
    const result = await api<PlatformEndpoint>(`/platform-endpoints/${encodeURIComponent(item.platform)}`, {
      method: 'PUT', body: JSON.stringify({custom_url: draft.custom_url.trim() || null, enabled: draft.enabled}),
    })
    if (disposed || current !== request) return
    apply(result)
    message.value = `${result.label} 已保存，${result.enabled ? '使用自定义地址' : '使用默认地址'}。`
  } catch (failure) { if (!disposed && current === request) error.value = errorMessage(failure) }
  finally { if (!disposed && current === request) busy.value = '' }
}
onMounted(load)
onBeforeUnmount(() => { disposed = true; ++request })
</script>

<template>
  <section id="platform-api-settings" class="panel settings-card platform-api-settings">
    <div class="settings-title"><Globe2 :size="21" /><div><h2>内容平台 API</h2><p>由书库服务器连接平台，Web 与 App 共用这些设置。</p></div></div>
    <p class="field-hint endpoint-notice">自定义地址须支持原平台协议，并使用公网 HTTPS；需要登录的请求会将平台凭据发送到该地址。</p>
    <div v-if="loading" class="inline-loading"><LoaderCircle class="spin" :size="18" />读取平台地址…</div>
    <div v-else class="endpoint-list">
      <form v-for="item in items" :key="item.platform" class="endpoint-row" @submit.prevent="save(item)">
        <div class="endpoint-heading"><h3>{{ item.label }}</h3><label class="endpoint-toggle"><input v-model="drafts[item.platform]!.enabled" type="checkbox" :disabled="!!busy" :aria-label="`${item.label} 使用自定义 API`" />使用自定义 API</label></div>
        <p class="field-hint">{{ item.description }}</p>
        <p class="endpoint-effective"><span>当前生效</span><code>{{ item.effective_url }}</code></p>
        <label class="field">自定义地址<input v-model="drafts[item.platform]!.custom_url" type="url" maxlength="2048" :placeholder="item.default_url" :required="drafts[item.platform]!.enabled" :disabled="!!busy || !drafts[item.platform]!.enabled" :aria-label="`${item.label} 自定义 API 地址`" autocomplete="off" spellcheck="false" /></label>
        <div class="endpoint-actions"><span v-if="dirty(item)" class="field-hint" role="status">修改尚未保存</span><button type="button" class="text-button" :disabled="!!busy || (!drafts[item.platform]!.enabled && !drafts[item.platform]!.custom_url)" @click="restoreDefault(item)"><RotateCcw :size="14" />恢复默认</button><button class="button secondary small" :disabled="!!busy || !dirty(item)"><LoaderCircle v-if="busy === item.platform" class="spin" :size="15" /><Save v-else :size="15" />保存 {{ item.label }}</button></div>
      </form>
    </div>
    <div v-if="error" class="alert error" role="alert">{{ error }}<button v-if="!items.length" class="text-button" :disabled="loading" @click="load">重试</button></div>
    <div v-if="message" class="alert info" role="status">{{ message }}</div>
  </section>
</template>

<style scoped>
.platform-api-settings {margin:20px 0;scroll-margin-top:24px;}.endpoint-notice {margin:0 0 20px;}.endpoint-list {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;}.endpoint-row {min-width:0;border:1px solid var(--line);border-radius:12px;padding:18px;}.endpoint-heading {display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;}.endpoint-heading h3 {margin:0;font-size:16px;}.endpoint-toggle {display:flex;align-items:center;gap:8px;font-size:13px;}.endpoint-row>.field-hint {margin:12px 0;}.endpoint-effective {display:flex;flex-direction:column;gap:6px;font-size:13px;margin:12px 0 16px;}.endpoint-effective>span {color:var(--text-secondary);}.endpoint-effective code {overflow-wrap:anywhere;font-size:12px;line-height:1.6;}.endpoint-actions {display:flex;align-items:center;justify-content:flex-end;gap:12px;flex-wrap:wrap;margin-top:16px;}.endpoint-actions>.field-hint {margin-right:auto;}.alert {margin-top:16px;}@media(max-width:900px){.endpoint-list {grid-template-columns:1fr;}}
</style>
