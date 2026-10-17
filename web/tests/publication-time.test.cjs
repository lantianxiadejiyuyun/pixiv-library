const test=require('node:test'),assert=require('node:assert/strict')
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path'),ts=require('typescript')
const source=fs.readFileSync(path.join(__dirname,'../src/types.ts'),'utf8')
const context={exports:{},Intl,Date,Number,Math}
vm.runInNewContext(ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020}}).outputText,context)
test('original publication has a year and invalid dates stay unknown',()=>{
 const date=context.exports.publicationDate
 assert.match(date('2020-02-28T23:15:20Z'),/2020/)
 for(const value of [null,undefined,'','bad'])assert.equal(date(value),'暂无')
 assert.equal(date('2020-02-29T08:15:20+09:00'),date('2020-02-28T23:15:20Z'))
})
