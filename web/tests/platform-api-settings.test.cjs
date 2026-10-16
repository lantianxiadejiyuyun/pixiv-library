const test=require('node:test'),assert=require('node:assert/strict')
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm')
const ts=require('typescript'),Vue=require('vue'),{parse,compileScript}=require('@vue/compiler-sfc')
const source=compileScript(parse(fs.readFileSync(path.join(__dirname,'../src/components/PlatformApiSettings.vue'),'utf8')).descriptor,{id:'platform-api-settings'}).content
const original=[
  {platform:'pixiv',label:'Pixiv',default_url:'https://app-api.pixiv.net',custom_url:null,enabled:false,effective_url:'https://app-api.pixiv.net',description:'作品 API'},
  {platform:'kemono',label:'Kemono',default_url:'https://kemono.cr/api/v1',custom_url:'https://mirror.example/api/v1',enabled:true,effective_url:'https://mirror.example/api/v1',description:'公开图文 API'},
]
function harness({reads=[],failure=false,write}={}) {
  const requests=[],unmounted=[]
  const imports={vue:{...Vue,onMounted(){},onBeforeUnmount(fn){unmounted.push(fn)}},'lucide-vue-next':{},'../api':{errorMessage:e=>e.message,api:async(url,options={})=>{
    const body=options.body?JSON.parse(options.body):null;requests.push({url,method:options.method,body})
    if(!options.method){const result=reads.length?reads.shift():{items:structuredClone(original)};if(result instanceof Error)throw result;return result}
    if(failure)throw new Error('API 地址未保存')
    if(write)return write(url,body)
    const item=original.find(item=>url.endsWith('/'+item.platform))
    return {...item,...body,effective_url:body.enabled?body.custom_url:item.default_url}
  }}}
  const context={exports:{},require:name=>imports[name]||require(name)}
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
  return {state:context.exports.default.setup({}, {expose(){}}),requests,unmounted}
}

test('平台 API 页面只读取书库服务配置，不访问或自动切换远端平台',async()=>{
  const h=harness();await h.state.load()
  assert.deepEqual(h.requests,[{url:'/platform-endpoints',method:undefined,body:null}])
  assert.equal(h.state.items.value.length,2)
  assert.equal(h.state.dirty(h.state.items.value[0]),false)
})

test('只保存所选平台，关闭自定义保留其地址和其他平台未保存编辑',async()=>{
  const h=harness();await h.state.load()
  h.state.drafts.pixiv.custom_url='https://pixiv-mirror.example';h.state.drafts.pixiv.enabled=true
  h.state.drafts.kemono.enabled=false
  await Promise.all([h.state.save(h.state.items.value[1]),h.state.save(h.state.items.value[1])])
  const writes=h.requests.filter(r=>r.method)
  assert.equal(writes.length,1)
  assert.deepEqual(writes[0],{url:'/platform-endpoints/kemono',method:'PUT',body:{custom_url:'https://mirror.example/api/v1',enabled:false}})
  assert.equal(h.state.drafts.kemono.custom_url,'https://mirror.example/api/v1')
  assert.equal(h.state.items.value[1].effective_url,'https://kemono.cr/api/v1')
  assert.equal(h.state.drafts.pixiv.custom_url,'https://pixiv-mirror.example')
  assert.equal(h.state.dirty(h.state.items.value[0]),true)
})

test('恢复默认先显示待保存，保存仅清除当前平台自定义配置',async()=>{
  const h=harness();await h.state.load()
  const item=h.state.items.value[1]
  h.state.restoreDefault(item)
  assert.equal(h.requests.length,1)
  assert.equal(item.effective_url,'https://mirror.example/api/v1')
  assert.equal(h.state.dirty(item),true)
  await h.state.save(item)
  assert.deepEqual(h.requests.at(-1).body,{custom_url:null,enabled:false})
  assert.equal(h.state.items.value[1].effective_url,'https://kemono.cr/api/v1')
})

test('保存失败保留输入与原生效地址，启用空地址不发请求',async()=>{
  const h=harness({failure:true});await h.state.load()
  const item=h.state.items.value[0]
  h.state.drafts.pixiv.enabled=true
  await h.state.save(item)
  assert.equal(h.requests.length,1);assert.match(h.state.error.value,/请填写/)
  h.state.drafts.pixiv.custom_url='https://bad.example'
  await h.state.save(item)
  assert.equal(h.state.drafts.pixiv.custom_url,'https://bad.example')
  assert.equal(h.state.items.value[0].effective_url,original[0].effective_url)
  assert.match(h.state.error.value,/未保存/)
  assert.equal(h.state.message.value,'')
})

test('旧读取和离开页面后的请求不能覆盖配置，失败读取可重试',async()=>{
  let finish
  const h=harness({reads:[new Promise(resolve=>finish=resolve),new Error('读取失败'),{items:structuredClone(original)}]})
  const slow=h.state.load();await h.state.load()
  assert.match(h.state.error.value,/读取失败/)
  await h.state.load();finish({items:[]});await slow
  assert.equal(h.state.items.value.length,2)
  h.unmounted.forEach(fn=>fn());const count=h.requests.length
  await h.state.load();await h.state.save(h.state.items.value[0])
  assert.equal(h.requests.length,count)
})

test('离开页面后忽略迟到保存，不替换原生效地址',async()=>{
  let finish
  const h=harness({write:()=>new Promise(resolve=>finish=resolve)});await h.state.load()
  const item=h.state.items.value[0];h.state.drafts.pixiv.enabled=true;h.state.drafts.pixiv.custom_url='https://new.example'
  const pending=h.state.save(item);h.unmounted.forEach(fn=>fn())
  finish({...item,enabled:true,custom_url:'https://new.example',effective_url:'https://new.example'});await pending
  assert.equal(h.state.items.value[0].effective_url,original[0].effective_url)
  assert.equal(h.state.message.value,'')
})
