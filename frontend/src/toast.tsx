import { useEffect, useState } from 'react'

type ToastKind = 'notice' | 'error'
type ToastAction = { label: string; onClick: () => void }
type ToastItem = { id: number; message: string; kind: ToastKind; sticky: boolean; action?: ToastAction }

let items: ToastItem[] = []
let nextId = 0
const listeners = new Set<() => void>()

function emit() { listeners.forEach(listener => listener()) }

/** 右下角弹窗提示。sticky 为 true 时不自动消失，返回的 id 可用 dismissToast 关闭；action 附带一个动作按钮（如「去横评」）。 */
export function toast(message: string, kind: ToastKind = 'notice', sticky = false, action?: ToastAction): number {
  const id = ++nextId
  const kept = items.filter(item => !(item.message === message && item.kind === kind))
  items = [...kept, { id, message, kind, sticky, action }].slice(-4)
  emit()
  return id
}

export function dismissToast(id: number) {
  items = items.filter(item => item.id !== id)
  emit()
}

export function ToastHost() {
  const [snapshot, setSnapshot] = useState(items)
  useEffect(() => {
    const update = () => setSnapshot(items)
    listeners.add(update)
    return () => { listeners.delete(update) }
  }, [])
  return <div className="toast-viewport" aria-live="polite">{snapshot.map(item => <Toast key={item.id} item={item} />)}</div>
}

function Toast({ item }: { item: ToastItem }) {
  useEffect(() => {
    if (item.sticky) return
    // 带动作按钮的提示给足阅读时间，避免用户还没点就消失。
    const timeout = item.kind === 'error' ? 8000 : item.action ? 8000 : 4500
    const timer = window.setTimeout(() => dismissToast(item.id), timeout)
    return () => window.clearTimeout(timer)
  }, [item])
  return <div className={`toast ${item.kind}`} role={item.kind === 'error' ? 'alert' : 'status'}>
    {item.sticky && <span className="spinner" aria-hidden="true" />}
    <span>{item.message}</span>
    {item.action ? <button type="button" className="toast-action" onClick={() => { const run = item.action?.onClick; dismissToast(item.id); run?.() }}>{item.action.label}</button> : null}
    <button type="button" aria-label={item.kind === 'error' ? '关闭错误提示' : '关闭通知'} onClick={() => dismissToast(item.id)}>×</button>
  </div>
}
