import { useEffect } from 'react'
import { Button } from './ui'

// 统一确认框（2026-11-24）：全站确认类交互一律用本组件，禁用 window.confirm 原生弹窗。
// 原生弹窗会阻塞页面 JS 主线程（自动化验收里点到它 evaluate 永不返回），样式也不随主题。
// danger=true 用于不可逆/覆盖类操作：确认键走红色按钮，且默认聚焦「取消」，回车不会直接触发。
export function ConfirmDialog({ title, message, confirmLabel = '确认', cancelLabel = '取消', danger = false, busy = false, onConfirm, onCancel }: {
  title: string
  message: string
  confirmLabel?: string
  cancelLabel?: string
  danger?: boolean
  busy?: boolean
  onConfirm: () => void
  onCancel: () => void
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) { event.stopPropagation(); onCancel() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onCancel() }}>
    <section className="modal paper-panel confirm-modal" role="dialog" aria-modal="true" aria-labelledby="confirm-dialog-title">
      <h2 id="confirm-dialog-title">{title}</h2>
      <p className="muted body-small">{message}</p>
      <div className="modal-actions">
        <button type="button" className="button secondary" onClick={onCancel} disabled={busy} autoFocus={danger}>{cancelLabel}</button>
        <Button type="button" className={danger ? 'button danger' : 'button primary'} busy={busy} autoFocus={!danger} onClick={onConfirm}>{confirmLabel}</Button>
      </div>
    </section>
  </div>
}
