<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { AlertCircle, ArrowRight, Check, CheckCircle2, ChevronDown, CircleHelp, Globe2, KeyRound, LoaderCircle, Network, RadioTower, RefreshCw, Save, Server, ShieldCheck, Timer, Waypoints, X } from 'lucide-vue-next'
import { api, post, errorMessage } from '../api'
import { sourceLabels, remotePlatforms } from '../types'

interface ProxyConfig {enabled: boolean; proxy_url: string; proxy_username: string; has_proxy_password: boolean; controller_url: string; has_controller_secret: boolean}
interface ClashNode {name: string; type: string; alive: boolean | null; delay: number | null}
interface ClashGroup {name: string; type: string; now: string; nodes: ClashNode[]}
interface ProbeResult {ok: boolean; reachable: boolean; status_code: number | null; elapsed_ms: number; message: string; via_proxy: boolean}
defineProps<{managedEnabled?: boolean}>()
const config = ref<ProxyConfig | null>(null)
const form = reactive({enabled: false, proxy_url: '', proxy_username: '', proxy_password: '', controller_url: '', controller_secret: ''})
const loading = ref(true)
const action = ref('')
const error = ref('')
const success = ref('')
const clearPassword = ref(false)
const clearSecret = ref(false)
const authExpanded = ref(false)
const groups = ref<ClashGroup[]>([])
const groupsLoaded = ref(false)
const selectedGroup = ref('')
const selectedNode = ref('')
const platform = ref('pixiv')
const probePlatform = ref('pixiv')
const probe = ref<ProbeResult | null>(null)
const currentGroup = computed(() => groups.value.find(group => group.name === selectedGroup.value))
const busy = computed(() => !!action.value || loading.value)
const dirty = computed(() => !config.value || form.enabled !== config.value.enabled || form.proxy_url !== config.value.proxy_url || form.proxy_username !== config.value.proxy_username || form.controller_url !== config.value.controller_url || !!form.proxy_password || !!form.controller_secret || clearPassword.value || clearSecret.value)
const controllerReady = computed(() => !!config.value?.controller_url && !dirty.value)
const probeStatus = computed(() => !probe.value ? '' : probe.value.ok ? '连接正常' : probe.value.reachable ? '可连接，平台返回异常' : '连接失败')

function setConfig(value: ProxyConfig) {
  config.value = value
  Object.assign(form, {enabled: value.enabled, proxy_url: value.proxy_url || '', proxy_username: value.proxy_username || '', proxy_password: '', controller_url: value.controller_url || '', controller_secret: ''})
  clearPassword.value = false; clearSecret.value = false
  authExpanded.value = !!value.proxy_username || value.has_proxy_password
}
function resetFeedback() { error.value = ''; success.value = '' }
async function load() {
  loading.value = true; resetFeedback()
  try { setConfig(await api<ProxyConfig>('/network/proxy')) }
  catch (err) { error.value = errorMessage(err) }
  finally { loading.value = false }
}
async function save() {
  action.value = 'save'; resetFeedback(); probe.value = null
  try {
    const payload = {enabled: form.enabled, proxy_url: form.proxy_url.trim(), proxy_username: form.proxy_username.trim(), controller_url: form.controller_url.trim(),
      ...(clearPassword.value ? {proxy_password: ''} : form.proxy_password ? {proxy_password: form.proxy_password} : {}),
      ...(clearSecret.value ? {controller_secret: ''} : form.controller_secret ? {controller_secret: form.controller_secret} : {})}
    setConfig(await api<ProxyConfig>('/network/proxy', {method: 'PUT', body: JSON.stringify(payload)}))
    groups.value = []; groupsLoaded.value = false; selectedGroup.value = ''; selectedNode.value = ''
    success.value = '网络配置已保存，新请求将使用此配置。'
  } catch (err) { error.value = errorMessage(err) }
  finally { form.proxy_password = ''; form.controller_secret = ''; action.value = '' }
}
async function loadGroups() {
  action.value = 'groups'; resetFeedback()
  try {
    const result = await api<{groups: ClashGroup[]}>('/network/clash/groups')
    groups.value = result.groups; groupsLoaded.value = true
    if (!groups.value.some(group => group.name === selectedGroup.value)) selectedGroup.value = groups.value[0]?.name || ''
    selectedNode.value = currentGroup.value?.now || ''
  } catch (err) { error.value = errorMessage(err) }
  finally { action.value = '' }
}
function groupChanged() { selectedNode.value = currentGroup.value?.now || '' }
async function selectNode() {
  if (!currentGroup.value || !selectedNode.value) return
  action.value = 'select'; resetFeedback(); probe.value = null
  try {
    const result = await post<{ok: boolean; group: string; node: string}>('/network/clash/select', {group: selectedGroup.value, node: selectedNode.value})
    if (!result.ok) throw new Error('节点切换未完成，请刷新状态后重试。')
    const group = groups.value.find(value => value.name === result.group)
    if (group) group.now = result.node
    success.value = `「${result.group}」已切换至「${result.node}」。`
  } catch (err) { error.value = errorMessage(err) }
  finally { action.value = '' }
}
async function testConnection() {
  action.value = 'test'; resetFeedback(); probe.value = null
  probePlatform.value = platform.value
  try { probe.value = await post<ProbeResult>('/network/proxy/test', {platform: platform.value}) }
  catch (err) { error.value = errorMessage(err) }
  finally { action.value = '' }
}
onMounted(load)
</script>

<template>
  <section class="panel network-settings" aria-labelledby="network-heading">
    <div class="network-heading"><div class="settings-title"><Network :size="22" /><div><h2 id="network-heading">外部代理与外部 Clash</h2></div></div><span v-if="config" class="status-pill" :class="config.enabled ? 'success' : 'neutral'"><span class="status-dot" :class="{offline: !config.enabled}"></span>{{ managedEnabled ? '内置出口优先' : config.enabled ? '外部代理已启用' : '外部代理未启用' }}</span><button class="icon-button" aria-label="重新读取代理配置" :disabled="busy || dirty && !!config" @click="load"><RefreshCw :size="16" :class="{spin: loading}" /></button></div>
    <div v-if="loading" class="inline-loading"><LoaderCircle :size="18" class="spin" />正在读取网络配置…</div>
    <template v-else-if="config"><div v-if="managedEnabled" class="alert info external-priority-note"><CircleHelp :size="17" /><span>内置代理已开启，外部配置暂不生效。关闭内置代理后使用外部配置。</span></div>
      <form class="proxy-form" @submit.prevent="save"><div class="network-enable"><div><strong>启用外部代理配置</strong></div><label class="toggle"><input v-model="form.enabled" type="checkbox" aria-label="启用外部代理配置" :disabled="busy" /><span></span></label></div><div class="network-columns"><div class="proxy-address-section"><div class="network-section-label"><Globe2 :size="15" /><span>内容访问代理</span></div><label class="field">HTTP / SOCKS5 代理地址<input v-model="form.proxy_url" type="text" placeholder="http://127.0.0.1:7890" spellcheck="false" autocomplete="off" :required="form.enabled" :disabled="busy" /></label><p class="field-hint">支持 http / https / socks5 / socks5h，由书库服务连接。</p><button type="button" class="advanced-toggle proxy-auth-toggle" :aria-expanded="authExpanded" :disabled="busy" @click="authExpanded = !authExpanded"><KeyRound :size="14" /><span>代理身份验证 <small v-if="config.has_proxy_password">已保存密码</small></span><ChevronDown :size="14" :class="{expanded: authExpanded}" /></button><div v-if="authExpanded" class="proxy-auth-fields"><label class="field">代理用户名<input v-model="form.proxy_username" placeholder="不需要认证时留空" autocomplete="off" :disabled="busy" /></label><label class="field">代理密码<input v-model="form.proxy_password" type="password" :placeholder="config.has_proxy_password ? '已有密码，留空保留' : '不需要认证时留空'" autocomplete="new-password" :disabled="busy || clearPassword" /></label><label v-if="config.has_proxy_password" class="checkbox-label secret-clear"><input v-model="clearPassword" type="checkbox" :disabled="busy" />清除已保存的代理密码</label></div></div><div class="clash-address-section"><div class="network-section-label"><Server :size="15" /><span>Clash 控制接口 <small>选填</small></span></div><label class="field">REST Controller 地址<input v-model="form.controller_url" type="text" placeholder="http://127.0.0.1:9090" spellcheck="false" autocomplete="off" :disabled="busy" /></label><p class="field-hint">填写 Clash / Mihomo 的 external-controller 地址。</p><label class="field">Controller Secret <span class="optional">{{ config.has_controller_secret ? '已加密保存' : '选填' }}</span><input v-model="form.controller_secret" type="password" :placeholder="config.has_controller_secret ? '已有 Secret，留空保留' : '填写 Clash 配置中的 secret'" autocomplete="new-password" :disabled="busy || clearSecret" /></label><label v-if="config.has_controller_secret" class="checkbox-label secret-clear"><input v-model="clearSecret" type="checkbox" :disabled="busy" />清除已保存的 Controller Secret</label></div></div><div class="network-deployment-note"><CircleHelp :size="16" /><p>电脑本机可使用 <code>127.0.0.1</code>；Docker 访问宿主机可使用 <code>http://host.docker.internal:7890</code>；NAS / 局域网部署请填写运行 Clash 的设备 IP，并允许局域网连接。</p></div><div class="network-save-row"><span><ShieldCheck :size="13" />密码和 Secret 加密保存，读取时不会回显。</span><button class="button primary small" :disabled="busy || !dirty"><LoaderCircle v-if="action === 'save'" :size="15" class="spin" /><Save v-else :size="15" />{{ action === 'save' ? '正在保存' : '保存网络配置' }}</button></div></form>

      <div class="clash-node-section"><div class="network-subheading"><div><h3><Waypoints :size="17" />Clash 策略组与节点</h3></div><button class="button secondary small" :disabled="busy || !controllerReady" @click="loadGroups"><LoaderCircle v-if="action === 'groups'" :size="14" class="spin" /><RefreshCw v-else :size="14" />{{ groupsLoaded ? '刷新节点' : '加载节点' }}</button></div><div v-if="dirty" class="network-inline-note"><Save :size="15" />请先保存网络配置，再加载节点或测试连接。</div><div v-else-if="!config.controller_url" class="network-inline-note"><RadioTower :size="16" />填写并保存 Controller 地址后，即可读取策略组。</div><div v-else-if="groupsLoaded && !groups.length" class="network-inline-note"><CircleHelp :size="16" />该控制接口未返回可选择的策略组。</div><template v-else-if="groups.length"><div class="clash-group-row"><label class="field">策略组<select v-model="selectedGroup" :disabled="busy" @change="groupChanged"><option v-for="group in groups" :key="group.name" :value="group.name">{{ group.name }} · {{ group.type }}</option></select></label><div v-if="currentGroup" class="current-node"><span>当前节点</span><strong>{{ currentGroup.now || '尚未选择' }}</strong></div></div><div v-if="currentGroup" class="node-list"><label v-for="node in currentGroup.nodes" :key="node.name" class="node-choice" :class="{selected: selectedNode === node.name, current: currentGroup.now === node.name}"><input v-model="selectedNode" type="radio" :value="node.name" name="clash-node" :disabled="busy || dirty" /><span class="node-choice-name">{{ node.name }}<small>{{ node.type }}</small></span><span v-if="currentGroup.now === node.name" class="node-current-badge"><Check :size="11" />当前</span><span class="node-delay" :class="{'unreachable': node.alive === false}"><Timer v-if="node.delay != null && node.delay > 0" :size="11" />{{ node.alive === false ? '不可达' : node.delay != null && node.delay > 0 ? `${node.delay} ms` : '未测速' }}</span></label></div><div class="node-switch-row"><span v-if="currentGroup && selectedNode && selectedNode !== currentGroup.now">{{ currentGroup.now || '未选择' }}<ArrowRight :size="13" />{{ selectedNode }}</span><button class="button primary small" :disabled="busy || dirty || !selectedNode || selectedNode === currentGroup?.now" @click="selectNode"><LoaderCircle v-if="action === 'select'" class="spin" :size="15" /><Waypoints v-else :size="15" />切换到此节点</button></div></template><div class="clash-impact-note"><AlertCircle :size="14" /><p>切换会影响该 Clash 实例的其他客户端。规则模式仍遵循 Clash 自身路由。</p></div></div>

      <div class="network-test-section"><div class="network-subheading"><div><h3><RadioTower :size="17" />平台连通性测试</h3></div></div><div class="network-test-controls"><label class="field"><span class="sr-only">测试平台</span><select v-model="platform" :disabled="busy"><option v-for="name in remotePlatforms" :key="name" :value="name">{{ sourceLabels[name] }}</option></select></label><button class="button secondary small" :disabled="busy || dirty" @click="testConnection"><LoaderCircle v-if="action === 'test'" class="spin" :size="15" /><Globe2 v-else :size="15" />{{ action === 'test' ? '正在测试连接' : '测试连接' }}</button><span class="test-route-label">{{ managedEnabled ? '当前使用内置代理' : config.enabled ? '通过外部代理' : '使用直连' }}</span></div><div v-if="probe && !dirty" class="network-test-result" :class="probe.ok ? 'test-success' : probe.reachable ? 'test-warning' : 'test-failed'" role="status"><component :is="probe.ok ? CheckCircle2 : AlertCircle" :size="20" /><div><strong>{{ sourceLabels[probePlatform] }} · {{ probeStatus }}</strong><p>{{ probe.message }}</p><div><span>{{ probe.via_proxy ? '代理连接' : '直接连接' }}</span><span>{{ Math.round(probe.elapsed_ms) }} ms</span><span v-if="probe.status_code != null">HTTP {{ probe.status_code }}</span></div></div></div></div>
    </template>
    <div v-if="error" class="alert error network-feedback" role="alert"><AlertCircle :size="17" /><span>{{ error }}</span><button v-if="!config" class="text-button" @click="load">重新读取</button><button class="icon-button" aria-label="关闭错误提示" @click="error = ''"><X :size="14" /></button></div><div v-if="success" class="alert info network-feedback" role="status"><CheckCircle2 :size="17" /><span>{{ success }}</span><button class="icon-button" aria-label="关闭提示" @click="success = ''"><X :size="14" /></button></div>
  </section>
</template>
