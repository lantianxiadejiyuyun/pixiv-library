<script setup lang="ts">
import { ref } from 'vue'
import { Heart, LoaderCircle } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { Item, ItemPreferencePatch } from '../types'
import ScoreControl from './ScoreControl.vue'
const props = defineProps<{item: Item}>()
const emit = defineEmits<{change: [patch: ItemPreferencePatch]}>()
const open = ref(false), busy = ref(false), loading = ref(false), error = ref('')
const groups = ref<{id: string; name: string}[]>([]), selected = ref<string[]>([])
let request = 0
async function edit() {
  open.value = true; loading.value = true; error.value = ''
  const current = ++request, id = props.item.id
  selected.value = [...(props.item.favorite_group_ids || [])]
  try {
    const data = await api<{items: {id: string; name: string}[]}>('/favorite-groups')
    if (current !== request || props.item.id !== id) return
    groups.value = data.items; selected.value = selected.value.filter(id => groups.value.some(g => g.id === id))
  } catch (e) { if (current === request) error.value = errorMessage(e) }
  finally { if (current === request) loading.value = false }
}
async function save(favorite: boolean) {
  if (busy.value || loading.value) return
  busy.value = true; error.value = ''; const item = props.item
  try {
    const result = await api<Pick<Item, 'is_favorite' | 'favorite_group_ids'>>(`/items/${encodeURIComponent(item.id)}/favorite`, {method: 'PUT', body: JSON.stringify({favorite, ...(favorite ? {group_ids: selected.value} : {})})})
    if (props.item.id !== item.id) return
    emit('change', {id: item.id, is_favorite: result.is_favorite, favorite_group_ids: result.favorite_group_ids}); open.value = false
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
</script>
<template>
  <section class="favorite-control">
    <div class="preference-actions"><button class="button secondary small" :disabled="busy" @click="edit"><Heart :size="16" :fill="item.is_favorite ? 'currentColor' : 'none'" />{{ item.is_favorite ? '管理收藏' : '收藏作品' }}</button><ScoreControl :key="item.id" :item="item" @change="emit('change', $event)" /></div>
    <div v-if="open" class="favorite-editor">
      <strong>收藏分组</strong>
      <p v-if="loading" class="field-hint">正在读取分组…</p>
      <div v-else class="favorite-group-options"><label v-for="group in groups" :key="group.id"><input v-model="selected" type="checkbox" :value="group.id" :disabled="busy" />{{ group.name }}</label></div>
      <p class="field-hint">可选择多个分组；不选分组时保存在「未分组」。</p>
      <p v-if="error" class="alert error" role="alert">{{ error }}</p>
      <div class="favorite-editor-actions"><button v-if="item.is_favorite" class="text-button danger-text" :disabled="busy || loading" @click="save(false)">取消收藏</button><button class="button secondary small" :disabled="busy" @click="open = false">收起</button><button class="button primary small" :disabled="busy || loading || (!!error && !groups.length)" @click="save(true)"><LoaderCircle v-if="busy" :size="15" class="spin" />保存收藏</button></div>
    </div>
  </section>
</template>
<style scoped>
.favorite-control {margin:18px 0;}.preference-actions {display:flex;gap:10px;flex-wrap:wrap;align-items:flex-start;}.favorite-editor {margin-top:12px;border:1px solid var(--line);border-radius:12px;padding:16px;background:var(--surface);}.favorite-editor-actions,.favorite-group-options {display:flex;gap:10px;flex-wrap:wrap;align-items:center;}.favorite-group-options {margin-top:12px;}.favorite-group-options label {display:flex;gap:7px;align-items:center;border:1px solid var(--line);padding:7px 10px;border-radius:7px;}.favorite-editor-actions {justify-content:flex-end;}.field-hint {margin:10px 0;}
</style>
