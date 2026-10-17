const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const Vue = require('vue')
const {parse, compileScript} = require('@vue/compiler-sfc')
const source = compileScript(parse(fs.readFileSync(path.join(__dirname, '../src/components/AuthorsPanel.vue'), 'utf8')).descriptor, {id: 'authors-test'}).content
function deferred() {let resolve; const promise = new Promise(r => resolve = r); return {promise, resolve}}
function harness(handler) {
  const requests = [], unmount = [], timers = []
  const api = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null
    requests.push({url, body})
    if (handler) { const result = handler(url, body); if (result !== undefined) return result }
    if (url === '/pixiv/authors/open') return {remote_id: body.url_or_id, section: 'works', category: 'illustration', restrict: 'public', job: {id: body.url_or_id}}
    if (url.startsWith('/pixiv/tasks/')) return {status: 'completed', author_id: url.split('/').at(-1)}
    if (url.includes('/feed?')) return {items: [], revision: 1, total: 0, complete: true, has_more: false, job: null}
    if (url.match(/\/pixiv\/authors\/\d+$/)) return {remote_id: url.split('/').at(-1), name: 'Author ' + url.split('/').at(-1)}
    return {items: [], total: 0}
  }
  const imports = {vue: {...Vue, onMounted() {}, onBeforeUnmount: fn => unmount.push(fn)}, '../api': {api, post: (url, body) => api(url, {body: JSON.stringify(body)}), errorMessage: error => error.message}, './BookCard.vue': {}}
  const context = {exports: {}, require: name => imports[name] || require(name), URLSearchParams, setTimeout: fn => {timers.push(fn); return timers.length}, clearTimeout() {}}
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020}}).outputText, context)
  const state = context.exports.default.setup({blurAdult: true}, {expose() {}, emit() {}})
  return {state, requests, unmount, timers}
}

test('作者作品和公开收藏开关独立保存；迟到保存不污染新作者', async () => {
  const slow = deferred()
  const h = harness((url, body) => url.endsWith('/download-settings') ? slow.promise : undefined)
  h.state.authorId.value='20';h.state.profile.value={remote_id:'20',download_settings:{download_works:false,download_bookmarks:true,subscribed:true}}
  const pending=h.state.saveDownloads('download_works')
  assert.deepEqual(h.requests[0].body,{download_works:true,download_bookmarks:true})
  h.state.authorId.value='21';h.state.profile.value={remote_id:'21',download_settings:{download_works:false,download_bookmarks:false}}
  slow.resolve({download_works:true,download_bookmarks:true});await pending
  assert.equal(h.state.profile.value.download_settings.download_works,false)
})
test('作者与收藏通过分类参数请求，不附带正文下载操作', async () => {
  const h = harness(); await h.state.open('10'); await Promise.resolve(); await Promise.resolve()
  h.state.section.value = 'bookmarks'; h.state.category.value = 'novel'; await h.state.change()
  const query = h.requests.findLast(request => request.url.includes('/feed?')).url
  assert.match(query, /section=bookmarks/); assert.match(query, /category=novel/)
  assert.equal(h.requests.some(request => request.url.includes('/download')), false)
})
test('切换作者后，旧作者资料不能覆盖新页面', async () => {
  const old = deferred()
  const h = harness(url => url === '/pixiv/authors/10' ? old.promise : undefined)
  const previous = h.state.open('10'); await Promise.resolve(); await Promise.resolve()
  await h.state.open('20'); await Promise.resolve(); await Promise.resolve()
  old.resolve({remote_id: '10', name: 'Stale author'}); await previous
  assert.equal(h.state.authorId.value, '20'); assert.notEqual(h.state.profile.value?.name, 'Stale author')
})
test('离开组件后忽略迟到的目录响应，取消后续轮询', async () => {
  const slow = deferred(); const h = harness(url => url.includes('/feed?') ? slow.promise : undefined)
  h.state.authorId.value = '10'
  const request = h.state.readFeed()
  h.unmount.forEach(fn => fn())
  slow.resolve({items: [{id: 'stale'}], revision: 1, total: 1, job: {id: 'old', status: 'queued'}})
  await request
  assert.equal(h.state.feed.value, null); assert.equal(h.timers.length, 0)
})
test('回到作者作品时自动退出私密收藏筛选', async () => {
  const h = harness(); h.state.authorId.value = '10'; h.state.privacy.value = 'private'; h.state.section.value = 'works'
  await h.state.change()
  assert.equal(h.state.privacy.value, 'public')
  assert.match(h.requests.at(-1).url, /restrict=public/)
})
