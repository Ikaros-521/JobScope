import { date, plain, safeUrl } from './api'
import type { Evidence } from './api'

export const kindNames: Record<string, string> = { job_posting: '岗位原文', disclosure_metadata: '公告索引', disclosure: '披露正文', company_statement: '企业自述', employee_statement: '员工自述', user_material: '用户材料', media_report: '媒体报道', announcement: '公告' }

export function EvidenceCard({ evidence }: { evidence: Evidence }) {
  const url = safeUrl(evidence.url)
  return <details className="evidence-card"><summary><div><strong>{evidence.title}</strong><span>{evidence.source} · {date(evidence.published_at || evidence.collected_at)} · {kindNames[evidence.kind] || evidence.kind}</span></div><span className="expand-sign" aria-hidden="true">+</span></summary><div className="evidence-body"><p>{plain(evidence.text)}</p><div className="evidence-meta"><span>证据 {evidence.id}</span>{url ? <a className="external-link" href={url} target="_blank" rel="noreferrer">出处</a> : <span>未提供原始链接</span>}</div><div className="tiny-label">采集于 {date(evidence.collected_at)}{!evidence.published_at && ' · 原文发布时间未知'}</div></div></details>
}
