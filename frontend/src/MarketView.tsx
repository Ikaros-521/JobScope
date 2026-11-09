import { useEffect, useMemo, useState } from 'react'
import { api, date, post, type MarketMcp, type MarketPayload, type MarketSkill } from './api'

// 旧后端进程没有市场路由：未知路径 GET 404 / POST 405，此时提示重启而不是抛状态码。
function friendly(err: unknown) {
  const message = (err as Error).message || ''
  return /40[45]|Method Not Allowed/.test(message) ? '本地服务版本落后于当前页面：请关闭旧的 JobScope 服务窗口，重新运行 .\\start.ps1，再刷新页面。' : message
}

type LiveResult = { term: string; mcp: MarketMcp[]; skills: MarketSkill[]; skills_error: string | null }

function McpCard({ item, badge, busy, onInstall }: { item: MarketMcp; badge: string; busy: string; onInstall: (entry: MarketMcp) => void }) {
  return <article className="market-card">
    <div className="market-card-head"><span className="market-icon" aria-hidden="true">{item.icon}</span><strong>{item.name}</strong><span className={`market-badge ${item.official ? 'official' : ''}`}>{badge}</span>{item.needs_key && <span className="market-badge warn">需 {item.needs_key}</span>}{item.needs_config && <span className="market-badge warn">需改配置</span>}{item.installed && <span className="market-badge ok">已添加</span>}</div>
    <p className="market-desc">{item.desc}</p>
    <code className="market-endpoint">{item.transport === 'stdio' ? [item.command, ...(item.args || [])].join(' ') : item.url}</code>
    <div className="market-card-actions">{item.repo && <a className="text-button" href={item.repo} target="_blank" rel="noreferrer">来源 ↗</a>}{item.transport === 'stdio' && !item.installed && <span className="muted">测试时会启动本地命令</span>}<button type="button" className={`button ${item.installed ? 'secondary' : 'primary'} small`} disabled={Boolean(busy)} onClick={() => onInstall(item)}>{item.installed ? '再次添加' : '添加'}</button></div>
  </article>
}

function SkillCard({ item, badge, busy, onInstall, onTemplate }: { item: MarketSkill; badge: string; busy: string; onInstall: (entry: MarketSkill) => void; onTemplate?: (content: string) => void }) {
  return <article className="market-card">
    <div className="market-card-head"><span className="market-icon" aria-hidden="true">{item.icon}</span><strong>{item.title || item.name}</strong><span className={`market-badge ${item.official ? 'official' : ''}`}>{badge}</span>{typeof item.stars === 'number' && <span className="market-badge">★ {item.stars}</span>}{item.installed && <span className="market-badge ok">已安装</span>}</div>
    <p className="market-desc">{item.desc}</p>
    <code className="market-endpoint">{item.name}</code>
    <div className="market-card-actions">{item.repo && <a className="text-button" href={item.repo.startsWith('http') ? item.repo : `https://github.com/${item.repo}`} target="_blank" rel="noreferrer">来源 ↗</a>}<button type="button" className={`button ${item.installed ? 'secondary' : 'primary'} small`} disabled={Boolean(busy)} onClick={() => onInstall(item)}>{item.installed ? '重新导入' : '安装'}</button>{item.content && onTemplate && <button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={() => onTemplate(item.content || '')}>基于此创建</button>}</div>
  </article>
}

export function MarketView({ busy, onInstallMcp, onInstallSkill, onTemplate }: { busy: string; onInstallMcp: (entry: MarketMcp) => void; onInstallSkill: (entry: MarketSkill) => void; onTemplate: (content: string) => void }) {
  const [market, setMarket] = useState<MarketPayload | null>(null)
  const [error, setError] = useState('')
  const [category, setCategory] = useState('全部')
  const [search, setSearch] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [live, setLive] = useState<LiveResult | null>(null)
  const [liveLoading, setLiveLoading] = useState(false)
  const [liveError, setLiveError] = useState('')
  const term = search.trim()
  async function load() {
    try { setMarket(await api<MarketPayload>('/agent/market')); setError('') }
    catch (err) { setError(friendly(err)) }
  }
  useEffect(() => { void load() }, [])
  useEffect(() => {
    // 输入 ≥2 字符时防抖 400ms 实时搜索 npm（MCP）与 GitHub（Skills）；短词仍走本地过滤。
    if (term.length < 2) { setLive(null); setLiveError(''); setLiveLoading(false); return }
    const timer = setTimeout(() => {
      setLiveLoading(true); setLiveError('')
      api<LiveResult>(`/agent/market/search?q=${encodeURIComponent(term)}`)
        .then(result => setLive(result))
        .catch((err: unknown) => { setLive(null); setLiveError(friendly(err)) })
        .finally(() => setLiveLoading(false))
    }, 400)
    return () => clearTimeout(timer)
  }, [term])
  async function refresh() {
    if (refreshing || busy) return
    setRefreshing(true); setError('')
    try { setMarket(await post<MarketPayload>('/agent/market/refresh')) } catch (err) { setError(friendly(err)) } finally { setRefreshing(false) }
  }
  const entries = useMemo(() => {
    const skills = [...(market?.curated_skills || []).map(item => ({ ...item, layer: '内置' as const })), ...(market?.online?.skills || []).map(item => ({ ...item, layer: '在线' as const }))]
    const mcps = [...(market?.curated_mcp || []).map(item => ({ ...item, layer: '内置' as const })), ...(market?.online?.mcp || []).map(item => ({ ...item, layer: '在线' as const }))]
    const match = (text: string) => text.toLowerCase().includes(term.toLowerCase())
    return {
      skills: skills.filter(item => (category === '全部' || item.category === category) && match(`${item.name} ${item.title || ''} ${item.desc || ''}`)),
      mcps: mcps.filter(item => (category === '全部' || item.category === category) && match(`${item.name} ${item.desc || ''} ${item.command || ''} ${item.url || ''}`)),
      categories: ['全部', ...Array.from(new Set([...skills, ...mcps].map(item => item.category)))],
    }
  }, [market, category, term])
  return <div className="market-view">
    <div className="market-toolbar">
      <div className="market-chips" role="group" aria-label="分类筛选">{entries.categories.map(item => <button key={item} type="button" className={`market-chip ${category === item ? 'active' : ''}`} aria-pressed={category === item} onClick={() => setCategory(item)}>{item}</button>)}</div>
      <div className="market-tools"><input type="search" aria-label="搜索市场" placeholder="搜索市场；输入 2 字以上实时检索 npm 与 GitHub" value={search} onChange={event => setSearch(event.target.value)} /><button type="button" className="button secondary small" disabled={refreshing || Boolean(busy)} onClick={() => void refresh()}>{refreshing ? '刷新中…' : '刷新在线目录'}</button></div>
    </div>
    <p className="field-help">精选目录离线可用，一键添加；输入 2 个以上字符会实时检索——MCP 走 npm 注册表（自动附加 mcp），Skills 走 GitHub 仓库搜索（免凭证，自动附加 skill），结果本机缓存 5 分钟。「刷新在线目录」拉取推荐技能仓库的 Skills 缓存 15 分钟，失败明确回退。远程服务保存后仍需测试与授权，本地命令测试时会启动进程，请只添加你信任的服务。</p>
    {error && <p role="alert" className="market-error">{error}</p>}
    {market && <p className="muted">在线目录{market.online ? `更新于 ${date(new Date((market.fetched_at || 0) * 1000).toISOString())}` : '暂未刷新，点击右上角按钮获取推荐仓库内容'}；精选 Skills 可「基于此创建」改成自己的版本。</p>}
    <h2 className="market-heading">MCP 服务 <span>{entries.mcps.length}</span></h2>
    <div className="market-grid">{entries.mcps.map(item => <McpCard key={item.id} item={item} badge={item.layer === '在线' ? '在线' : item.official ? '官方' : '社区'} busy={busy} onInstall={onInstallMcp} />)}{!entries.mcps.length && <p className="muted">本地目录没有匹配的 MCP 服务，试试下方在线搜索</p>}</div>
    <h2 className="market-heading">Skills 工作方法 <span>{entries.skills.length}</span></h2>
    <div className="market-grid">{entries.skills.map(item => <SkillCard key={item.id} item={item} badge={item.layer === '在线' ? '在线' : '内置'} busy={busy} onInstall={onInstallSkill} onTemplate={item.content ? onTemplate : undefined} />)}{!entries.skills.length && <p className="muted">本地目录没有匹配的 Skills，试试下方在线搜索</p>}</div>
    {term.length >= 2 && <><h2 className="market-heading">在线搜索 · 「{term}」 <span>{liveLoading ? 'npm / GitHub 检索中…' : live ? `${live.mcp.length} 个 MCP · ${live.skills.length} 个 Skills` : ''}</span></h2>
      {liveError && <p role="alert" className="market-error">{liveError}</p>}
      <div className="market-grid">{(live?.mcp || []).map(item => <McpCard key={item.id} item={item} badge="npm 在线" busy={busy} onInstall={onInstallMcp} />)}</div>
      {!liveLoading && !liveError && live && !live.mcp.length && <p className="muted">npm 上没有匹配「{term}」的 MCP 服务；也可在 MCP 工具页手动配置地址</p>}
      {live?.skills_error && <p role="status" className="muted">{live.skills_error}</p>}
      {!live?.skills_error && Boolean(live?.skills.length) && <div className="market-grid">{live!.skills.map(item => <SkillCard key={item.id} item={item} badge="GitHub 在线" busy={busy} onInstall={onInstallSkill} />)}</div>}
      {!liveLoading && !liveError && !live?.skills_error && live && !live.skills.length && <p className="muted">GitHub 上没有匹配「{term}」且带 SKILL.md 的仓库</p>}
      <p className="field-help">在线搜索结果与本页上方「刷新在线目录」的推荐目录相互独立；GitHub 检索免凭证，受公开 API 限额约束。</p></>}
  </div>
}
