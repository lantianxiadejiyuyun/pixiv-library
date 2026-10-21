const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),ts=require('typescript'),Vue=require('vue')
const {parse,compileScript}=require('@vue/compiler-sfc')
function component(name,imports={}){
 const source=compileScript(parse(fs.readFileSync(path.join(__dirname,'../src/components/'+name+'.vue'),'utf8')).descriptor,{id:name}).content
 const context={exports:{},require:n=>imports[n]||({'vue':{...Vue,onMounted(){}}}[n])||require(n)}
 vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
 return context.exports.default
}
test('tag tracking uses original selected tags and enables queue downloads',async()=>{
 const calls=[],api=async(url,options={})=>{calls.push({url,body:options.body?JSON.parse(options.body):null});return {items:[]}}
 const panel=component('TagTrackingPanel',{'../api':{api,errorMessage:e=>e.message},'../types':{}})
 const state=panel.setup({tags:[{id:'1',name:'Original',name_zh:'翻译标签'}]},{expose(){}})
 await state.create(true)
 assert.equal(calls[0].url,'/tag-tracking');assert.deepEqual(calls[0].body.tags,['Original'])
 assert.equal(calls[0].body.auto_download,true);assert.equal(calls[0].body.content_scope,'all')
 assert.equal(state.status.value,'追踪已保存，检查任务已排队')
})
test('novel image has stable geometry, permits local asset URLs and retries independently',()=>{
 const props={image:{type:'image',url:'/api/assets/a1-b2/content',width:600,height:900}}
 const state=component('NovelInlineImage').setup(props,{expose(){}})
 assert.equal(state.ratio.value,2/3);assert.equal(state.url.value,props.image.url)
 state.failed.value=true;state.retry();assert.equal(state.failed.value,false);assert.equal(state.revision.value,1)
 const invalid=component('NovelInlineImage').setup({image:{url:'https://evil.test/image.png'}},{expose(){}})
 assert.equal(invalid.url.value,undefined);assert.equal(invalid.ratio.value,.75)
})
