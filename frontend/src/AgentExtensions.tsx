import { useEffect, useState } from 'react'
import { api, type Extensions } from './api'
import { toast } from './toast'
import { MarketView } from './MarketView'
import { McpManager } from './McpManager'
import { SkillManager } from './SkillManager'
import type { MarketMcp, MarketSkill } from './api'
import { navigateTabs } from './tabNavigation'

type Tab = 'mcp-market' | 'mcp' | 'skill-market' | 'skills'

const TAB_DESCRIPTIONS: Record<Tab, string> = {
  'mcp-market': '从内置目录与 npm 在线检索发现 MCP 服务，添加后在「MCP 工具」测试与授权。',
  mcp: '先保存连接，再测试、授权并启用。',
  'skill-market': '从内置目录与 GitHub 在线检索发现 Skills，安装后在「Skills」管理启用状态。',
  skills: '导入 SKILL.md，管理资源与启用状态。',
}

export function AgentExtensions() {
  const [data, setData] = useState<Extensions>({ servers: [], skills: [] })
  const [tab, setTab] = useState<Tab>('mcp-market')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState('')
  const [busy, setBusy] = useState('')
  const [focusServer, setFocusServer] = useState<string | null>(null)
  const [skillPrefill, setSkillPrefill] = useState('')

  async function refresh() { setData(await api<Extensions>('/agent/extensions')); setLoadError('') }
  async function load() {
    setLoading(true); setLoadError('')
    try { await refresh() }
    catch (err) { const message = (err as Error).message || '无法读取扩展配置'; setLoadError(message); toast(message, 'error') }
    finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [])
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
  const countsReady = !loading && !loadError
  return <section className="extensions-workspace" aria-label="Agent 扩展管理">
    <section className="page-intro"><div><h1>Agent 扩展</h1><p>为通用 Agent 连接工具、补充工作方法。</p></div></section>
    <div className="extensions-overview" aria-label="扩展启用概览" aria-busy={loading}>
      <article className="extension-summary"><span>MCP 工具</span><strong>{countsReady ? `${enabledServers} / ${data.servers.length}` : '—'}</strong><small>启用 / 总计</small><p>连接外部服务，按工具授权。</p></article>
      <article className="extension-summary"><span>Skills</span><strong>{countsReady ? `${enabledSkills} / ${data.skills.length}` : '—'}</strong><small>启用 / 总计</small><p>保存工作方法，按需读取。</p></article>
    </div>
    <div className="agent-extension-body">
      <div className="extensions-toolbar">
        <div className="agent-tabs" role="tablist" aria-label="扩展类型" onKeyDown={navigateTabs}>
          <button id="extensions-tab-mcp-market" type="button" role="tab" aria-controls="extensions-panel" aria-selected={tab === 'mcp-market'} tabIndex={tab === 'mcp-market' ? 0 : -1} onClick={() => setTab('mcp-market')}>MCP 市场</button>
          <button id="extensions-tab-mcp" type="button" role="tab" aria-controls="extensions-panel" aria-selected={tab === 'mcp'} tabIndex={tab === 'mcp' ? 0 : -1} onClick={() => { setTab('mcp'); setFocusServer(null) }}>MCP 工具 <span>{countsReady ? data.servers.length : '—'}</span></button>
          <button id="extensions-tab-skill-market" type="button" role="tab" aria-controls="extensions-panel" aria-selected={tab === 'skill-market'} tabIndex={tab === 'skill-market' ? 0 : -1} onClick={() => setTab('skill-market')}>Skills 市场</button>
          <button id="extensions-tab-skills" type="button" role="tab" aria-controls="extensions-panel" aria-selected={tab === 'skills'} tabIndex={tab === 'skills' ? 0 : -1} onClick={() => setTab('skills')}>Skills <span>{countsReady ? data.skills.length : '—'}</span></button>
        </div>
      </div>
      {loading && <p className="alert" role="status">正在读取扩展配置…</p>}
      {loadError && <div className="alert" role="alert"><p>扩展配置读取失败：{loadError}</p><button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={() => void load()}>重试</button></div>}
      <div id="extensions-panel" role="tabpanel" aria-labelledby={`extensions-tab-${tab}`} tabIndex={0}>
        <div className="extensions-description"><p>{TAB_DESCRIPTIONS[tab]}</p></div>
        {tab === 'mcp-market' && <MarketView kind="mcp" busy={busy} onInstallMcp={entry => void installMcp(entry)} onInstallSkill={entry => void installSkill(entry)} onTemplate={useTemplate} />}
        {tab === 'skill-market' && <MarketView kind="skill" busy={busy} onInstallMcp={entry => void installMcp(entry)} onInstallSkill={entry => void installSkill(entry)} onTemplate={useTemplate} />}
        {tab === 'mcp' && countsReady && <McpManager servers={data.servers} busy={busy} act={act} focusId={focusServer} onFocusConsumed={() => setFocusServer(null)} />}
        {tab === 'skills' && countsReady && <SkillManager skills={data.skills} busy={busy} act={act} prefill={skillPrefill} onPrefillConsumed={() => setSkillPrefill('')} />}
      </div>
      <p className="field-help extensions-safety">扩展配置与凭证保存在本机数据库，未加密；导出不含凭证。MCP 与 Skills 仅用于通用 Agent，不进入公司或岗位隔离对话；Skill 脚本只登记不执行。</p>
    </div>
  </section>
}
