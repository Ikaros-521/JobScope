import { useEffect, useState } from 'react'

type ToastKind = 'notice' | 'error'
type ToastItem = { id: number; message: string; kind: ToastKind; sticky: boolean }

let items: ToastItem[] = []
let nextId = 0
const listeners = new Set<() => void>()

function emit() { listeners.forEach(listener => listener()) }

/** 右下角弹窗提示。sticky 为 true 时不自动消失，返回的 id 可用 dismissToast 关闭。 */
export function toast(message: string, kind: ToastKind = 'notice', sticky = false): number {
  const id = ++nextId
  const kept = items.filter(item => !(item.message === message && item.kind === kind))
  items = [...kept, { id, message, kind, sticky }].slice(-4)
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
    const timer = window.setTimeout(() => dismissToast(item.id), item.kind === 'error' ? 8000 : 4500)
    return () => window.clearTimeout(timer)
  }, [item])
  return <div className={`toast ${item.kind}`} role={item.kind === 'error' ? 'alert' : 'status'}>
    {item.sticky && <span className="spinner" aria-hidden="true" />}
    <span>{item.message}</span>
    <button type="button" aria-label={item.kind === 'error' ? '关闭错误提示' : '关闭通知'} onClick={() => dismissToast(item.id)}>×</button>
  </div>
}
