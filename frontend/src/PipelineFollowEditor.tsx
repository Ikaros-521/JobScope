import { useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { date, patchJobs } from './api'
import type { Job } from './api'
import { toast } from './toast'
import { Button } from './ui'
import { Icon } from './Icon'

// 机会管线跟进编辑（2026-11-18）：给一份管线岗位写「下一步行动 + 截止日 + 求职线」。
// 收藏夹与岗位详情共用：note/due 独立于状态保存（清空状态会连带清掉，后端语义），
// 截止日到期的条目会出现在今日概览的待办里。求职线（2026-11-22 P1）：主线/转型线
// 双轨标记——漏斗按线分母，两类岗位的转化率不搅在一起。
const TRACK_NAMES: Record<string, string> = { main: '主线（现有经验保底）', shift: '转型线（新叙事）' }

export function PipelineFollowEditor({ job, onClose, onSaved }: {
  job: Job
  onClose: () => void
  onSaved: (patch: { pipeline_note?: string; pipeline_due?: string; pipeline_track?: string }) => void
}) {
  const [note, setNote] = useState(job.pipeline_note || '')
  const [due, setDue] = useState(job.pipeline_due || '')
  const [track, setTrack] = useState(job.pipeline_track || '')
  const [busy, setBusy] = useState(false)
  useEffect(() => { setNote(job.pipeline_note || ''); setDue(job.pipeline_due || ''); setTrack(job.pipeline_track || '') }, [job])

  function submit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    // '' 是显式清空（后端按键移除），不是「没改」。
    void patchJobs([job.id], { pipeline_note: note.trim(), pipeline_due: due, pipeline_track: track as 'main' | 'shift' | '' })
      .then(() => {
        toast(note.trim() || due || track ? '跟进信息已保存' : '跟进信息已清空')
        onSaved({ pipeline_note: note.trim() || undefined, pipeline_due: due || undefined, pipeline_track: track || undefined })
        onClose()
      })
      .catch((err: Error) => toast(err.message, 'error'))
      .finally(() => setBusy(false))
  }

  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose() }}>
    <section className="modal paper-panel" role="dialog" aria-modal="true" aria-labelledby="follow-title" onKeyDown={event => { if (event.key === 'Escape' && !busy) onClose() }}>
      <div className="section-heading"><div><h2 id="follow-title">跟进安排</h2></div><button className="icon-button" aria-label="关闭弹窗" onClick={onClose} disabled={busy}><Icon name="close" /></button></div>
      <p className="muted body-small">{job.title} · {job.company}{job.pipeline_status_changed_at ? ` · 最近状态变化 ${date(job.pipeline_status_changed_at)}` : ''}</p>
      <form onSubmit={submit}>
        <label className="field-label" htmlFor="follow-note">下一步行动</label>
        <input id="follow-note" value={note} onChange={event => setNote(event.target.value)} maxLength={300} autoFocus
               placeholder="例如：周三前投递 / 等 HR 回复二面时间" />
        <label className="field-label" htmlFor="follow-due">截止日（可选，出现在「今日概览」待办）</label>
        <input id="follow-due" type="date" value={due} onChange={event => setDue(event.target.value)} />
        <label className="field-label" htmlFor="follow-track">求职线（可选，漏斗按线分开算转化率）</label>
        <select id="follow-track" value={track} onChange={event => setTrack(event.target.value)}>
          <option value="">未设线</option>
          {Object.entries(TRACK_NAMES).map(([key, label]) => <option key={key} value={key}>{label}</option>)}
        </select>
        <div className="modal-actions">
          <button type="button" className="button secondary" onClick={onClose} disabled={busy}>取消</button>
          <Button className="button primary" type="submit" busy={busy} disabled={busy}>保存<Icon name="check" size={16} /></Button>
        </div>
      </form>
    </section>
  </div>
}
