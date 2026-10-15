const test=require('node:test'),assert=require('node:assert/strict')
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm')
const ts=require('typescript'),Vue=require('vue'),{parse,compileScript}=require('@vue/compiler-sfc')
const source=compileScript(parse(fs.readFileSync(path.join(__dirname,'../src/components/DownloadAccounts.vue'),'utf8')).descriptor,{id:'download-accounts'}).content
const original={enabled:false,main_account_id:'main',content_account_id:'first',content_account_ids:['first'],download_accounts:[
  {id:'main',label:'主账号',status:'verified',running:0,completed:0,failed:0},
  {id:'first',label:'副账号一',status:'verified',running:2,completed:7,failed:1},
  {id:'second',label:'副账号二',status:'unverified',running:0,completed:0,failed:0},
]}
function harness({failure=false,reads=[]}={}) {
  const requests=[],events=[],unmounted=[],watches=[]
  const imports={vue:{...Vue,onMounted(){},onBeforeUnmount(fn){unmounted.push(fn)},watch(fn,callback){watches.push(callback)}},'lucide-vue-next':{},'../api':{errorMessage:e=>e.message,api:async(url,options={})=>{
    requests.push({url,...options})
    if(!options.method){const value=reads.length?reads.shift():JSON.parse(JSON.stringify(original));if(value instanceof Error)throw value;return value}
    if(failure)throw new Error('账号设置未保存')
    if(options.method==='DELETE')return {ok:true}
    return {...original,...JSON.parse(options.body)}
  }}}
  const context={exports:{},require:name=>imports[name]||require(name),setInterval(){},clearInterval(){}}
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
  return {state:context.exports.default.setup({accounts:[]},{expose(){},emit:(...args)=>events.push(args)}),requests,events,unmounted,watches}
}
test('账号页仅加载配置与独立统计，不自动启用或下载',async()=>{
  const h=harness();await h.state.load()
  assert.equal(h.requests.length,1);assert.equal(h.requests[0].method,undefined)
  assert.equal(h.state.form.enabled,false);assert.equal(h.state.accounts.value[1].running,2)
  assert.equal(h.state.dirty.value,false)
})
test('账号数量不限制为并发数，保存全部副账号且不提交凭据',async()=>{
  const ids=Array.from({length:100},(_,index)=>`secondary-${index}`)
  const config={...original,download_accounts:[...original.download_accounts,...ids.map(id=>({id,label:id,status:'verified',running:0,completed:0,failed:0}))]}
  const h=harness({reads:[config]});await h.state.load()
  h.state.form.content_account_ids=ids
  h.state.form.enabled=true
  await Promise.all([h.state.save(),h.state.save()])
  const writes=h.requests.filter(r=>r.method)
  assert.equal(writes.length,1)
  const payload=JSON.parse(writes[0].body)
  assert.equal(payload.content_account_ids.length,100)
  assert.deepEqual(Object.keys(payload).sort(),['account_limits','check_account_ids','content_account_ids','detail_account_ids','enabled','main_account_id'])
  assert.equal(h.state.error.value,'')
})

test('旧响应中的已移除账号不出现在列表、计数和下拉框，清除残留选择和并发值',async()=>{
  const removed={...original,enabled:true,detail_account_ids:['first','second'],check_account_ids:['first'],account_limits:{main:3,first:4,second:2},download_accounts:original.download_accounts.map(row=>row.id==='first'?{...row,status:'missing'}:row)}
  const h=harness({reads:[removed]});await h.state.load()
  assert.deepEqual(Array.from(h.state.accounts.value,row=>row.id),['main','second'])
  assert.deepEqual(Array.from(h.state.form.content_account_ids),[])
  assert.deepEqual(Array.from(h.state.form.detail_account_ids),['second'])
  assert.deepEqual(Array.from(h.state.form.check_account_ids),[])
  assert.deepEqual(Object.keys(h.state.form.account_limits).sort(),['main','second'])
  assert.equal(h.state.dirty.value,true)
})

test('轮询保留有效编辑但剔除刚删除的账号，缺失主账号需要重新选择',async()=>{
  const updated={...original,enabled:true,missing_account_ids:['main','first'],download_accounts:original.download_accounts.filter(row=>row.id==='second')}
  const h=harness({reads:[original,updated]});await h.state.load()
  h.state.form.detail_account_ids=['first','second'];h.state.form.account_limits.second=7
  await h.state.load(true)
  assert.equal(h.state.form.main_account_id,'')
  assert.deepEqual(Array.from(h.state.form.content_account_ids),[])
  assert.deepEqual(Array.from(h.state.form.detail_account_ids),['second'])
  assert.equal(h.state.form.account_limits.second,7)
})

test('移除成功后立即隐藏，后续读取失败也不恢复旧行',async()=>{
  const h=harness({reads:[original,new Error('刷新失败')]})
  await h.state.load();h.state.form.detail_account_ids=['second']
  await h.state.remove('second');await h.state.remove('second')
  assert(!h.state.accounts.value.some(row=>row.id==='second'))
  assert(!h.state.form.detail_account_ids.includes('second'))
  assert.equal(h.state.error.value,'刷新失败')
})
test('轮询刷新统计但保留未保存选择，失败保留输入和当前生效配置',async()=>{
  const h=harness({failure:true});await h.state.load()
  h.state.form.content_account_ids=['second'];h.state.form.enabled=true
  await h.state.load(true);await h.state.save()
  assert.deepEqual(Array.from(h.state.form.content_account_ids),['second'])
  assert.equal(h.state.state.value.enabled,false);assert.equal(h.state.dirty.value,true)
  assert.match(h.state.error.value,/未保存/)
})
test('搜索与批量选择排除主账号和已移除账号，切换主账号移除同名下载选择',async()=>{
  const h=harness();await h.state.load()
  h.state.query.value='副账号二';h.state.selectVisible(true)
  assert.deepEqual(Array.from(h.state.form.content_account_ids),['first','second'])
  h.state.selectVisible(false)
  assert.deepEqual(Array.from(h.state.form.content_account_ids),['first'])
  h.watches[0]('first')
  assert.equal(h.state.form.content_account_ids.length,0)
  h.state.query.value='';h.state.selectVisible(true)
  assert(!h.state.form.content_account_ids.includes('main'))
})
test('功能统计和筛选可组合搜索，批量下载只操作当前结果',async()=>{
  const h=harness();await h.state.load()
  h.state.form.detail_account_ids=['second']
  h.state.functionFilter.value='details'
  assert.equal(h.state.roleCounts.value.details,1)
  assert.deepEqual(Array.from(h.state.filtered.value,row=>row.id),['second'])
  h.state.selectVisible(true)
  assert.deepEqual(Array.from(h.state.form.content_account_ids),['first','second'])
  h.state.query.value='不存在'
  assert.equal(h.state.filtered.value.length,0)
  h.state.functionFilter.value='all';h.state.query.value=''
  assert.equal(h.state.roleCounts.value.all,3)
})
test('启用分工必须选择主账号及副账号',async()=>{
  const h=harness();await h.state.load()
  h.state.form.enabled=true;h.state.form.content_account_ids=[]
  await h.state.save();assert.equal(h.requests.filter(r=>r.method).length,0)
  assert.match(h.state.error.value,/至少一种/)
})
test('移除需明确确认，通知外层刷新登录列表',async()=>{
  const h=harness();await h.state.load();await h.state.remove('second')
  assert.equal(h.requests.filter(r=>r.method).length,0)
  assert.equal(h.state.confirmRemove.value,'second')
  await h.state.remove('second')
  assert.equal(h.requests.filter(r=>r.method==='DELETE').length,1)
  assert(h.events.some(e=>e[0]==='removed'))
})
test('迟到响应和离开页面后的响应不能覆盖新账号状态',async()=>{
  let finish;const slow=new Promise(resolve=>finish=resolve)
  const h=harness({reads:[slow,{...original,enabled:true}]})
  const pending=h.state.load();await h.state.load()
  finish(original);await pending
  assert.equal(h.state.form.enabled,true)
  h.unmounted.forEach(fn=>fn());const count=h.requests.length;await h.state.load()
  assert.equal(h.requests.length,count)
})


test('同账号可多选下载详情检查，分别保存各账号并发',async()=>{
  const h=harness();await h.state.load()
  h.state.form.enabled=true;h.state.form.detail_account_ids=['first'];h.state.form.check_account_ids=['first','second']
  h.state.form.account_limits.first=4;h.state.form.account_limits.second=2;h.state.form.account_limits.main=3
  await h.state.save()
  const payload=JSON.parse(h.requests.find(r=>r.method==='PUT').body)
  assert.deepEqual(payload.detail_account_ids,['first']);assert.deepEqual(payload.check_account_ids,['first','second'])
  assert.deepEqual(payload.account_limits,{main:3,first:4,second:2})
  assert.deepEqual(payload.content_account_ids,['first'])
})
