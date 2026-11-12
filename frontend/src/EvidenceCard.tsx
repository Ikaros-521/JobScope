import { useState } from 'react'
import type { ReactNode } from 'react'
import { api, date, plain, safeUrl } from './api'
import type { Evidence } from './api'

export const kindNames: Record<string, string> = { job_posting: '岗位原文', disclosure_metadata: '公告索引', disclosure: '披露正文', company_statement: '企业自述', employee_statement: '员工自述', user_material: '用户材料', media_report: '媒体报道', announcement: '公告', web_company_profile: '企业信息快照' }

export function searchTerms(query: string) { return query.trim().split(/\s+/).filter(Boolean) }

function escapePattern(value: string) { return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') }

export function highlight(text: string, words: string[]): ReactNode {
  if (!words.length) return text
  return text.split(new RegExp(`(${words.map(escapePattern).join('|')})`, 'gi'))
    .map((part, index) => index % 2 ? <mark className="search-hit" key={index}>{part}</mark> : part)
}

// 命中位置常在长报告中段，取命中处的窗口文字，避免逐条展开才能确认
export function excerpt(text: string, words: string[]) {
  const positions = words.map(word => text.toLowerCase().indexOf(word.toLowerCase())).filter(index => index >= 0)
  if (!positions.length) return ''
  const at = Math.min(...positions)
  const start = Math.max(0, at - 60)
  const end = Math.min(text.length, at + 200)
  return `${start > 0 ? '…' : ''}${text.slice(start, end)}${end < text.length ? '…' : ''}`.replace(/\s+/g, ' ')
}

export function EvidenceCard({ evidence, query = '' }: { evidence: Evidence; query?: string }) {
  const url = safeUrl(evidence.url)
  const words = searchTerms(query)
  // 列表接口对长正文做了裁剪：展开时按需取全文；摘要窗口优先用后端基于全文算出的 text_hit
  // （命中可能落在长报告中段，裁剪后的本地 text 找不到）。
  const [fullText, setFullText] = useState<string | null>(null)
  const [fetching, setFetching] = useState(false)
  const content = plain(fullText ?? evidence.text)
  const hit = evidence.text_hit || excerpt(content, words)
  async function ensureFull(event: React.SyntheticEvent<HTMLDetailsElement>) {
    if (!event.currentTarget.open || !evidence.text_truncated || fullText !== null || fetching) return
    setFetching(true)
    try {
      const data = await api<{ evidence: Evidence }>(`/evidence/${encodeURIComponent(evidence.id)}`)
      setFullText(data.evidence.text)
    } catch { /* 取全文失败：展开区保留已裁剪的正文 */ }
    finally { setFetching(false) }
  }
  return <details className="evidence-card" onToggle={ensureFull}><summary><div><strong>{highlight(evidence.title, words)}</strong><span>{evidence.source} · {date(evidence.published_at || evidence.collected_at)} · {kindNames[evidence.kind] || evidence.kind}</span>{hit && <span className="evidence-hit">{highlight(hit, words)}</span>}</div><span className="expand-sign" aria-hidden="true">+</span></summary><div className="evidence-body">{evidence.text_truncated && fullText === null && fetching ? <p className="muted">正在读取全文…</p> : <p>{highlight(content, words)}</p>}<div className="evidence-meta"><span>证据 {evidence.id}</span>{url ? <a className="external-link" href={url} target="_blank" rel="noreferrer">出处</a> : <span>未提供原始链接</span>}</div><div className="tiny-label">采集于 {date(evidence.collected_at)}{!evidence.published_at && ' · 原文发布时间未知'}</div></div></details>
}
