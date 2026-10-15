<script setup lang="ts">
import { BookOpen, FileText, Image, ArrowUpRight, LoaderCircle, Star, Heart } from 'lucide-vue-next'
import type { Item } from '../types'
import { typeLabels, sourceLabels, ratingLabels, contentReady, contentStateLabel, contentStateTone, contentCountLabel } from '../types'
defineProps<{item: Item; blurAdult: boolean}>()
defineEmits<{open: [item: Item]}>()
</script>

<template>
  <button class="book-card" @click="$emit('open', item)">
    <div class="book-cover" :class="[`cover-${item.type}`, {'is-blurred': blurAdult && item.rating === 'adult'}]">
      <img v-if="item.cover_url" :src="item.cover_url" :alt="item.title" loading="lazy" />
      <div v-else class="typographic-cover">

        <component :is="item.type === 'novel' || item.type === 'article' ? FileText : item.type === 'illustration' ? Image : BookOpen" :size="36" :stroke-width="1.1" />
        <span class="cover-title">{{ item.title_zh || item.title }}</span>
        <span class="cover-rule"></span>
        <span class="cover-kind">{{ typeLabels[item.type] }}</span>
      </div>
      <span class="cover-type">{{ typeLabels[item.type] }}</span>
      <div class="cover-counts"><span v-if="!['novel', 'article'].includes(item.type) && (item.remote_page_count || item.page_count)">{{ item.remote_page_count || item.page_count }} P</span><span v-if="item.source_bookmark_count != null" title="平台收藏人数（同步时）"><Star :size="12" fill="currentColor" />{{ item.source_bookmark_count.toLocaleString() }}</span></div>
      <span v-if="item.is_favorite" class="local-favorite" aria-label="已收藏"><Heart :size="17" fill="currentColor" /></span>
      <span v-if="item.rating === 'adult' || item.rating === 'sensitive'" class="rating-chip" :class="item.rating">{{ ratingLabels[item.rating] }}</span>
      <span class="book-open-action">打开作品 <ArrowUpRight :size="16" /></span>
      <div v-if="item.progress && contentReady(item) && !['novel', 'article'].includes(item.type)" class="reading-line"><span :style="{width: `${Math.min(100, (item.progress.page + 1) / item.page_count * 100)}%`}"></span></div>
    </div>
    <h3 :title="item.title">{{ item.title_zh || item.title }}</h3>
    <div class="book-author">{{ item.author || '未填写作者' }}</div>
    <div v-if="item.user_score" class="card-user-score">我的评分 {{ item.user_score }}/5</div>
    <div class="book-meta"><span>{{ sourceLabels[item.source] || item.source }}</span><span>{{ contentCountLabel(item) }}</span></div>
    <div class="book-content-state"><span class="status-pill" :class="contentStateTone(item)"><LoaderCircle v-if="item.content_state === 'downloading'" class="spin" :size="13" />{{ contentStateLabel(item) }}</span></div>
  </button>
</template>
<style scoped>
.cover-counts {position:absolute;left:8px;bottom:10px;right:8px;display:flex;gap:5px;flex-wrap:wrap;z-index:2;}.cover-counts span {display:flex;align-items:center;gap:4px;background:rgba(0,0,0,.76);color:#fff;padding:3px 6px;border-radius:5px;font-size:12px;line-height:1.3;font-variant-numeric:tabular-nums;}.local-favorite {position:absolute;top:38px;right:10px;color:#f472b6;filter:drop-shadow(0 1px 3px #000);}.card-user-score {font-size:12px;color:var(--accent);margin-top:4px;}
</style>
