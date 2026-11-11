import { useRef, useState } from 'react'
import type { ScopeExpansion } from '../api'

export function ScopeExpansionCard({ action, disabled, onAccept }: {
  action: ScopeExpansion
  disabled: boolean
  onAccept: (action: ScopeExpansion) => Promise<void>
}) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const inFlight = useRef(false)
  const isCompany = action.target_scope.kind === 'company'
  const label = action.target_scope.kind === 'company' ? `本公司 · ${action.target_scope.company}` : '全项目资料'

  async function accept() {
    if (disabled || inFlight.current) return
    inFlight.current = true
    setBusy(true)
    setError('')
    try { await onAccept(action) }
    catch (err) { setError((err as Error).message || '复制失败，请重试') }
    finally { inFlight.current = false; setBusy(false) }
  }

  return <section className="scope-expansion-card" aria-label={isCompany ? '扩大到公司范围' : '扩大到全局范围'}>
    <div className="scope-expansion-heading"><span aria-hidden="true">↗</span><strong>{isCompany ? '扩大到公司范围' : '扩大到全局范围'}</strong></div>
    <span className="scope-expansion-target">{label}</span>
    <p>{action.reason || '需要更多资料来继续这个问题，可以在扩大范围后的对话中查看。'}</p>
    <button type="button" className="button secondary small" disabled={disabled || busy} onClick={() => void accept()}>
      {busy ? '正在复制…' : action.target_conversation_id ? '打开已复制对话' : isCompany ? '复制对话并进入公司对话' : '复制对话并进入全局对话'}
    </button>
    <small>原对话保留，前往后可继续提问</small>
    {error && <p className="scope-expansion-error" role="alert">{error}</p>}
  </section>
}
