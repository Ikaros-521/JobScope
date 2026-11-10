import { useEffect, useState } from 'react'
import { api, type Extensions } from './api'
import { toast } from './toast'
import { MarketView } from './MarketView'
import { McpManager } from './McpManager'
import { SkillManager } from './SkillManager'
import type { MarketMcp, MarketSkill } from './api'

type Tab = 'market' | 'mcp' | 'skills'

export function AgentExtensions() {
  const [data, setData] = useState<Extensions>({ servers: [], skills: [] })
  const [tab, setTab] = useState<Tab>('market')
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [focusServer, setFocusServer] = useState<string | null>(null)
  const [skillPrefill, setSkillPrefill] = useState('')

  async function refresh() { setData(await api<Extensions>('/agent/extensions')) }
  useEffect(() => { void refresh().catch(err => toast((err as Error).message, 'error')).finally(() => setLoading(false)) }, [])
  async function act(key: string, action: () => Promise<void>, message: string): Promise<boolean> {
    if (busy) return false
    setBusy(key)
    try { await action(); await refresh(); toast(message); return true }
    catch (err) {
      await refresh().catch(() => {}) // 失败时以服务端状态为准，避免乐观更新残留
      toast((err as Error).message, 'error'); return false
    }
    finally { setBusy('') }
  }
  function jumpToServer(id: string | null) { setTab('mcp'); setFocusServer(id) }

  async function installMcp(entry: MarketMcp) {
    const ok = await act('market-mcp', async () => {
      await api('/agent/mcp', { method: 'POST', body: JSON.stringify({ name: entry.name, transport: entry.transport, url: entry.url || '', command: entry.command || '', args: entry.args || [], env: {}, headers: {}, token: '', clear_secrets: false }) })
    }, '已添加到 MCP 工具，尚未测试与启用。请在详情中完成测试与工具授权。')
    if (ok) jumpToServer(null)
  }
  async function installSkill(entry: MarketSkill) {
    await act('market-skill', async () => {
      if (entry.path && entry.repo) await api('/agent/skills/install', { method: 'POST', body: JSON.stringify({ repo: entry.repo, path: entry.path }) })
      else await api('/agent/skills', { method: 'POST', body: JSON.stringify({ content: (entry as MarketSkill & { content?: string }).content || '' }) })
    }, entry.installed ? '同名 Skill 已被新版本覆盖。' : 'Skill 已导入并启用。下一次通用 Agent 对话可按需读取。')
  }
  function useTemplate(content: string) { setSkillPrefill(content); setTab('skills') }

  const enabledServers = data.servers.filter(item => item.enabled).length
  const enabledSkills = data.skills.filter(item => item.enabled).length
  return <section className="extensions-workspace" aria-label="Agent 扩展管理">
    <section className="page-intro"><div><h1>Agent 扩展</h1><p>从市场安装工具与方法，或手动接入自己的 MCP 服务</p></div><span className="revision-label">{enabledServers} 个连接启用 · {enabledSkills} 个 Skills 启用</span></section>
    <div className="agent-extension-body">
      <div className="extensions-toolbar"><div className="agent-tabs" role="group" aria-label="扩展类型"><button type="button" aria-pressed={tab === 'market'} onClick={() => setTab('market')}>市场</button><button type="button" aria-pressed={tab === 'mcp'} onClick={() => { setTab('mcp'); setFocusServer(null) }}>MCP 工具 <span>{data.servers.length}</span></button><button type="button" aria-pressed={tab === 'skills'} onClick={() => setTab('skills')}>Skills <span>{data.skills.length}</span></button></div></div>
      {loading && tab !== 'market' && <p role="status">正在读取扩展…</p>}
      {tab === 'market' && <MarketView busy={busy} onInstallMcp={entry => void installMcp(entry)} onInstallSkill={entry => void installSkill(entry)} onTemplate={useTemplate} />}
      {tab === 'mcp' && !loading && <McpManager servers={data.servers} busy={busy} act={act} focusId={focusServer} onFocusConsumed={() => setFocusServer(null)} />}
      {tab === 'skills' && !loading && <SkillManager skills={data.skills} busy={busy} act={act} prefill={skillPrefill} onPrefillConsumed={() => setSkillPrefill('')} />}
      <p className="field-help">扩展配置与凭证保存在本机数据库，未加密；导出不含凭证。MCP 与 Skills 仅用于通用 Agent，不进入公司或岗位隔离对话；Skill 脚本只登记不执行。</p>
    </div>
  </section>
}
