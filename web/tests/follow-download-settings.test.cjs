const test = require('node:test'), assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript'), Vue = require('vue')
const {parse,compileScript} = require('@vue/compiler-sfc')
const text = fs.readFileSync(path.join(__dirname,'../src/components/PixivFollowDownloadSettings.vue'),'utf8')
const source = ts.transpileModule(compileScript(parse(text).descriptor,{id:'follow-download-test'}).content,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText
function deferred(){let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}}
function harness(handler) {
  const calls=[],unmount=[]
  const imports={vue:{...Vue,onMounted(){},onBeforeUnmount:fn=>unmount.push(fn)},'../api':{errorMessage:e=>e.message,api:async(url,options={})=>{
    const call={url,method:options.method||'GET',body:options.body?JSON.parse(options.body):null};calls.push(call);return handler(call)
  }}}
  const context={exports:{},require:name=>imports[name]||require(name)}
  vm.runInNewContext(source,context)
  return {state:context.exports.default.setup({}, {expose(){}}),calls,unmount}
}
test('default switch loads persisted state and saves separately from bookmarks',async()=>{
  let saved=true
  const h=harness(call=>{if(call.method==='PUT')saved=call.body.enabled;return {enabled:saved}})
  await h.state.load();assert.equal(h.state.enabled.value,true)
  await h.state.save();assert.equal(saved,false);assert.equal(h.state.enabled.value,false)
  assert.deepEqual(h.calls[1],{url:'/pixiv/follow-download-settings',method:'PUT',body:{enabled:false}})
  const reopened=harness(()=>({enabled:saved}));await reopened.state.load();assert.equal(reopened.state.enabled.value,false)
  assert.match(text,/role="switch"/);assert.match(text,/:aria-checked="enabled"/)
})
test('failed initial load cannot overwrite settings and can be retried',async()=>{
  let failing=true
  const h=harness(()=>{if(failing)throw Error('network');return {enabled:true}})
  await h.state.load();await h.state.save();assert.equal(h.calls.length,1);assert.equal(h.state.loaded.value,false)
  failing=false;await h.state.load();assert.equal(h.state.loaded.value,true);assert.equal(h.state.enabled.value,true)
})
test('failed save retains original value and concurrent clicks send one write',async()=>{
  let fail=true;const pending=deferred()
  const h=harness(call=>{if(call.method==='GET')return {enabled:false};if(fail)throw Error('save failed');return pending.promise})
  await h.state.load();await h.state.save();assert.equal(h.state.enabled.value,false);assert.equal(h.state.error.value,'save failed')
  fail=false;const write=h.state.save();await h.state.save();assert.equal(h.calls.length,3)
  pending.resolve({enabled:true});await write;assert.equal(h.state.enabled.value,true);assert.equal(h.state.busy.value,false)
})
test('leaving the page ignores a delayed response',async()=>{
  const pending=deferred(),h=harness(()=>pending.promise)
  const read=h.state.load();h.unmount.forEach(fn=>fn());pending.resolve({enabled:true});await read
  assert.equal(h.state.loaded.value,false);assert.equal(h.state.enabled.value,false)
})
