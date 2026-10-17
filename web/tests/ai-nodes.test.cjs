const test=require('node:test'),assert=require('node:assert/strict')
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm')
const ts=require('typescript'),Vue=require('vue'),{parse,compileScript}=require('@vue/compiler-sfc')
const source=compileScript(parse(fs.readFileSync(path.join(__dirname,'../src/components/AINodes.vue'),'utf8')).descriptor,{id:'ai-nodes'}).content
const original={items:[{id:'node1',label:'AI 电脑',base_url:'http://192.168.1.20:9010',enabled:true,has_key:true},{id:'node2',label:'备用电脑',base_url:'http://192.168.1.21:9010',enabled:true,has_key:true}],image_node_id:null}
function harness({fail=false,confirm=true,slowStatus=null}={}) {
  const requests=[],events=[],unmount=[],timers=[]
  async function api(url,options={}) {
    const body=options.body?JSON.parse(options.body):undefined
    requests.push({url,...options,body})
    if(fail && options.method)throw new Error('远程操作失败')
    if(url==='/ai/nodes' && !options.method)return JSON.parse(JSON.stringify(original))
    if(url.endsWith('/test'))return slowStatus||{services:[{service:'ollama',state:'running',status:'Up'}]}
    if(url.endsWith('/models') && !options.method)return {models:[{name:'synthetic:1'}],running:[]}
    if(url.endsWith('/vision'))return {available:true,models:[]}
    if(url.endsWith('/operations'))return {items:[]}
    return {id:'node1',...body}
  }
  const imports={vue:{...Vue,onMounted(){},onBeforeUnmount(fn){unmount.push(fn)}},'lucide-vue-next':{},'../api':{api,post:(url,body)=>api(url,{method:'POST',body:JSON.stringify(body)}),errorMessage:e=>e.message}}
  const context={exports:{},require:name=>imports[name]||require(name),window:{confirm:()=>confirm},setTimeout(fn){timers.push(fn);return timers.length},clearTimeout(){}}
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
  return {state:context.exports.default.setup({},{expose(){},emit:(...args)=>events.push(args)}),requests,events,unmount,timers}
}

test('节点页读取配置不自动访问远程电脑、启停容器或下载模型',async()=>{
  const h=harness();await h.state.load()
  assert.equal(h.requests.length,1);assert.equal(h.requests[0].url,'/ai/nodes')
  assert.equal(h.state.selected.value,'node1');assert.equal(h.state.imageNode.value,'')
})
test('节点密钥仅保存时发送，成功清空；编辑不读取或回填旧密钥',async()=>{
  const h=harness();await h.state.load();h.state.edit(h.state.nodes.value[0])
  assert.equal(h.state.form.key,'')
  h.state.form.key='synthetic-controller-key';await h.state.save()
  const request=h.requests.find(r=>r.method==='PUT')
  assert.equal(request.body.key,'synthetic-controller-key')
  assert.equal(h.state.form.key,'');assert.equal(h.state.editor.value,false)
  assert(!h.requests.some(r=>r.url.endsWith('/routing')))
})
test('保存失败保留连接编辑和密钥，失败容器操作不会被刷新掩盖',async()=>{
  const h=harness({fail:true});await h.state.load();h.state.edit(h.state.nodes.value[0]);h.state.form.key='keep-me'
  await h.state.save();assert.equal(h.state.form.key,'keep-me');assert.equal(h.state.editor.value,true)
  await h.state.action('/services/ollama/stop')
  assert.match(h.state.error.value,/远程操作失败/)
  assert(!h.requests.some(r=>r.url.endsWith('/test')))
})
test('切换图片执行位置必须显式保存，只提交所选节点编号',async()=>{
  const h=harness();await h.state.load();h.state.imageNode.value='node2'
  assert.equal(h.requests.length,1)
  await h.state.routeImages()
  assert.deepEqual(h.requests.at(-1).body,{image_node_id:'node2'})
  assert.equal(h.requests.at(-1).url,'/ai/nodes/routing')
  assert.match(h.state.message.value,/新的图片识别任务/)
})
test('停止容器和删除模型取消时不发请求',async()=>{
  const h=harness({confirm:false});await h.state.load()
  await h.state.control('ollama','stop');await h.state.removeModel('synthetic:1');await h.state.removeNode()
  assert.equal(h.requests.length,1)
})
test('迟到的节点状态不会覆盖已经切换的节点',async()=>{
  let finish;const pending=new Promise(resolve=>finish=resolve)
  const h=harness({slowStatus:pending});await h.state.load()
  const loading=h.state.refresh();h.state.selected.value='node2';h.state.selectNode()
  finish({services:[{service:'ollama',state:'running',status:'old-node'}]});await loading
  assert.equal(h.state.services.value.length,0)
  assert.equal(h.state.models.value.length,0)
  assert.equal(h.state.selected.value,'node2')
})
test('为汉化选择远程模型后保留明确的成功反馈',async()=>{
  const h=harness();await h.state.load()
  await h.state.action('/translation',{model:'synthetic:1'})
  assert.match(h.state.message.value,/已启用此节点/)
  assert.deepEqual(h.requests.find(r=>r.url.endsWith('/translation')).body,{model:'synthetic:1'})
  assert.equal(h.state.models.value[0].name,'synthetic:1')
})
