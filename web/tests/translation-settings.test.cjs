const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const Vue = require('vue')
const {parse, compileScript} = require('@vue/compiler-sfc')
const source = compileScript(parse(fs.readFileSync(path.join(__dirname, '../src/components/TranslationSettings.vue'), 'utf8')).descriptor, {id: 'translation-settings'}).content
const original = {enabled: false, provider: 'ollama', base_url: 'http://host.docker.internal:11434', model: '', use_proxy: false, has_api_key: true}
function harness({confirm = true, failure = false} = {}) {
  const requests = []
  const api = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : null
    requests.push({url, body})
    if (failure && options.method) throw new Error('模型配置未保存')
    return {...original, ...body}
  }
  const post = async (url, body) => {requests.push({url, body}); return {queued: 2, has_more: false}}
  const imports = {vue: {...Vue, onMounted() {}}, '../api': {api, post, errorMessage: value => value.message}, 'lucide-vue-next': {}}
  const context = {exports: {}, require: name => imports[name] || require(name), window: {confirm: () => confirm}}
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020}}).outputText, context)
  return {state: context.exports.default.setup({}, {expose() {}}), requests}
}
test('汉化设置读取不触发测试或批量发送，未保存配置保持脏状态', async () => {
  const h = harness(); await h.state.load()
  assert.equal(h.requests.length, 1)
  assert.equal(h.state.form.enabled, false)
  assert.equal(h.state.dirty.value, false)
  h.state.form.model = 'local-model'
  assert.equal(h.state.dirty.value, true)
})
test('密钥仅保存时发送一次，成功后清空输入；更换接口清除模型与输入密钥', async () => {
  const h = harness(); await h.state.load()
  h.state.form.model = 'local-model'; h.state.form.enabled = true
  h.state.apiKey.value = 'synthetic-secret'
  await h.state.save()
  assert.equal(h.requests.at(-1).body.api_key, 'synthetic-secret')
  assert.equal(h.state.apiKey.value, '')
  h.state.form.provider = 'openai'; h.state.apiKey.value = 'old-key'
  h.state.changeProvider()
  assert.equal(h.state.apiKey.value, '')
  assert.equal(h.state.form.model, '')
  assert.equal(h.state.form.base_url, 'https://api.openai.com/v1')
})
test('批量汉化取消时不发送内容，同意后提交明确限额', async () => {
  const declined = harness({confirm: false}); await declined.state.load(); await declined.state.batch()
  assert.equal(declined.requests.length, 1)
  const allowed = harness(); await allowed.state.load(); await allowed.state.batch()
  assert.equal(allowed.requests.at(-1).url, '/translation/batch')
  assert.deepEqual(JSON.parse(JSON.stringify(allowed.requests.at(-1).body)), {consent: true, limit: 50})
  assert.match(allowed.state.message.value, /2 部/)
})
test('配置保存失败保留输入并显示错误，不宣称已保存', async () => {
  const h = harness({failure: true}); await h.state.load()
  h.state.form.model = 'keep-this'; await h.state.save()
  assert.equal(h.state.form.model, 'keep-this')
  assert.equal(h.state.dirty.value, true)
  assert.match(h.state.error.value, /未保存/)
  assert.equal(h.state.message.value, '')
})
