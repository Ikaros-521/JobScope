import { useState } from 'react'
import { date } from '../api'
import type { ConversationSummary } from '../api'

export function SessionList({ sessions, loading, activeId, onOpen, onRename, onDelete, collapsed }: {
  sessions: ConversationSummary[]
  loading?: boolean
  activeId: string | null
  onOpen: (id: string) => void
  onRename: (id: string, title: string) => void
  onDelete: (id: string) => void
  collapsed: boolean
}) {
  const [editingId, setEditingId] = useState<string | null>(null)
  const [draftTitle, setDraftTitle] = useState('')
  const [confirmId, setConfirmId] = useState<string | null>(null)

  function submitRename(id: string) {
    const title = draftTitle.trim()
    if (title) onRename(id, title)
    setEditingId(null)
  }

  if (collapsed) return null
  return <aside className="chat-sessions" aria-label="会话列表">
    {loading && sessions.length === 0 && <p className="chat-sessions-empty" role="status"><span className="spinner" aria-hidden="true" />正在读取会话记录…</p>}
    {!loading && sessions.length === 0 && <p className="chat-sessions-empty">还没有会话记录。发送第一条消息后会自动保存到这里。</p>}
    <ul>
      {sessions.map(item => <li key={item.id} className={item.id === activeId ? 'active' : ''}>
        {editingId === item.id ? <div className="session-rename">
          <input value={draftTitle} autoFocus onChange={event => setDraftTitle(event.target.value)}
            onKeyDown={event => { if (event.key === 'Enter' && !event.nativeEvent.isComposing) submitRename(item.id); if (event.key === 'Escape') setEditingId(null) }} maxLength={100} />
          <button type="button" onClick={() => submitRename(item.id)}>保存</button>
        </div> : <>
          <button type="button" className="session-item" onClick={() => onOpen(item.id)} title={item.preview || item.title}>
            <span className="session-title">{item.title}</span>
            <span className="session-meta">{item.message_count} 条 · {date(item.updated_at)}</span>
          </button>
          <div className="session-actions">
            <button type="button" aria-label="重命名会话" onClick={() => { setEditingId(item.id); setDraftTitle(item.title) }}>改名</button>
            {confirmId === item.id
              ? <button type="button" className="danger" onClick={() => { onDelete(item.id); setConfirmId(null) }}>确认删除</button>
              : <button type="button" aria-label="删除会话" onClick={() => { setConfirmId(item.id); window.setTimeout(() => setConfirmId(current => current === item.id ? null : current), 3000) }}>删除</button>}
          </div>
        </>}
      </li>)}
    </ul>
  </aside>
}
