<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { api, errorMessage } from '../api'
import type { Tag } from '../types'
import TagTrackingPanel from './TagTrackingPanel.vue'
const props = defineProps<{ selected?: Tag[]; mode?: 'all' | 'any' }>()
const emit = defineEmits<{browse:[tags:Tag[], mode:'all'|'any']}>()
const selected=ref<Tag[]>([...(props.selected || [])]), mode=ref<'all'|'any'>(props.mode || 'all')
function toggle(tag:Tag) {
  if (selected.value.some(old=>old.id===tag.id)) selected.value=selected.value.filter(old=>old.id!==tag.id)
  else if(selected.value.length<20) selected.value=[...selected.value,tag]
}
const rows=ref<Tag[]>([]),query=ref(''),loading=ref(false),error=ref(''),more=ref(false),offset=ref(0)
let revision=0,disposed=false,timer:ReturnType<typeof setTimeout>|undefined
async function load(append=false) {
  if(append&&loading.value)return
  const request=++revision,target=append?offset.value:0
  loading.value=true;error.value=''
  const params=new URLSearchParams({q:query.value.trim(),limit:'200',offset:String(target)})
  try {
    const result=await api<Tag[]>(`/tags?${params}`)
    if(disposed||request!==revision)return
    rows.value=append?[...rows.value,...result.filter(tag=>!rows.value.some(old=>old.id===tag.id))]:result
    offset.value=target+result.length;more.value=result.length===200
  } catch(failure) { if(!disposed&&request===revision)error.value=errorMessage(failure) }
  finally { if(!disposed&&request===revision)loading.value=false }
}
watch(query,()=>{revision++;clearTimeout(timer);timer=setTimeout(()=>load(),350)})
onMounted(()=>load())
onBeforeUnmount(()=>{disposed=true;revision++;clearTimeout(timer)})
</script>
<template>
  <TagTrackingPanel :tags="selected" />
  <section class="panel tag-panel">
    <div class="panel-header"><h2>内容标签 <span class="muted">已加载 {{rows.length}}</span></h2><form class="search-field" @submit.prevent="load()"><input v-model="query" placeholder="搜索全部标签…" aria-label="搜索全部标签" maxlength="191" /></form></div>
    <div class="tag-selection">
      <span>已选 {{selected.length}} / 20</span>
      <select v-model="mode" aria-label="标签匹配方式"><option value="all">全部匹配</option><option value="any">匹配任一</option></select>
      <button class="text-button" :disabled="!selected.length" @click="selected=[]">清空选择</button>
      <button class="button primary" @click="emit('browse',selected,mode)">查看作品</button>
    </div>
    <div v-if="selected.length" class="tag-selection"><button v-for="tag in selected" :key="tag.id" class="tag-filter" :aria-label="`移除标签 ${tag.name_zh||tag.name}`" @click="toggle(tag)">{{tag.name_zh||tag.name}} ×</button></div>
    <p v-if="error" class="inline-error" role="alert">{{error}} <button class="text-button" @click="load()">重试</button></p>
    <div v-if="rows.length" class="tag-cloud"><button v-for="tag in rows" :key="tag.id" class="tag-tile" :class="{selected:selected.some(old=>old.id===tag.id)}" :aria-pressed="selected.some(old=>old.id===tag.id)" :disabled="selected.length>=20&&!selected.some(old=>old.id===tag.id)" @click="toggle(tag)"><span class="tag-hash">{{selected.some(old=>old.id===tag.id)?'✓':'#'}}</span><span>{{tag.name_zh||tag.name}}</span><span class="tag-count">{{tag.count||0}}</span></button></div>
    <p v-else-if="!loading&&!error" class="muted">{{query?'没有匹配的标签':'暂无标签'}}</p>
    <p v-if="loading" class="muted" role="status">正在加载…</p>
    <button v-if="more" class="button secondary" :disabled="loading" @click="load(true)">加载更多</button>
  </section>
</template>
<style scoped>
.tag-selection{display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin:0 24px 18px}.tag-selection select{padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--surface);color:var(--text)}.tag-tile.selected{border-color:var(--accent);background:var(--accent-soft)}
</style>
