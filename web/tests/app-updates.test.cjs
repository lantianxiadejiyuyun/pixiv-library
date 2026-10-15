const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const Vue = require('vue')
const {parse, compileScript} = require('@vue/compiler-sfc')
const source = compileScript(parse(fs.readFileSync(path.join(__dirname, '../src/components/AppUpdateSettings.vue'), 'utf8')).descriptor, {id: 'app-updates'}).content
function harness() {
  const requests = [], uploads = [], events = []
  class XHR {
    upload = {}; headers = {}
    constructor() { uploads.push(this) }
    open(method, url) { this.method = method; this.url = url }
    setRequestHeader(key, value) { this.headers[key] = value }
    send(body) { this.body = body }
    finish(status, payload) { this.status = status; this.responseText = JSON.stringify(payload); this.onload() }
  }
  const current = {version_code: 4, version_name: '0.4.0', size_bytes: 100, notes: '', download_path: '/api/app-updates/android/digest/apk'}
  const imports = {vue: {...Vue, onMounted() {}}, '../api': {api: async url => {requests.push(url); return {release: current}}, errorMessage: e => e.message}, 'lucide-vue-next': {}}
  const context = {exports: {}, require: name => imports[name] || require(name), XMLHttpRequest: XHR, URLSearchParams, Event: class {constructor(type) {this.type = type}}, window: {dispatchEvent: e => events.push(e.type)}}
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText, context)
  return {state: context.exports.default.setup({}, {expose() {}}), requests, uploads, events}
}
test('查看更新不自动发布，非法文件不上传', async () => {
  const h = harness(); await h.state.load()
  assert.equal(h.requests.length, 1); assert.equal(h.uploads.length, 0)
  h.state.selected.value = {name: 'bad.zip', size: 10}; await h.state.publish()
  assert.equal(h.uploads.length, 0); assert.match(h.state.error.value, /APK/)
})
test('发布使用原始 APK，防重复提交并展示上传进度', async () => {
  const h = harness(); h.state.selected.value = {name: 'qiye.apk', size: 100}; h.state.notes.value = '修复 & 加载'
  await h.state.publish(); await h.state.publish()
  assert.equal(h.uploads.length, 1)
  const xhr = h.uploads[0]
  assert.equal(xhr.headers['Content-Type'], 'application/vnd.android.package-archive')
  assert.equal(new URL(xhr.url, 'http://local').searchParams.get('notes'), '修复 & 加载')
  xhr.upload.onprogress({lengthComputable: true, loaded: 50, total: 100})
  assert.equal(h.state.progress.value, 50)
  xhr.finish(200, {release: {version_code: 5, version_name: '0.5.0'}})
  assert.equal(h.state.release.value.version_code, 5); assert.equal(h.state.busy.value, false)
})
test('校验失败保留当前发布版本，授权失效通知主界面', async () => {
  const h = harness(); await h.state.load(); h.state.selected.value = {name: 'qiye.apk', size: 100}
  await h.state.publish(); h.uploads[0].finish(400, {detail: '签名不一致'})
  assert.equal(h.state.release.value.version_code, 4); assert.match(h.state.error.value, /签名/)
  assert.equal(h.state.message.value, '')
  await h.state.publish(); h.uploads[1].finish(401, {detail: '请登录'})
  assert.deepEqual(h.events, ['library:unauthorized']); assert.equal(h.state.busy.value, false)
})
