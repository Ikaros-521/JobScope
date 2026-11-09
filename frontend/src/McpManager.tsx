import { useEffect, useMemo, useState } from 'react'
import { api, post, type Server } from './api'
import { connectionPayload, emptyConnection, exportMcpConfig } from './mcpConfig'
import { MCPConnectionFields } from './MCPConnectionFields'

type Act = (key: string, action: () => Promise<void>, message: string) => Promise<boolean>

export function serverState(server: Server) {
  if (server.enabled && server.allowed_tools.length) return { label: '可供 Agent 使用', ready: true }
  if (!server.tested) return { label: '待测试', ready: false }
  if (!server.allowed_tools.length) return { label: '待授权工具', ready: false }
  return { label: '待启用', ready: false }
}

function saveTextFile(filename: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }))
  const link = document.createElement('a')
  link.href = url; link.download = filename; link.click()
  URL.revokeObjectURL(url)
}

export function McpManager({ servers, busy, act, focusId, onFocusConsumed }: { servers: Server[]; busy: string; act: Act; focusId: string | null; onFocusConsumed: () => void }) {
  const [selected, setSelected] = useState<string | null>(servers[0]?.id ?? null)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(emptyConnection)
  const [search, setSearch] = useState('')
  const [toolSearch, setToolSearch] = useState('')
  useEffect(() => {
    if (!focusId) return
    setSelected(focusId)
    const server = servers.find(item => item.id === focusId)
    if (server && !server.tested) { setEditing(true); setDraft({ ...emptyConnection(), name: server.name, transport: server.transport, url: server.url, command: server.command, args: [...server.args] }) }
    onFocusConsumed()
  }, [focusId]) // eslint-disable-line react-hooks/exhaustive-deps
  const server = servers.find(item => item.id === selected) || null
  const filtered = useMemo(() => servers.filter(item => `${item.name} ${item.url} ${item.command}`.toLowerCase().includes(search.toLowerCase())), [servers, search])
  const tools = useMemo(() => (server?.tools || []).filter(item => `${item.name} ${item.description}`.toLowerCase().includes(toolSearch.toLowerCase())), [server, toolSearch])
  function openEdit(item: Server) {
    setSelected(item.id); setEditing(true)
    setDraft({ name: item.name, transport: item.transport, url: item.url, command: item.command, args: [...item.args], env: [], headers: [], token: '', clear_secrets: false })
  }
  function startCreate() {
    setSelected(null); setEditing(true); setToolSearch(''); setDraft(emptyConnection())
  }
  async function save() {
    const ok = await act('save', async () => {
      if (selected) await api(`/agent/mcp/${selected}`, { method: 'PUT', body: JSON.stringify(connectionPayload(draft)) })
      else {
        const created = await api<Server>('/agent/mcp', { method: 'POST', body: JSON.stringify(connectionPayload(draft)) })
        setSelected(created.id)
      }
    }, '连接已保存。点击「测试并发现工具」，选择工具后再启用。')
    if (ok) setEditing(false)
  }
  function patch(item: Server, changes: Partial<Server>) {
    void act(item.id, async () => {
      await api(`/agent/mcp/${item.id}`, { method: 'PATCH', body: JSON.stringify(changes) })
    }, '工具权限已更新，后续 Agent 请求生效。')
  }
  return <div className="ext-shell">
    <aside className="ext-list" aria-label="MCP 连接列表">
      <input type="search" aria-label="搜索连接" placeholder="搜索连接" value={search} onChange={event => setSearch(event.target.value)} />
      <div className="ext-list-items">{filtered.map(item => <button key={item.id} type="button" className={`ext-item ${selected === item.id ? 'active' : ''}`} onClick={() => { setSelected(item.id); setEditing(false); setToolSearch('') }}>
        <span className={`ext-dot ${serverState(item).ready ? 'ready' : ''}`} aria-hidden="true" />
        <span className="ext-item-body"><strong>{item.name}</strong><small>{item.transport === 'stdio' ? '本地命令' : item.transport === 'sse' ? 'SSE' : 'HTTP'} · {item.allowed_tools.length}/{item.tools.length} 工具 · {serverState(item).label}</small></span>
      </button>)}{!servers.length && <div className="extensions-empty"><span className="extensions-empty-mark" aria-hidden="true">＋</span><h3>连接第一个 MCP 工具</h3><p>去市场一键添加，或点击下方按钮手动配置服务地址</p><small>项目检索、薪资查询和简历读取已内置，无需另行配置</small></div>}{Boolean(servers.length) && !filtered.length && <p className="muted">没有匹配的连接</p>}</div>
      <button type="button" className="button secondary small" onClick={startCreate} disabled={Boolean(busy)}>添加 MCP 连接</button>
    </aside>
    <section className="ext-detail" aria-label="MCP 连接详情">
      {editing ? <>
        <div className="ext-detail-head"><h2>{selected ? '编辑 MCP 连接' : '添加 MCP 连接'}</h2>{selected && <button type="button" className="inline-button" onClick={() => setEditing(false)}>返回详情</button>}</div>
        <form className="agent-connection-form" onSubmit={event => { event.preventDefault(); void save() }}><fieldset disabled={Boolean(busy)}><MCPConnectionFields key={server?.id || 'new'} draft={draft} onChange={setDraft} editing={Boolean(selected)} envKeys={server?.env_keys || []} headerKeys={server?.headers_set || []} />
          <p className="field-help">保存后：测试连接 → 选择允许调用的工具 → 启用连接</p><div className="agent-form-actions"><button className="button secondary" type="button" onClick={() => setEditing(false)}>取消</button><button className="button primary" type="submit">{busy === 'save' ? '保存中…' : '保存连接'}</button></div></fieldset></form>
      </> : server ? <>
        <div className="ext-detail-head"><h2>{server.name}<span className={`extension-state ${serverState(server).ready ? 'ready' : ''}`}>{serverState(server).label}</span></h2><code className="market-endpoint">{server.transport === 'stdio' ? [server.command, ...server.args].join(' ') : server.url}</code></div>
        <div className="ext-detail-meta">{server.token_set && <span className="market-badge">令牌已保存</span>}{server.env_keys.map(key => <span key={key} className="market-badge">env · {key}</span>)}{server.headers_set.map(key => <span key={key} className="market-badge">头 · {key}</span>)}{server.transport !== 'stdio' && !server.token_set && !server.headers_set.length && <span className="muted">未配置认证</span>}</div>
        <div className="ext-detail-actions">
          <button type="button" className="button primary small" disabled={Boolean(busy)} onClick={() => void act(server.id, async () => { await post(`/agent/mcp/${server.id}/test`, {}) }, '测试成功，已更新工具目录。请选择工具并启用连接；重新测试会重置工具授权。')}>{busy === server.id ? '处理中…' : '测试并发现工具'}</button>
          <label className="agent-checkbox"><input type="checkbox" checked={server.enabled} disabled={Boolean(busy) || !server.tested} onChange={event => patch(server, { enabled: event.target.checked })} />启用连接</label>
          <button type="button" className="button secondary small" onClick={() => saveTextFile(`${server.name || 'mcp'}.json`, exportMcpConfig([server]))} disabled={Boolean(busy)}>导出配置</button>
          <button type="button" className="button secondary small" onClick={() => openEdit(server)} disabled={Boolean(busy)}>编辑</button>
          <button type="button" className="inline-button" disabled={Boolean(busy)} onClick={() => void act(server.id, async () => { setSelected(null); await api(`/agent/mcp/${server.id}`, { method: 'DELETE' }) }, '连接已移除。')}>移除</button>
        </div>
        {server.tested && !server.tools.length && <p className="field-help">连接成功，但服务没有返回工具。</p>}
        {Boolean(server.tools.length) && <>
          <div className="ext-tools-head"><h3>工具权限 · {server.allowed_tools.length} / {server.tools.length} 已允许</h3><div className="ext-tools-bulk"><button type="button" className="text-button" disabled={Boolean(busy)} onClick={() => patch(server, { allowed_tools: server.tools.map(item => item.name), auto_tools: server.auto_tools })}>全部允许</button><button type="button" className="text-button" disabled={Boolean(busy)} onClick={() => patch(server, { allowed_tools: [], auto_tools: [] })}>清空</button><input type="search" aria-label="搜索工具" placeholder="筛选工具" value={toolSearch} onChange={event => setToolSearch(event.target.value)} /></div></div>
          <p className="field-help">「允许调用」会在执行前请你确认；勾选「自动执行」后，该工具可直接运行。</p>
          <div className="agent-tools-list">{tools.map(tool => <div key={tool.name} className="agent-tool"><div><strong>{tool.name}</strong><p>{tool.description || '服务未提供工具说明'}</p></div><label className="agent-checkbox"><input type="checkbox" checked={server.allowed_tools.includes(tool.name)} disabled={Boolean(busy)} onChange={event => patch(server, { allowed_tools: event.target.checked ? [...server.allowed_tools, tool.name] : server.allowed_tools.filter(name => name !== tool.name), auto_tools: server.auto_tools.filter(name => name !== tool.name) })} />允许调用</label><label className="agent-checkbox"><input type="checkbox" checked={server.auto_tools.includes(tool.name)} disabled={Boolean(busy) || !server.allowed_tools.includes(tool.name)} onChange={event => patch(server, { auto_tools: event.target.checked ? [...server.auto_tools, tool.name] : server.auto_tools.filter(name => name !== tool.name) })} />自动执行</label></div>)}</div>
        </>}
        <p className="field-help">保存与测试都不会自动授予执行权限；启用后 Agent 每次调用仍需你确认，除非工具被标记为自动执行。</p>
      </> : <div className="extensions-empty"><span className="extensions-empty-mark" aria-hidden="true">＋</span><h3>选择左侧连接查看详情</h3><p>测试、工具授权、编辑与导出都在这里完成</p></div>}
    </section>
  </div>
}
