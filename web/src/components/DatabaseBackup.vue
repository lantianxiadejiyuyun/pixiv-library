<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { AlertCircle, Check, DatabaseBackup, Download, KeyRound, LoaderCircle } from 'lucide-vue-next'
import { errorMessage } from '../api'
import { requestDatabaseExport } from '../databaseExport'

const props = defineProps<{databaseKind?: string}>()
const available = computed(() => props.databaseKind === 'mysql')
const exporting = ref(false)
const error = ref('')
const filename = ref('')
let controller: AbortController | undefined
let downloadURL = ''
let cleanupTimer: ReturnType<typeof setTimeout> | undefined
let disposed = false

function clearDownload() {
  clearTimeout(cleanupTimer)
  if (downloadURL) URL.revokeObjectURL(downloadURL)
  downloadURL = ''
}
async function exportDatabase() {
  if (exporting.value || !available.value) return
  exporting.value = true; error.value = ''; filename.value = ''
  controller = new AbortController()
  clearDownload()
  try {
    const result = await requestDatabaseExport(controller.signal)
    if (disposed) return
    downloadURL = URL.createObjectURL(result.file)
    const anchor = document.createElement('a')
    anchor.href = downloadURL; anchor.download = result.filename; anchor.hidden = true
    document.body.appendChild(anchor)
    try { anchor.click() } finally { anchor.remove() }
    filename.value = result.filename
    cleanupTimer = setTimeout(clearDownload, 60000)
  } catch (failure) {
    clearDownload()
    if (!disposed) error.value = errorMessage(failure)
  } finally { if (!disposed) exporting.value = false }
}
onBeforeUnmount(() => { disposed = true; controller?.abort(); clearDownload() })
</script>

<template>
  <section class="panel settings-card database-backup-card" :aria-busy="exporting">
    <div class="settings-title"><DatabaseBackup :size="21" /><div><h2>数据库备份</h2></div><span class="status-pill neutral">SQL</span></div>
    <p class="database-backup-description">导出表结构和数据，包括作品资料、标签、阅读进度、订阅及配置。不含书籍文件、封面和模型。</p>
    <div class="setting-note database-backup-key"><KeyRound :size="17" /><p>请另行保存原 <code>.env</code> 中的加密密钥，恢复账号与代理配置时需要。</p></div>
    <div class="database-backup-actions"><button class="button secondary" :disabled="exporting || !available" @click="exportDatabase"><LoaderCircle v-if="exporting" class="spin" :size="17" /><Download v-else :size="17" />{{ exporting ? '正在导出…' : '导出数据库' }}</button><span v-if="!databaseKind">正在读取数据库类型…</span><span v-else-if="!available">目前仅支持 MySQL。</span></div>
    <p v-if="exporting" class="database-backup-progress" role="status">正在生成并接收 SQL 文件，请保持此页面打开。</p>
    <div v-if="error" class="alert error database-backup-error" role="alert"><AlertCircle :size="18" /><span>{{ error }}</span></div>
    <div v-if="filename" class="database-backup-result" role="status"><Check :size="18" /><div><strong>已请求浏览器下载</strong><span>{{ filename }}</span><p>请在浏览器下载记录中确认文件已保存。</p></div></div>
  </section>
</template>
