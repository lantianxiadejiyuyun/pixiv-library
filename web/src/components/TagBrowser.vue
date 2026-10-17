<script setup lang="ts">
import { ref, watch, onMounted, onBeforeUnmount } from 'vue'
import { api, errorMessage } from '../api'
import type { Tag } from '../types'
const emit = defineEmits<{browse:[name:string]}>()
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
  <section class="panel tag-panel">
    <div class="panel-header"><h2>内容标签 <span class="muted">已加载 {{rows.length}}</span></h2><form class="search-field" @submit.prevent="load()"><input v-model="query" placeholder="搜索全部标签…" aria-label="搜索全部标签" maxlength="191" /></form></div>
    <p v-if="error" class="inline-error" role="alert">{{error}} <button class="text-button" @click="load()">重试</button></p>
    <div v-if="rows.length" class="tag-cloud"><button v-for="tag in rows" :key="tag.id" class="tag-tile" @click="emit('browse',tag.name)"><span class="tag-hash">#</span><span>{{tag.name_zh||tag.name}}</span><span class="tag-count">{{tag.count||0}}</span></button></div>
    <p v-else-if="!loading&&!error" class="muted">{{query?'没有匹配的标签':'暂无标签'}}</p>
    <p v-if="loading" class="muted" role="status">正在加载…</p>
    <button v-if="more" class="button secondary" :disabled="loading" @click="load(true)">加载更多</button>
  </section>
</template>
