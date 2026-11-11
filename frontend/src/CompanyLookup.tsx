import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { api, post, safeUrl } from './api'
import { toast } from './toast'
import type { BrowserStatus, Task } from './api'

type LookupCard = { name: string; url: string; fields: Record<string, string>; text: string }
type LookupResult = { task: Task; cards: LookupCard[] }

const CHANNELS = [
  { id: 'qcc', name: '企查查' },
  { id: 'tyc', name: '天眼查' },
] as const

export function CompanyLookup({ refreshMetadata }: { refreshMetadata: () => Promise<unknown> }) {
  const [channel, setChannel] = useState<'qcc' | 'tyc'>('qcc')
  const [company, setCompany] = useState('')
  const [browser, setBrowser] = useState<BrowserStatus | null>(null)
  const [result, setResult] = useState<LookupResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [startBusy, setStartBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    let active = true
    api<BrowserStatus>('/browser/status').then(data => { if (active) setBrowser(data) }).catch(() => {})
    return () => { active = false }
  }, [])
  async function startBrowser() {
    setStartBusy(true); setError('')
    try {
      // 与岗位浏览器采集共用同一入口；公司快查渠道只要求打开站点首页完成登录/验证。
      const status = await post<BrowserStatus>(`/browser/start?source=${channel}`)
      setBrowser(status); toast(status.message)
      await refreshMetadata()
    } catch (err) { setError((err as Error).message) } finally { setStartBusy(false) }
  }
  async function lookup(event: FormEvent) {
    event.preventDefault()
    setBusy(true); setError('')
    try {
      const data = await post<LookupResult>('/company/lookup', { channel, company })
      setResult(data)
      toast(data.task.message)
      if (data.cards.length) await refreshMetadata()
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }
  return <section className="lookup-panel paper-panel">
    <div className="section-heading"><div><h2>公司快查</h2><p className="body-small muted">用项目浏览器打开企查查 / 天眼查公开搜索页，把页面可见的公司信息快照存进资料库。公开页数据有限，取到什么存什么。</p></div></div>
    <form onSubmit={lookup}>
      <div className="browser-fields">
        <div><label className="field-label" htmlFor="lookup-channel">快查渠道</label><select id="lookup-channel" value={channel} onChange={event => setChannel(event.target.value as 'qcc' | 'tyc')}>{CHANNELS.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
        <div><label className="field-label" htmlFor="lookup-company">公司名称</label><input id="lookup-company" value={company} onChange={event => setCompany(event.target.value)} placeholder="例如：华为技术有限公司" required maxLength={120} /></div>
      </div>
      <div className="browser-form-actions">
        {!browser?.running && <button className="button secondary" type="button" disabled={startBusy || busy} onClick={() => void startBrowser()}>{startBusy ? <span className="spinner" /> : null}打开专用浏览器</button>}
        <button className="button primary" type="submit" disabled={busy || startBusy}>{busy ? <span className="spinner" /> : null}查询快照</button>
      </div>
    </form>
    <div className="browser-status"><span className={`connection-dot ${browser?.running ? 'connected' : ''}`} />{browser?.message || '浏览器状态未知'}</div>
    {result && <div className="lookup-result">
      <p className="lookup-message" role="status">{result.task.message}</p>
      {Boolean(result.cards.length) && <div className="lookup-cards">{result.cards.map(card => {
        const url = safeUrl(card.url)
        return <article className="lookup-card" key={card.url + card.name}>
          <div className="lookup-card-heading"><strong>{card.name}</strong>{url ? <a className="external-link" href={url} target="_blank" rel="noreferrer">出处</a> : null}</div>
          <p className="lookup-fields">{Object.entries(card.fields).map(([label, value]) => `${label}：${value}`).join(' · ') || '该页未给出更多结构化字段，原文已按快照保存'}</p>
        </article>
      })}</div>}
      <p className="tiny-label">快照按解析出的公司名归档到公司资料库（企业信息快照）；重复查询按同一出处去重，不会产生重复条目。</p>
    </div>}
    {error && <p className="alert error" role="alert">{error}</p>}
    <p className="tiny-label">仅读取公开页可见内容；遇到登录或验证时，请在项目浏览器内手动完成后重试，本应用不会自动绕过。</p>
  </section>
}
