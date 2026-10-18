<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api, errorMessage } from '../api'

const enabled = ref(false), loading = ref(false), loaded = ref(false), busy = ref(false)
const error = ref(''), message = ref('')
let alive = true
onBeforeUnmount(() => { alive = false })
onMounted(load)
async function load() {
  loading.value = true; error.value = ''
  try {
    const result = await api<{enabled:boolean}>('/pixiv/follow-download-settings')
    if (alive) { enabled.value = result.enabled; loaded.value = true }
  } catch (failure) { if (alive) error.value = errorMessage(failure) }
  finally { if (alive) loading.value = false }
}
async function save() {
  if (!loaded.value || loading.value || busy.value) return
  busy.value = true; error.value = ''; message.value = ''
  try {
    const result = await api<{enabled:boolean}>('/pixiv/follow-download-settings', {method:'PUT', body:JSON.stringify({enabled:!enabled.value})})
    if (alive) { enabled.value = result.enabled; message.value = enabled.value ? '已开启，作者更新将进入检查与下载队列' : '已关闭，已排队任务继续执行' }
  } catch (failure) { if (alive) error.value = errorMessage(failure) }
  finally { if (alive) busy.value = false }
}
</script>
<template>
  <section class="pixiv-follow-download-settings">
    <strong>关注作者作品自动下载</strong>
    <p class="field-hint">检查更新时，默认下载已关注及新增关注作者的插画、漫画和小说。单个作者的设置优先，作者公开收藏独立控制。</p>
    <div class="source-actions">
      <button class="button secondary small" role="switch" aria-label="关注作者作品自动下载" :aria-checked="enabled" :disabled="loading||busy||!loaded" @click="save">{{loading?'读取中…':enabled?'已开启 · 点击关闭':'已关闭 · 点击开启'}}</button>
      <button v-if="!loaded&&!loading" class="button quiet small" @click="load">重新读取</button>
    </div>
    <p v-if="error" class="inline-error" role="alert">{{error}}</p>
    <p v-if="message" class="field-hint" role="status">{{message}}</p>
  </section>
</template>
<style scoped>
.pixiv-follow-download-settings{margin-top:16px;padding-top:16px;border-top:1px solid var(--border,#ddd)}
</style>
