<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { api, errorMessage } from '../api'
import { date as displayDate } from '../types'
const date=(value:string|null)=>displayDate(value||undefined)
type Entry = {id:string;kind:string;title:string;author:string;content_count:number;has_update:boolean;enabled:boolean;auto_download:boolean;last_error:string|null;remote_updated_at:string|null}
type State = {enabled:boolean;interval_minutes:number;last_checked_at:string|null;next_check_at:string|null;last_error:string|null;configured:boolean;pending_tasks:number}
type Catalog = {entries:{item_id:string;title:string;position:number;content_state:string}[];complete:boolean}
const emit = defineEmits<{open:[id:string]}>()
const rows=ref<Entry[]>([]),kind=ref('novel'),query=ref(''),updates=ref(false),page=ref(1),total=ref(0)
const loading=ref(false),busy=ref(''),error=ref(''),message=ref(''),sync=ref<State|null>(null)
const selected=ref<Entry|null>(null),catalog=ref<Catalog|null>(null),catalogLoading=ref(false),shown=ref(50)
let revision=0,detailRevision=0,disposed=false,timer:ReturnType<typeof setTimeout>|undefined
async function load(append=false,quiet=false) {
  if(append&&loading.value)return
  const target=append?page.value+1:1,id=++revision
  if(!quiet)loading.value=true
  error.value=''
  const params=new URLSearchParams({kind:kind.value,page:String(target),q:query.value,updates:String(updates.value)})
  try {
    const result=await api<{items:Entry[];total:number;sync:State}>(`/pixiv/watchlist?${params}`)
    if(disposed||id!==revision)return
    if(quiet&&page.value>1){const changed=new Map(result.items.map(row=>[row.id,row]));rows.value=rows.value.map(row=>changed.get(row.id)||row)}
    else{rows.value=append?[...rows.value,...result.items.filter(row=>!rows.value.some(old=>old.id===row.id))]:result.items;page.value=target}
    total.value=result.total;sync.value=result.sync
    clearTimeout(timer)
    if(result.sync.pending_tasks>0)timer=setTimeout(()=>load(false,true),5000)
  }catch(failure){if(!disposed&&id===revision)error.value=errorMessage(failure)}
  finally{if(!disposed&&id===revision)loading.value=false}
}
async function action(path:string,body?:unknown,method='POST') {
  if(busy.value)return
  busy.value=path;error.value='';message.value=''
  try{await api(path,{method,body:JSON.stringify(body??{})});message.value=path.endsWith('/sync')||path.endsWith('/check')?'任务已进入作者与订阅检查队列。':'已保存';await load(false,true)}
  catch(failure){error.value=errorMessage(failure)}finally{busy.value=''}
}
async function detail(row:Entry) {
  selected.value=row;catalog.value=null;shown.value=50;catalogLoading.value=true;const id=++detailRevision
  try{const result=await api<{catalog:Catalog|null}>(`/pixiv/watchlist/${row.id}`);if(!disposed&&id===detailRevision)catalog.value=result.catalog}
  catch(failure){if(!disposed&&id===detailRevision)error.value=errorMessage(failure)}finally{if(id===detailRevision)catalogLoading.value=false}
}
function close(){selected.value=null;detailRevision++}
onMounted(()=>load())
onBeforeUnmount(()=>{disposed=true;revision++;detailRevision++;clearTimeout(timer)})
</script>

<template>
  <section class="panel watchlist-panel">
    <div class="panel-header"><h2>连载追更</h2><button class="button primary" :disabled="!!busy||loading||!sync?.configured" @click="action('/pixiv/watchlist/sync')">同步主账号追更</button></div>
    <form class="watchlist-filters" @submit.prevent="load()"><label>类型<select v-model="kind" @change="load()"><option value="novel">小说</option><option value="manga">漫画</option></select></label><label>搜索<input v-model="query" placeholder="系列名称或作者" maxlength="100" /></label><label class="watchlist-check"><input v-model="updates" type="checkbox" @change="load()" />只看未读更新</label><button class="button secondary" :disabled="loading">查询</button></form>
    <p v-if="sync" class="muted">上次同步：{{date(sync.last_checked_at)}} · {{sync.enabled?`每 ${sync.interval_minutes} 分钟检查`:'已关闭定时同步'}}<template v-if="sync.pending_tasks"> · {{sync.pending_tasks}} 个检查任务等待或运行</template></p>
    <p v-if="sync&&!sync.configured" class="field-hint">请先在账号管理中登录并设置 Pixiv 主账号。</p>
    <p v-if="error||sync?.last_error" class="inline-error" role="alert">{{error||sync?.last_error}}</p><p v-if="message" class="field-hint" role="status">{{message}}</p>
    <p v-if="!rows.length&&!loading" class="muted">{{updates?'暂无未读更新':'暂无追更系列，点击同步读取 Pixiv 官方漫画与小说追更列表。'}}</p>
    <article v-for="row in rows" :key="row.id" class="watchlist-row"><div class="watchlist-description"><button class="text-button" @click="detail(row)"><strong>{{row.title}}</strong></button><span v-if="row.has_update" class="status-pill success">未读更新</span><p>{{row.author}} · {{row.content_count}} 篇 · {{row.auto_download?'自动下载正文':'仅封面与资料'}}{{row.enabled?'':' · 本地已暂停'}}</p><small>更新：{{date(row.remote_updated_at)}}</small><p v-if="row.last_error" class="inline-error">{{row.last_error}}</p></div><div class="watchlist-actions"><button class="button secondary small" :disabled="!!busy||!row.enabled" @click="action(`/pixiv/watchlist/${row.id}/check`)">检查目录</button><button class="text-button" :disabled="!!busy||!row.has_update" @click="action(`/pixiv/watchlist/${row.id}/read`)">标为已读</button><label><input type="checkbox" :checked="row.auto_download" :disabled="!!busy" @change="action(`/pixiv/watchlist/${row.id}`,{auto_download:!row.auto_download},'PUT')" />自动下载</label><button class="text-button" :disabled="!!busy" @click="action(`/pixiv/watchlist/${row.id}`,{enabled:!row.enabled},'PUT')">{{row.enabled?'暂停追更':'恢复追更'}}</button></div></article>
    <button v-if="rows.length<total" class="button secondary" :disabled="loading" @click="load(true)">{{loading?'加载中…':'加载更多'}}（{{rows.length}} / {{total}}）</button><p v-else-if="loading" class="muted" role="status">加载中…</p>
    <div v-if="sync" class="watchlist-settings"><label><input type="checkbox" :checked="sync.enabled" :disabled="!!busy" @change="action('/pixiv/watchlist/settings',{enabled:!sync.enabled,interval_minutes:sync.interval_minutes},'PUT')" />定时同步追更列表</label><span class="muted">修改远端追更列表请在 Pixiv 操作；本地暂停、已读和自动下载设置不会修改 Pixiv。</span></div>
  </section>
  <section v-if="selected" class="panel watchlist-catalog"><div class="panel-header"><h2>{{selected.title}}</h2><button class="button secondary small" @click="close">关闭目录</button></div><p v-if="catalogLoading" role="status">加载目录…</p><template v-else><p v-if="!catalog?.entries.length" class="muted">目录正在排队检查，稍后点击系列名称刷新。</p><p v-else-if="!catalog.complete" class="field-hint">当前目录尚不完整。</p><button v-for="entry in catalog?.entries.slice(0,shown)" :key="entry.item_id" class="watchlist-chapter text-button" @click="emit('open',entry.item_id)">{{entry.position+1}}. {{entry.title}} <small>{{entry.content_state==='ready'?'已保存':'待下载'}}</small></button><button v-if="catalog&&shown<catalog.entries.length" class="button secondary small" @click="shown+=50">更多章节</button></template></section>
</template>

<style scoped>
.watchlist-filters input:not([type=checkbox]),.watchlist-filters select{min-height:42px;border:1px solid var(--border,#d9dce7);border-radius:8px;padding:9px 12px;background:var(--surface,#fff);color:inherit;font:inherit}.watchlist-filters input:not([type=checkbox]){width:260px;max-width:100%}.watchlist-filters label{font-size:14px}.watchlist-filters button{min-height:42px}
.watchlist-panel,.watchlist-catalog{padding:24px}.watchlist-filters{display:flex;align-items:end;gap:16px;flex-wrap:wrap;margin:20px 0}.watchlist-filters label{display:grid;gap:6px}.watchlist-check{display:flex!important;align-items:center;align-self:center}.watchlist-row{display:flex;justify-content:space-between;gap:20px;padding:20px 0;border-bottom:1px solid var(--border,#e3e5ed)}.watchlist-description{min-width:0}.watchlist-description strong{font-size:16px;overflow-wrap:anywhere}.watchlist-description p{margin:8px 0}.watchlist-actions{display:flex;align-items:center;justify-content:flex-end;gap:12px;flex-wrap:wrap;flex-shrink:0;max-width:400px}.watchlist-actions label,.watchlist-settings label{display:flex;gap:6px;align-items:center}.watchlist-settings{display:grid;gap:12px;margin-top:24px}.watchlist-catalog{margin-top:20px}.watchlist-chapter{display:block;text-align:left;padding:12px 0;width:100%;overflow-wrap:anywhere}.watchlist-chapter small{margin-left:12px}@media(max-width:700px){.watchlist-panel,.watchlist-catalog{padding:16px}.watchlist-row{flex-direction:column}.watchlist-actions{justify-content:flex-start;max-width:none}.watchlist-filters{gap:12px}}
</style>
