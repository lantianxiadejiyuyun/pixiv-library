const test=require('node:test'),assert=require('node:assert/strict')
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm')
const ts=require('typescript'),Vue=require('vue'),{parse,compileScript}=require('@vue/compiler-sfc')
const root=path.resolve(__dirname,'..')
function evaluate(source,imports={},globals={}) {
  const context={exports:{},require:name=>imports[name]||{},...globals}
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
  return context.exports
}
const preferences=evaluate(fs.readFileSync(path.join(root,'src/item_preferences.ts'),'utf8'))
const types=evaluate(fs.readFileSync(path.join(root,'src/types.ts'),'utf8'))
const source=compileScript(parse(fs.readFileSync(path.join(root,'src/App.vue'),'utf8')).descriptor,{id:'item-preferences-test'}).content
const work={id:'work',title:'旧名称',tags:[],user_score:2,is_favorite:true,favorite_group_ids:['g1']}
function harness(handler) {
  const imports={vue:{...Vue,watch(){},onMounted(){},onBeforeUnmount(){}},'./api':{api:handler,errorMessage:e=>e.message},'./types':types,'./item_preferences':preferences}
  return evaluate(source,imports,{localStorage:{getItem(){return null}},URLSearchParams,setTimeout,clearTimeout}).default.setup({}, {expose(){},emit(){}})
}
test('详情轮询迟到时保留其间保存的评分，仍接受其他设备的收藏变更',async()=>{
  let finish;const state=harness(()=>new Promise(resolve=>finish=resolve))
  state.selectedItem.value={...work};state.modal.value='detail'
  const pending=state.refreshSelectedContent()
  state.favoriteChanged({id:'work',user_score:5})
  finish({...work,title:'新名称',is_favorite:false,favorite_group_ids:[]});await pending
  assert.equal(state.selectedItem.value.user_score,5)
  assert.equal(state.selectedItem.value.title,'新名称');assert.equal(state.selectedItem.value.is_favorite,false)
})
test('书库列表迟到时保留其间保存的收藏，仍接受远端评分变更',async()=>{
  let finish;const state=harness(()=>new Promise(resolve=>finish=resolve))
  state.items.value=[{...work}]
  const pending=state.loadLibrary()
  state.favoriteChanged({id:'work',is_favorite:false,favorite_group_ids:[]})
  finish({items:[{...work,user_score:3}],total:1});await pending
  assert.equal(state.items.value[0].is_favorite,false);assert.equal(state.items.value[0].user_score,3)
})
test('评分和收藏同时修改后，迟到详情与列表均保留各字段的新值',async()=>{
  const resolve=[];const state=harness(()=>new Promise(done=>resolve.push(done)))
  state.selectedItem.value={...work};state.modal.value='detail';state.items.value=[{...work}]
  const detail=state.refreshSelectedContent(),list=state.loadLibrary()
  state.favoriteChanged({id:'work',user_score:0})
  state.favoriteChanged({id:'work',is_favorite:true,favorite_group_ids:['g2']})
  resolve[1]({items:[{...work}],total:1});resolve[0]({...work});await Promise.all([detail,list])
  for(const item of [state.selectedItem.value,state.items.value[0]]) {
    assert.equal(item.user_score,0);assert.deepEqual(item.favorite_group_ids,['g2'])
  }
})
test('新请求接受其他设备最新评分，不永久固定本地旧值',async()=>{
  const state=harness(async()=>({...work,user_score:1,is_favorite:false,favorite_group_ids:[]}))
  state.selectedItem.value={...work};state.modal.value='detail'
  state.favoriteChanged({id:'work',user_score:5})
  await state.refreshSelectedContent()
  assert.equal(state.selectedItem.value.user_score,1);assert.equal(state.selectedItem.value.is_favorite,false)
})
test('偏好更改不影响另一作品及非偏好字段',()=>{
  const tracker=preferences.createItemPreferenceTracker(),merge=tracker.snapshot()
  tracker.record({id:'other',user_score:5})
  assert.deepEqual(merge(work),work)
})
