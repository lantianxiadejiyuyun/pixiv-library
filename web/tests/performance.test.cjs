const test = require('node:test'), assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm')
const ts = require('typescript'), Vue = require('vue')
const {parse, compileScript} = require('@vue/compiler-sfc')
function compile(source, context) {
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
  return context.exports
}
const metrics = compile(fs.readFileSync(path.join(__dirname,'../src/performance.ts'),'utf8'),{exports:{}})
function panel(api) {
  const hooks={}, timers=new Map(), listeners=new Map(), document={hidden:false,addEventListener:(k,v)=>listeners.set(k,v),removeEventListener:k=>listeners.delete(k)}
  let id=0
  const source=compileScript(parse(fs.readFileSync(path.join(__dirname,'../src/components/ServerPerformance.vue'),'utf8')).descriptor,{id:'performance'}).content
  const component=compile(source,{exports:{},AbortController,document,
    setTimeout:(fn,delay)=>{timers.set(++id,{fn,delay});return id},clearTimeout:id=>timers.delete(id),
    require:n=>({'vue':{...Vue,onMounted:fn=>hooks.mount=fn,onBeforeUnmount:fn=>hooks.unmount=fn},'../api':{api,errorMessage:e=>e.message},'../types':{bytes:String},'../performance':metrics,'./PerformanceChart.vue':{}}[n]||require(n))}).default
  const state=component.setup({}, {expose(){}})
  return {state,hooks,timers,listeners,document}
}
const tick=async()=>{for(let i=0;i<5;i++)await Promise.resolve()}
test('trend chart uses a fixed time window and breaks at missing samples',()=>{
  const points=[{at:100,host_cpu:50},{at:102,host_cpu:null},{at:104,host_cpu:120}]
  assert.deepEqual(Array.from(metrics.chartLines(points,'host_cpu')),['592.00,73.00','600.00,8.00'])
  assert.equal(metrics.percent(null),'采样中');assert.equal(metrics.percent(0),'0.0%')
  assert.equal(metrics.uptime(90061),'1天 1小时 1分')
})
test('performance polling preserves data on failure and stops while paused or hidden',async()=>{
  let fail=false,calls=0
  const snapshot={status:'ready',current:{platform:'Linux'},history:[],database:{status:'ok',queues:[{id:'download',queued:4,running:2}]}}
  const p=panel(async(url)=>{assert.equal(url,'/server/performance');calls++;if(fail)throw Error('network unavailable');return snapshot})
  p.hooks.mount();await tick()
  assert.equal(calls,1);assert.equal(p.state.queued.value,4);assert.equal(p.timers.size,1)
  fail=true;await p.state.load()
  assert.equal(p.state.current.value.platform,'Linux');assert.equal(p.state.error.value,'network unavailable')
  p.state.toggle();assert.equal(p.timers.size,0)
  await p.state.load();assert.equal(calls,2)
  p.state.toggle();await tick();assert.equal(calls,3)
  p.document.hidden=true;p.listeners.get('visibilitychange')();assert.equal(p.timers.size,0)
  await p.state.load();assert.equal(calls,3)
  p.document.hidden=false;p.listeners.get('visibilitychange')();await tick()
  assert.equal(calls,4);assert.equal([...p.timers.values()][0].delay,10000)
  p.hooks.unmount();assert.equal(p.listeners.size,0);assert.equal(p.timers.size,0)
})
test('late responses cannot update an unmounted dashboard',async()=>{
  let resolve,signal
  const p=panel((_,options)=>{signal=options.signal;return new Promise(done=>resolve=done)})
  p.hooks.mount();p.hooks.unmount();assert.equal(signal.aborted,true)
  resolve({status:'ready',current:{}});await tick()
  assert.equal(p.state.data.value,null);assert.equal(p.timers.size,0)
})
