const test=require('node:test'),assert=require('node:assert/strict')
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript'),Vue=require('vue')
const {parse,compileScript}=require('@vue/compiler-sfc')
const source=compileScript(parse(fs.readFileSync(path.join(__dirname,'../src/components/TagBrowser.vue'),'utf8')).descriptor,{id:'tag-browser'}).content
function harness(){
 const requests=[],unmount=[];let failure=false,resolver=null
 const context={exports:{},URLSearchParams,setTimeout,clearTimeout,require:name=>({'vue':{...Vue,onMounted(){},onBeforeUnmount(fn){unmount.push(fn)}},'../api':{errorMessage:e=>e.message,async api(url){requests.push(url);if(failure)throw Error('offline');if(resolver)return new Promise(resolve=>resolver(resolve));const p=new URL('http://test'+url).searchParams;return Array.from({length:200},(_,i)=>({id:String(Number(p.get('offset'))+i),name:'Tag '+i,count:1}))}}}[name]||require(name))}
 vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
 return {state:context.exports.default.setup({},{expose(){},emit(){}}),requests,unmount,fail(){failure=true},delay(fn){resolver=fn}}
}
test('tag browser requests bounded pages and appends without losing first page',async()=>{
 const h=harness();await h.state.load();await h.state.load(true)
 assert.equal(h.state.rows.value.length,400);assert.match(h.requests[0],/limit=200/);assert.match(h.requests[1],/offset=200/)
})
test('failed tag requests retain visible rows and allow retry',async()=>{
 const h=harness();await h.state.load();h.fail();await h.state.load()
 assert.equal(h.state.rows.value.length,200);assert.equal(h.state.error.value,'offline');assert.equal(h.state.loading.value,false)
})
test('late tag requests after leaving cannot replace visible rows',async()=>{
 const h=harness();await h.state.load();let finish;h.delay(resolve=>finish=resolve)
 const pending=h.state.load();h.unmount.forEach(fn=>fn());finish([{id:'late',name:'Late'}]);await pending
 assert.equal(h.state.rows.value.length,200)
})
