<script setup lang="ts">
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { LoaderCircle, UsersRound } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { SourceAccount } from '../types'
type Status = {id: string | null; label: string | null; status: 'verified' | 'unverified' | 'missing' | 'unconfigured'; verified_at: string | null}
type Config = {enabled: boolean; main_account_id: string | null; content_account_id: string | null; content_account_ids?: string[]; main: Status; content: Status}
const props = defineProps<{accounts: SourceAccount[]; compact?: boolean}>()
const emit = defineEmits<{change: [config: Config]; login: []; manage: []}>()
const state = ref<Config | null>(null)
const form = reactive({enabled: false, main_account_id: '', content_account_id: ''})
const busy = ref(false), loading = ref(true), error = ref(''), message = ref(''), saved = ref('')
const dirty = computed(() => JSON.stringify(form) !== saved.value)
let request = 0
const names = {verified: '已登录', unverified: '凭据未验证', missing: '账号已移除', unconfigured: '未选择账号'}
function apply(config: Config) {
  state.value = config
  Object.assign(form, {enabled: config.enabled, main_account_id: config.main_account_id || '', content_account_id: config.content_account_id || ''})
  saved.value = JSON.stringify(form)
  emit('change', config)
}
async function load() {
  const current = ++request
  loading.value = true; error.value = ''
  try { const config = await api<Config>('/sources/pixiv/account-roles'); if (current === request) apply(config) }
  catch (e) { if (current === request) error.value = errorMessage(e) }
  finally { if (current === request) loading.value = false }
}
async function save() {
  if (busy.value || loading.value) return
  ++request
  busy.value = true; error.value = ''; message.value = ''
  try {
    apply(await api<Config>('/sources/pixiv/account-roles', {method: 'PUT', body: JSON.stringify({
      enabled: form.enabled, main_account_id: form.main_account_id || null, content_account_id: form.content_account_id || null,
    })}))
    message.value = form.enabled ? '已启用账号分工，请在账号管理中配置各项功能。' : '已关闭账号分工，恢复原有账号选择。'
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
// Login/removal changes the two displayed statuses without discarding unsaved selections.
watch(() => props.accounts, async () => {
  if (!dirty.value && !busy.value) await load()
}, {deep: true})
onMounted(load)
</script>

<template>
  <section class="pixiv-account-roles">
    <h3><UsersRound :size="17" />主副账号分工</h3>
    <div v-if="loading" class="field-hint">读取账号分工…</div>
    <template v-if="state">
      <div v-if="compact" class="roles-summary"><p class="field-hint">{{ state.enabled ? '已启用账号分工' : '账号分工未启用' }} · 主账号：{{ state.main.label || '未选择' }} · 图片下载账号：{{ state.content_account_ids?.length ?? (state.content_account_id ? 1 : 0) }} 个</p><button class="button secondary small" @click="emit('manage')">管理账号分工</button></div>
      <template v-else>
      <div class="role-statuses">
        <div v-for="(role, key) in {main: state.main, content: state.content}" :key="key" class="role-status">
          <strong>{{ key === 'main' ? '主账号 · 检索作品' : '副账号 · 图片下载' }}</strong>
          <span>{{ role.label || '—' }}</span>
          <small class="status-pill" :class="role.status === 'verified' ? 'success' : role.status === 'missing' ? 'failed' : 'neutral'" :title="role.status === 'verified' ? '添加账号时通过登录验证；实际会话可能过期，任务失败时请重新登录并选择。' : ''">{{ names[role.status] }}</small>
        </div>
      </div>
      <form @submit.prevent="save">
        <label class="role-enabled"><input v-model="form.enabled" type="checkbox" :disabled="busy || loading" />启用主副账号分工</label>
        <label class="field">主账号<select v-model="form.main_account_id" :disabled="busy || loading" :required="form.enabled"><option value="">请选择检索账号</option><option v-for="account in accounts" :key="account.id" :value="account.id">{{ account.label }}</option></select></label>
        <label class="field">图片下载账号<select v-model="form.content_account_id" :disabled="busy || loading" :required="form.enabled"><option value="">请选择下载账号</option><option v-for="account in accounts" :key="account.id" :value="account.id" :disabled="account.id === form.main_account_id">{{ account.label }}</option></select></label>
        <p class="field-hint">主账号检索作品、同步关注及作者／收藏／系列目录；图片下载账号获取原图。作品详情／小说正文和作者资料需在账号管理中另外选择。未配置对应功能时不会改用主账号。</p>
        <div class="role-actions"><button class="button secondary small" :disabled="busy || loading || !dirty"><LoaderCircle v-if="busy" class="spin" :size="15" />保存分工</button><button type="button" class="text-button" :disabled="busy" @click="emit('login')">登录其他账号</button></div>
        <p v-if="accounts.length < 2" class="field-hint">需要两个 Pixiv 账号。授权副账号前，请在 Pixiv 官方页切换到该账号。</p>
      </form>
      </template>
    </template>
    <div v-if="error" class="alert error" role="alert">{{ error }}<button v-if="!state" class="text-button" @click="load">重试</button></div>
    <div v-if="message" class="alert info" role="status">{{ message }}</div>
  </section>
</template>

<style scoped>
.pixiv-account-roles {margin:16px 0;padding:16px 0;border-top:1px solid var(--border, #e3e5ed);border-bottom:1px solid var(--border, #e3e5ed);}
h3 {display:flex;align-items:center;gap:8px;font-size:15px;margin:0 0 12px;}
.role-statuses {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;margin-bottom:12px;}
.role-status {display:flex;align-items:flex-start;flex-direction:column;gap:6px;font-size:13px;min-width:0;}.role-status span {overflow-wrap:anywhere;}
.role-enabled {display:flex;align-items:center;gap:8px;font-size:14px;margin:12px 0;}.field {margin-top:10px;}.role-actions {display:flex;align-items:center;flex-wrap:wrap;gap:12px;}.alert {margin-top:12px;}
</style>
