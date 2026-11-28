import { useEffect, useState } from 'react'
import { date, getOutreachVersions, markOutreachUsed, openOutreachStream, startOutreachRun } from './api'
import type { Job, OutreachVersion } from './api'
import { toast } from './toast'
import { Button } from './ui'
import { Icon } from './Icon'

// 触达话术面板（2026-11-22）：打招呼/跟进消息生成与版本 A/B。
// 平台发送仍由用户在招聘站窗口手动完成（人工把关），这里负责生成、复制与「已使用」标记——
// 用了哪条标记哪条，漏斗侧的回复数据对比才有归因依据。模型不可用时后端自动退规则模板。
// 生成走后台 run + SSE（2026-11-28 审计 P1-4）：不再在请求线程里等模型，done 帧带回
// 已落库的版本行；断流/失败一律解锁（同 PrepPlanView 的收尾纪律）。
const KINDS = [
  { id: 'greeting' as const, label: '打招呼', hint: 'BOSS 首条消息' },
  { id: 'followup' as const, label: '跟进', hint: '投后催进展' },
]
const ORIGIN_NAMES: Record<string, string> = { model: '模型生成', rules: '规则模板' }

export function OutreachEditor({ job, onClose }: { job: Job; onClose: () => void }) {
  const [versions, setVersions] = useState<OutreachVersion[]>([])
  const [kind, setKind] = useState<'greeting' | 'followup'>(
    job.pipeline_status === 'applied' || job.pipeline_status === 'interviewing' ? 'followup' : 'greeting')
  const [busy, setBusy] = useState('')
  const [runId, setRunId] = useState('')

  useEffect(() => {
    void getOutreachVersions(job.id).then(data => setVersions(data.versions)).catch(() => setVersions([]))
  }, [job.id])

  function run(key: string, action: () => Promise<void>) {
    setBusy(key)
    void action().catch((err: Error) => toast(err.message, 'error')).finally(() => setBusy(''))
  }

  const generate = () => {
    if (runId) return // 生成已在进行中
    setBusy('generate')
    void startOutreachRun(job.id, kind)
      .then(start => setRunId(start.run_id))
      .catch((err: Error) => { toast(err.message, 'error'); setBusy('') })
  }

  useEffect(() => {
    if (!runId) return
    const source = openOutreachStream(job.id, runId, (event, data) => {
      if (event === 'done') {
        const version = data.version as OutreachVersion
        toast('已生成新话术，复制后到招聘站发送', 'notice')
        setVersions(previous => [version, ...previous])
        setRunId(''); setBusy('')
        source.close()
      } else if (event === 'error') {
        toast(String((data as { message?: string }).message ?? '生成失败'), 'error')
        setRunId(''); setBusy('')
        source.close()
      }
    })
    source.onerror = () => {
      // 致命断流（CLOSED）不会再有任何帧，就地解锁；短暂抖动交给自动重连。
      if (source.readyState !== EventSource.CLOSED) return
      setRunId(''); setBusy('')
      toast('生成连接中断，请重试', 'error')
    }
    return () => source.close()
  }, [runId, job.id])
  const copy = (item: OutreachVersion) => run(`copy-${item.id}`, async () => {
    await navigator.clipboard.writeText(item.text)
    toast('话术已复制')
  })
  const markUsed = (item: OutreachVersion) => run(`used-${item.id}`, async () => {
    await markOutreachUsed(job.id, item.id)
    setVersions(previous => previous.map(v => v.id === item.id ? { ...v, used_at: new Date().toISOString() } : v))
    toast('已标记为使用过', 'notice')
  })

  const shown = versions.filter(item => item.kind === kind)
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose() }}>
    <section className="modal paper-panel outreach-editor" role="dialog" aria-modal="true" aria-labelledby="outreach-title" onKeyDown={event => { if (event.key === 'Escape' && !busy) onClose() }}>
      <div className="section-heading"><div><h2 id="outreach-title">触达话术</h2></div><button className="icon-button" aria-label="关闭弹窗" onClick={onClose} disabled={Boolean(busy)}><Icon name="close" /></button></div>
      <p className="muted body-small">{job.title} · {job.company} · 发送仍在招聘站手动完成，这里只生成与记录</p>
      <div className="outreach-kinds" role="group" aria-label="话术类型">
        {KINDS.map(item => <button key={item.id} type="button" title={item.hint}
          className={`pipeline-filter-chip${kind === item.id ? ' active' : ''}`}
          onClick={() => setKind(item.id)}>{item.label}</button>)}
        <Button className="button primary small outreach-generate" type="button" busy={busy === 'generate'} disabled={Boolean(busy)}
          onClick={generate}>生成<Icon name="wand" size={14} /></Button>
      </div>
      {shown.length === 0 ? <p className="muted body-small outreach-empty">还没有「{KINDS.find(item => item.id === kind)?.label}」话术，点「生成」创建第一版。</p>
        : <ul className="outreach-versions">{shown.map(item => <li key={item.id} className={item.used_at ? 'used' : ''}>
          <p className="outreach-text">{item.text}</p>
          <div className="outreach-meta">
            <span className="tiny-label">{ORIGIN_NAMES[item.origin] || item.origin} · {date(item.created_at)}{item.used_at ? ' · 已使用' : ''}</span>
            <span className="outreach-actions">
              <button type="button" className="text-button" disabled={Boolean(busy)} onClick={() => copy(item)}>复制</button>
              {!item.used_at && <button type="button" className="text-button" disabled={Boolean(busy)} title="把这条发到了招聘站后标一下，A/B 对比靠它" onClick={() => markUsed(item)}>标记已用</button>}
            </span>
          </div>
        </li>)}</ul>}
    </section>
  </div>
}
