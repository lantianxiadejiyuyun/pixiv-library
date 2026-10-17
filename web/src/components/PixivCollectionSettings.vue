<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { api, errorMessage } from '../api'
const enabled = ref(false), loading = ref(true), busy = ref(false), error = ref(''), message = ref('')
onMounted(async () => {
  try { enabled.value = (await api<{enabled: boolean}>('/pixiv/collection-settings')).enabled }
  catch (failure) { error.value = errorMessage(failure) }
  finally { loading.value = false }
})
async function save() {
  busy.value = true; error.value = ''; message.value = ''
  try {
    enabled.value = (await api<{enabled:boolean}>('/pixiv/collection-settings', {
      method: 'PUT', body: JSON.stringify({enabled: !enabled.value})
    })).enabled
    message.value = '已保存'
  } catch (failure) { error.value = errorMessage(failure) }
  finally { busy.value = false }
}
async function sync() {
  busy.value = true; error.value = ''; message.value = ''
  try {
    const result = await api<{job_ids:string[]}>('/pixiv/bookmarks/sync', {method:'POST'})
    message.value = result.job_ids.length ? '收藏同步已进入检查队列' : '收藏同步已在队列中'
  } catch (failure) { error.value = errorMessage(failure) }
  finally { busy.value = false }
}
</script>
<template>
  <section class="pixiv-collection-settings">
    <strong>主账号收藏自动下载</strong>
    <p class="field-hint">每小时检查主账号的公开、私密收藏，包含插画、漫画和小说。缺失正文进入下载队列；取消远端收藏时保留本地收藏。</p>
    <div class="source-actions">
      <button class="button secondary small" :disabled="loading||busy" :aria-pressed="enabled" @click="save">{{enabled?'已开启 · 点击关闭':'已关闭 · 点击开启'}}</button>
      <button class="button quiet small" :disabled="loading||busy||!enabled" @click="sync">立即同步收藏</button>
    </div>
    <p v-if="error" class="inline-error" role="alert">{{error}}</p>
    <p v-if="message" class="field-hint" role="status">{{message}}</p>
  </section>
</template>
<style scoped>
.pixiv-collection-settings{margin-top:16px;padding-top:16px;border-top:1px solid var(--border,#ddd)}
</style>
