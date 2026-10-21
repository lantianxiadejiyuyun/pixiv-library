<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, errorMessage } from '../api'
import type { Subscription, Tag } from '../types'
import { date, sourceLabels } from '../types'
const props = defineProps<{tags?:Tag[]}>()
const rows=ref<Subscription[]>([]), target=ref(''), platform=ref('pixiv'), scope=ref('all'), download=ref(true), interval=ref(360)
const busy=ref(false), error=ref(''), status=ref('')
async function load(){rows.value=(await api<{items:Subscription[]}>('/tag-tracking')).items}
async function action(work:()=>Promise<unknown>,message='已保存'){
  if(busy.value)return
  busy.value=true;error.value='';status.value=''
  try{await work();await load();status.value=message}catch(e){error.value=errorMessage(e)}finally{busy.value=false}
}
async function create(selected=false){
  const tags=selected?(props.tags||[]).map(t=>t.name):target.value.split(/[\n,，]+/).map(t=>t.trim()).filter(Boolean)
  if(!tags.length||tags.length>20){error.value='请填写 1～20 个标签，每行一个';return}
  await action(()=>api('/tag-tracking',{method:'POST',body:JSON.stringify({platform:platform.value,tags,auto_download:download.value,content_scope:scope.value,interval_minutes:interval.value})}),'追踪已保存，检查任务已排队')
  if(!error.value)target.value=''
}
function patch(row:Subscription,changes:object){void action(()=>api(`/tag-tracking/${row.id}`,{method:'PATCH',body:JSON.stringify(changes)}))}
onMounted(()=>action(load,''))
</script>
<template>
  <section class="panel tag-tracking-panel">
    <div class="panel-header"><h2>标签追踪与下载</h2><span class="muted">{{rows.length}} 条</span></div>
    <form class="tracking-form" @submit.prevent="create()">
      <label class="field">平台<select v-model="platform"><option value="pixiv">Pixiv</option><option value="picacg">哔咔 PicACG</option><option value="kemono">Kemono</option></select></label>
      <label v-if="platform==='pixiv'" class="field">内容<select v-model="scope"><option value="all">插画、漫画与小说</option><option value="images">插画 / 漫画</option><option value="novels">小说</option></select></label>
      <label class="field tracking-tags">标签<textarea v-model="target" placeholder="每行一个原文标签，最多 20 个" aria-label="追踪标签" rows="2" maxlength="4000" /></label>
      <label class="field">检查间隔（分钟）<input v-model.number="interval" type="number" min="30" max="10080" required /></label>
      <div class="tracking-controls"><label class="checkbox-label"><input v-model="download" type="checkbox" />自动下载匹配作品</label>
      <div class="tracking-actions"><button class="button primary" :disabled="busy||!target.trim()">添加追踪</button><button type="button" class="button secondary" :disabled="busy||!tags?.length" @click="create(true)">追踪已选标签{{tags?.length?` (${tags.length})`:''}}</button></div></div>
    </form>
    <p class="muted tracking-help">每个标签独立追踪；每次检查最新一页。检索使用主账号，下载遵守账号权限和队列并发。</p>
    <p v-if="error" class="inline-error" role="alert">{{error}}</p><p v-if="status" role="status">{{status}}</p>
    <div v-for="row in rows" :key="row.id" class="tracking-row">
      <div><strong># {{row.target}}</strong><small>{{sourceLabels[row.platform]}} · {{row.enabled?'追踪中':'已暂停'}} · 上次检查 {{date(row.last_checked_at)}}</small><p v-if="row.last_error" class="inline-error">{{row.last_error}}</p></div>
      <div class="tracking-actions"><label class="checkbox-label"><input type="checkbox" :checked="row.auto_download" :disabled="busy" @change="patch(row,{auto_download:!row.auto_download})" />自动下载</label><button class="button secondary small" :disabled="busy||!row.enabled" @click="action(()=>api(`/tag-tracking/${row.id}/check`,{method:'POST'}),'检查任务已排队')">立即检查</button><button class="text-button" :disabled="busy" @click="patch(row,{enabled:!row.enabled})">{{row.enabled?'暂停':'恢复'}}</button><button class="text-button danger-text" :disabled="busy" @click="action(()=>api(`/tag-tracking/${row.id}`,{method:'DELETE'}),'已取消追踪，已下载作品保留')">取消追踪</button></div>
    </div>
  </section>
</template>
<style scoped>
.tag-tracking-panel{margin-bottom:24px}.tracking-form{display:grid;grid-template-columns:1fr 1.3fr 2fr 1fr;gap:16px;align-items:end;padding:24px}.tracking-form .field{min-width:0}.tracking-form textarea{width:100%;resize:vertical;padding:10px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--text);font:inherit}.tracking-controls{grid-column:1/-1;display:flex;gap:16px;align-items:center;justify-content:space-between;flex-wrap:wrap}.tracking-actions{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.tracking-help{margin:0;padding:0 24px 24px}.tracking-row{display:flex;gap:20px;justify-content:space-between;align-items:center;padding:16px 0;margin:0 24px;border-top:1px solid var(--line)}.tracking-row>div:first-child{min-width:0;overflow-wrap:anywhere}.tracking-row small{display:block;margin-top:6px;color:var(--muted)}.tag-tracking-panel>.inline-error,.tag-tracking-panel>[role=status]{margin:0 24px 16px}@media(max-width:1000px){.tracking-form{grid-template-columns:repeat(2,minmax(0,1fr))}}@media(max-width:600px){.tracking-form{grid-template-columns:minmax(0,1fr);padding:16px}.tracking-help{padding:0 16px 16px}.tracking-row{align-items:stretch;flex-direction:column;margin:0 16px}.tracking-controls{align-items:stretch;flex-direction:column}}
</style>
