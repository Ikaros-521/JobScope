import { useEffect, useState } from 'react'
import { date, getFailureMuseum } from './api'
import type { FailureMuseum } from './api'

// 失败博物馆（2026-11-23）：跨场次失分模式报告（规则版）+ 已标注场次一览。
// 价值主张：孤立的失败是消耗，重复的失败被看见才是信号——报告只点名重复，
// 不给人打分；不可控因素列出来只为「这不是你的问题」。

const OUTCOME_NAMES: Record<string, string> = {
  self_positive: '自评满意', self_neutral: '自评一般', self_negative: '自评不满意',
  pass: '通过', fail: '未通过', unknown: '结果未知',
}
const SOURCE_NAMES: Record<string, string> = { mock: '模拟', real: '真实' }

export function FailureMuseumView() {
  const [data, setData] = useState<FailureMuseum | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    void getFailureMuseum()
      .then(value => { if (alive) setData(value) })
      .catch(err => { if (alive) setError((err as Error).message) })
    return () => { alive = false }
  }, [])

  return <div className="fm-page">
    <section className="page-intro compact"><h1 className="sr-only">失败博物馆</h1>
      <span className="revision-label">{data ? `已归档 ${data.report.total_sessions} 场 · 纯本地规则统计，不调用模型` : '加载中…'}</span>
    </section>
    {error && <div className="iv-error" role="alert"><span>{error}</span></div>}
    {!data && !error && <div className="iv-state">加载博物馆…<span className="spinner" /></div>}
    {data && <PatternBoard data={data} />}
  </div>
}

function PatternBoard({ data }: { data: FailureMuseum }) {
  const report = data.report
  const records = data.records
  // 归档行只存 tag id，显示名从分类学解析（失败时退回 id 本身）。
  const labels = new Map<string, string>(
    [...data.controlled, ...data.uncontrolled].map(item => [item.id, item.label]))
  if (!report.total_sessions) return <div className="iv-state fm-empty">
    还没有馆藏。在「模拟面试」或「真实复盘」的报告页底部完成第一场归因标注，<br />
    系统会在这里点名跨场次重复出现的失分点。
  </div>
  return <>
    <section className="paper-panel fm-report">
      <h2>重复失分点</h2>
      {report.recurring.length
        ? <div className="fm-recurring">
            {report.recurring.map(item => <div key={item.tag} className="fm-recurring-item">
              <span className="fm-tag-chip">{item.label}</span>
              <strong>{item.count} 次</strong>
              {(item.negative || item.positive)
                ? <span className="tiny-label">失败 {item.negative} · 成功 {item.positive}</span> : null}
              <p className="fm-advice">下一场：{item.advice}</p>
            </div>)}
          </div>
        : <p className="muted body-small">还没有跨场次重复出现的可控失分点。</p>}
      {report.uncontrolled.length > 0 && <>
        <h2>不可控环境因素</h2>
        <div className="fm-uncontrolled">
          {report.uncontrolled.map(item => <div key={item.tag} className="fm-uncontrolled-item" title={item.hint}>
            <span className="fm-tag-chip muted-chip">{item.label}</span>
            <span className="tiny-label">×{item.count}</span>
          </div>)}
        </div>
        <p className="tiny-label">这些不是你的问题：记下来，别内化。</p>
      </>}
      {report.limitations.map(line => <p key={line} className="tiny-label fm-limit">{line}</p>)}
    </section>
    <section className="paper-panel">
      <h2>馆藏一览</h2>
      {records.length === 0 ? <p className="muted">没有已归档的场次。</p> : <div className="fm-records">
        {records.map(item => <div key={item.id} className="fm-record">
          <span className={`fm-source ${item.source}`}>{SOURCE_NAMES[item.source] ?? item.source}</span>
          <strong className="fm-record-title">{[item.company, item.job_title].filter(Boolean).join(' · ') || '未命名场次'}</strong>
          {item.outcome && <span className="fm-outcome">{OUTCOME_NAMES[item.outcome] ?? item.outcome}</span>}
          <span className="tiny-label">{date(item.updated_at)}</span>
          <div className="fm-record-tags">
            {item.tags.map(tag => <span key={tag.tag} className={`fm-tag-chip small${tag.scope === 'uncontrolled' ? ' muted-chip' : ''}`}
              title={tag.note || undefined}>{labels.get(tag.tag) ?? tag.tag}{tag.note ? `：${tag.note}` : ''}</span>)}
          </div>
          {item.note && <p className="fm-record-note">{item.note}</p>}
        </div>)}
      </div>}
    </section>
  </>
}
