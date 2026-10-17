const test = require('node:test'), assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm')
const ts = require('typescript'), Vue = require('vue')
const {parse,compileScript} = require('@vue/compiler-sfc')
const source = compileScript(parse(fs.readFileSync(path.join(__dirname,'../src/components/DownloadQueue.vue'),'utf8')).descriptor,{id:'pacing'}).content
function harness({failure=false,initial={}}={}) {
  const calls=[]
  const lanes=[{id:'download',label:'内容下载',paused:false,concurrency:2,counts:{queued:4,running:1,failed:0,completed:0}},{id:'ai',label:'AI 识别',paused:false,concurrency:1,counts:{queued:1,running:0,failed:0,completed:0}},{id:'retry',label:'失败重试',paused:false,blocked_by_master:false,concurrency:1,counts:{queued:2,running:0,failed:0,completed:0}}]
  let settings={paused:true,rate_limit_enabled:true,min_interval_seconds:2,max_interval_seconds:5,concurrency:1,...initial}
  const imports={vue:{...Vue,onMounted(){},onBeforeUnmount(){}},'lucide-vue-next':{},'../types':{},'../api':{errorMessage:e=>e.message,api:async (url,options={})=>{
    calls.push({url,...options})
    if (!options.method) return {lanes:lanes.map(lane=>({...lane})),settings:{...settings},counts:{queued:2,running:0,failed:0},active:null}
    if(failure) throw new Error('设置未保存')
    if(url.startsWith('/queue/lanes/')) {const lane=lanes.find(lane=>url.endsWith('/'+lane.id));Object.assign(lane,JSON.parse(options.body));return {...lane}}
    settings={...settings,...JSON.parse(options.body)};return {...settings}
  }}}
  const context={exports:{},require:name=>imports[name]||require(name),setInterval(){},clearInterval(){}}
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
  return {state:context.exports.default.setup({}, {expose(){},emit(){}}),calls}
}
test('开关只更改限速，不提交未保存间隔或恢复暂停队列', async()=>{
  const h=harness(); await h.state.load(); h.state.minInterval.value=3;h.state.intervalDirty.value=true
  const first=h.state.save('rate_limit'), duplicate=h.state.save('rate_limit');await Promise.all([first,duplicate])
  const writes=h.calls.filter(c=>c.method)
  assert.equal(writes.length,1);assert.deepEqual(JSON.parse(writes[0].body),{rate_limit_enabled:false})
  assert.equal(h.state.state.value.settings.paused,true)
  assert.equal(h.state.state.value.settings.rate_limit_enabled,false)
  assert.equal(h.state.minInterval.value,3);assert.equal(h.state.intervalDirty.value,true)
})
test('开关保存失败保留显示状态，不宣称关闭成功', async()=>{
  const h=harness({failure:true});await h.state.load();await h.state.save('rate_limit')
  assert.equal(h.state.state.value.settings.rate_limit_enabled,true)
  assert.match(h.state.error.value,/未保存/);assert.equal(h.state.feedback.value,'')
})
test('重新开启沿用服务器保存的间隔',async()=>{
  const h=harness();await h.state.load();await h.state.save('rate_limit');await h.state.save('rate_limit')
  assert.equal(h.state.state.value.settings.rate_limit_enabled,true)
  assert.equal(h.state.state.value.settings.min_interval_seconds,2)
  assert.equal(h.state.state.value.settings.max_interval_seconds,5)
})

test('保存并发数只提交并发，限速关闭或队列暂停也可设置', async()=>{
  const h=harness({initial:{rate_limit_enabled:false}});await h.state.load()
  h.state.minInterval.value=3;h.state.intervalDirty.value=true
  h.state.concurrency.value=4;h.state.concurrencyDirty.value=true
  await Promise.all([h.state.save('concurrency'),h.state.save('concurrency')])
  const writes=h.calls.filter(c=>c.method)
  assert.equal(writes.length,1);assert.deepEqual(JSON.parse(writes[0].body),{concurrency:4})
  assert.equal(h.state.state.value.settings.concurrency,4)
  assert.equal(h.state.state.value.settings.paused,true)
  assert.equal(h.state.state.value.settings.rate_limit_enabled,false)
  assert.equal(h.state.concurrencyDirty.value,false)
  assert.equal(h.state.minInterval.value,3);assert.equal(h.state.intervalDirty.value,true)
})
test('并发保存失败保留输入，轮询不会覆盖待保存值',async()=>{
  const h=harness({failure:true});await h.state.load()
  h.state.concurrency.value=5;h.state.concurrencyDirty.value=true
  await h.state.save('concurrency');await h.state.load()
  assert.equal(h.state.concurrency.value,5)
  assert.equal(h.state.concurrencyDirty.value,true)
  assert.equal(h.state.state.value.settings.concurrency,1)
  assert.equal(h.state.feedback.value,'')
})
test('并发仅接受1至8整数',async()=>{
  const h=harness();await h.state.load()
  for(const value of [0,9,1.5,NaN]) {
    h.state.concurrency.value=value;await h.state.save('concurrency')
    assert.match(h.state.error.value,/1–8/)
  }
  assert.equal(h.calls.filter(c=>c.method).length,0)
})
test('多任务进度全部展示，兼容只有active的旧响应',async()=>{
  const h=harness();await h.state.load()
  const first={id:'one',kind:'import',progress:35},second={id:'two',kind:'import',progress:80}
  h.state.state.value.active=first
  assert.equal(h.state.activeJobs.value.length,1)
  h.state.state.value.active_jobs=[first,second]
  assert.equal(h.state.activeJobs.value.length,2)
  assert.equal(h.state.progress(h.state.activeJobs.value[1]),80)
  assert.equal(h.state.progress({...first,progress:120}),100)
})


test('单队列保存只提交本队列上限，保留采集总暂停和其他队列',async()=>{
  const h=harness();await h.state.load()
  const lane=h.state.state.value.lanes[0]
  h.state.laneLimits.value.download=5;h.state.dirtyLanes.add('download')
  await h.state.saveLane(lane)
  const writes=h.calls.filter(c=>c.method)
  assert.equal(writes.length,1);assert.equal(writes[0].url,'/queue/lanes/download')
  assert.deepEqual(JSON.parse(writes[0].body),{concurrency:5})
  assert.equal(h.state.state.value.settings.paused,true)
  assert.equal(h.state.laneLimits.value.ai,1)
  assert.equal(h.state.dirtyLanes.has('download'),false)
})

test('单队列暂停不带未保存并发，保存失败保留输入',async()=>{
  const h=harness({failure:true});await h.state.load()
  h.state.laneLimits.value.download=7;h.state.dirtyLanes.add('download')
  await h.state.load(true)
  assert.equal(h.state.laneLimits.value.download,7)
  await h.state.saveLane(h.state.state.value.lanes[0],true)
  const write=h.calls.find(c=>c.method)
  assert.deepEqual(JSON.parse(write.body),{paused:true})
  assert.equal(h.state.state.value.lanes[0].paused,false)
  assert.equal(h.state.laneLimits.value.download,7)
  assert.match(h.state.error.value,/未保存/)
})


test('暂停成功也保留本队列尚未保存的并发输入',async()=>{
  const h=harness();await h.state.load()
  h.state.laneLimits.value.download=6;h.state.dirtyLanes.add('download')
  await h.state.saveLane(h.state.state.value.lanes[0],true)
  await h.state.load(true)
  assert.equal(h.state.state.value.lanes[0].paused,true)
  assert.equal(h.state.laneLimits.value.download,6)
  assert(h.state.dirtyLanes.has('download'))
})


test('队列展示实际进程 CPU，未生效时不把计划绑定冒充为成功',async()=>{
  const h=harness();await h.state.load()
  h.state.state.value.runtime={mode:'process',api_cpus:[0,1],shared_cpus:false,affinity_applied:true,workers:[
    {lane:'download',pid:42,state:'running',cpus:[2,3],requested_cpus:[2,3],affinity_applied:true,active:2,restarts:0},
    {lane:'ai',pid:43,state:'starting',cpus:[],requested_cpus:[8,9],affinity_applied:false,active:0,restarts:1}
  ]}
  assert.equal(h.state.runningWorkers.value,1)
  assert.match(h.state.workerLabel('download'),/42.*CPU 2, 3/)
  assert.match(h.state.workerLabel('ai'),/进程启动中.*未绑定 CPU/)
  assert(!h.state.workerLabel('ai').includes('8, 9'))
})

test('兼容没有进程信息的旧响应，不影响队列设置',async()=>{
  const h=harness();await h.state.load()
  assert.equal(h.state.runningWorkers.value,0)
  assert.equal(h.state.workerLabel('download'),'')
  h.state.concurrency.value=3;await h.state.save('concurrency')
  assert.equal(h.state.state.value.settings.concurrency,3)
})

test('采集暂停提示只显示在被阻止的任务上，手动队列只遵守自身暂停',async()=>{
  const h=harness();await h.state.load()
  h.state.state.value.lanes=[
    {id:'manual_download',paused:false,blocked_by_master:false},
    {id:'download',paused:false,blocked_by_master:true}
  ]
  assert.equal(h.state.pauseMessage({queue:'manual_download'}),'')
  assert.match(h.state.pauseMessage({queue:'download'}),/采集已暂停/)
  h.state.state.value.lanes[0].paused=true
  assert.match(h.state.pauseMessage({queue:'manual_download'}),/本队列已暂停/)
})


test('重试队列独立保存并发与暂停，采集暂停不阻止重试',async()=>{
  const h=harness();await h.state.load()
  const lane=h.state.state.value.lanes.find(row=>row.id==='retry')
  assert.equal(h.state.pauseMessage({queue:'retry'}),'')
  h.state.laneLimits.value.retry=3;h.state.dirtyLanes.add('retry')
  await h.state.saveLane(lane);await h.state.load(true)
  assert.equal(h.state.state.value.settings.paused,true)
  assert.equal(h.state.laneLimits.value.download,2)
  assert.equal(h.state.laneLimits.value.retry,3)
  await h.state.saveLane(h.state.state.value.lanes.find(row=>row.id==='retry'),true)
  await h.state.load(true)
  assert.match(h.state.pauseMessage({queue:'retry'}),/本队列已暂停/)
  const writes=h.calls.filter(c=>c.method)
  assert.equal(writes.length,2)
  assert(writes.every(row=>row.url==='/queue/lanes/retry'))
  assert.deepEqual(JSON.parse(writes[0].body),{concurrency:3})
  assert.deepEqual(JSON.parse(writes[1].body),{paused:true})
})
