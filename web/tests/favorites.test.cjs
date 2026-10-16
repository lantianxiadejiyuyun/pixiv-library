const test=require('node:test'),assert=require('node:assert/strict')
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm')
const ts=require('typescript'),Vue=require('vue'),{parse,compileScript}=require('@vue/compiler-sfc')
function harness(component,{history=false,failure=false,reads=[],writes=[]}={}) {
  const source=compileScript(parse(fs.readFileSync(path.join(__dirname,`../src/components/${component}.vue`),'utf8')).descriptor,{id:'favorites-test'}).content
  const calls=[],events=[],unmounted=[]
  const item={id:'work',title:'漫画',is_favorite:true,favorite_group_ids:['g1'],user_score:4}
  const imports={vue:{...Vue,onMounted(){},onBeforeUnmount(fn){unmounted.push(fn)},watch(){}},'lucide-vue-next':{},'../api':{errorMessage:e=>e.message,api:async(url,options={})=>{
    calls.push({url,...options})
    if(!options.method){
      if(url==='/favorite-groups')return {items:[{id:'g1',name:'想看',count:1},{id:'g2',name:'推荐',count:0}],total_favorites:1,ungrouped_count:0}
      return reads.length?reads.shift():{items:[item],total:1}
    }
    if(failure)throw new Error('保存失败')
    if(options.method==='DELETE')return {ok:true}
    const body=JSON.parse(options.body)
    if(writes.length)return writes.shift()
    if(url.endsWith('/score'))return {id:item.id,user_score:body.score}
    return url.includes('/favorite-groups')?{id:'new',name:body.name}:{is_favorite:body.favorite,favorite_group_ids:body.group_ids||[],user_score:item.user_score}
  }}}
  const context={exports:{},require:name=>imports[name]||{},URLSearchParams,setTimeout,clearTimeout}
  vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
  const props={item,blurAdult:true,history,revision:0}
  return {state:context.exports.default.setup(props,{expose(){},emit:(...args)=>events.push(args)}),calls,events,unmounted,props}
}
test('我的收藏组合分组、搜索、平台、类型及已下载条件，不发起写入',async()=>{
  const h=harness('FavoritesPanel');await h.state.loadGroups()
  h.state.selected.value='g1';h.state.query.value='中文';h.state.source.value='pixiv';h.state.type.value='manga';h.state.downloaded.value=true
  await h.state.load()
  const params=new URLSearchParams(h.calls.at(-1).url.split('?')[1])
  assert.equal(params.get('favorite'),'true');assert.equal(params.get('favorite_group'),'g1');assert.equal(params.get('q'),'中文');assert.equal(params.get('source'),'pixiv');assert.equal(params.get('type'),'manga');assert.equal(params.get('downloaded'),'true')
  assert(!h.calls.some(call=>call.method));assert.equal(h.state.all.value,1)
})
test('浏览历史只查询历史，明确清空才删除历史，不取消收藏',async()=>{
  const h=harness('FavoritesPanel',{history:true});await h.state.refresh()
  assert.equal(h.calls.length,1);assert.match(h.calls[0].url,/history=true/);assert(!h.calls[0].url.includes('favorite'))
  await h.state.remove()
  assert.deepEqual(h.calls.filter(call=>call.method).map(call=>[call.url,call.method]),[['/history','DELETE']])
})
test('分组新建和删除只写分组，失败保留输入',async()=>{
  const h=harness('FavoritesPanel');h.state.begin('create');h.state.name.value='珍藏';await h.state.saveGroup()
  assert.equal(h.state.selected.value,'new');assert.equal(JSON.parse(h.calls.find(call=>call.method==='POST').body).name,'珍藏')
  await h.state.remove();assert(h.calls.some(call=>call.url==='/favorite-groups/new'&&call.method==='DELETE'))
  const bad=harness('FavoritesPanel',{failure:true});bad.state.begin('create');bad.state.name.value='保留输入';await bad.state.saveGroup()
  assert.equal(bad.state.name.value,'保留输入');assert.equal(bad.state.edit.value,'create');assert.match(bad.state.editError.value,/失败/)
})
test('收藏旧请求和卸载后的响应不会覆盖新列表',async()=>{
  let finish;const h=harness('FavoritesPanel',{reads:[new Promise(resolve=>finish=resolve),{items:[{id:'new'}],total:1}]})
  const old=h.state.load();await h.state.load();finish({items:[{id:'old'}],total:1});await old
  assert.equal(h.state.items.value[0].id,'new')
  h.unmounted.forEach(fn=>fn());await h.state.load();assert.equal(h.state.items.value[0].id,'new')
})
test('编辑收藏只读取分组，明确保存只提交多个分组，不提交评分',async()=>{
  const h=harness('FavoriteControl');await h.state.edit();assert(!h.calls.some(call=>call.method))
  h.state.selected.value=['g1','g2'];await h.state.save(true)
  const body=JSON.parse(h.calls.find(call=>call.method==='PUT').body)
  assert.deepEqual(body,{favorite:true,group_ids:['g1','g2']});assert.equal(h.events[0][0],'change')
  assert.deepEqual(JSON.parse(JSON.stringify(h.events[0][1])),{id:'work',is_favorite:true,favorite_group_ids:['g1','g2']})
})

test('收藏保存失败保留分组，不向父级报告成功',async()=>{
  const h=harness('FavoriteControl',{failure:true});await h.state.edit();h.state.selected.value=['g2'];await h.state.save(true)
  assert.deepEqual(h.state.selected.value,['g2']);assert.equal(h.state.open.value,true);assert.equal(h.events.length,0);assert.match(h.state.error.value,/失败/)
})

test('取消收藏不提交失效分组，评分独立保留',async()=>{
  const h=harness('FavoriteControl');await h.state.edit();await h.state.save(false)
  const body=JSON.parse(h.calls.find(call=>call.method==='PUT').body)
  assert.deepEqual(body,{favorite:false});assert.equal(h.events[0][1].is_favorite,false);assert(!('user_score' in h.events[0][1]))
})

test('未收藏作品可单独评分，打开和取消评分不读写收藏',async()=>{
  const h=harness('ScoreControl');h.props.item={...h.props.item,is_favorite:false,favorite_group_ids:[],user_score:0}
  h.state.edit();assert.equal(h.state.score.value,0);assert.equal(h.calls.length,0)
  h.state.score.value=3;h.state.open.value=false;assert.equal(h.calls.length,0)
  h.state.edit();assert.equal(h.state.score.value,0)
  h.state.score.value=5;await h.state.save()
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].url,'/items/work/score')
  assert.deepEqual(JSON.parse(h.calls[0].body),{score:5})
  assert.deepEqual(JSON.parse(JSON.stringify(h.events[0])),['change',{id:'work',user_score:5}])
  assert.equal(h.state.open.value,false);assert.equal(h.props.item.is_favorite,false)
})

test('评分清零只调用评分接口，收藏状态与分组不变',async()=>{
  const h=harness('ScoreControl');h.state.edit();assert.equal(h.state.score.value,4)
  h.state.score.value=0;await h.state.save()
  assert.deepEqual(JSON.parse(h.calls[0].body),{score:0})
  assert.deepEqual(JSON.parse(JSON.stringify(h.events[0][1])),{id:'work',user_score:0})
  assert.equal(h.props.item.is_favorite,true);assert.deepEqual(h.props.item.favorite_group_ids,['g1'])
})

test('评分保存失败保留选择，评分编辑器保持打开，不发出成功事件',async()=>{
  const h=harness('ScoreControl',{failure:true});h.state.edit();h.state.score.value=3;await h.state.save()
  assert.equal(h.state.score.value,3);assert.equal(h.state.open.value,true);assert.equal(h.events.length,0);assert.match(h.state.error.value,/失败/)
})

test('收藏旧响应中的评分不会覆盖稍后保存的评分',async()=>{
  let finish;const h=harness('FavoriteControl',{writes:[new Promise(resolve=>finish=resolve)]})
  await h.state.edit();const pending=h.state.save(false)
  h.props.item={...h.props.item,user_score:5}
  finish({is_favorite:false,favorite_group_ids:[],user_score:4});await pending
  const merged={...h.props.item,...h.events[0][1]}
  assert.equal(merged.user_score,5);assert.equal(merged.is_favorite,false)
})

test('评分旧响应不会覆盖稍后修改的收藏和分组，提交期间不重复写入',async()=>{
  let finish;const h=harness('ScoreControl',{writes:[new Promise(resolve=>finish=resolve)]})
  h.state.edit();h.state.score.value=2;const pending=h.state.save();await h.state.save()
  h.props.item={...h.props.item,is_favorite:false,favorite_group_ids:[]}
  finish({id:'work',user_score:2,is_favorite:true,favorite_group_ids:['g1']});await pending
  const merged={...h.props.item,...h.events[0][1]}
  assert.equal(h.calls.length,1);assert.equal(merged.user_score,2);assert.equal(merged.is_favorite,false);assert.deepEqual(merged.favorite_group_ids,[])
})

test('切换作品后不应用旧评分响应',async()=>{
  let finish;const h=harness('ScoreControl',{writes:[new Promise(resolve=>finish=resolve)]})
  h.state.edit();const pending=h.state.save();h.props.item={...h.props.item,id:'next'}
  finish({id:'work',user_score:4});await pending
  assert.equal(h.events.length,0)
})
