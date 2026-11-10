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
    <div className="market-card-head"><svg className="market-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="M9 3v5m6-5v5M7 8h10v3a5 5 0 0 1-10 0V8Zm5 8v5" /></svg><strong>{item.name}</strong><span className={`market-badge ${item.official ? 'official' : ''}`}>{badge}</span>{item.needs_key && <span className="market-badge warn">需 {item.needs_key}</span>}{item.needs_config && <span className="market-badge warn">需改配置</span>}{item.installed && <span className="market-badge ok">已添加</span>}</div>
    <p className="market-card-meta">{item.transport === 'stdio' ? '本地命令' : item.transport === 'sse' ? 'SSE' : 'HTTP'}{item.category ? ` · ${item.category}` : ''}</p>
    <p className="market-desc">{item.desc}</p>
    <code className="market-endpoint">{item.transport === 'stdio' ? [item.command, ...(item.args || [])].join(' ') : item.url}</code>
    <div className="market-card-actions">{item.repo && <a className="text-button" href={item.repo} target="_blank" rel="noreferrer">来源 ↗</a>}{item.transport === 'stdio' && <span className="muted">测试时会启动本地命令</span>}<button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={() => onInstall(item)}>{item.installed ? '再次添加' : '添加'}</button></div>
  </article>
}

function SkillCard({ item, badge, busy, onInstall, onTemplate }: { item: MarketSkill; badge: string; busy: string; onInstall: (entry: MarketSkill) => void; onTemplate?: (content: string) => void }) {
  return <article className="market-card">
    <div className="market-card-head"><svg className="market-icon" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d="M14 3H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9l-6-6Zm0 0v6h6M8 13h8m-8 4h6" /></svg><strong>{item.title || item.name}</strong><span className={`market-badge ${item.official ? 'official' : ''}`}>{badge}</span>{typeof item.stars === 'number' && <span className="market-badge">★ {item.stars}</span>}{item.installed && <span className="market-badge ok">已安装</span>}</div>
    {item.category && <p className="market-card-meta">{item.category}</p>}
    <p className="market-desc">{item.desc}</p>
    <code className="market-endpoint">{item.name}</code>
    <div className="market-card-actions">{item.repo && <a className="text-button" href={item.repo.startsWith('http') ? item.repo : `https://github.com/${item.repo}`} target="_blank" rel="noreferrer">来源 ↗</a>}<button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={() => onInstall(item)}>{item.installed ? '重新导入' : '安装'}</button>{item.content && onTemplate && <button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={() => onTemplate(item.content || '')}>基于此创建</button>}</div>
  </article>
}

export function MarketView({ busy, onInstallMcp, onInstallSkill, onTemplate }: { busy: string; onInstallMcp: (entry: MarketMcp) => void; onInstallSkill: (entry: MarketSkill) => void; onTemplate: (content: string) => void }) {
  const [market, setMarket] = useState<MarketPayload | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [category, setCategory] = useState('全部')
  const [search, setSearch] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [live, setLive] = useState<LiveResult | null>(null)
  const [liveLoading, setLiveLoading] = useState(false)
  const [liveError, setLiveError] = useState('')
  const term = search.trim()
  async function load() {
    setLoading(true); setError('')
    try { setMarket(await api<MarketPayload>('/agent/market')) }
    catch (err) { setError(friendly(err)) }
    finally { setLoading(false) }
  }
  useEffect(() => { void load() }, [])
  useEffect(() => {
    // 输入 ≥2 字符时防抖 400ms；换词或卸载后忽略旧请求的结果与状态。
    let active = true
    setLive(null); setLiveError(''); setLiveLoading(term.length >= 2)
    if (term.length < 2) return
    const timer = setTimeout(() => {
      api<LiveResult>(`/agent/market/search?q=${encodeURIComponent(term)}`)
        .then(result => { if (active) setLive({ ...result, term }) })
        .catch((err: unknown) => { if (active) { setLive(null); setLiveError(friendly(err)) } })
        .finally(() => { if (active) setLiveLoading(false) })
    }, 400)
    return () => { active = false; clearTimeout(timer) }
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
  const currentLive = live?.term === term ? live : null
  const fetchedAt = market?.online?.fetched_at || market?.fetched_at
  return <div className="market-view">
    <div className="market-toolbar">
      <div className="market-tools">
        <div className="market-search-field">
          <label htmlFor="market-search">搜索扩展</label>
          <input id="market-search" type="search" aria-describedby="market-search-hint" placeholder="搜索工具、服务或工作方法" value={search} onChange={event => setSearch(event.target.value)} />
          <p id="market-search-hint" className="field-help">输入至少 2 个字符在线检索 npm / GitHub；短词仅筛选本地目录。</p>
        </div>
        <button type="button" className="button secondary small" disabled={loading || refreshing || Boolean(busy)} onClick={() => void refresh()}>{refreshing ? '刷新中…' : '刷新在线目录'}</button>
      </div>
      <div className="market-chips" role="group" aria-label="本地目录分类筛选">{entries.categories.map(item => <button key={item} type="button" className={`market-chip ${category === item ? 'active' : ''}`} aria-pressed={category === item} onClick={() => setCategory(item)}>{item}</button>)}</div>
    </div>
    <p className="field-help market-trust-note">远程服务保存后仍需测试与授权；本地命令测试时会启动进程。请只添加你信任的服务。</p>
    <details className="market-help"><summary>目录来源与缓存说明{market?.online && fetchedAt ? ` · ${date(new Date(fetchedAt * 1000).toISOString())} 更新` : ''}</summary><p className="field-help">精选目录离线可用。在线搜索 MCP 使用 npm 注册表（自动附加 mcp），Skills 使用 GitHub 仓库搜索（自动附加 skill）；结果在本机缓存 5 分钟。GitHub 检索免凭证，受公开 API 限额约束。「刷新在线目录」独立拉取推荐技能仓库的 Skills，缓存 15 分钟；失败时保留当前目录并提示。精选 Skills 可「基于此创建」修改。</p></details>
    {loading && <p role="status" className="muted">正在读取市场目录…</p>}
    {refreshing && <p role="status" className="muted">正在刷新在线目录，当前目录仍可浏览…</p>}
    {error && <div role="alert" className="market-error"><p>{market ? '在线目录刷新失败，保留当前目录：' : '市场目录读取失败：'}{error}</p>{!market && <button type="button" className="button secondary small" disabled={loading || refreshing || Boolean(busy)} onClick={() => void load()}>重试读取目录</button>}</div>}
    {market && <>
      <h2 className="market-heading">MCP 服务 <span>{entries.mcps.length}</span></h2>
      <div className="market-grid">{entries.mcps.map(item => <McpCard key={item.id} item={item} badge={item.layer === '在线' ? '在线' : item.official ? '官方' : '社区'} busy={busy} onInstall={onInstallMcp} />)}{!entries.mcps.length && <p className="muted">当前目录无匹配的 MCP 服务，可调整分类或搜索词。</p>}</div>
      <h2 className="market-heading">Skills 工作方法 <span>{entries.skills.length}</span></h2>
      <div className="market-grid">{entries.skills.map(item => <SkillCard key={item.id} item={item} badge={item.layer === '在线' ? '在线' : '内置'} busy={busy} onInstall={onInstallSkill} onTemplate={item.content ? onTemplate : undefined} />)}{!entries.skills.length && <p className="muted">当前目录无匹配的 Skills，可调整分类或搜索词。</p>}</div>
    </>}
    {term.length >= 2 && <><h2 className="market-heading">在线搜索 · 「{term}」 <span role="status">{liveLoading ? 'npm / GitHub 检索中…' : currentLive ? `${currentLive.mcp.length} 个 MCP${currentLive.skills_error ? ' · Skills 检索失败' : ` · ${currentLive.skills.length} 个 Skills`}` : ''}</span></h2>
      {liveError && <p role="alert" className="market-error">{liveError}</p>}
      <div className="market-grid">{(currentLive?.mcp || []).map(item => <McpCard key={item.id} item={item} badge="npm 在线" busy={busy} onInstall={onInstallMcp} />)}</div>
      {!liveLoading && !liveError && currentLive && !currentLive.mcp.length && <p className="muted">npm 上没有匹配「{term}」的 MCP 服务；也可在 MCP 工具页手动配置地址</p>}
      {currentLive?.skills_error && <p role="status" className="muted">{currentLive.skills_error}</p>}
      {!currentLive?.skills_error && Boolean(currentLive?.skills.length) && <div className="market-grid">{currentLive!.skills.map(item => <SkillCard key={item.id} item={item} badge="GitHub 在线" busy={busy} onInstall={onInstallSkill} />)}</div>}
      {!liveLoading && !liveError && !currentLive?.skills_error && currentLive && !currentLive.skills.length && <p className="muted">GitHub 上没有匹配「{term}」且带 SKILL.md 的仓库</p>}
    </>}
  </div>
}
