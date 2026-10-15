<script setup lang="ts">
import { computed } from 'vue'
import { ArrowUpRight, Tags, BookOpen, FileText, Globe2 } from 'lucide-vue-next'
import { ratingLabels } from '../types'
const props = defineProps<{result: Record<string, unknown>}>()
defineEmits<{open: [id: string]; lookup: [value: {query: string; itemId: string}]}>()
type PageEvidence = {asset_id: string; position: number; rating: string; rating_scores?: Record<string, number>; model_version?: string}
type PredictedTag = {name: string; score?: number; category?: number}
const pages = computed(() => (Array.isArray(props.result.page_evidence) ? props.result.page_evidence : []) as PageEvidence[])
const tags = computed(() => (Array.isArray(props.result.tags) ? props.result.tags : []) as PredictedTag[])
const characters = computed(() => tags.value.filter(tag => tag.category === 4 || /^角色[:：]/.test(tag.name)))
const itemId = computed(() => typeof props.result.item_id === 'string' ? props.result.item_id : '')
const suggestedRating = computed(() => typeof props.result.suggested_rating === 'string' ? props.result.suggested_rating : '')
function highestScore(scores?: Record<string, number>) { return scores ? `${Math.round(Math.max(...Object.values(scores)) * 100)}%` : '—' }
</script>

<template>
  <details class="job-result"><summary>查看处理结果</summary><div class="result-content">
    <div class="result-topline"><p v-if="typeof result.message === 'string'">{{ result.message }}</p><button v-if="itemId" class="text-button" @click="$emit('open', itemId)">查看作品<ArrowUpRight :size="13" /></button></div>
    <div class="result-metrics">
      <span v-if="typeof result.analyzed_pages === 'number'"><BookOpen :size="12" />{{ result.analyzed_pages }} 页已检测</span>
      <span v-if="typeof result.analyzed_texts === 'number' && result.analyzed_texts"><FileText :size="12" />{{ result.analyzed_texts }} 份正文已检测</span>
      <span v-if="tags.length"><Tags :size="12" />{{ tags.length }} 个标签</span>
      <span v-if="suggestedRating">分级建议：<strong>{{ ratingLabels[suggestedRating] || suggestedRating }}</strong>{{ result.rating_applied === false ? '（保留已有分级）' : '' }}</span>
      <span v-if="typeof result.discovered === 'number'">发现 {{ result.discovered }} 部作品</span>
      <span v-if="typeof result.added_subscriptions === 'number'">新增 {{ result.added_subscriptions }} 条订阅</span>
      <span v-if="typeof result.remote_favorites === 'number'">远端收藏 {{ result.remote_favorites }} 部</span>
      <span v-if="Array.isArray(result.queued_jobs)">创建 {{ result.queued_jobs.length }} 个后续任务</span>
      <span v-if="result.metadata_only === true">封面与资料阶段结果 · 本阶段不下载正文</span>
      <span v-if="typeof result.content_job_id === 'string'">已创建正文下载任务</span>
    </div>
    <div v-if="tags.length" class="result-tags"><span v-for="tag in tags.slice(0, 20)" :key="tag.name" :title="tag.score != null ? `置信度 ${Math.round(tag.score * 100)}%` : undefined">{{ tag.name }}</span><span v-if="tags.length > 20">另有 {{ tags.length - 20 }} 个</span></div>
    <div v-if="characters.length" class="result-characters"><div v-for="character in characters" :key="character.name"><span>{{ character.name }}<small v-if="character.score != null">{{ Math.round(character.score * 100) }}%</small></span><button class="text-button" @click="$emit('lookup', {query: character.name.replace(/^角色[:：]/, '').replace(/_/g, ' '), itemId})"><Globe2 :size="14" />联网查证</button></div></div>
    <details v-if="pages.length" class="page-evidence"><summary>逐页分级依据 · {{ pages.length }} 页</summary><div class="evidence-table"><table><thead><tr><th>页码</th><th>分级建议</th><th>最高分类分数</th><th>模型版本</th></tr></thead><tbody><tr v-for="entry in pages.slice(0, 100)" :key="entry.asset_id"><td>{{ entry.position + 1 }}</td><td>{{ ratingLabels[entry.rating] || entry.rating }}</td><td>{{ highestScore(entry.rating_scores) }}</td><td>{{ entry.model_version || '—' }}</td></tr></tbody></table></div><p v-if="pages.length > 100" class="muted">此处显示前 100 页，完整记录见下方。</p></details>
    <details class="result-raw"><summary>原始处理记录</summary><pre>{{ JSON.stringify(result, null, 2) }}</pre></details>
  </div></details>
</template>
