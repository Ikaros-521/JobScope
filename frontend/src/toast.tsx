import { useEffect, useState } from 'react'

type ToastKind = 'notice' | 'error'
type ToastAction = { label: string; onClick: () => void }
type ToastItem = { id: number; message: string; kind: ToastKind; sticky: boolean; action?: ToastAction; durationMs?: number }

let items: ToastItem[] = []
let nextId = 0
const listeners = new Set<() => void>()

function emit() { listeners.forEach(listener => listener()) }

/** 右下角弹窗提示。sticky 为 true 时不自动消失，返回的 id 可用 dismissToast 关闭；action 附带一个动作按钮（如「去横评」）；durationMs 覆盖默认停留时长。 */
export function toast(message: string, kind: ToastKind = 'notice', sticky = false, action?: ToastAction, durationMs?: number): number {
  const id = ++nextId
  const kept = items.filter(item => !(item.message === message && item.kind === kind))
  items = [...kept, { id, message, kind, sticky, action, durationMs }].slice(-4)
  emit()
  return id
}

export function dismissToast(id: number) {
  items = items.filter(item => item.id !== id)
  emit()
}

type TaskLike = { status: string; message: string }

/** 采集/快查未完成时的统一提示。needs_login / needs_verification 要用户去浏览器动手处理，
 *  普通短提示用户注意不到，所以这类状态用红色 20 秒长提示，且文案写明状态与后端给出的处理办法。 */
export function failedTaskToast(task: TaskLike, sourceName = ''): number {
  const labels: Record<string, string> = { needs_login: '需要登录', needs_verification: '需要人工验证', backoff: '浏览器正忙', unsupported: '本次未采集', failed: '采集失败' }
  const label = labels[task.status] || '未完成'
  const head = sourceName ? `${sourceName}${label}` : label
  const body = task.message || '请按页面提示处理后重试'
  const long = task.status === 'needs_login' || task.status === 'needs_verification'
  return toast(`${head}：${body}`, 'error', false, undefined, long ? 20000 : undefined)
}

/** 任务是否已成功落库（各来源的状态别名不止一个，判断收口在这里）。 */
export function taskSucceeded(task: TaskLike): boolean {
  return task.status === 'completed' || task.status === 'success'
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
    // 带动作按钮的提示给足阅读时间，避免用户还没点就消失；错误提示停留更久，尤其需要登录/验证这类要用户动手的。
    const timeout = item.durationMs ?? (item.kind === 'error' ? 15000 : item.action ? 8000 : 4500)
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
