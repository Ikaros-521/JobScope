import { useEffect, useState } from 'react'
import { api, post } from './api'
import type { Task } from './api'

type Status = { total: number; complete: number; pending: number; task: Task | null }

export function DetailBatch({ onUpdate }: { onUpdate: () => Promise<unknown> }) {
  const [status, setStatus] = useState<Status | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const running = status?.task?.status === 'running'
  useEffect(() => {
    let active = true
    const load = () => api<Status>('/browser/details/status').then(data => { if (active) { setStatus(data); setError('') } }).catch(err => { if (active) setError((err as Error).message) })
    void load()
    const timer = window.setInterval(() => void load(), 3000)
    return () => { active = false; window.clearInterval(timer) }
  }, [])
  useEffect(() => { if (status?.task && !running) void onUpdate() }, [running, status?.task?.id])
  async function start() {
    setBusy(true); setError('')
    try {
      await post('/browser/details', { limit: 500 })
      setStatus(await api<Status>('/browser/details/status'))
    } catch (err) { setError((err as Error).message) } finally { setBusy(false) }
  }
  return <section className="paper-panel detail-batch"><div className="section-heading"><div><h2>完整岗位职责</h2><p className="body-small muted">补全职责和任职要求后，再与简历评估适配</p></div><button className="button primary" onClick={() => void start()} disabled={busy || running || !status?.pending}>{busy || running ? <span className="spinner" /> : null}{running ? '正在补全' : '补全未完成岗位'}</button></div>
    <div className="batch-stats"><strong>{status?.complete ?? '—'} <span>/ {status?.total ?? '—'} 份完整JD</span></strong><span>{status?.pending ?? '—'} 份待补全</span></div>
    <progress className="coverage-progress" max={status?.total || 1} value={status?.complete || 0} aria-label="完整JD补全进度" />
    {status?.task && <p className="batch-message" role="status">{status.task.message}{running && ` · 已处理 ${status.task.processed || 0}/${status.task.total || 0}`}</p>}
    {error && <p className="alert error" role="alert">{error}</p>}
    <p className="tiny-label">后台逐条保存，已完成的自动跳过；遇登录或人工验证时暂停，完成验证后可继续</p>
  </section>
}
