const test=require('node:test'),assert=require('node:assert/strict')
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm')
const ts=require('typescript'),Vue=require('vue'),{parse,compileScript}=require('@vue/compiler-sfc')
const source=compileScript(parse(fs.readFileSync(path.join(__dirname,'../src/components/WatchlistPanel.vue'),'utf8')).descriptor,{id:'watchlist'}).content
function harness(){
 const requests=[],unmount=[],timers=[],events=[];let failure=false
 async function api(url,options={}){
  requests.push({url,...options})
  if(failure)throw new Error('合成断网')
  if(options.method)return {id:'job1'}
  if(url==='/pixiv/watchlist/one')return {catalog:{entries:[{item_id:'book1',title:'章节'}],complete:true}}
  return {items:[{id:url.includes('page=2')?'two':'one',title:'系列'}],total:60,sync:{pending_tasks:1,configured:true}}
 }
 const context={exports:{},require:name=>({'vue':{...Vue,onMounted(){},onBeforeUnmount(fn){unmount.push(fn)}},'../api':{api,errorMessage:e=>e.message},'../types':{date:x=>x}}[name]||require(name)),URLSearchParams,setTimeout(fn){timers.push(fn);return timers.length},clearTimeout(){}}
 vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
 return {state:context.exports.default.setup({},{expose(){},emit:(...args)=>events.push(args)}),requests,unmount,timers,events,fail(){failure=true}}
}
test('追更列表请求类型、未读与搜索过滤并支持分页追加',async()=>{
 const h=harness();h.state.kind.value='manga';h.state.updates.value=true;h.state.query.value='合成'
 await h.state.load();await h.state.load(true)
 const url=new URL('http://local'+h.requests[0].url)
 assert.equal(url.searchParams.get('kind'),'manga');assert.equal(url.searchParams.get('updates'),'true');assert.equal(url.searchParams.get('q'),'合成')
 assert.deepEqual(Array.from(h.state.rows.value,x=>x.id),['one','two'])
})
test('弱网刷新保留原列表；失败不会提交采集或下载操作',async()=>{
 const h=harness();await h.state.load();h.fail();await h.state.load()
 assert.equal(h.state.rows.value[0].id,'one');assert.equal(h.state.error.value,'合成断网');assert.ok(h.requests.every(r=>!r.method))
})
test('自动轮询保留已展开分页；离开页面丢弃迟到轮询',async()=>{
 const h=harness();await h.state.load();await h.state.load(true);await h.state.load(false,true)
 assert.equal(h.state.rows.value.length,2);h.unmount.forEach(fn=>fn());const old=h.state.rows.value
 await h.state.load(false,true);assert.equal(h.state.rows.value,old)
})
test('同步与自动下载设置使用独立操作，目录关闭不重置列表',async()=>{
 const h=harness();await h.state.load();await h.state.action('/pixiv/watchlist/sync')
 assert.equal(h.requests.find(r=>r.method==='POST').url,'/pixiv/watchlist/sync')
 await h.state.action('/pixiv/watchlist/one',{auto_download:true},'PUT')
 assert.deepEqual(JSON.parse(h.requests.find(r=>r.method==='PUT').body),{auto_download:true})
 await h.state.detail(h.state.rows.value[0]);assert.equal(h.state.catalog.value.entries[0].item_id,'book1');h.state.close();assert.equal(h.state.rows.value.length,1)
})
