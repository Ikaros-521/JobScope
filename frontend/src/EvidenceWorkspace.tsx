import { useEffect, useRef, useState } from 'react'
import { api, post } from './api'
import type { Evidence, PageInfo, Task } from './api'
import { Pagination, emptyPage } from './Pagination'
import { EvidenceCard, kindNames } from './EvidenceCard'
import { ChatWorkspace } from './chat/ChatWorkspace'
import { navigateTabs } from './tabNavigation'

export function EvidenceWorkspace({ refreshMetadata, modelConfigured, active }: { refreshMetadata: () => Promise<void>; modelConfigured: boolean; active: boolean }) {
  const [items, setItems] = useState<Evidence[]>([])
  const [info, setInfo] = useState<PageInfo>(emptyPage)
  const [company, setCompany] = useState('')
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState({ company: '', q: '' })
  const [chatCompany, setChatCompany] = useState('')
  const [companies, setCompanies] = useState<{ company: string; evidence_count: number }[]>([])
  const [tab, setTab] = useState<'records' | 'chat'>('records')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const request = useRef(0)
  const filtersRef = useRef(filter)
  filtersRef.current = filter
  async function load(page = 1, values = filtersRef.current) {
    const token = ++request.current
    const params = new URLSearchParams({ page: String(page), page_size: '10', q: values.q })
    if (values.company) params.set('company', values.company)
    const data = await api<PageInfo & { evidence: Evidence[] }>(`/evidence?${params}`)
    if (token === request.current) { setItems(data.evidence); setInfo(data) }
  }
  useEffect(() => { if (active) void load().catch(err => setError((err as Error).message)); return () => { request.current++ } }, [active])
  useEffect(() => {
    if (!active) return
    const q = search.trim()
    if (q === filtersRef.current.q) return
    const timer = window.setTimeout(() => { const values = { company: filtersRef.current.company, q }; setFilter(values); void run('search', () => load(1, values)) }, 300)
    return () => window.clearTimeout(timer)
  }, [search, active])
  useEffect(() => {
    let active = true
    const timer = window.setTimeout(() => {
      void api<{ companies: typeof companies }>(`/companies?q=${encodeURIComponent(company)}&page_size=20`).then(data => { if (active) setCompanies(data.companies) }).catch(() => { if (active) setCompanies([]) })
    }, 200)
    return () => { active = false; window.clearTimeout(timer) }
  }, [company])
  async function run(key: string, action: () => Promise<void>) {
    setBusy(key); setError(''); setNotice('')
    try { await action() } catch (err) { setError((err as Error).message) } finally { setBusy('') }
  }
  function chooseCompany(value: string) { setCompany(value); setChatCompany(value); setTab('chat') }
  return <>
    <section className="page-intro"><div><h1>公司资料库</h1><p>按公司核对来源，围绕单一主体展开调查</p></div><span className="revision-label">精确主体 · 独立对话</span></section>
    {error && <div role="alert" className="alert error">{error}</div>}{notice && <div role="status" className="alert notice">{notice}</div>}
    <section className="paper-panel evidence-library-controls"><form onSubmit={event => { event.preventDefault(); const values = { company: company.trim(), q: search.trim() }; setFilter(values); setChatCompany(values.company); void run('search', () => load(1, values)) }}>
      <div><label className="field-label" htmlFor="evidence-company">公司主体（精确匹配）</label><input id="evidence-company" list="known-companies" value={company} onChange={event => setCompany(event.target.value)} placeholder="输入或选择已保存的公司" maxLength={300} /><datalist id="known-companies">{companies.map(item => <option value={item.company} key={item.company}>{item.evidence_count} 条证据</option>)}</datalist></div>
      <button className="button secondary" type="submit" disabled={Boolean(busy)}>{busy === 'search' ? <span className="spinner" /> : null}查询资料</button>
      <button className="button primary" type="button" disabled={Boolean(busy) || !company.trim()} onClick={() => void run('collect', async () => { const result = await post<{ task: Task }>('/collect', { source: 'cninfo', query: company.trim(), limit: 10 }); setNotice(`${result.task.message}。发行人简称可能不同，可清空公司筛选查看结果`); await Promise.all([load(), refreshMetadata()]) })}>{busy === 'collect' ? <span className="spinner" /> : null}检索巨潮公告</button>
    </form><div className="library-scope"><span>资料筛选：{filter.company || '全部公司'}{filter.q && ` · ${filter.q}`}</span>{(filter.company || filter.q) && <button className="text-button" onClick={() => { setCompany(''); setSearch(''); const values = { company: '', q: '' }; setFilter(values); void run('search', () => load(1, values)) }}>清除筛选</button>}<span>品牌、集团和子公司不自动合并</span></div></section>
    <div className="workspace-tabs" role="tablist" aria-label="公司资料库内容" onKeyDown={navigateTabs}><button id="library-records-tab" role="tab" aria-controls="library-records-panel" tabIndex={tab === 'records' ? 0 : -1} aria-selected={tab === 'records'} onClick={() => setTab('records')}>来源档案 <span>{info.total}</span></button><button id="library-chat-tab" role="tab" aria-controls="library-chat-panel" tabIndex={tab === 'chat' ? 0 : -1} aria-selected={tab === 'chat'} onClick={() => setTab('chat')}>公司对话{chatCompany && <span> · {chatCompany}</span>}</button></div>
    <section hidden={tab !== 'records'} id="library-records-panel" className="paper-panel evidence-library-list" role="tabpanel" aria-labelledby="library-records-tab"><div className="section-heading"><div><h2>来源档案</h2><p className="body-small muted">原文保留出处和时间；点击公司可进入独立对话</p></div><div className="library-search"><label className="field-label" htmlFor="evidence-text-search">搜索标题与内容</label><div className="library-search-field"><input id="evidence-text-search" value={search} onChange={event => setSearch(event.target.value)} placeholder={filter.company ? `在${filter.company}内搜索` : '输入关键词，立即搜索'} maxLength={200} />{busy === 'search' ? <span className="spinner" aria-hidden="true" /> : search && <button className="text-button" type="button" onClick={() => setSearch('')}>清空</button>}</div><p className="tiny-label" aria-live="polite">{filter.q ? `「${filter.q}」命中 ${info.total} 条来源` : `共 ${info.total} 条来源 · 搜索覆盖标题与正文`}</p></div></div>
      <Pagination info={info} label="来源档案" busy={Boolean(busy)} onPage={page => void run('page', () => load(page))} />
      {items.length ? items.map(item => <article className="library-item" key={item.id}><div className="library-company"><button className="company-link" onClick={() => chooseCompany(item.company)} aria-label={`与${item.company}对话`}>{item.company || '主体未知'}</button><span>{kindNames[item.kind] || item.kind}</span></div><EvidenceCard evidence={item} query={filter.q} />{item.source === 'cninfo' && item.kind === 'disclosure_metadata' && <div className="extraction-action"><p>提取PDF正文后依据页码和原始报告核对</p><button className="button secondary small" disabled={Boolean(busy)} onClick={() => void run(item.id, async () => { const result = await post<{ pages: number; total_pages: number; truncated: boolean }>(`/evidence/${encodeURIComponent(item.id)}/extract`); setNotice(`已提取 ${result.pages}/${result.total_pages} 页${result.truncated ? '，报告已截断，请核对覆盖范围' : ''}`); await load(info.page) })}>{busy === item.id ? <span className="spinner" /> : null}提取报告正文</button></div>}</article>) : <div className="empty"><h3>{filter.q ? `标题和正文里没有「${filter.q}」` : '还没有符合条件的资料'}</h3><p>{filter.q ? '关键词按空格拆分，需同时命中；也可清除筛选查看全部来源，或检索公司公告' : '调整筛选、检索公司公告，或在岗位档案中补充材料'}</p></div>}
      <Pagination info={info} label="来源档案底部" busy={Boolean(busy)} onPage={page => void run('page', () => load(page))} />
    </section><div hidden={tab !== 'chat'} id="library-chat-panel" className="library-chat-panel" role="tabpanel" aria-labelledby="library-chat-tab"><section className="paper-panel company-picker"><div><label className="field-label" htmlFor="chat-company-select">对话主体（精确匹配）</label><div className="company-picker-row"><input id="chat-company-select" list="known-companies" value={company} onChange={event => setCompany(event.target.value)} placeholder="输入资料库中的精确公司名称" maxLength={300} /><button className="button secondary" disabled={!company.trim() || company.trim() === chatCompany} onClick={() => setChatCompany(company.trim())}>确认公司</button></div><p className="tiny-label">切换主体会另开新会话，之前的会话仍保留在左侧列表；资料筛选不会改变已锁定的对话范围。</p></div>{chatCompany && <span className="company-scope-chip"><span>已锁定主体</span><strong>{chatCompany}</strong></span>}</section>{tab === 'chat' && <ChatWorkspace key={chatCompany || 'unselected'} scope={{ kind: 'company', company: chatCompany }} modelConfigured={modelConfigured} />}</div>
  </>
}
