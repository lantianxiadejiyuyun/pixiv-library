<script setup lang="ts">
import { computed } from 'vue'
import { AlertCircle, Cpu, LoaderCircle, RefreshCw } from 'lucide-vue-next'
import type { AIStatus } from '../types'
const props = defineProps<{status: AIStatus; refreshing?: boolean; error?: string}>()
defineEmits<{refresh: []}>()
const requested = computed(() => ({auto: '自动选择', cpu: 'CPU', cuda: 'NVIDIA CUDA', intel: 'Intel 核显优先'} as Record<string, string>)[props.status.requested_provider || ''] || '尚未报告')
const engine = computed(() => ({CPUExecutionProvider: 'ONNX Runtime · CPU', CUDAExecutionProvider: 'ONNX Runtime · CUDA', OpenVINOExecutionProvider: 'OpenVINO · Intel GPU'} as Record<string, string>)[props.status.actual_provider || ''] || props.status.actual_provider || '尚未执行推理')
const state = computed(() => ({not_loaded: '尚未加载', ready: '已验证运行', fallback: '已回退运行', error: '运行异常'} as Record<string, string>)[props.status.execution_state || ''] || '状态未报告')
const stateClass = computed(() => props.status.execution_state === 'error' ? 'failed' : props.status.execution_state === 'fallback' ? 'queued' : props.status.execution_state === 'ready' ? 'success' : 'neutral')
</script>
<template>
  <section class="panel ai-execution-panel">
    <header><div><Cpu :size="21" /><h3>推理设备</h3><span class="status-pill" :class="stateClass">{{ state }}</span></div><button class="text-button" :disabled="refreshing" @click="$emit('refresh')"><LoaderCircle v-if="refreshing" class="spin" :size="15" /><RefreshCw v-else :size="15" />刷新状态</button></header>
    <dl class="ai-execution-details"><div><dt>配置策略</dt><dd>{{ requested }}</dd></div><div><dt>实际设备</dt><dd>{{ status.device || '尚未执行推理' }}</dd></div><div><dt>实际推理引擎</dt><dd>{{ engine }}</dd></div></dl>
    <p v-if="status.execution_state === 'not_loaded'" class="ai-runtime-note">首次整理图片后显示实际设备；自动模式和 Intel 模式在加速不可用时可回退 CPU。</p>
    <div v-if="status.fallback_reason" class="alert warning"><AlertCircle :size="18" /><span>{{ status.fallback_reason }}</span></div>
    <div v-if="error" class="alert error" role="alert"><AlertCircle :size="18" /><span>{{ error }}</span></div>
  </section>
</template>
