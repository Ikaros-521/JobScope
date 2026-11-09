import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import ts from 'typescript'

const source = readFileSync(new URL('../src/mcpConfig.ts', import.meta.url), 'utf8')
const js = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const { readMcpConfig, connectionPayload } = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'))
let passed = 0
function check(name, run) { run(); passed++; console.log('PASS', name) }
check('standard stdio config preserves spaces and credentials', () => {
  const result = readMcpConfig(JSON.stringify({ mcpServers: { research: { command: 'node', args: ['C:\\My Project\\server.js', '--query', 'two words'], env: { API_KEY: 'fixture-value' } } } }))
  assert.equal(result[0].name, 'research')
  assert.deepEqual(connectionPayload(result[0]).args, ['C:\\My Project\\server.js', '--query', 'two words'])
  assert.equal(connectionPayload(result[0]).env.API_KEY, 'fixture-value')
})
check('multiple servers stay distinct for explicit choice', () => {
  const result = readMcpConfig('{"mcpServers":{"one":{"command":"node"},"two":{"url":"https://example.com/mcp"}}}')
  assert.equal(result.length, 2); assert.equal(result[1].transport, 'http')
})
check('remote Bearer authentication', () => {
  const result = readMcpConfig('{"name":"remote","type":"streamable-http","url":"https://example.com/mcp","headers":{"Authorization":"Bearer fixture-token"}}')[0]
  assert.equal(result.token, 'fixture-token')
})
check('unsupported settings never silently discarded', () => {
  for (const value of [{ url: 'https://example.com/mcp', headers: { 'X-API-Key': 'test' } }, { command: 'node', cwd: 'C:/project' }, { type: 'sse', url: 'https://example.com/sse' }, { command: 'node', args: 'bad' }, { command: 'node', env: { KEY: 1 } }]) {
    assert.throws(() => readMcpConfig(JSON.stringify(value)))
  }
})
check('malformed JSON is actionable and never echoed', () => {
  assert.throws(() => readMcpConfig('fixture-secret-invalid'), error => !error.message.includes('fixture-secret'))
})
check('duplicate environment names are rejected', () => {
  const draft = readMcpConfig('{"command":"node"}')[0]
  draft.env = [{ name: 'KEY', value: 'one' }, { name: ' KEY ', value: 'two' }]
  assert.throws(() => connectionPayload(draft), /重复/)
})
check('empty argument values remain exact and blank variable rows are ignored', () => {
  const draft = readMcpConfig('{"command":"node","args":["", "--x", " a b "]}')[0]
  draft.env = [{ name: '', value: '' }]
  assert.deepEqual(connectionPayload(draft).args, ['', '--x', ' a b '])
  assert.deepEqual(connectionPayload(draft).env, {})
})
check('hidden fields from the other transport are not submitted', () => {
  const draft = readMcpConfig('{"command":"node","args":["server.js"],"env":{"KEY":"value"}}')[0]
  draft.transport = 'http'; draft.url = 'https://example.com/mcp'
  const payload = connectionPayload(draft)
  assert.deepEqual(payload.env, {}); assert.deepEqual(payload.args, []); assert.equal(payload.command, '')
})
console.log(`${passed} MCP configuration checks passed`)
