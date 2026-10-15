<script setup lang="ts">
import { computed } from 'vue'
import { LibraryBig, FolderHeart, ChevronRight, HardDrive, AlertCircle, RefreshCw } from 'lucide-vue-next'
import type { Collection } from '../types'
import { bytes } from '../types'
const props = defineProps<{collections: Collection[]; selected: string; total: number; error?: string}>()
defineEmits<{select: [source: string]; retry: []}>()
const current = computed(() => props.collections.find(collection => collection.source === props.selected))
const initials: Record<string, string> = {pixiv: 'P', ehentai: 'E', picacg: '哔', jm: 'JM'}
</script>

<template>
  <section class="collection-switcher" aria-label="选择来源书库">
    <div class="collection-switcher-heading"><div><h2>来源书库</h2></div><span>按来源独立保存</span></div>
    <div class="collection-entry-grid"><button class="collection-entry all-collections" :class="{active: !selected}" @click="$emit('select', '')"><div class="collection-entry-symbol"><LibraryBig :size="22" :stroke-width="1.6" /></div><div><strong>全库汇总</strong><span>{{ total }} 部作品</span></div><ChevronRight :size="14" /></button><button v-for="collection in collections" :key="collection.source" class="collection-entry" :class="[collection.source, {active: selected === collection.source}]" @click="$emit('select', collection.source)"><div class="collection-entry-symbol"><FolderHeart v-if="collection.source === 'local'" :size="22" :stroke-width="1.5" /><span v-else>{{ initials[collection.source] || collection.name.slice(0, 1) }}</span></div><div><strong>{{ collection.name }}</strong><span>{{ collection.item_count }} 部 · {{ bytes(collection.size_bytes) }}</span></div><ChevronRight :size="14" /></button></div>
    <div v-if="error" class="collection-load-error"><AlertCircle :size="14" /><span>{{ error }}</span><button class="text-button" @click="$emit('retry')"><RefreshCw :size="12" />重新读取</button></div>
    <div v-if="current" class="collection-storage-context"><HardDrive :size="14" /><span>独立文件目录 <code>{{ current.storage_directory }}</code></span><span class="collection-status-count">{{ current.saved_item_count }} 部已保存 · {{ current.metadata_item_count }} 部仅元数据</span><p v-if="current.legacy_asset_count">{{ current.legacy_asset_count }} 个历史文件仍在旧目录，可正常读取；迁移后将归入本书库。</p></div>
    <div v-else class="collection-overview-note"><FolderHeart :size="13" />平台内容按来源分库；上传文件保存到本地书库。</div>
  </section>
</template>
