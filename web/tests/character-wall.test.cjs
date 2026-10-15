const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm')
const ts=require('typescript'),Vue=require('vue'),{parse,compileScript}=require('@vue/compiler-sfc')
const source=compileScript(parse(fs.readFileSync(path.join(__dirname,'../src/components/CharacterWall.vue'),'utf8')).descriptor,{id:'character-wall'}).content
const person={id:'role',name:'hero',display_name:'英雄',count:1,covers:[],profile:{name:'Hero',aliases:['英雄'],description:'已保存简介',works:[],source_url:'https://anilist.co/character/17'}}
function harness({slow,failSave=false,failMore=false}={}){
  const calls=[],events=[],unmounted=[];const candidate={id:'17',name:'Hero',aliases:[],works:[],source_url:'https://anilist.co/character/17'}
  const imports={vue:{...Vue,onMounted(){},onBeforeUnmount(fn){unmounted.push(fn)},watch(){}},'lucide-vue-next':{},'../components/BookCard.vue':{},'./BookCard.vue':{},'./Modal.vue':{},'../types':{sourceLabels:{}},'../api':{errorMessage:e=>e.message,api:async(url,options={})=>{
    calls.push({url,...options});if(url==='/characters/search')return {candidates:[candidate]}
    if(options.method){if(failSave)throw new Error('保存失败');return {...person}}
    if(url.startsWith('/characters/role?'))return {...person}
    if(url.startsWith('/items?'))return {items:[{id:'book',title:'本地漫画'}],total:1}
    if(slow){const result=slow;slow=null;return result}
    if(failMore&&url.includes('page=2'))throw new Error('第二页失败')
    return {items:[{...person,id:url.includes('page=2')?'second':'role'}],total:2}
  }}}
  const context={exports:{},require:name=>imports[name]||require(name),URLSearchParams,setTimeout,clearTimeout}
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
  return{state:context.exports.default.setup({blurAdult:true},{expose(){},emit:(...args)=>events.push(args)}),calls,unmounted}
}
test('人物墙分页只读取本地，角色作品带正确标签与筛选',async()=>{
  const h=harness();await h.state.load();await h.state.load(true)
  assert.equal(h.state.characters.value.length,2);assert(!h.calls.some(c=>c.method))
  h.state.source.value='pixiv';h.state.downloaded.value=true;h.state.open(person);await new Promise(r=>setImmediate(r))
  assert.equal(h.state.selected.value.profile.description,'已保存简介')
  const url=h.calls.find(c=>c.url.startsWith('/items?')).url
  assert.match(url,/tag=role/);assert.match(url,/source=pixiv/);assert.match(url,/downloaded=true/)
})
test('联网候选不会自动绑定，明确确认时只发送候选 ID',async()=>{
  const h=harness();await h.state.load();h.state.openLookup();h.state.lookupQuery.value='Hero';await h.state.search()
  assert.equal(h.calls.filter(c=>c.method).length,1)
  const candidate=h.state.candidates.value[0];await h.state.confirm(candidate)
  const write=h.calls.find(c=>c.url==='/characters'&&c.method)
  assert.deepEqual(JSON.parse(write.body),{candidate_id:17});assert.equal(h.state.selected.value.id,'role')
})
test('保存失败保留资料和候选，补全绑定到当前人物',async()=>{
  const h=harness({failSave:true});await h.state.load();h.state.selected.value=person
  h.state.openLookup();h.state.lookupQuery.value='Hero';await h.state.search();await h.state.confirm(h.state.candidates.value[0])
  assert(h.calls.some(c=>c.url==='/characters/role/profile'));assert.equal(h.state.lookup.value,true)
  assert.equal(h.state.selected.value.profile.description,'已保存简介');assert.match(h.state.lookupError.value,/保存失败/)
})
test('旧列表不能覆盖新结果，追加失败保留旧页并允许重试',async()=>{
  let finish;const h=harness({slow:new Promise(r=>finish=r),failMore:true})
  const old=h.state.load();await h.state.load();finish({items:[{...person,id:'old'}],total:1});await old
  assert.equal(h.state.characters.value[0].id,'role')
  await h.state.load(true);assert.equal(h.state.characters.value.length,1);assert.equal(h.state.failedMore.value,true)
  h.unmounted.forEach(fn=>fn());const count=h.calls.length;await h.state.load();assert.equal(h.calls.length,count)
})
