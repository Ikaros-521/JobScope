import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, TouchEvent as ReactTouchEvent, WheelEvent as ReactWheelEvent } from 'react'
import { safeUrl } from '../api'
import type { ScopeExpansion, StoredMessage } from '../api'
import { ScopeExpansionCard } from './ScopeExpansionCard'
import { Markdown } from './Markdown'
import { ToolTimeline } from './ToolTimeline'

async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true } catch { return false }
}

// 与主聊天线程同一套阈值吸底策略：距底 ≤80px 跟随，上翻解除，滚回恢复。
const REASONING_STICK_THRESHOLD = 80

export function ReasoningBlock({ text, live }: { text: string; live?: boolean }) {
  const [open, setOpen] = useState(Boolean(live))
  const [showJump, setShowJump] = useState(false)
  const wasLive = useRef(Boolean(live))
  const bodyRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)
  const touchYRef = useRef<number | null>(null)
  useEffect(() => {
    if (live && !wasLive.current) setOpen(true)
    if (!live && wasLive.current) setOpen(false)
    wasLive.current = Boolean(live)
  }, [live])

  const measureStick = () => {
    const body = bodyRef.current
    if (!body) return
    const distance = body.scrollHeight - body.scrollTop - body.clientHeight
    const stick = distance <= REASONING_STICK_THRESHOLD
    stickRef.current = stick
    // 只在流式思维链里提供回到底部；历史内容用户自由阅读。
    setShowJump(Boolean(live) && open && !stick)
  }
  const scrollToBottom = () => {
    const body = bodyRef.current
    // 即时滚动：内嵌浏览器后台面板会冻结 smooth 动画。
    if (body) body.scrollTop = body.scrollHeight
  }
  const pauseStick = () => { stickRef.current = false; setShowJump(true) }
  const measureDeferred = () => window.setTimeout(measureStick, 60)
  const onWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    if (event.deltaY < 0) pauseStick()
    else measureDeferred()
  }
  const onTouchStart = (event: ReactTouchEvent<HTMLDivElement>) => { touchYRef.current = event.touches[0]?.clientY ?? null }
  const onTouchMove = (event: ReactTouchEvent<HTMLDivElement>) => {
    const current = event.touches[0]?.clientY ?? null
    if (touchYRef.current !== null && current !== null && current > touchYRef.current) pauseStick()
    touchYRef.current = current
    measureDeferred()
  }
  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (['PageUp', 'Home', 'ArrowUp'].includes(event.key)) pauseStick()
    else if (['PageDown', 'End', 'ArrowDown'].includes(event.key)) measureDeferred()
  }

  // 流式文本增长时跟随；details 从折叠到展开后高度才真实存在，也要落一次底。
  useEffect(() => {
    if (!open || !live || !stickRef.current) return
    scrollToBottom()
    const timer = window.setTimeout(() => { if (stickRef.current && live) scrollToBottom() }, 120)
    return () => window.clearTimeout(timer)
  }, [text, open, live])

  if (!text) return null
  return <details className="reasoning-block" open={open} onToggle={event => setOpen((event.target as HTMLDetailsElement).open)}>
    <summary>{live ? '正在思考…' : '思考过程'}<span className="reasoning-meta">{text.length.toLocaleString()} 字符</span></summary>
    <div className="reasoning-body-wrap">
      <div className="reasoning-body" ref={bodyRef} tabIndex={-1}
        onScroll={measureStick} onWheel={onWheel} onTouchStart={onTouchStart} onTouchMove={onTouchMove} onKeyDown={onKeyDown}
      >{text}</div>
      {showJump && <button type="button" className="reasoning-jump" onClick={() => { stickRef.current = true; setShowJump(false); scrollToBottom() }}>↓ 回到底部</button>}
    </div>
  </details>
}

export function ChatMessageItem({ message, live, showReasoning, editing, canMutate, onStartEdit, onEditSubmit, onEditCancel, onResend, onDelete, actionsDisabled = false, onAcceptScope }: {
  message: StoredMessage
  live?: boolean
  showReasoning: boolean
  editing?: boolean
  canMutate: boolean
  onStartEdit: (message: StoredMessage) => void
  onEditSubmit: (message: StoredMessage, content: string) => void
  onEditCancel: () => void
  onResend: (message: StoredMessage) => void
  onDelete: (message: StoredMessage) => void
  actionsDisabled?: boolean
  onAcceptScope?: (action: ScopeExpansion) => Promise<void>
}) {
  const [copied, setCopied] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [draft, setDraft] = useState(message.content)
  const isUser = message.role === 'user'
  useEffect(() => { if (editing) setDraft(message.content) }, [editing, message.content])

  async function copy() {
    if (await copyText(message.content)) {
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1500)
    }
  }

  const reasoning = showReasoning ? message.reasoning || '' : ''
  const author = isUser ? '我' : message.mode === 'evidence_search' ? '资料检索' : message.mode === 'model' ? '资料问答' : '知根 Agent'

  if (message.kind === 'compaction') {
    // 一键压缩产出的摘要检查点：不按普通气泡渲染，只提供可展开的摘要卡。
    return <article className="chat-bubble compaction-card">
      <details>
        <summary>
          <span className="compaction-pill">🗜️ 历史上下文已提炼</span>
          {Boolean(message.compacted_count) && <span className="compaction-meta">已折叠 {message.compacted_count} 条较早对话；模型只看摘要与之后的内容，原文仍保留在上方</span>}
          <button type="button" className="compaction-copy" onClick={() => void copy()}>{copied ? '已复制' : '复制摘要'}</button>
        </summary>
        <div className="compaction-body"><Markdown content={message.content} /></div>
      </details>
    </article>
  }

  return <article className={`chat-bubble ${isUser ? 'user' : 'assistant'} ${message.compacted ? 'compacted-msg' : ''}`}>
    <div className="chat-bubble-head">
      <span className="chat-avatar" aria-hidden="true">{isUser ? '我' : '知'}</span>
      <span className="message-author">{author}</span>
      {message.compacted && <span className="archived-chip">已折叠 · 不进模型上下文</span>}
      {message.interrupted && <span className="interrupted-chip">已手动停止</span>}
      <div className="msg-actions">
        <button type="button" onClick={() => void copy()}>{copied ? '已复制' : '复制'}</button>
        {canMutate && isUser && <button type="button" className="msg-edit-toggle" onClick={() => onStartEdit(message)}>编辑</button>}
        {canMutate && <button type="button" onClick={() => onResend(message)}>重发</button>}
        {canMutate && (confirmDelete
          ? <button type="button" className="danger" onClick={() => onDelete(message)}>确认删除</button>
          : <button type="button" onClick={() => { setConfirmDelete(true); window.setTimeout(() => setConfirmDelete(false), 3000) }}>删除</button>)}
      </div>
    </div>

    {editing ? <div className="msg-editing">
      <textarea value={draft} onChange={event => setDraft(event.target.value)} rows={Math.min(8, Math.max(2, Math.ceil(draft.length / 40)))} autoFocus />
      <div className="msg-editing-actions">
        <button type="button" className="button secondary small" onClick={onEditCancel}>取消</button>
        <button type="button" className="button primary small" disabled={!draft.trim()} onClick={() => onEditSubmit(message, draft.trim())}>保存并重新生成</button>
      </div>
    </div> : isUser ? <div className="message-content">{message.content}</div> : <>
      {reasoning && <ReasoningBlock text={reasoning} live={live} />}
      {message.trace?.events?.length ? <ToolTimeline steps={message.trace.events} /> : null}
      <div className="message-content"><Markdown content={message.content} /></div>
      {onAcceptScope && message.actions?.map(action => <ScopeExpansionCard key={action.id} action={action} disabled={actionsDisabled} onAccept={onAcceptScope} />)}
      {message.metrics && <p className="tiny-label">薪资统计基于 {message.metrics.matched_jobs} 个符合筛选条件的岗位，招聘报价不代表实际工资。</p>}
      {message.citations?.map(citation => <details className="citation" key={citation.id}>
        <summary>{citation.company && <span className="citation-company">{citation.company} · </span>}{citation.title}</summary>
        <p>{citation.excerpt}</p>
        {citation.locators?.map((locator, index) => <p className="tiny-label" key={index}>{locator.page || '原文'} · 字符 {locator.start}–{locator.end}</p>)}
        {safeUrl(citation.url) ? <a className="external-link" href={safeUrl(citation.url)} target="_blank" rel="noreferrer">原始来源</a> : <span className="tiny-label">未提供原始链接</span>}
      </details>)}
    </>}
  </article>
}
