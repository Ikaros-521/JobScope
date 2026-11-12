import { useCallback, useEffect, useRef, useState } from 'react'
import {
  approveChatRun, cancelChatRun, compressConversation, deleteConversation, deleteConversationMessage, getConversation,
  listConversations, openRunStream, patchConversation, startChatRun,
} from '../api'
import type { ApprovalRequest, ChatMode, ChatScope, ConversationDetail, ConversationNavigation, ConversationSummary, CompressResult, PermissionMode, ScopeExpansion, StoredMessage, ToolStep } from '../api'

export type RunState = {
  id: string
  steps: ToolStep[]
  answer: string
  reasoning: string
  approval?: ApprovalRequest
  error?: string
  notice?: string
}

export type ChatConversation = { id: string | null; title: string; mode: ChatMode; model: string; llm_channel: string; permission_mode: PermissionMode }

function scopeMatches(stored: { kind: string; company?: string; job_id?: string } | undefined, scope: ChatScope) {
  if (!stored) return false
  if (scope.kind === 'global') return stored.kind === 'global'
  if (scope.kind === 'company') return stored.kind === 'company' && stored.company === scope.company
  return stored.kind === 'job' && stored.job_id === scope.jobId
}

export function useChatRun(scope: ChatScope = { kind: 'global' }, navigation?: ConversationNavigation | null, onOpened?: (requestId: string) => void) {
  const [sessions, setSessions] = useState<ConversationSummary[]>([])
  const [conversation, setConversation] = useState<ChatConversation>({ id: null, title: '新对话', mode: 'general', model: '', llm_channel: '', permission_mode: 'autonomous' })
  const [messages, setMessages] = useState<StoredMessage[]>([])
  const [scopeActions, setScopeActions] = useState<ScopeExpansion[]>([])
  const [lineage, setLineage] = useState<ConversationDetail['lineage']>(null)
  const [run, setRun] = useState<RunState | null>(null)
  const [error, setError] = useState('')
  const [opening, setOpening] = useState(false)
  const [starting, setStarting] = useState(false)
  const startingRef = useRef(false)
  const openingRef = useRef(false)
  const selectionRef = useRef(0)
  const onOpenedRef = useRef(onOpened)
  onOpenedRef.current = onOpened
  const sourceRef = useRef<EventSource | null>(null)
  const runIdRef = useRef<string | null>(null)
  const conversationIdRef = useRef<string | null>(null)
  const reloadRef = useRef<(id: string | null) => Promise<void>>(async () => {})
  const scopeRef = useRef(scope)
  scopeRef.current = scope
  const scopeKey = scope.kind === 'global' ? 'global' : scope.kind === 'job' ? `job:${scope.jobId}` : `company:${scope.company}`
  const storageKeyRef = useRef('')
  storageKeyRef.current = `jobscope-conversation:${scopeKey}`

  conversationIdRef.current = conversation.id

  function closeStream() {
    sourceRef.current?.close()
    sourceRef.current = null
    runIdRef.current = null
  }

  const refreshSessions = useCallback(async () => {
    const current = scopeRef.current
    if (current.kind === 'company' && !current.company) { setSessions([]); return }
    try { setSessions((await listConversations(current)).conversations) } catch { /* 会话列表失败不阻塞对话 */ }
  }, [scopeKey])

  useEffect(() => { void refreshSessions(); return closeStream }, [refreshSessions])

  const attach = useCallback((runId: string) => {
    closeStream()
    runIdRef.current = runId
    setRun({ id: runId, steps: [], answer: '', reasoning: '' })
    const source = openRunStream(runId, (event, data) => {
      if (conversationIdRef.current === null || runIdRef.current !== runId) return // 会话已切换，静默丢弃迟到事件
      if (event === 'start') {
        setRun(current => current && current.id === data.run_id ? { ...current, steps: [], answer: '', reasoning: '', approval: undefined, error: undefined, notice: undefined } : current)
        return
      }
      if (event === 'resumed') {
        // 审批通过后沿原流续传：只清掉确认卡，保留已有时间线与流式内容，避免整段重放闪空。
        setRun(current => current && current.id === data.run_id ? { ...current, approval: undefined, error: undefined, notice: undefined } : current)
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
        // 出错时同步撤下确认卡：失败后卡片已不可用，留着只会让人反复点到 409。
        setRun(current => current ? { ...current, error: String(data.message ?? '生成失败'), approval: undefined } : current)
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
        // 流终止于 error 时（done 路径已提前 closeStream），这里补解锁发送：不清理会让会话永远锁在“生成中”。
        source.close()
        runIdRef.current = null
        if (sourceRef.current === source) sourceRef.current = null
      }
    })
    sourceRef.current = source
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshSessions])

  const applyDetail = useCallback((detail: ConversationDetail) => {
    conversationIdRef.current = detail.id
    setMessages(detail.messages)
    setScopeActions(detail.scope_expansions || [])
    setLineage(detail.lineage || null)
    setConversation({ id: detail.id, title: detail.title, mode: 'general', model: detail.model, llm_channel: detail.llm_channel || '', permission_mode: detail.permission_mode || 'autonomous' })
    if (detail.active_run && (!detail.active_run.done || detail.active_run.awaiting_approval) && runIdRef.current !== detail.active_run.run_id) {
      attach(detail.active_run.run_id)
    }
  }, [attach])

  const reload = useCallback(async (conversationId: string | null) => {
    if (!conversationId) { setMessages([]); setScopeActions([]); return }
    const selection = selectionRef.current
    const storedKey = storageKeyRef.current
    try {
      const detail = await getConversation(conversationId)
      if (selection !== selectionRef.current || storedKey !== storageKeyRef.current || conversationIdRef.current !== conversationId) return
      if (!scopeMatches(detail.scope, scopeRef.current)) throw new Error('该会话属于其他对话范围')
      applyDetail(detail)
    } catch (err) {
      if (selection !== selectionRef.current || storedKey !== storageKeyRef.current) return
      setError((err as Error).message)
    }
  }, [applyDetail])

  reloadRef.current = reload

  const openConversation = useCallback(async (id: string | null): Promise<boolean> => {
    const selection = ++selectionRef.current
    const storedKey = storageKeyRef.current
    closeStream()
    conversationIdRef.current = null
    setError('')
    setRun(null)
    setMessages([])
    setScopeActions([])
    setLineage(null)
    setConversation({ id: null, title: '新对话', mode: 'general', model: '', llm_channel: '', permission_mode: 'autonomous' })
    if (!id) { sessionStorage.removeItem(storedKey); setOpening(false); openingRef.current = false; return true }
    setOpening(true)
    openingRef.current = true
    try {
      const detail = await getConversation(id)
      if (selection !== selectionRef.current || storedKey !== storageKeyRef.current) return false
      if (!scopeMatches(detail.scope, scopeRef.current)) throw new Error('该会话属于其他对话范围，已改为新会话')
      sessionStorage.setItem(storedKey, id)
      applyDetail(detail)
      return true
    } catch (err) {
      if (selection !== selectionRef.current || storedKey !== storageKeyRef.current) return false
      sessionStorage.removeItem(storedKey)
      setError((err as Error).message)
      return false
    } finally {
      if (selection === selectionRef.current) { setOpening(false); openingRef.current = false }
    }
  }, [applyDetail])

  // 显式跳转优先于范围内的历史恢复；消费一次后允许正常切换会话。
  const restoredScope = useRef('')
  const navigationSeen = useRef('')
  useEffect(() => {
    if (navigation && scopeMatches(navigation.scope, scopeRef.current) && navigationSeen.current !== navigation.requestId) {
      navigationSeen.current = navigation.requestId
      restoredScope.current = scopeKey
      void openConversation(navigation.conversationId).then(ok => { if (ok) onOpenedRef.current?.(navigation.requestId) })
      return
    }
    if (restoredScope.current === scopeKey) return
    restoredScope.current = scopeKey
    void openConversation(sessionStorage.getItem(storageKeyRef.current))
  }, [scopeKey, navigation, openConversation])

  useEffect(() => () => { selectionRef.current++ }, [])

  const send = useCallback(async (text: string, options?: { editMessageId?: string }) => {
    const content = text.trim()
    if (!content || runIdRef.current || openingRef.current || startingRef.current) return false
    const target = scopeRef.current
    const company = target.kind === 'company' ? target.company : ''
    const jobId = target.kind === 'job' ? target.jobId : ''
    if (target.kind === 'company' && !company) return false
    startingRef.current = true
    setStarting(true)
    setError('')
    const targetId = conversation.id
    const tempId = `tmp-${Date.now()}`
    setMessages(current => [...current, { id: tempId, ts: '', role: 'user', content }])
    try {
      const started = await startChatRun({
        message: content,
        conversation_id: targetId ?? undefined,
        agent_mode: 'general',
        // 显式传空串表示“跟随全局/渠道默认”：后端据此区分“未指定（沿用会话）”与“重置”。
        model: conversation.model,
        llm_channel: conversation.llm_channel,
        permission_mode: conversation.permission_mode,
        edit_message_id: options?.editMessageId,
        ...(company ? { company } : {}),
        ...(jobId ? { job_id: jobId } : {}),
      })
      setConversation(current => ({ ...current, id: started.conversation_id, title: started.title || current.title }))
      conversationIdRef.current = started.conversation_id
      sessionStorage.setItem(storageKeyRef.current, started.conversation_id)
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
    } finally { startingRef.current = false; setStarting(false) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attach, conversation, refreshSessions])

  const stop = useCallback(async () => {
    const runId = runIdRef.current
    if (!runId) return
    try {
      await cancelChatRun(runId)
    } catch (err) {
      setError((err as Error).message) // 取消失败：保持现状，继续等 SSE 终止事件
      return
    }
    // 服务端已确认取消：立即复位 UI，不等 cancelled 事件（僵尸线程的迟到事件会被服务端丢弃）
    closeStream()
    setRun(null)
    void reloadRef.current(conversationIdRef.current)
    void refreshSessions()
  }, [refreshSessions])

  const approve = useCallback(async (approved: boolean) => {
    const runId = runIdRef.current
    const approval = run?.approval
    if (!runId || !approval) return
    try {
      await approveChatRun(runId, approved, approval.approval_id)
      // 审批挂起时流保持打开，resumed 及后续事件沿原流续传，无需重连；
      // 只有流已断开或正在重连（如后台面板被冻结、服务重启）时才回退到重放整份日志重建状态。
      if (!sourceRef.current || sourceRef.current.readyState !== EventSource.OPEN) attach(runId)
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

  const changeModel = useCallback(async (model: string, channelId: string) => {
    setConversation(current => ({ ...current, model, llm_channel: channelId }))
    if (conversation.id) {
      try { await patchConversation(conversation.id, { model, llm_channel: channelId }) } catch (err) { setError((err as Error).message) }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation.id])

  const changePermissionMode = useCallback(async (permissionMode: PermissionMode) => {
    setConversation(current => ({ ...current, permission_mode: permissionMode }))
    if (conversation.id) {
      try { await patchConversation(conversation.id, { permission_mode: permissionMode }) } catch (err) { setError((err as Error).message) }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation.id])

  const compress = useCallback(async (): Promise<CompressResult & { ok: boolean; message?: string }> => {
    const id = conversation.id
    if (!id) return { ok: false, compacted_count: 0, summary_message_id: '', message_count: 0, message: '新对话还没有可压缩的历史' }
    try {
      const result = await compressConversation(id)
      await reloadRef.current(id)
      return { ...result, ok: true }
    } catch (err) {
      return { ok: false, compacted_count: 0, summary_message_id: '', message_count: 0, message: (err as Error).message }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [conversation.id])

  const clearError = useCallback(() => setError(''), [])

  return {
    sessions, conversation, messages, scopeActions, lineage, run, error, opening, starting,
    openConversation, send, stop, approve, removeMessage, renameSession, removeSession, changeMode, changeModel, changePermissionMode, compress, refreshSessions, clearError,
  }
}
