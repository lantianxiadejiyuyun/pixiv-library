<script setup lang="ts">
import { chartLines } from '../performance'
import type { PerformancePoint } from '../performance'
defineProps<{points:PerformancePoint[];first:'host_cpu'|'host_memory';second:'service_cpu'|'service_memory';title:string}>()
</script>
<template>
  <article class="panel trend"><header><h2>{{title}}</h2><span>最近 5 分钟</span></header>
    <div class="trend-plot"><span class="trend-top">100%</span><svg viewBox="0 0 600 140" preserveAspectRatio="none" role="img" :aria-label="`${title}最近五分钟趋势，重启后重新采样`"><path d="M0 8H600 M0 73H600 M0 138H600" class="grid"/><polyline v-for="(line,i) in chartLines(points,first)" :key="`h${i}`" :points="line" class="host-line"/><polyline v-for="(line,i) in chartLines(points,second)" :key="`s${i}`" :points="line" class="service-line"/></svg><span class="trend-bottom">0%</span></div>
    <footer><span><i class="host-dot"/>服务器整体</span><span><i class="service-dot"/>书库服务</span><span class="trend-now">现在</span></footer>
  </article>
</template>
<style scoped>
.trend{padding:22px}.trend header,.trend footer{display:flex;gap:16px;align-items:center}.trend header{justify-content:space-between;margin-bottom:18px}.trend h2{font-size:15px;margin:0}.trend header span,.trend footer{font-size:12px;color:var(--text-secondary)}.trend-plot{position:relative;padding-left:36px}.trend svg{display:block;width:100%;height:150px;overflow:visible}.trend polyline{fill:none;stroke-width:2;stroke-linejoin:round;stroke-linecap:round;vector-effect:non-scaling-stroke}.grid{stroke:var(--border,#e2e8f0);stroke-width:1;fill:none;stroke-dasharray:3 5}.host-line{stroke:#94a3b8}.service-line{stroke:var(--accent,#635bce)}.trend-top,.trend-bottom{position:absolute;left:0;font-size:11px;color:var(--text-secondary)}.trend-top{top:0}.trend-bottom{bottom:0}.trend footer{margin-top:16px}.trend footer span{display:flex;gap:6px;align-items:center}.trend footer i{height:7px;width:7px;border-radius:50%;display:inline-block}.host-dot{background:#94a3b8}.service-dot{background:var(--accent,#635bce)}.trend-now{margin-left:auto}
</style>
