const test=require('node:test'),assert=require('node:assert/strict')
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm')
const ts=require('typescript'),Vue=require('vue'),{parse,compileScript}=require('@vue/compiler-sfc')
const root=path.resolve(__dirname,'..')
function evaluate(source,imports={},globals={}) {
  const context={exports:{},require:name=>imports[name]||{},...globals}
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
  return context.exports
}
const types=evaluate(fs.readFileSync(path.join(root,'src/types.ts'),'utf8'))
const preferences=evaluate(fs.readFileSync(path.join(root,'src/item_preferences.ts'),'utf8'))
const source=compileScript(parse(fs.readFileSync(path.join(root,'src/App.vue'),'utf8')).descriptor,{id:'kemono-source'}).content
const kemono={platform:'kemono',label:'Kemono',status:'available',capabilities:[],accounts:[]}
function harness() {
  const calls=[]
  const imports={vue:{...Vue,watch(){},onMounted(){},onBeforeUnmount(){}},'./api':{api:async()=>({}),post:async(url,body)=>{calls.push({url,body});return {id:'job'}},errorMessage:e=>e.message},'./types':types,'./item_preferences':preferences}
  const state=evaluate(source,imports,{localStorage:{getItem(){return null}},URLSearchParams,setTimeout:()=>1,clearTimeout(){},window:{history:{replaceState(){}}}}).default.setup({}, {expose(){}})
  return {state,calls}
}

test('Kemono 公开访问不打开登录，不声称关注同步可用',()=>{
  const {state,calls}=harness()
  assert.equal(state.sourceStatus(kemono),'公开访问')
  assert.equal(state.canSync(kemono),false)
  state.openAccount(kemono)
  assert.equal(state.modal.value,'');assert.equal(calls.length,0)
  assert(types.remotePlatforms.includes('kemono'));assert.equal(types.sourceLabels.kemono,'Kemono')
})

test('Kemono 链接和复合 ID 只提交书库导入接口，忽略残留账号',async()=>{
  const {state,calls}=harness()
  state.openRemote(kemono);state.remoteForm.url='https://kemono.cr/fanbox/user/12/post/34';state.remoteForm.account_id='stale-account'
  await state.importRemote()
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])),{url:'/sources/kemono/import',body:{url:'https://kemono.cr/fanbox/user/12/post/34'}})
  state.openRemote(kemono);state.remoteForm.url=' fanbox:12:34 '
  await state.importRemote()
  assert.deepEqual(JSON.parse(JSON.stringify(calls[1])),{url:'/sources/kemono/import',body:{remote_id:'fanbox:12:34'}})
})

test('Kemono 作者、标签与搜索订阅无需账号，切换平台保留订阅类型',async()=>{
  const {state,calls}=harness()
  state.openSourceSubscription(kemono)
  assert.equal(state.subscriptionForm.platform,'kemono')
  assert.equal(state.subscriptionForm.target_type,'author')
  state.subscriptionForm.target='fanbox:12';state.subscriptionForm.label='作者更新';state.subscriptionForm.account_id='stale-account'
  await state.saveSubscription()
  assert.equal(calls[0].url,'/subscriptions');assert.equal(calls[0].body.account_id,null)
  assert.equal(calls[0].body.target,'fanbox:12')
  state.subscriptionForm.target_type='search';state.subscriptionForm.target='landscape';await state.saveSubscription()
  assert.equal(calls.length,2);assert.equal(calls[1].body.target_type,'search')
  state.subscriptionForm.target_type='tag';state.subscriptionForm.target='art';await state.saveSubscription()
  assert.equal(calls.length,3);assert.equal(calls[2].body.target_type,'tag')
  state.changeSubscriptionPlatform()
  assert.equal(state.subscriptionForm.target_type,'tag');assert.equal(state.subscriptionForm.account_id,'')
})
