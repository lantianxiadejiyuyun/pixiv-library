<script setup lang="ts">
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { X } from 'lucide-vue-next'
defineProps<{title: string; subtitle?: string; wide?: boolean; busy?: boolean}>()
const emit = defineEmits<{close: []}>()
const panel = ref<HTMLElement | null>(null)
let previousFocus: Element | null = null
function handleKey(event: KeyboardEvent) {
  if (event.key === 'Escape') emit('close')
  if (event.key !== 'Tab') return
  const focusable = panel.value?.querySelectorAll<HTMLElement>('button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]')
  if (!focusable?.length) return
  const first = focusable[0]; const last = focusable[focusable.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(() => { previousFocus = document.activeElement; document.body.style.overflow = 'hidden'; panel.value?.focus(); document.addEventListener('keydown', handleKey) })
onBeforeUnmount(() => { document.body.style.overflow = ''; document.removeEventListener('keydown', handleKey); if (previousFocus instanceof HTMLElement) previousFocus.focus() })
</script>
<template>
  <Teleport to="body"><div class="modal-overlay" @mousedown.self="!busy && emit('close')"><section ref="panel" class="modal" :class="{'modal-wide': wide}" role="dialog" aria-modal="true" :aria-label="title" tabindex="-1">
    <header class="modal-header"><div><h2>{{ title }}</h2><p v-if="subtitle">{{ subtitle }}</p></div><button class="icon-button" aria-label="关闭窗口" :disabled="busy" @click="emit('close')"><X :size="21" /></button></header>
    <div class="modal-body"><slot /></div>
  </section></div></Teleport>
</template>
