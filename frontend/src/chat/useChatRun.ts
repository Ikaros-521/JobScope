import { useCallback, useEffect, useRef, useState } from 'react'
import {
  approveChatRun, cancelChatRun, deleteConversation, deleteConversationMessage, getConversation,
  listConversations, openRunStream, patchConversation, startChatRun,
} from '../api'
import type { ApprovalRequest, ChatMode, ConversationSummary, StoredMessage, ToolStep } from '../api'

export type RunState = {
  id: string
  steps: ToolStep[]
  answer: string
  reasoning: string
  approval?: ApprovalRequest
  error?: string
  notice?: string
}

export type ChatConversation = { id: string | null; title: string; mode: ChatMode; model: string }

export function useChatRun() {
  const [sessions, setSessions] = useState<ConversationSummary[]>([])
  const [conversation, setConversation] = useState<ChatConversation>({ id: null, title: '新对话', mode: 'research', model: '' })
  const [messages, setMessages] = useState<StoredMessage[]>([])
  const [run, setRun] = useState<RunState | null>(null)
  const [error, setError] = useState('')
  const sourceRef = useRef<EventSource | null>(null)
  const runIdRef = useRef<string | null>(null)
  const conversationIdRef = useRef<string | null>(null)
  const reloadRef = useRef<(id: string | null) => Promise<void>>(async () => {})

  conversationIdRef.current = conversation.id

  function closeStream() {
    sourceRef.current?.close()
    sourceRef.current = null
    runIdRef.current = null
  }

  const refreshSessions = useCallback(async () => {
    try { setSessions((await listConversations()).conversations) } catch { /* 会话列表失败不阻塞对话 */ }
  }, [])

  useEffect(() => { void refreshSessions(); return closeStream }, [refreshSessions])

  const attach = useCallback((runId: string) => {
    closeStream()
    runIdRef.current = runId
    setRun({ id: runId, steps: [], answer: '', reasoning: '' })
    const source = openRunStream(runId, (event, data) => {
      if (conversationIdRef.current === null) return // 会话已切换，静默丢弃迟到事件
      if (event === 'start') {
        setRun(current => current && current.id === data.run_id ? { ...current, steps: [], answer: '', reasoning: '', approval: undefined, error: undefined, notice: undefined } : current)
        return
      }
      if (event === 'resumed') {
        setRun(current => current && current.id === data.run_id ? { ...current, steps: [], answer: '', reasoning: '', approval: undefined, error: undefined, notice: undefined } : current)
        return
      }
      if (event === 'step') {
        setRun(current => current ? { ...current, steps: [...current.steps, data as unknown as ToolStep] } : current)
        return
      }
      if (event === 'delta') {
        setRun(current => current ? { ...current, answer: current.answer + String(data.text ?? '') } : current)
        return
      }
      if (event === 'reasoning_delta') {
        setRun(current => current ? { ...current, reasoning: current.reasoning + String(data.text ?? '') } : current)
        return
      }
      if (event === 'approval_required') {
        setRun(current => current ? { ...current, approval: data as unknown as ApprovalRequest } : current)
        return
      }
      if (event === 'error') {
        setRun(current => current ? { ...current, error: String(data.message ?? '生成失败') } : current)
        return
      }
      if (event === 'cancelled') {
        closeStream()
        setRun(null)
        setError(String(data.message ?? '已停止本轮生成'))
        void reloadRef.current(conversationIdRef.current)
        void refreshSessions()
        return
      }
      if (event === 'done') {
        closeStream()
        setRun(null)
        void reloadRef.current(conversationIdRef.current)
        void refreshSessions()
        return
      }
      if (event === 'end') {
        source.close()
        if (sourceRef.current === source) sourceRef.current = null
      }
    })
    sourceRef.current = source
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshSessions])

  const reload = useCallback(async (conversationId: string | null) => {
    if (!conversationId) { setMessages([]); return }
    try {
      const detail = await getConversation(conversationId)
      if (conversationIdRef.current !== conversationId) return
      setMessages(detail.messages)
      setConversation(current => current.id === conversationId ? { ...current, title: detail.title, mode: detail.mode, model: detail.model } : current)
      if (detail.active_run && !detail.active_run.done && runIdRef.current !== detail.active_run.run_id) {
        attach(detail.active_run.run_id)
      }
    } catch (err) { setError((err as Error).message) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attach])

  reloadRef.current = reload

  const openConversation = useCallback(async (id: string | null) => {
    closeStream()
    setError('')
    setRun(null)
    setMessages([])
    setConversation({ id, title: '新对话', mode: 'research', model: '' })
    if (id) await reload(id)
  }, [reload])

  const send = useCallback(async (text: string, options?: { editMessageId?: string }) => {
    const content = text.trim()
    if (!content || runIdRef.current) return false
    setError('')
    const targetId = conversation.id
    const tempId = `tmp-${Date.now()}`
    setMessages(current => [...current, { id: tempId, ts: '', role: 'user', content }])
    try {
      const started = await startChatRun({
        message: content,
        conversation_id: targetId ?? undefined,
        agent_mode: conversation.mode,
        model: conversation.model || undefined,
        edit_message_id: options?.editMessageId,
      })
      setConversation(current => ({ ...current, id: started.conversation_id, title: started.title || current.title }))
      conversationIdRef.current = started.conversation_id
      setMessages(current => {
        const withoutTemp = current.filter(item => item.id !== tempId)
        const entry: StoredMessage = { id: started.user_message_id, ts: '', role: 'user', content }
        if (options?.editMessageId) {
          const index = withoutTemp.findIndex(item => item.id === options.editMessageId)
          // 服务端会把该消息之后的历史一并截断重跑，乐观更新保持同样形状。
          if (index >= 0) return [...withoutTemp.slice(0, index), entry]
        }
        return [...withoutTemp, entry]
      })
      attach(started.run_id)
      void refreshSessions()
      return true
    } catch (err) {
      setMessages(current => current.filter(item => item.id !== tempId))
      setError((err as Error).message)
      return false
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attach, conversation, refreshSessions])

  const stop = useCallback(async () => {
    const runId = runIdRef.current
    if (!runId) return
    try { await cancelChatRun(runId) } catch (err) { setError((err as Error).message) }
  }, [])

  const approve = useCallback(async (approved: boolean) => {
    const runId = runIdRef.current
    const approval = run?.approval
    if (!runId || !approval) return
    try {
      await approveChatRun(runId, approved, approval.approval_id)
      attach(runId) // 重连后事件日志全量回放，自动重建执行时间线
    } catch (err) { setError((err as Error).message) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attach, run])

  const removeMessage = useCallback(async (messageId: string) => {
    const conversationId = conversation.id
    if (!conversationId) return
    try {
      await deleteConversationMessage(conversationId, messageId)
      await reload(conversationId)
    } catch (err) { setError((err as Error).message) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation.id, reload])

  const renameSession = useCallback(async (id: string, title: string) => {
    try {
      await patchConversation(id, { title })
      setConversation(current => current.id === id ? { ...current, title } : current)
      await refreshSessions()
    } catch (err) { setError((err as Error).message) }
  }, [refreshSessions])

  const removeSession = useCallback(async (id: string) => {
    try {
      await deleteConversation(id)
      if (conversationIdRef.current === id) await openConversation(null)
      await refreshSessions()
    } catch (err) { setError((err as Error).message) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openConversation, refreshSessions])

  const changeMode = useCallback(async (mode: ChatMode) => {
    setConversation(current => ({ ...current, mode }))
    if (conversation.id) {
      try { await patchConversation(conversation.id, { mode }) } catch (err) { setError((err as Error).message) }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation.id])

  const changeModel = useCallback(async (model: string) => {
    setConversation(current => ({ ...current, model }))
    if (conversation.id) {
      try { await patchConversation(conversation.id, { model }) } catch (err) { setError((err as Error).message) }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation.id])

  const clearError = useCallback(() => setError(''), [])

  return {
    sessions, conversation, messages, run, error,
    openConversation, send, stop, approve, removeMessage, renameSession, removeSession, changeMode, changeModel, refreshSessions, clearError,
  }
}
