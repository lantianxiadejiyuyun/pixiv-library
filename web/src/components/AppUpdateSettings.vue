<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { Smartphone, LoaderCircle } from 'lucide-vue-next'
import { api, errorMessage } from '../api'
type Release = {version_name: string; version_code: number; size_bytes: number; sha256: string; notes: string; download_path: string}
const release = ref<Release | null>(null)
const selected = ref<File | null>(null)
const notes = ref(''), error = ref(''), message = ref('')
const busy = ref(false), loading = ref(true), progress = ref(0)
async function load() {
  try { release.value = (await api<{release: Release | null}>('/app-updates/android')).release }
  catch (e) { error.value = errorMessage(e) }
  finally { loading.value = false }
}
async function publish() {
  const file = selected.value
  if (!file || busy.value) return
  if (!file.name.toLowerCase().endsWith('.apk') || file.size > 256 * 1024 * 1024) { error.value = '请选择不超过 256 MiB 的 APK'; return }
  busy.value = true; error.value = ''; message.value = ''; progress.value = 0
  const xhr = new XMLHttpRequest()
  xhr.open('POST', `/api/app-updates/android?${new URLSearchParams({notes: notes.value})}`)
  xhr.setRequestHeader('Content-Type', 'application/vnd.android.package-archive')
  xhr.timeout = 390000
  xhr.upload.onprogress = e => { if (e.lengthComputable) progress.value = Math.round(e.loaded * 100 / e.total) }
  xhr.onload = () => {
    try {
      const payload = JSON.parse(xhr.responseText)
      if (xhr.status === 401) window.dispatchEvent(new Event('library:unauthorized'))
      if (xhr.status !== 200) throw new Error(typeof payload.detail === 'string' ? payload.detail : '发布失败')
      release.value = payload.release; message.value = '已发布，App 可在“服务器 → 检查更新”下载。'
    } catch (e) { error.value = errorMessage(e) }
    busy.value = false
  }
  xhr.onerror = xhr.ontimeout = () => { error.value = '上传失败，请检查连接后重试'; busy.value = false }
  xhr.send(file)
}
onMounted(load)
</script>
<template>
  <section class="panel settings-card app-update-settings">
    <div class="settings-title"><Smartphone :size="21" /><h2>Android 客户端更新</h2></div>
    <p v-if="loading">读取版本…</p>
    <div v-else-if="release">
      <p>当前版本 {{ release.version_name }}（{{ release.version_code }}） · {{ (release.size_bytes / 1024 / 1024).toFixed(1) }} MiB</p>
      <p v-if="release.notes" class="release-notes">{{ release.notes }}</p>
      <a class="button secondary small" :href="release.download_path" download>下载安装包</a>
    </div>
    <p v-else>尚未发布安装包</p>
    <form @submit.prevent="publish">
      <label class="field">新版 APK<input type="file" accept=".apk,application/vnd.android.package-archive" :disabled="busy" required @change="selected = ($event.target as HTMLInputElement).files?.[0] || null" /></label>
      <label class="field">更新说明<textarea v-model="notes" :disabled="busy" maxlength="2000" rows="3" /></label>
      <p class="field-hint">上传正式签名的完整 APK，最大 256 MiB。包名、版本和签名自动校验，版本号必须递增。</p>
      <button class="button primary small" :disabled="busy || !selected"><LoaderCircle v-if="busy" class="spin" :size="16" />{{ busy ? (progress < 100 ? `上传 ${progress}%` : '校验并发布…') : '发布更新' }}</button>
    </form>
    <div v-if="error" class="alert error" role="alert">{{ error }}</div>
    <div v-if="message" class="alert info" role="status">{{ message }}</div>
  </section>
</template>
<style scoped>
.app-update-settings {margin-top:20px;}.field {margin:16px 0;}.release-notes {white-space:pre-wrap;}.alert {margin-top:12px;}
</style>
