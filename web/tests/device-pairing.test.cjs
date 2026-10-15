const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const vm = require('node:vm')
const ts = require('typescript')
const Vue = require('vue')
const {parse, compileScript} = require('@vue/compiler-sfc')
const QRCode = require('qrcode')
const root = path.resolve(__dirname, '..')
const helperSource = fs.readFileSync(path.join(root, 'src/devicePairing.ts'), 'utf8')
const componentSource = compileScript(parse(fs.readFileSync(path.join(root, 'src/components/DevicePairing.vue'), 'utf8')).descriptor, {id: 'device-pairing-test'}).content
function evaluate(source, imports = {}, globals = {}) {
  const context = {exports: {}, require: name => imports[name] || require(name), ...globals}
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020}}).outputText, context)
  return context.exports
}
function harness({origin = 'http://192.168.1.20:8000', handler, clipboard = true} = {}) {
  let timestamp = Date.parse('2026-10-14T08:00:00Z'), clears = 0, copies = 0
  class Clock extends Date {constructor(...args) {super(...(args.length ? args : [timestamp]))} static now() {return timestamp}}
  const helpers = evaluate(helperSource, {}, {Date: Clock, URL})
  const requests = [], drawings = [], mounted = [], unmounted = [], listeners = new Map()
  const fixture = () => ({id: 'pair-fixture', server_url: origin, expires_at: new Date(timestamp + 300000).toISOString(), qr_payload: JSON.stringify({type: 'qiye-library', version: 1, server_url: origin, pairing_token: 'synthetic-test-token'})})
  const api = async (url, options = {}) => {requests.push({url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null}); return handler ? handler(url, options, fixture) : url === '/devices' ? {items: []} : fixture()}
  const qr = {toCanvas: async (canvas, value) => {drawings.push(value); assert.ok(canvas)}}
  const globals = {Date: Clock, URL, AbortController, setInterval: () => 1, clearInterval: () => {}, window: {location: {origin}, addEventListener: (type, fn) => listeners.set(type, fn), removeEventListener: type => listeners.delete(type)}, navigator: clipboard ? {clipboard: {writeText: async value => {assert.equal(typeof value, 'string'); copies++}}} : {}}
  const component = evaluate(componentSource, {vue: {...Vue, onMounted: fn => mounted.push(fn), onBeforeUnmount: fn => unmounted.push(fn)}, qrcode: {default: qr}, '../api': {api, errorMessage: value => value.message}, '../devicePairing': helpers}, globals).default
  const state = component.setup({}, {expose() {}})
  state.canvas.value = {width: 280, height: 280, getContext: () => ({clearRect() {clears++}})}
  return {state, helpers, requests, drawings, fixture, mounted, unmounted, listeners, advance: milliseconds => {timestamp += milliseconds}, clears: () => clears, copies: () => copies}
}
test('绑定页只读取设备，不自动签发二维码；回环地址必须改为手机可达地址', async () => {
  const h = harness({origin: 'http://127.0.0.1:8000'})
  h.mounted.forEach(fn => fn()); await Promise.resolve(); await Promise.resolve()
  assert.equal(h.requests.length, 1); assert.equal(h.requests[0].url, '/devices')
  await h.state.generatePairing()
  assert.equal(h.requests.length, 1)
  assert.match(h.state.pairingError.value, /局域网/)
  assert.equal(h.state.pairing.value, null)
})
test('地址限制阻止公网 HTTP、监听地址、凭据、参数和非根路径', () => {
  const {helpers} = harness()
  for (const value of ['http://example.com', 'http://0.0.0.0:8000', 'http://[::]:8000', 'https://name:secret@example.com', 'https://example.com?token=x', 'https://example.com/#settings', 'https://example.com/api']) assert.ok(helpers.serverAddressError(value), value)
  for (const value of ['https://example.com', 'http://192.168.1.2:8000', 'http://10.0.2.2:8000', 'http://172.16.1.2', 'http://nas.local', 'http://nas']) assert.equal(helpers.serverAddressError(value), '', value)
})
test('二维码只交给本地 canvas 库，绑定码不进入请求 URL 或后续存储', async () => {
  const h = harness(); await h.state.generatePairing()
  assert.equal(h.state.qrReady.value, true)
  assert.equal(h.drawings.length, 1)
  assert.equal(h.requests[0].url, '/devices/pairing')
  assert.deepEqual(Object.keys(h.requests[0].body), ['server_url'])
  assert.equal(h.requests[0].body.server_url, 'http://192.168.1.20:8000')
  assert.equal(h.copies(), 0)
  const qr = QRCode.create(h.drawings[0], {errorCorrectionLevel: 'M'})
  assert.ok(qr.modules.size > 0)
})
test('五分钟到期立即清除二维码、绑定码和复制状态', async () => {
  const h = harness(); await h.state.generatePairing(); h.state.revealCode.value = true
  h.advance(300001); h.state.updateClock()
  assert.equal(h.state.pairing.value, null)
  assert.equal(h.state.qrReady.value, false)
  assert.equal(h.state.revealCode.value, false)
  assert.equal(h.state.copyMessage.value, '')
  assert.match(h.state.pairingNotice.value, /过期/)
  assert.ok(h.clears() > 0)
})
test('生成中的重复点击只发一次，关闭后忽略迟到的签发响应', async () => {
  let finish
  const pending = new Promise(resolve => {finish = resolve})
  const h = harness({handler: async () => pending})
  const generating = h.state.generatePairing()
  await h.state.generatePairing()
  assert.equal(h.requests.length, 1)
  h.state.clearPairing(); finish(h.fixture()); await generating
  assert.equal(h.state.pairing.value, null)
  assert.equal(h.drawings.length, 0)
})
test('修改服务器地址与卸载页面都会擦除绑定内容', async () => {
  const h = harness(); await h.state.generatePairing()
  h.state.serverURL.value = 'http://192.168.1.21:8000'; h.state.addressChanged()
  assert.equal(h.state.pairing.value, null)
  assert.match(h.state.pairingNotice.value, /地址已更改/)
  h.state.serverURL.value = 'http://192.168.1.20:8000'; await h.state.generatePairing()
  h.unmounted.forEach(fn => fn())
  assert.equal(h.state.pairing.value, null)
  assert.equal(h.state.revealCode.value, false)
})
test('复制仅响应明确点击；不支持剪贴板时展示手动复制区域', async () => {
  const supported = harness(); await supported.state.generatePairing()
  assert.equal(supported.copies(), 0); await supported.state.copyCode(); assert.equal(supported.copies(), 1)
  const fallback = harness({clipboard: false}); await fallback.state.generatePairing(); await fallback.state.copyCode()
  assert.equal(fallback.state.revealCode.value, true)
  assert.match(fallback.state.copyMessage.value, /手动复制/)
})
test('失效或异常签发响应不显示二维码', async () => {
  const h = harness({handler: async (_url, _options, fixture) => ({...fixture(), qr_payload: '{invalid'})})
  await h.state.generatePairing()
  assert.equal(h.state.pairing.value, null); assert.equal(h.drawings.length, 0)
  assert.match(h.state.pairingError.value, /无效/)
})
test('撤销失败保留设备授权状态，列表刷新不掩盖错误', async () => {
  const device = {id: 'device-fixture', device_name: '测试手机', status: 'active', revoked_at: null}
  const h = harness({handler: async (_url, options) => {if (options.method === 'DELETE') throw new Error('撤销失败，请重试'); return {items: [device]}}})
  await h.state.loadDevices(); await h.state.revokeDevice(device); await h.state.loadDevices(true)
  assert.equal(h.state.devices.value[0].status, 'active')
  assert.match(h.state.deviceActionError.value, /撤销失败/)
})
test('设备列表读取失败显示错误，不伪造空设备状态', async () => {
  const h = harness({handler: async () => {throw new Error('服务不可达')}})
  await h.state.loadDevices()
  assert.equal(h.state.devicesLoading.value, false)
  assert.match(h.state.devicesError.value, /不可达/)
})
