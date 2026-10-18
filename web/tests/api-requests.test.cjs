const test = require('node:test'), assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript')
const source = ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src/api.ts'),'utf8'), {compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText
function deferred() { let resolve,reject; const promise=new Promise((yes,no)=>{resolve=yes;reject=no});return {promise,resolve,reject} }
function harness() {
  const calls=[], events=[]
  const context={exports:{},Headers,FormData,Event,window:{dispatchEvent:event=>events.push(event.type)},fetch:(url,options)=>{
    const pending=deferred();calls.push({url,options,...pending});return pending.promise
  }}
  vm.runInNewContext(source,context)
  function reply(index,value,status=200) { calls[index].resolve({ok:status>=200&&status<300,status,json:async()=>value}) }
  return {...context.exports,calls,events,reply}
}
test('simultaneous JSON reads share transport and return independent objects',async()=>{
  const h=harness(), first=h.api('/jobs'), second=h.api('/jobs')
  assert.equal(h.calls.length,1);h.reply(0,{items:[{id:'a'}]})
  const [a,b]=await Promise.all([first,second]);a.items[0].id='changed'
  assert.equal(b.items[0].id,'a')
  const next=h.api('/jobs');assert.equal(h.calls.length,2);h.reply(1,{items:[]});await next
})
test('a failed read is not retained and can be retried',async()=>{
  const h=harness(), first=h.api('/jobs'), second=h.api('/jobs')
  h.calls[0].reject(new Error('network'));assert.equal((await Promise.allSettled([first,second])).every(result=>result.status==='rejected'),true)
  const next=h.api('/jobs');assert.equal(h.calls.length,2);h.reply(1,{items:[]});await next
})
test('writes fence old reads and late cleanup cannot remove a newer flight',async()=>{
  const h=harness(), old=h.api('/jobs'), write=h.post('/jobs/a/retry')
  h.reply(1,{ok:true});await write
  const current=h.api('/jobs');assert.equal(h.calls.length,3)
  h.reply(0,{state:'old'});await old
  const joined=h.api('/jobs');assert.equal(h.calls.length,3)
  h.reply(2,{state:'new'});assert.equal((await current).state,'new');assert.equal((await joined).state,'new')
})
test('custom signals and headers keep independent requests',async()=>{
  const h=harness(), first=h.api('/jobs',{headers:{'X-Test':'one'}}), second=h.api('/jobs',{headers:{'X-Test':'two'}})
  assert.equal(h.calls.length,2);h.reply(0,{});h.reply(1,{});await Promise.all([first,second])
})
test('raw response streams are never shared',async()=>{
  const h=harness(), first=h.apiResponse('/download'), second=h.apiResponse('/download')
  assert.equal(h.calls.length,2);h.reply(0,{});h.reply(1,{});assert.notEqual(await first,await second)
})
test('unauthorized requests dispatch once per transport and do not retain responses',async()=>{
  const h=harness(), first=h.api('/jobs'), second=h.api('/jobs')
  h.reply(0,{detail:'expired'},401);await Promise.allSettled([first,second]);assert.deepEqual(h.events,['library:unauthorized'])
  const next=h.api('/jobs');assert.equal(h.calls.length,2);h.reply(1,{});await next
})
