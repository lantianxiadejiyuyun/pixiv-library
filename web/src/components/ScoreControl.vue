<script setup lang="ts">
import { ref } from 'vue'
import { Star, LoaderCircle } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
import type { Item, ItemPreferencePatch } from '../types'

const props = defineProps<{item: Item}>()
const emit = defineEmits<{change: [patch: ItemPreferencePatch]}>()
const open = ref(false), busy = ref(false), error = ref(''), score = ref(0)

function edit() {
  score.value = props.item.user_score || 0
  error.value = ''; open.value = true
}

async function save() {
  if (busy.value) return
  busy.value = true; error.value = ''
  const id = props.item.id
  try {
    const result = await api<Pick<Item, 'id' | 'user_score'>>(`/items/${encodeURIComponent(id)}/score`, {method: 'PUT', body: JSON.stringify({score: score.value})})
    if (props.item.id !== id) return
    emit('change', {id, user_score: result.user_score})
    open.value = false
  } catch (e) { error.value = errorMessage(e) }
  finally { busy.value = false }
}
</script>
<template>
  <div class="score-control">
    <button class="button secondary small" :disabled="busy" @click="edit"><Star :size="16" :fill="item.user_score ? 'currentColor' : 'none'" />{{ item.user_score ? `我的评分 · ${item.user_score}/5` : '评分' }}</button>
    <div v-if="open" class="score-editor">
      <strong>我的评分</strong>
      <div class="score-options" aria-label="我的五星评分"><button v-for="n in 5" :key="n" type="button" class="icon-button" :aria-label="`${n} 星`" :aria-pressed="score >= n" :disabled="busy" @click="score = n"><Star :size="23" :fill="score >= n ? 'currentColor' : 'none'" /></button><button class="text-button" :disabled="busy" @click="score = 0">清除评分</button></div>
      <p v-if="error" class="alert error" role="alert">{{ error }}</p>
      <div class="score-editor-actions"><button class="button secondary small" :disabled="busy" @click="open = false">取消</button><button class="button primary small" :disabled="busy" @click="save"><LoaderCircle v-if="busy" :size="15" class="spin" />保存评分</button></div>
    </div>
  </div>
</template>
<style scoped>
.score-control {display:contents;}.score-editor {flex-basis:100%;margin-top:2px;border:1px solid var(--line);border-radius:12px;padding:16px;background:var(--surface);}.score-options,.score-editor-actions {display:flex;gap:10px;flex-wrap:wrap;align-items:center;}.score-options {margin:10px 0;color:var(--accent);}.score-editor-actions {justify-content:flex-end;}
</style>
