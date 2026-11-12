import { useEffect, useState } from 'react'
import { safeUrl } from '../api'
import type { ScopeExpansion, StoredMessage } from '../api'
import { useSystemSettings } from '../systemSettingsStore'
import { ScopeExpansionCard } from './ScopeExpansionCard'
import { Markdown } from './Markdown'
import { ToolTimeline } from './ToolTimeline'
import { ChatSegments } from './ChatSegments'
import { ReasoningBlock } from './ReasoningBlock'

export { ReasoningBlock }

async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true } catch { return false }
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
  // 顺序显示模式只对带分段日志的消息生效；旧消息（含资料问答等历史模式）回落合并布局。
  const system = useSystemSettings()
  const sequential = system.chat_display_mode === 'sequential' && Boolean(message.timeline?.length)

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
    </div>

    {editing ? <div className="msg-editing">
      <textarea value={draft} onChange={event => setDraft(event.target.value)} rows={Math.min(8, Math.max(2, Math.ceil(draft.length / 40)))} autoFocus />
      <div className="msg-editing-actions">
        <button type="button" className="button secondary small" onClick={onEditCancel}>取消</button>
        <button type="button" className="button primary small" disabled={!draft.trim()} onClick={() => onEditSubmit(message, draft.trim())}>保存并重新生成</button>
      </div>
    </div> : isUser ? <div className="message-content">{message.content}</div> : <>
      {sequential ? <ChatSegments segments={message.timeline || []} showReasoning={showReasoning} /> : <>
        {reasoning && <ReasoningBlock text={reasoning} live={live} />}
        {message.trace?.events?.length ? <ToolTimeline steps={message.trace.events} /> : null}
        <div className="message-content"><Markdown content={message.content} /></div>
      </>}
      {onAcceptScope && message.actions?.map(action => <ScopeExpansionCard key={action.id} action={action} disabled={actionsDisabled} onAccept={onAcceptScope} />)}
      {message.metrics && <p className="tiny-label">薪资统计基于 {message.metrics.matched_jobs} 个符合筛选条件的岗位，招聘报价不代表实际工资。</p>}
      {message.citations?.map(citation => <details className="citation" key={citation.id}>
        <summary>{citation.company && <span className="citation-company">{citation.company} · </span>}{citation.title}</summary>
        <p>{citation.excerpt}</p>
        {citation.locators?.map((locator, index) => <p className="tiny-label" key={index}>{locator.page || '原文'} · 字符 {locator.start}–{locator.end}</p>)}
        {safeUrl(citation.url) ? <a className="external-link" href={safeUrl(citation.url)} target="_blank" rel="noreferrer">原始来源</a> : <span className="tiny-label">未提供原始链接</span>}
      </details>)}
    </>}
    <div className="msg-actions">
      <button type="button" onClick={() => void copy()}>{copied ? '已复制' : '复制'}</button>
      {canMutate && isUser && <button type="button" className="msg-edit-toggle" onClick={() => onStartEdit(message)}>编辑</button>}
      {canMutate && <button type="button" onClick={() => onResend(message)}>重发</button>}
      {canMutate && (confirmDelete
        ? <button type="button" className="danger" onClick={() => onDelete(message)}>确认删除</button>
        : <button type="button" onClick={() => { setConfirmDelete(true); window.setTimeout(() => setConfirmDelete(false), 3000) }}>删除</button>)}
    </div>
  </article>
}
