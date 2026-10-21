<script setup lang="ts">
import { computed, ref } from 'vue'
import type { NovelBlock } from '../types'
const props = defineProps<{ image: NovelBlock }>()
const failed = ref(false), revision = ref(0)
const url = computed(() => /^\/api\/assets\/[a-f0-9-]+\/content$/.test(props.image.url || '') ? props.image.url : undefined)
const ratio = computed(() => props.image.width && props.image.height ? Math.max(.02, Math.min(100, props.image.width / props.image.height)) : .75)
function retry() { failed.value = false; revision.value++ }
</script>
<template>
  <figure class="novel-illustration" :style="{aspectRatio: String(ratio)}">
    <div v-if="!url" class="novel-image-status" role="status">插图暂不可用，请在服务器补全</div>
    <button v-else-if="failed" class="novel-image-status text-button" @click="retry">重新加载此插图</button>
    <img v-else :key="revision" :src="url" :width="image.width || undefined" :height="image.height || undefined" alt="正文插图" loading="lazy" decoding="async" @error="failed = true" />
  </figure>
</template>
<style scoped>
.novel-illustration{display:flex;width:100%;max-height:20000px;margin:1.5em 0;align-items:center;justify-content:center;overflow:hidden;background:rgb(127 127 127 / .06)}
.novel-illustration img{display:block;width:100%;height:100%;object-fit:contain}.novel-image-status{display:flex;align-items:center;justify-content:center;width:100%;height:100%;color:inherit;font:inherit;text-align:center}
</style>
