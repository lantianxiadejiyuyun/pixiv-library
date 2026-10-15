const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm')
const ts = require('typescript'), Vue = require('vue')
const {parse, compileScript} = require('@vue/compiler-sfc')
const source = compileScript(parse(fs.readFileSync(path.join(__dirname, '../src/components/PixivAccountRoles.vue'), 'utf8')).descriptor, {id:'pixiv-roles'}).content
const original = {enabled:false, main_account_id:'main', content_account_id:'content', main:{id:'main', label:'主账号', status:'verified'}, content:{id:'content', label:'副账号', status:'unverified'}}
function harness({failure=false, reads=[]}={}) {
  const requests=[], events=[]
  let changed
  const imports = {
    vue: {...Vue, onMounted(){}, watch(_source, callback) {changed=callback}},
    '../api': {api:async (url, options={})=> {
      requests.push({url, ...options})
      if (!options.method) return reads.length ? reads.shift() : {...original}
      if (failure) throw new Error('账号未保存')
      return {...original, ...JSON.parse(options.body)}
    }, errorMessage:e=>e.message}, 'lucide-vue-next': {},
  }
  const context={exports:{}, require:name=>imports[name] || require(name)}
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
  const state=context.exports.default.setup({accounts:[{id:'main',label:'主账号'},{id:'content',label:'副账号'}]}, {expose(){}, emit:(...args)=>events.push(args)})
  return {state, requests, events, changed}
}
test('读取分工展示两个独立状态，不自动启用或同步',async()=>{
  const h=harness(); await h.state.load()
  assert.equal(h.requests.length,1); assert.equal(h.state.form.enabled,false)
  assert.equal(h.state.state.value.main.status,'verified'); assert.equal(h.state.state.value.content.status,'unverified')
  assert.equal(h.state.dirty.value,false)
})
test('只有保存时发送账号分工，不发送凭据；重复保存请求合并',async()=>{
  const h=harness(); await h.state.load(); h.state.form.enabled=true
  const first=h.state.save(), duplicate=h.state.save(); await Promise.all([first,duplicate])
  assert.equal(h.requests.length,2)
  assert.deepEqual(JSON.parse(h.requests.at(-1).body),{enabled:true,main_account_id:'main',content_account_id:'content'})
  assert.equal(h.state.state.value.enabled,true); assert.equal(h.state.dirty.value,false)
  assert.match(h.state.message.value,/已启用/)
})
test('保存失败保留修改，账号列表变化不覆盖未保存选择',async()=>{
  const h=harness({failure:true}); await h.state.load(); h.state.form.content_account_id='replacement'
  await h.changed(); assert.equal(h.requests.length,1)
  await h.state.save(); assert.equal(h.state.form.content_account_id,'replacement')
  assert.match(h.state.error.value,/未保存/); assert.equal(h.state.state.value.enabled,false)
  assert.equal(h.state.dirty.value,true)
})
test('较晚返回的旧读取不会覆盖新分工状态',async()=>{
  let finish
  const deferred=new Promise(resolve=>finish=resolve)
  const h=harness({reads:[deferred,{...original,enabled:true}]})
  const slow=h.state.load(); await h.state.load()
  finish({...original,enabled:false}); await slow
  assert.equal(h.state.form.enabled,true); assert.equal(h.state.loading.value,false)
})
