const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const Vue = require('vue')
const {parse, compileScript} = require('@vue/compiler-sfc')
const root = path.resolve(__dirname, '..')

function evaluate(source, imports = {}, globals = {}) {
  const context = {exports: {}, require: name => imports[name] || require(name), ...globals}
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020}}).outputText, context)
  return context.exports
}
const types = evaluate(fs.readFileSync(path.join(root, 'src/types.ts'), 'utf8'))
const reading = evaluate(fs.readFileSync(path.join(root, 'src/reading.ts'), 'utf8'))
const {descriptor} = parse(fs.readFileSync(path.join(root, 'src/components/Reader.vue'), 'utf8'))
const readerSource = compileScript(descriptor, {id: 'reader-state-test'}).content
const asset = (id, position, mime_type = 'text/plain') => ({id, position, mime_type, original_name: `${id}.txt`, size_bytes: 20, url: `/api/assets/${id}/content`})
const item = (overrides = {}) => ({id: 'novel-upper', title: '测试正文', type: 'novel', source: 'pixiv', page_count: 1, content_state: 'ready', assets: [asset('text-1', 0)], ...overrides})
function harness(record, handler) {
  const calls = [], events = []
  const api = async (url, options = {}) => { calls.push({url, body: options.body ? JSON.parse(options.body) : null}); return handler(url, options) }
  const component = evaluate(readerSource, {
    vue: {...Vue, onMounted: () => {}, onBeforeUnmount: () => {}},
    '../api': {api, errorMessage: value => value.message}, '../types': types, '../reading': reading, './SeriesPanel.vue': {},
  }, {AbortController, requestAnimationFrame: callback => callback(), setTimeout: () => 1, clearTimeout: () => {}}).default
  const state = component.setup(record && {item: record}, {expose() {}, emit: (...args) => events.push(args)})
  state.content.value = {scrollHeight: 2000, clientHeight: 500, scrollTop: 0}
  return {state, calls, events}
}
const sections = [{index: 0, title: '开篇', text: '第一节'}, {index: 1, title: '中篇', text: '第二节'}, {index: 2, title: '尾声', text: '第三节'}]
const handler = async (url, options) => url.endsWith('/text-sections') ? {sections} : JSON.parse(options.body)

test('Pixiv 小说数字 ID 规范化，作品与系列链接保持原输入', () => {
  assert.equal(reading.pixivImportInput(' 12345 ', 'novel'), 'novel:12345')
  assert.equal(reading.pixivImportInput('12345', 'manga'), '12345')
  assert.equal(reading.pixivImportInput('novel:12345', 'novel'), 'novel:12345')
  assert.equal(reading.pixivImportInput('https://www.pixiv.net/novel/show.php?id=12345', 'novel'), 'https://www.pixiv.net/novel/show.php?id=12345')
})
test('封面和部分正文不满足离线阅读；已下载多 P 与远端总页数分开', () => {
  for (const state of ['metadata', 'queued', 'downloading', 'failed']) assert.equal(types.contentReady(item({content_state: state})), false)
  assert.equal(types.contentReady(item({assets: []})), false)
  assert.equal(types.contentReady(item()), true)
  assert.equal(types.contentCountLabel(item({type: 'manga', content_state: 'metadata', page_count: 0, remote_page_count: 12})), '共 12 P · 未完整下载')
  assert.equal(types.contentCountLabel(item({type: 'manga', page_count: 3, remote_page_count: 12})), '3 P')
})
test('恢复章节及章内位置，不把 TXT 文件数量当作阅读完成比例', async () => {
  const {state} = harness(item(), handler)
  await state.displayAsset({page: 0, section: 1, offset: 0.45})
  assert.equal(state.section.value, 1)
  assert.equal(state.offset.value, 0.45)
  assert.equal(state.content.value.scrollTop, 675)
  assert.equal(state.currentSection.value.title, '中篇')
  assert.equal(state.percent.value, 48)
})
test('单节短正文打开为 0%，明确标记读完才保存 100%', async () => {
  const {state, calls} = harness(item(), async (url, options) => url.endsWith('/text-sections') ? {sections: [sections[0]]} : JSON.parse(options.body))
  state.content.value.scrollHeight = state.content.value.clientHeight
  await state.displayAsset()
  state.scrolled()
  assert.equal(state.percent.value, 0)
  assert.equal(calls.filter(call => call.url.endsWith('/progress')).length, 0)
  await state.markFinished()
  assert.equal(state.percent.value, 100)
  assert.equal(calls.at(-1).body.offset, 1)
  assert.equal(calls.at(-1).body.section, 0)
})
test('切换章节归零章内位置，保存旧章和新章真实快照', async () => {
  const {state, calls, events} = harness(item(), handler)
  await state.displayAsset({page: 0, section: 0, offset: 0.8})
  await state.goSection(1)
  await state.saveProgress()
  assert.equal(state.section.value, 1)
  assert.equal(state.offset.value, 0)
  assert.equal(state.content.value.scrollTop, 0)
  const saved = calls.filter(call => call.url.endsWith('/progress'))
  assert.equal(saved[0].body.section, 0)
  assert.equal(saved[0].body.offset, 0.8)
  assert.equal(saved.at(-1).body.section, 1)
  assert.equal(saved.at(-1).body.offset, 0)
  assert.equal(events.at(-1)[1].item_id, 'novel-upper')
})
test('漫画按 position 阅读，UI 第三 P 保存为 page=2', async () => {
  const record = item({type: 'manga', page_count: 3, assets: [asset('p3', 2, 'image/png'), asset('p1', 0, 'image/png'), asset('p2', 1, 'image/png')]})
  const {state, calls} = harness(record, handler)
  assert.equal(state.currentAsset.value.id, 'p1')
  await state.goPage(3)
  await state.saveProgress()
  assert.equal(state.currentAsset.value.id, 'p3')
  assert.equal(state.pageInput.value, 3)
  assert.equal(calls.at(-1).body.page, 2)
})
test('切换页面后忽略较早返回的正文，不把章节写入新页面', async () => {
  let resolve
  const deferred = new Promise(done => {resolve = done})
  const record = item({page_count: 2, assets: [asset('slow-text', 0), asset('page-image', 1, 'image/png')]})
  const {state} = harness(record, async (url, options) => url.endsWith('/text-sections') ? deferred : JSON.parse(options.body))
  const pending = state.displayAsset()
  await state.goPage(2)
  resolve({sections})
  await pending
  assert.equal(state.currentAsset.value.id, 'page-image')
  assert.equal(state.sections.value.length, 0)
  assert.equal(state.isText.value, false)
})
test('正文未就绪时不请求章节、不写阅读进度', async () => {
  const {state, calls} = harness(item({content_state: 'queued'}), handler)
  await state.displayAsset()
  await state.saveProgress()
  assert.equal(state.assets.value.length, 0)
  assert.equal(calls.length, 0)
})
test('超界阅读位置收敛到实际章节，异常值不会造成 NaN', () => {
  assert.equal(reading.restoreTextPosition({page: 0, section: 50, offset: 9}, sections).section, 2)
  assert.equal(reading.restoreTextPosition({page: 0, section: -1, offset: -2}, sections).offset, 0)
  assert.equal(reading.textReadingPercent(0, 1, Number.NaN), 0)
})

const seriesSource = compileScript(parse(fs.readFileSync(path.join(root, 'src/components/SeriesPanel.vue'), 'utf8')).descriptor, {id: 'series-state-test'}).content
const manualSeries = () => ({id: 'series-1', title: '上下篇', kind: 'novel', origin: 'manual', source: 'local', source_id: null, complete: true, revision: 3, entries: [{item_id: 'novel-upper', title: '上篇正文', label: '上篇', position: 0, content_state: 'ready', page_count: 1}, {item_id: 'novel-lower', title: '下篇正文', label: '下篇', position: 1, content_state: 'metadata', page_count: 0}]})
async function seriesHarness(record, detail, failUpdate = false) {
  const calls = [], events = []
  const api = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null; calls.push({url, method: options.method || 'GET', body})
    if (url.startsWith('/items?')) return {items: []}
    if (options.method === 'PUT' && failUpdate) throw new Error('系列已被其他操作修改，请刷新后重试')
    if (options.method === 'PUT') return {...detail, ...body, revision: detail.revision + 1}
    return detail
  }
  const post = async (url, body) => { calls.push({url, method: 'POST', body}); return url === '/series/import' ? {id: 'series-job', kind: 'series_sync', status: 'queued'} : {...detail, ...body} }
  const component = evaluate(seriesSource, {
    vue: {...Vue, onBeforeUnmount: () => {}, watch: (_source, callback, options) => {if (options?.immediate) callback()}},
    '../api': {api, post, errorMessage: value => value.message}, '../types': types,
  }, {setTimeout: () => 1, clearTimeout: () => {}}).default
  const state = component.setup({item: record, editable: true}, {expose() {}, emit: (...args) => events.push(args)})
  await Promise.resolve(); await Promise.resolve()
  return {state, calls, events}
}
test('手动上下集顺序只按人工操作保存，并允许撤销最后一条关联', async () => {
  const detail = manualSeries()
  const {state, calls} = await seriesHarness(item({series: [detail]}), detail)
  state.beginEdit(true); state.move(1, -1)
  assert.equal(state.entries.value[0].item_id, 'novel-lower')
  await state.saveSeries()
  const saved = calls.find(call => call.method === 'PUT')
  assert.equal(saved.body.revision, 3)
  assert.equal(saved.body.entries[0].item_id, 'novel-lower')
  state.beginEdit(true); state.entries.value = []
  await state.saveSeries()
  assert.equal(calls.at(-1).method, 'PUT')
  assert.equal(calls.at(-1).body.entries.length, 0)
})
test('手动系列版本冲突保留未保存的顺序和集名', async () => {
  const detail = manualSeries()
  const {state} = await seriesHarness(item({series: [detail]}), detail, true)
  state.beginEdit(true); state.entries.value[0].label = '修改中的上篇'; state.move(0, 1)
  await state.saveSeries()
  assert.equal(state.editing.value, true)
  assert.equal(state.entries.value[1].label, '修改中的上篇')
  assert.match(state.error.value, /刷新/)
})
test('官方目录不允许手工重排，获取目录不附带正文自动下载参数', async () => {
  const detail = {...manualSeries(), origin: 'source', source: 'pixiv', source_id: '123'}
  const {state, calls, events} = await seriesHarness(item({series: [detail], source_series: {remote_id: '123', title: '官方系列', kind: 'novel'}}), detail)
  state.beginEdit(true)
  assert.equal(state.editing.value, false)
  await state.fetchOfficial()
  const request = calls.find(call => call.url === '/series/import')
  assert.equal(request.body.url_or_id, '123')
  assert.equal(request.body.kind, 'novel')
  assert.equal('auto_download' in request.body, false)
  assert.equal(events.at(-1)[0], 'queued')
})
