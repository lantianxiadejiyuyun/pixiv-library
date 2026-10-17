<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { Server, Plus, LoaderCircle, RefreshCw, Download } from 'lucide-vue-next'
import { api, errorMessage, post } from '../api'

type Node = {id: string; label: string; base_url: string; enabled: boolean; has_key: boolean}
type State = {items: Node[]; image_node_id: string | null}
type Service = {service: 'ollama' | 'vision'; state: string; status: string; image?: string}
type Model = {name: string; size?: number; size_vram?: number}
type Operation = {id: string; model: string; status: string; phase: string; total: number; completed: number; error?: string}
const emit = defineEmits<{change: []}>()
const nodes = ref<Node[]>([]), selected = ref(''), imageNode = ref('')
const services = ref<Service[]>([]), models = ref<Model[]>([]), running = ref<Model[]>([]), operations = ref<Operation[]>([])
const vision = ref(''), pullName = ref(''), loading = ref(true), busy = ref(''), error = ref(''), message = ref('')
const editor = ref(false), editing = ref('')
const form = reactive({label: '', base_url: '', key: '', enabled: true})
const current = computed(() => nodes.value.find(node => node.id === selected.value))
let disposed = false, request = 0, timer: ReturnType<typeof setTimeout> | undefined
const statuses: Record<string, string> = {running: '运行中', exited: '已停止', created: '已创建', restarting: '重启中', not_created: '尚未部署', unavailable: '不可连接', queued: '排队中', completed: '已完成', failed: '失败'}
const nodePath = (id = selected.value) => `/ai/nodes/${encodeURIComponent(id)}`

async function load() {
  loading.value = true; error.value = ''
  try {
    const data = await api<State>('/ai/nodes')
    if (disposed) return
    nodes.value = data.items; imageNode.value = data.image_node_id || ''
    if (!data.items.some(node => node.id === selected.value)) selected.value = data.image_node_id || data.items[0]?.id || ''
  } catch (failure) { if (!disposed) error.value = errorMessage(failure) }
  finally { if (!disposed) loading.value = false }
}
function edit(node?: Node) {
  editing.value = node?.id || ''; editor.value = true
  Object.assign(form, {label: node?.label || '', base_url: node?.base_url || '', enabled: node?.enabled ?? true, key: ''})
}
function selectNode() {
  request++; clearTimeout(timer); services.value = []; models.value = []; running.value = []; operations.value = []; vision.value = ''; error.value = ''; message.value = ''
}
async function save() {
  if (busy.value || loading.value) return
  busy.value = 'save'; error.value = ''; message.value = ''
  try {
    const data = await api<Node>(editing.value ? nodePath(editing.value) : '/ai/nodes', {method: editing.value ? 'PUT' : 'POST', body: JSON.stringify({label: form.label, base_url: form.base_url, enabled: form.enabled, ...(form.key ? {key: form.key} : {})})})
    if (disposed) return
    form.key = ''; editor.value = false; selected.value = data.id; selectNode(); await load(); emit('change'); message.value = '节点已保存，点击“连接并刷新”读取远程状态。'
  } catch (failure) { if (!disposed) error.value = errorMessage(failure) }
  finally { if (!disposed) busy.value = '' }
}
async function routeImages() {
  if (busy.value) return
  busy.value = 'routing'; error.value = ''; message.value = ''
  try {
    await api('/ai/nodes/routing', {method: 'PUT', body: JSON.stringify({image_node_id: imageNode.value || null})})
    if (!disposed) { message.value = imageNode.value ? '新的图片识别任务使用所选远程节点。' : '新的图片识别任务使用书库本机模型。'; emit('change') }
  } catch (failure) { if (!disposed) error.value = errorMessage(failure) }
  finally { if (!disposed) busy.value = '' }
}
async function refresh() {
  if (!selected.value || busy.value) return
  const id = selected.value, revision = ++request
  busy.value = 'refresh'; error.value = ''; message.value = ''
  const result = await Promise.allSettled([
    post<{services: Service[]}>(nodePath(id) + '/test'),
    api<{models: Model[]; running: Model[]}>(nodePath(id) + '/models'),
    api<{available: boolean; models: {message: string}[]}>(nodePath(id) + '/vision'),
    api<{items: Operation[]}>(nodePath(id) + '/operations'),
  ])
  if (disposed || revision !== request) { if (!disposed) busy.value = ''; return }
  if (result[0].status === 'fulfilled') services.value = result[0].value.services
  if (result[1].status === 'fulfilled') { models.value = result[1].value.models; running.value = result[1].value.running }
  if (result[2].status === 'fulfilled') vision.value = result[2].value.available ? '图像模型文件与推理依赖已就绪' : result[2].value.models?.[0]?.message || '图像模型未就绪'
  if (result[3].status === 'fulfilled') { operations.value = result[3].value.items; schedulePoll() }
  error.value = result.flatMap((value, index) => value.status === 'rejected' ? [`${['容器', 'Ollama', '图像服务', '模型任务'][index]}：${errorMessage(value.reason)}`] : []).join('；')
  busy.value = ''
}
function schedulePoll() {
  clearTimeout(timer)
  if (disposed || !operations.value.some(operation => ['running', 'queued'].includes(operation.status))) return
  const id = selected.value, revision = request
  timer = setTimeout(async () => {
    try {
      const result = await api<{items: Operation[]}>(nodePath(id) + '/operations')
      if (disposed || revision !== request) return
      const finished = operations.value.some(row => ['running', 'queued'].includes(row.status)) && !result.items.some(row => ['running', 'queued'].includes(row.status))
      operations.value = result.items; schedulePoll()
      if (finished && !busy.value) await refresh()
    } catch (failure) { if (!disposed && revision === request) error.value = errorMessage(failure) }
  }, 2000)
}
async function action(path: string, body?: unknown, method = 'POST') {
  if (!selected.value || busy.value) return
  const id = selected.value, revision = request
  let completedMessage = ''
  busy.value = path; error.value = ''; message.value = ''
  try {
    await api(nodePath(id) + path, {method, ...(body === undefined ? {} : {body: JSON.stringify(body)})})
    if (disposed || revision !== request) return
    completedMessage = path.endsWith('/translation') ? '已启用此节点的 Ollama 汉化模型；原文继续保留。' : '操作已提交。'
    emit('change')
  } catch (failure) { if (!disposed && revision === request) error.value = errorMessage(failure) }
  finally { if (!disposed) busy.value = '' }
  if (!disposed && revision === request && completedMessage) {
    await refresh()
    if (!disposed && revision + 1 === request) message.value = completedMessage
  }
}
function control(service: Service['service'], operation: 'start' | 'stop' | 'restart') {
  if (operation !== 'start' && !window.confirm(`${operation === 'stop' ? '停止' : '重启'} ${service === 'vision' ? '图像识别服务' : 'Ollama'}？该节点正在处理的任务可能失败，需要重试。`)) return
  return action(`/services/${service}/${operation}`)
}
function removeModel(model: string) {
  if (window.confirm(`从远程电脑删除模型“${model}”？再次使用时需要重新下载。`)) return action('/models', {model}, 'DELETE')
}
async function removeNode() {
  if (!current.value || busy.value || !window.confirm(`移除节点“${current.value.label}”的连接配置？远程容器和模型会保留。`)) return
  busy.value = 'remove'; error.value = ''
  try { await api(nodePath(), {method: 'DELETE'}); if (!disposed) { selectNode(); await load(); emit('change') } }
  catch (failure) { if (!disposed) error.value = errorMessage(failure) }
  finally { if (!disposed) busy.value = '' }
}
onMounted(load)
onBeforeUnmount(() => { disposed = true; request++; clearTimeout(timer); form.key = '' })
</script>

<template>
  <section class="panel ai-nodes">
    <header><div><Server :size="22" /><h2>远程 AI 控制器</h2></div><button class="button secondary small" :disabled="!!busy" @click="edit()"><Plus :size="16" />添加节点</button></header>
    <p class="field-hint">另一台电脑运行配套控制器、Ollama 和图像识别容器，主服务器统一调度。控制器只管理本节点标记的 AI 容器。</p>
    <a href="/api/ai/nodes/deployment" class="button secondary small"><Download :size="16" />下载节点部署包</a>
    <p v-if="loading" class="inline-loading"><LoaderCircle class="spin" :size="16" />读取节点…</p>
    <form v-if="editor" class="node-editor" @submit.prevent="save">
      <div class="form-grid"><label class="field">节点名称<input v-model="form.label" required maxlength="80" /></label><label class="field">控制器地址<input v-model="form.base_url" type="url" required placeholder="http://192.168.1.20:9010" /></label></div>
      <label class="field">控制器密钥<input v-model="form.key" type="password" autocomplete="new-password" :required="!editing" minlength="32" maxlength="256" :placeholder="editing ? '留空保留；更换地址需重新输入' : '填写部署包生成的 QIYE_CONTROLLER_KEY'" /></label>
      <label class="node-checkbox"><input v-model="form.enabled" type="checkbox" />启用节点连接</label>
      <div class="node-actions"><button class="button primary small" :disabled="!!busy">保存节点</button><button type="button" class="button secondary small" :disabled="!!busy" @click="editor = false; form.key = ''">取消</button></div>
    </form>
    <form class="node-routing" @submit.prevent="routeImages"><label class="field">图片识别执行位置<select v-model="imageNode" :disabled="loading || !!busy"><option value="">主服务器本机</option><option v-for="node in nodes" :key="node.id" :value="node.id" :disabled="!node.enabled">{{ node.label }}{{ node.enabled ? '' : '（停用）' }}</option></select></label><button class="button primary small" :disabled="loading || !!busy">保存执行位置</button></form>
    <p class="field-hint">远程识别会将待分析图片发送到所选节点。连接失败时保留原标签，不自动改用其他节点；文章关键词分类仍在主服务器执行。</p>
    <div v-if="nodes.length" class="node-tools"><label class="field">管理节点<select v-model="selected" :disabled="!!busy" @change="selectNode"><option v-for="node in nodes" :key="node.id" :value="node.id">{{ node.label }}{{ node.enabled ? '' : '（停用）' }}</option></select></label><button class="button secondary small" :disabled="!!busy || !current?.enabled" @click="refresh"><RefreshCw :size="15" :class="{spin: busy === 'refresh'}" />连接并刷新</button><button class="text-button" :disabled="!!busy" @click="edit(current)">编辑连接</button><button class="text-button danger-text" :disabled="!!busy" @click="removeNode">移除节点</button></div>
    <p v-else-if="!loading" class="field-hint">先在 AI 电脑部署控制器，再添加地址与密钥。</p>
    <div v-if="services.length" class="node-services"><article v-for="service in services" :key="service.service"><h3>{{ service.service === 'ollama' ? 'Ollama' : '图像识别' }}</h3><strong>{{ statuses[service.state] || service.state }}</strong><p>{{ service.status }}</p><small v-if="service.image">{{ service.image }}</small><div class="node-actions"><button class="button secondary small" :disabled="!!busy" @click="control(service.service, 'start')">启动</button><button class="button secondary small" :disabled="!!busy" @click="control(service.service, 'restart')">重启</button><button class="text-button danger-text" :disabled="!!busy" @click="control(service.service, 'stop')">停止</button></div></article></div>
    <p v-if="vision" class="field-hint">图像服务：{{ vision }}</p>
    <template v-if="current?.enabled">
      <form class="model-pull" @submit.prevent="action('/models/pull', {model: pullName})"><label class="field">下载 Ollama 模型<input v-model="pullName" required maxlength="191" placeholder="模型名称:标签" /></label><button class="button secondary small" :disabled="!!busy">加入模型下载队列</button></form>
      <p v-if="running.length" class="field-hint">内存中已加载：{{ running.map(model => model.name).join('、') }}</p>
      <article v-for="model in models" :key="model.name" class="node-model"><div><strong>{{ model.name }}</strong><small v-if="model.size">{{ (model.size / 1024 ** 3).toFixed(2) }} GiB</small></div><div class="node-actions"><button class="button secondary small" :disabled="!!busy" @click="action('/translation', {model: model.name})">用于标题与标签汉化</button><button class="text-button" :disabled="!!busy" @click="action('/models/unload', {model: model.name})">释放模型内存</button><button class="text-button danger-text" :disabled="!!busy" @click="removeModel(model.name)">删除模型</button></div></article>
      <article v-for="operation in operations" :key="operation.id" class="node-operation"><div><strong>{{ operation.model }}</strong><span>{{ statuses[operation.status] || operation.status }} · {{ operation.phase }}</span></div><progress v-if="['queued', 'running'].includes(operation.status)" :value="operation.total ? operation.completed : undefined" :max="operation.total || 1"></progress><p v-if="operation.error" class="inline-error">{{ operation.error }}</p></article>
    </template>
    <div v-if="error" class="alert error" role="alert">{{ error }}</div><div v-if="message" class="alert info" role="status">{{ message }}</div>
  </section>
</template>

<style scoped>
.ai-nodes {padding:24px;margin-bottom:24px;}.ai-nodes header,.ai-nodes header>div,.node-actions,.node-tools,.node-routing,.model-pull {display:flex;align-items:center;gap:12px;flex-wrap:wrap;}.ai-nodes header {justify-content:space-between;}.ai-nodes h2 {font-size:18px;margin:0;}.ai-nodes .field-hint {line-height:1.7;margin:12px 0;}.node-editor {padding:18px;margin-top:18px;border:1px solid var(--line);border-radius:12px;}.node-editor .field {margin-bottom:14px;}.node-checkbox {display:flex;gap:8px;align-items:center;margin-bottom:16px;}.node-routing,.node-tools,.model-pull {align-items:flex-end;margin-top:20px;}.node-routing .field,.node-tools .field,.model-pull .field {flex:1;min-width:190px;}.node-services {display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,270px),1fr));gap:14px;margin-top:20px;}.node-services article {padding:18px;border:1px solid var(--line);border-radius:12px;}.node-services h3 {margin:0 0 10px;}.node-services p,.node-services small {display:block;color:var(--text-secondary);font-size:13px;overflow-wrap:anywhere;}.node-services .node-actions {margin-top:16px;}.node-model,.node-operation {padding:16px 0;border-bottom:1px solid var(--line);}.node-model {display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;}.node-model small {display:block;margin-top:6px;color:var(--text-secondary);}.node-operation>div {display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;font-size:14px;}.node-operation progress {width:100%;height:7px;margin-top:12px;}.node-operation span {color:var(--text-secondary);}@media(max-width:600px){.ai-nodes {padding:18px;}.node-model .node-actions {gap:10px;}}
</style>
