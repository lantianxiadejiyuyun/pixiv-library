const test = require('node:test'), assert = require('node:assert/strict')
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript'), Vue = require('vue')
const { parse, compileScript } = require('@vue/compiler-sfc')
function load(source, context) {
  context.exports = {}
  vm.runInNewContext(ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 } }).outputText, context)
  return context.exports
}
function historyStore() {
  let value = null
  const localStorage = { getItem: () => value, setItem: (_key, data) => value = data }
  const helpers = load(fs.readFileSync(path.join(__dirname, '../src/search_history.ts'), 'utf8'), { localStorage })
  return { helpers, corrupt: data => value = data }
}
function harness() {
  const { helpers } = historyStore(), requests = [], pending = [], unmount = [], events = []
  const props = Vue.reactive({ modelValue: '', tags: [], mode: 'all', source: 'pixiv' })
  const context = { URLSearchParams, setTimeout, clearTimeout, require: name => ({
    vue: { ...Vue, onMounted() {}, onBeforeUnmount: fn => unmount.push(fn) }, 'lucide-vue-next': {},
    '../api': { errorMessage: e => e.message, api: url => { requests.push(url); return new Promise((resolve, reject) => pending.push({ resolve, reject })) } },
    '../search_history': helpers,
  })[name] || {} }
  const source = compileScript(parse(fs.readFileSync(path.join(__dirname, '../src/components/LibrarySearch.vue'), 'utf8')).descriptor, { id: 'search' }).content
  const component = load(source, context)
  const state = component.default.setup(props, { expose() {}, emit(name, value) { events.push([name, value]); if (name.startsWith('update:')) props[name.slice(7)] = value } })
  return { state, props, requests, pending, events, dispose: () => unmount.forEach(fn => fn()) }
}
const blue = { id: 'blue', name: 'blue_eyes', name_zh: '蓝眼睛', count: 5 }
test('history survives reload, deduplicates unordered tags, caps at 20 and tolerates corrupt storage', () => {
  const h = historyStore(); let entries = []
  for (let n = 0; n < 25; n++) entries = h.helpers.rememberSearch(entries, { query: String(n), tags: [], mode: 'all' })
  assert.equal(h.helpers.readSearchHistory().length, 20)
  entries = h.helpers.rememberSearch(entries, { query: '', tags: [blue, { id: 'red', name: 'red' }], mode: 'any' })
  entries = h.helpers.rememberSearch(entries, { query: '', tags: [{ id: 'red', name: 'red' }, blue], mode: 'any' })
  assert.equal(entries.filter(e => e.tags.length).length, 1)
  assert.match(h.helpers.searchLabel(entries[0]), /蓝眼睛/)
  h.corrupt('{broken'); assert.equal(h.helpers.readSearchHistory().length, 0)
  h.corrupt('[{"query":5}]'); assert.equal(h.helpers.readSearchHistory().length, 0)
})
test('suggestions use source, ignore late queries and exclude selected tags', async () => {
  const h = harness(); h.props.modelValue = 'blue'; h.state.open.value = true; await Vue.nextTick()
  const first = h.state.suggest()
  h.props.modelValue = '蓝眼'; h.props.tags = [blue]; await Vue.nextTick()
  const second = h.state.suggest()
  h.pending[1].resolve([blue, { id: 'new', name: '蓝眼睛2' }]); await second
  h.pending[0].resolve([{ id: 'old', name: 'old' }]); await first
  assert.equal(h.state.suggestions.value[0].id, 'new')
  assert.match(h.requests[1], /source=pixiv/); assert.match(h.requests[1], /suggest=true/)
  h.dispose()
})
test('choosing a suggestion adds a stable tag, clears keyword and records/restores the full search', async () => {
  const h = harness(); h.props.modelValue = '蓝'; h.state.choose(blue); await Vue.nextTick()
  assert.equal(h.props.tags[0].id, 'blue'); assert.equal(h.props.modelValue, '')
  assert.equal(h.state.history.value[0].tags[0].name_zh, '蓝眼睛')
  h.state.restore({ query: 'novel', tags: [blue], mode: 'any' }); await Vue.nextTick()
  assert.equal(h.props.modelValue, 'novel'); assert.equal(h.props.mode, 'any')
  assert.equal(h.events.filter(e => e[0] === 'submit').length, 2)
  h.state.removeHistory(h.state.history.value[0]); assert.equal(h.state.history.value.length, 1)
  h.state.removeHistory(); assert.equal(h.state.history.value.length, 0); h.dispose()
})
test('failed suggestions can retry and disposed response cannot change state', async () => {
  const h = harness(); h.props.modelValue = 'blue'; h.state.open.value = true; await Vue.nextTick()
  const first = h.state.suggest(); h.pending[0].reject(Error('offline')); await first
  assert.equal(h.state.error.value, 'offline')
  const next = h.state.suggest(); h.dispose(); h.pending[1].resolve([blue]); await next
  assert.equal(h.state.suggestions.value.length, 0)
})
test('IME confirmation does not submit an unfinished search', () => {
  const h = harness(); h.props.modelValue = 'lan'
  h.state.keydown({ key: 'Enter', isComposing: true, preventDefault() { throw Error('must not consume IME') } })
  assert.equal(h.events.length, 0)
  h.state.compositionEnd({ target: { value: '蓝' } }); assert.equal(h.props.modelValue, '蓝')
  h.state.keydown({ key: 'Enter', isComposing: false, preventDefault() {} })
  assert.equal(h.events.at(-1)[0], 'submit'); h.dispose()
})
