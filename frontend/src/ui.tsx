import type { ReactNode } from 'react'
import { safeUrl } from './api'
import { Icon } from './Icon'

// App 与各视图共享的最小组件面（2026-11-17 自 App.tsx 拆出）：纯展示原子，无状态无请求。
// 任务/来源状态的中文表随 Badge 搬来；新增视图直接从这里取，不要在 App 里再长副本。

export const statusNames: Record<string, string> = { completed: '已完成', success: '已完成', running: '采集中', pending: '等待中', cancelled: '已停止', failed: '失败', needs_login: '需要登录', needing_login: '需要登录', login_required: '需要登录', needs_verification: '需要验证', verification_required: '需要验证', requires_verification: '需要验证', unsupported: '暂不支持', available: '可用', ready: '可用', not_configured: '未配置', blocked: '访问受限', backoff: '退避等待', opt_in: '需你自行开启', skipped: '已跳过', degraded: '已降级留档' }

export function Button({ children, busy, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { busy?: boolean }) { return <button {...props} disabled={props.disabled || busy}>{busy ? <span className="spinner" /> : null}{children}</button> }
export function Empty({ title, children, icon = 'book' }: { title: string; children: ReactNode; icon?: string }) { return <div className="empty"><div className="empty-icon"><Icon name={icon} /></div><h3>{title}</h3><p>{children}</p></div> }
export function Badge({ value }: { value: string }) { return <span className={`badge ${['completed', 'success', 'available', 'ready'].includes(value) ? 'good' : ['failed', 'blocked'].includes(value) ? 'bad' : ''}`}><span />{statusNames[value] || value}</span> }
export function External({ url, children }: { url: string; children: ReactNode }) { const href = safeUrl(url); return href ? <a className="external-link" href={href} target="_blank" rel="noreferrer">{children}<Icon name="external" size={13} /></a> : <span className="muted">无来源链接</span> }
