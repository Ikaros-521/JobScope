export type Transport = 'http' | 'sse' | 'stdio'
export type ConnectionDraft = {
  name: string; transport: Transport; url: string; command: string
  args: string[]; env: { name: string; value: string }[]; headers: { name: string; value: string }[]
  token: string; clear_secrets: boolean
}
export const emptyConnection = (): ConnectionDraft => ({ name: '', transport: 'http', url: '', command: '', args: [], env: [], headers: [], token: '', clear_secrets: false })
const record = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === 'object' && !Array.isArray(value)
const HEADER_NAME = /^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$/

export function connectionPayload(draft: ConnectionDraft) {
  const env: Record<string, string> = Object.create(null)
  const headers: Record<string, string> = Object.create(null)
  if (draft.args.length > 40 || draft.env.length > 30) throw new Error('最多填写 40 个参数和 30 个环境变量。')
  if (draft.headers.length > 10) throw new Error('自定义请求头最多 10 个。')
  for (const row of draft.env) {
    const name = row.name.trim()
    if (!name && !row.value) continue
    if (!name) throw new Error('请填写环境变量名称，或删除未使用的行。')
    if (Object.prototype.hasOwnProperty.call(env, name)) throw new Error('环境变量名称重复，请合并后保存。')
    if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) throw new Error('环境变量名称只支持英文字母、数字和下划线，且不能以数字开头。')
    if (row.value.length > 4000) throw new Error('环境变量值超过 4,000 字符。')
    env[name] = row.value
  }
  for (const row of draft.headers) {
    const name = row.name.trim()
    if (!name && !row.value) continue
    if (!name) throw new Error('请填写请求头名称，或删除未使用的行。')
    if (Object.prototype.hasOwnProperty.call(headers, name)) throw new Error('请求头名称重复，请合并后保存。')
    if (!HEADER_NAME.test(name)) throw new Error('请求头名称不符合 HTTP 头格式，请使用英文字母、数字与 - _ 等常规字符。')
    if (row.value.length > 2000) throw new Error('请求头值超过 2,000 字符。')
    headers[name] = row.value
  }
  const remote = draft.transport !== 'stdio'
  return { ...draft, name: draft.name.trim(), url: remote ? draft.url.trim() : '', command: !remote ? draft.command.trim() : '', args: !remote ? draft.args : [], env: { ...env }, headers: { ...headers }, token: remote ? draft.token.trim() : '' }
}

export function readMcpConfig(text: string): ConnectionDraft[] {
  if (text.length > 100000) throw new Error('配置过长，请只粘贴需要导入的 MCP 配置。')
  let input: unknown
  try { input = JSON.parse(text) } catch { throw new Error('无法读取 JSON，请粘贴完整配置，检查引号与逗号。') }
  if (!record(input)) throw new Error('MCP 配置必须是 JSON 对象。')
  const entries = Object.prototype.hasOwnProperty.call(input, 'mcpServers')
    ? record(input.mcpServers) ? Object.entries(input.mcpServers) : null
    : [[typeof input.name === 'string' ? input.name : '导入的 MCP', input] as const]
  if (!entries?.length || entries.length > 12) throw new Error('mcpServers 需包含 1–12 个连接。')
  return entries.map(([name, value]) => {
    if (!record(value)) throw new Error('连接配置必须是 JSON 对象。')
    const accepted = ['name', 'type', 'transport', 'command', 'args', 'env', 'url', 'headers', 'token']
    if (Object.keys(value).some(key => !accepted.includes(key))) throw new Error('配置包含暂不支持的字段（例如 cwd、timeout 或 OAuth）。请仅保留地址/命令、参数、环境变量、请求头和 Bearer 认证。')
    const type = value.type ?? value.transport
    if (type !== undefined && !['stdio', 'http', 'sse', 'streamable-http', 'streamableHttp'].includes(String(type))) throw new Error('当前只支持 stdio / Streamable HTTP / SSE，不支持 OAuth。')
    if (value.command !== undefined && value.url !== undefined) throw new Error('同一个连接不能同时配置命令和服务地址。')
    const remote = value.url !== undefined
    const transport: Transport = !remote ? 'stdio' : String(type).toLowerCase() === 'sse' ? 'sse' : 'http'
    if ((remote && type === 'stdio') || (!remote && type && type !== 'stdio')) throw new Error('传输方式与命令/地址不一致，请检查配置。')
    if (remote ? typeof value.url !== 'string' || !value.url.trim() : typeof value.command !== 'string' || !value.command.trim()) throw new Error('请提供非空的 url 服务地址或 command 可执行命令。')
    if (value.args !== undefined && (!Array.isArray(value.args) || value.args.some(arg => typeof arg !== 'string'))) throw new Error('args 必须是字符串数组，每项对应一个启动参数。')
    if (value.env !== undefined && (!record(value.env) || Object.values(value.env).some(item => typeof item !== 'string'))) throw new Error('env 必须是名称和值均为字符串的对象。')
    if (remote && (value.args !== undefined || value.env !== undefined)) throw new Error('远程连接不支持 args 或 env，请改用本地命令或移除这些字段。')
    if (!remote && (value.headers !== undefined || value.token !== undefined)) throw new Error('stdio 连接请通过 env 配置认证与请求头，不支持 HTTP headers/token。')
    let token = ''
    if (value.token !== undefined) {
      if (typeof value.token !== 'string') throw new Error('token 必须是字符串。')
      token = value.token
    }
    const headerEntries: { name: string; value: string }[] = []
    if (value.headers !== undefined) {
      if (!record(value.headers)) throw new Error('headers 必须是名称和值均为字符串的对象。')
      for (const [key, item] of Object.entries(value.headers)) {
        if (typeof item !== 'string') throw new Error('headers 必须是名称和值均为字符串的对象。')
        if (key.toLowerCase() === 'authorization') {
          if (!/^Bearer\s+\S+/i.test(item)) throw new Error('Authorization 需使用 Bearer 令牌。')
          const headerToken = item.replace(/^Bearer\s+/i, '')
          if (token && headerToken && token !== headerToken) throw new Error('token 与 Authorization 令牌不一致，请保留一个。')
          token = token || headerToken
          continue
        }
        headerEntries.push({ name: key, value: item })
      }
    }
    const draft: ConnectionDraft = { ...emptyConnection(), name, transport, url: remote ? value.url as string : '', command: remote ? '' : value.command as string, args: (value.args || []) as string[], env: Object.entries(value.env || {}).map(([key, item]) => ({ name: key, value: item as string })), headers: headerEntries, token }
    if (!name.trim() || name.length > 80) throw new Error('连接名称为空或超过 80 字符。')
    connectionPayload(draft)
    return draft
  })
}

type PublicServer = { name: string; transport: Transport; url: string; command: string; args: string[]; env_keys: string[]; headers_set: string[] }

/** 导出为通用 mcpServers JSON；凭证只保留键名占位，绝不导出值。 */
export function exportMcpConfig(servers: PublicServer[]): string {
  const entries = Object.fromEntries(servers.map(server => {
    if (server.transport === 'stdio') return [server.name, { command: server.command, args: server.args, ...(server.env_keys.length ? { env: Object.fromEntries(server.env_keys.map(key => [key, '<在本地填写>'])) } : {}) }]
    return [server.name, { type: server.transport, url: server.url, ...(server.headers_set.length ? { headers: Object.fromEntries(server.headers_set.map(key => [key, '<在本地填写>'])) } : {}) }]
  }))
  return JSON.stringify({ mcpServers: entries }, null, 2)
}
