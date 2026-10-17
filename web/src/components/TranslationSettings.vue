<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Languages, LoaderCircle } from 'lucide-vue-next'
import { api, errorMessage, post } from '../api'

type State = {enabled: boolean; provider: 'ollama' | 'openai' | 'controller'; base_url: string; model: string; has_api_key: boolean; use_proxy: boolean; controller_node_id?: string | null}
const form = reactive({enabled: false, provider: 'ollama' as State['provider'], base_url: '', model: '', use_proxy: false, controller_node_id: null as string | null})
const nodes = ref<{id: string; label: string; enabled: boolean}[]>([])
async function loadNodes() {
  try { nodes.value = (await api<{items: typeof nodes.value}>('/ai/nodes')).items }
  catch (failure) { error.value = errorMessage(failure) }
}
const apiKey = ref('')
const hasKey = ref(false)
const loading = ref(true)
const busy = ref('')
const error = ref('')
const message = ref('')
const saved = ref('')
const dirty = computed(() => JSON.stringify(form) !== saved.value || !!apiKey.value)
function apply(state: State) {
  Object.assign(form, {enabled: state.enabled, provider: state.provider, base_url: state.base_url, model: state.model, use_proxy: state.use_proxy, controller_node_id: state.controller_node_id || null})
  hasKey.value = state.has_api_key; apiKey.value = ''; saved.value = JSON.stringify(form)
}
async function load() {
  loading.value = true; error.value = ''
  try { apply(await api<State>('/translation/settings')); if (form.provider === 'controller') await loadNodes() }
  catch (failure) { error.value = errorMessage(failure) }
  finally { loading.value = false }
}
function changeProvider() {
  form.base_url = form.provider === 'ollama' ? 'http://host.docker.internal:11434' : 'https://api.openai.com/v1'
  form.model = ''; apiKey.value = ''; message.value = ''
  form.controller_node_id = null
  if (form.provider === 'controller') { form.base_url = 'http://host.docker.internal:11434'; form.use_proxy = false; void loadNodes() }
}
async function save(clearKey = false) {
  if (busy.value) return
  busy.value = 'save'; error.value = ''; message.value = ''
  try {
    apply(await api<State>('/translation/settings', {method: 'PUT', body: JSON.stringify({
      ...form, ...(clearKey ? {api_key: ''} : apiKey.value.trim() ? {api_key: apiKey.value.trim()} : {}),
    })}))
    message.value = form.enabled ? '配置已保存，可在 App 作品详情中开始汉化。' : '配置已保存，AI 汉化已关闭。'
  } catch (failure) { error.value = errorMessage(failure) }
  finally { busy.value = '' }
}
async function test() {
  busy.value = 'test'; error.value = ''; message.value = ''
  try {
    const result = await post<{title: string; tag: string}>('/translation/test')
    message.value = `模型返回：${result.title} / ${result.tag}`
  } catch (failure) { error.value = errorMessage(failure) }
  finally { busy.value = '' }
}
async function batch() {
  if (!window.confirm('将最多 50 部作品的标题和标签加入汉化队列。使用远程模型时，这些文字会发送给对应服务商。继续？')) return
  busy.value = 'batch'; error.value = ''; message.value = ''
  try {
    const result = await post<{queued: number; has_more: boolean}>('/translation/batch', {consent: true, limit: 50})
    message.value = `已加入 ${result.queued} 部作品，可在任务页查看进度。${result.has_more ? '还有未完成作品，稍后可继续提交。' : ''}`
  } catch (failure) { error.value = errorMessage(failure) }
  finally { busy.value = '' }
}
onMounted(load)
</script>

<template>
  <section class="panel settings-card translation-settings">
    <div class="settings-title"><Languages :size="21" /><div><h2>标题与标签 AI 汉化</h2></div></div>
    <div v-if="loading" class="inline-loading"><LoaderCircle class="spin" :size="18" />读取配置…</div>
    <form v-else-if="saved" @submit.prevent="save()">
      <label class="translation-enabled"><input v-model="form.enabled" type="checkbox" />启用 AI 汉化</label>
      <div class="form-grid">
        <label class="field">模型接口<select v-model="form.provider" @change="changeProvider"><option value="ollama">直接连接 Ollama</option><option value="openai">兼容 OpenAI 的接口</option><option value="controller">远程 AI 控制器</option></select></label>
        <label class="field">模型标识<input v-model="form.model" placeholder="填写已安装或已开通的模型" :required="form.enabled" maxlength="191" /></label>
      </div>
      <label v-if="form.provider === 'controller'" class="field">AI 节点<select v-model="form.controller_node_id" required><option :value="null">请选择 AI 控制器</option><option v-for="node in nodes" :key="node.id" :value="node.id" :disabled="!node.enabled">{{ node.label }}</option></select></label>
      <template v-else>
      <label class="field">服务地址<input v-model="form.base_url" type="url" required maxlength="2048" placeholder="http://host.docker.internal:11434" /></label>
      <p class="field-hint">地址由服务器访问。Docker 连接这台电脑上的 Ollama 可用 host.docker.internal；NAS 或其他电脑请填写对应地址。Ollama 填根地址，兼容接口可填 /v1 地址。</p>
      <label class="field">API Key <span class="optional">选填</span><input v-model="apiKey" type="password" autocomplete="new-password" :placeholder="hasKey ? '已保存；留空保留' : '本地服务无密钥时留空'" maxlength="2048" /></label>
      <p class="field-hint">切换接口类型或服务地址后，旧密钥会清除；需要密钥时请重新输入。</p>
      <label class="translation-enabled"><input v-model="form.use_proxy" type="checkbox" />模型请求使用书库代理</label>
      </template>
      <p class="field-hint">仅发送标题和标签，不发送漫画图片或正文。原文保留，中文译名全设备共享。模型需自行安装或开通；远程服务可能计费或拒绝部分内容，失败时保留已有资料。</p>
      <div class="translation-actions">
        <button class="button primary small" :disabled="!!busy"><LoaderCircle v-if="busy === 'save'" class="spin" :size="16" />保存配置</button>
        <button type="button" class="button secondary small" :disabled="!!busy || dirty || !form.enabled" @click="test"><LoaderCircle v-if="busy === 'test'" class="spin" :size="16" />测试汉化</button>
        <button type="button" class="button secondary small" :disabled="!!busy || dirty || !form.enabled" @click="batch">汉化未翻译作品（最多 50 部）</button>
        <button v-if="hasKey" type="button" class="text-button" :disabled="!!busy" @click="save(true)">清除密钥</button>
      </div>
      <p v-if="dirty" class="field-hint">先保存配置，再测试或提交批量汉化。</p>
    </form>
    <div v-if="error" class="alert error" role="alert">{{ error }}<button v-if="!saved" class="text-button" @click="load">重试</button></div>
    <div v-if="message" class="alert info" role="status">{{ message }}</div>
  </section>
</template>

<style scoped>
.translation-settings {margin-top: 20px;}
.translation-enabled {display:flex;align-items:center;gap:9px;margin:12px 0;font-size:14px;}
.translation-actions {display:flex;flex-wrap:wrap;gap:8px;margin-top:16px;}
.translation-settings .field {margin-top:12px;}
.translation-settings .alert {margin-top:12px;}
</style>
