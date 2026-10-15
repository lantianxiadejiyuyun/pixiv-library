<script setup lang="ts">
import { computed } from 'vue'
import { AlertCircle, Download, ListTodo, LoaderCircle, RefreshCw, Upload } from 'lucide-vue-next'
import type { Item, Job } from '../types'
import { contentStateLabel, contentStateTone } from '../types'
const props = defineProps<{item: Item; job?: Job; busy?: boolean}>()
defineEmits<{download: []; tasks: []; import: []}>()
const active = computed(() => ['queued', 'downloading'].includes(props.item.content_state))
const progress = computed(() => props.job && props.job.status === 'running' ? Math.min(100, Math.max(0, Number(props.job.progress) || 0)) : null)
const description = computed(() => ({
  metadata: '封面和基本资料可先查看，正文下载完成后才能阅读、离线打包和 AI 整理。',
  queued: '正文已加入采集队列，等待后台处理。',
  downloading: '正在下载正文，完成后开放阅读、离线打包和 AI 整理。',
  failed: '正文尚未下载完成，可重试下载。',
  ready: '正文尚未就绪，请刷新作品或重新下载。',
} as Record<string, string>)[props.item.content_state] || '正文状态尚未确认，请稍后刷新。')
</script>
<template>
  <section class="content-download-panel" :class="{'content-download-failed': item.content_state === 'failed'}">
    <div class="content-download-heading"><LoaderCircle v-if="item.content_state === 'downloading'" class="spin" :size="20" /><AlertCircle v-else-if="item.content_state === 'failed'" :size="20" /><Download v-else :size="20" /><strong>{{ contentStateLabel(item) }}</strong><span class="status-pill" :class="contentStateTone(item)">正文未就绪</span></div>
    <p>{{ description }}</p>
    <div v-if="progress != null" class="content-download-progress"><div role="progressbar" aria-label="正文下载进度" :aria-valuenow="progress" aria-valuemin="0" aria-valuemax="100"><span :style="{width: `${progress}%`}"></span></div><span>{{ progress }}%</span></div>
    <p v-if="item.content_error" class="inline-error">{{ item.content_error }}</p>
    <div class="content-download-actions"><button v-if="!active && item.source !== 'local'" type="button" class="button primary small" :disabled="busy" @click="$emit('download')"><LoaderCircle v-if="busy" class="spin" :size="16" /><RefreshCw v-else-if="item.content_state === 'failed'" :size="16" /><Download v-else :size="16" />{{ item.content_state === 'failed' ? '重试正文下载' : '下载正文' }}</button><button v-if="item.source === 'local'" type="button" class="button secondary small" :disabled="busy" @click="$emit('import')"><Upload :size="16" />重新导入本地文件</button><button v-if="item.content_job_id" type="button" class="button secondary small" :disabled="busy" @click="$emit('tasks')"><ListTodo :size="16" />查看下载任务</button></div>
  </section>
</template>
