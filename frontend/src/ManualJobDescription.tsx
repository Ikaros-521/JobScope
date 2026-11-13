import { useState } from 'react'
import { post } from './api'
import type { Job } from './api'
import { toast } from './toast'

export function ManualJobDescription({ job, onSaved }: { job: Job; onSaved: (job: Job) => Promise<void> }) {
  const [open, setOpen] = useState(false)
  const [description, setDescription] = useState('')
  const [source, setSource] = useState('')
  const [saving, setSaving] = useState(false)
  return <div className="manual-jd">
    <button className="text-button" onClick={() => setOpen(!open)} aria-expanded={open}>{open ? '收起补录' : '手动补录完整职责'}</button>
    {job.description_source === 'user' && <p className="tiny-label">用户补录 · {job.description_source_note}（原平台证据仍保留）</p>}
    {open && <form onSubmit={event => {
      event.preventDefault(); setSaving(true)
      void post<Job>(`/jobs/${encodeURIComponent(job.id)}/manual-description`, { description, source_note: source })
        .then(onSaved).then(() => { setOpen(false); toast('完整职责已补入原档案，来源标记为用户补录') })
        .catch(error => toast((error as Error).message, 'error')).finally(() => setSaving(false))
    }}>
      <label className="field-label" htmlFor={`manual-jd-${job.id}`}>完整职责与任职要求</label>
      <textarea id={`manual-jd-${job.id}`} value={description} onChange={event => setDescription(event.target.value)} required maxLength={100000} rows={8} />
      <label className="field-label" htmlFor={`manual-source-${job.id}`}>出处说明</label>
      <input id={`manual-source-${job.id}`} value={source} onChange={event => setSource(event.target.value)} required maxLength={1000} placeholder="例如：招聘邮件、与招聘人员的沟通日期" />
      <p className="tiny-label">补录作为用户材料保存，不替换原平台证据。</p>
      <button type="submit" className="button secondary small" disabled={saving || !description.trim() || !source.trim()}>{saving ? '保存中…' : '保存完整职责'}</button>
    </form>}
  </div>
}
