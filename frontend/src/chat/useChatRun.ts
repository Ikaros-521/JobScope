import { useCallback, useEffect, useRef, useState } from 'react'
import {
  approveChatRun, cancelChatRun, compressConversation, deleteConversation, deleteConversationMessage, getConversation,
  listConversations, openRunStream, patchConversation, startChatRun,
} from '../api'
import type { ApprovalRequest, ChatMode, ChatScope, ChatSegment, ConversationDetail, ConversationNavigation, ConversationSummary, CompressResult, PermissionMode, ScopeExpansion, StoredMessage, ToolStep } from '../api'

export type RunState = {
  id: string
  steps: ToolStep[]
  answer: string
  reasoning: string
  // 顺序显示模式：与 steps/answer/reasoning 同源的事件流，但保留三类内容的相对顺序。
  segments: ChatSegment[]
  approval?: ApprovalRequest
  error?: string
  notice?: string
}

// 追加一段思维链/正文：相邻同 kind 合并（同轮内的增量）；被 step 打断后自然开新段。
function appendSegment(segments: ChatSegment[], kind: 'reasoning' | 'text', piece: string): ChatSegment[] {
  if (!piece) return segments
  const last = segments[segments.length - 1]
  if (last && last.kind === kind) {
    const copy = segments.slice()
    copy[copy.length - 1] = { kind, text: last.text + piece }
    return copy
  }
  return [...segments, { kind, text: piece }]
}

export type ChatConversation = { id: string | null; title: string; mode: ChatMode; model: string; llm_channel: string; permission_mode: PermissionMode }

// 排队消息：生成中发送的待发内容，本轮完成后自动逐条发送（IDE 队列行为）。
export type QueuedMessage = { id: string; content: string }

function scopeMatches(stored: { kind: string; company?: string; job_id?: string } | undefined, scope: ChatScope) {
  if (!stored) return false
  if (scope.kind === 'global') return stored.kind === 'global'
  if (scope.kind === 'company') return stored.kind === 'company' && stored.company === scope.company
  return stored.kind === 'job' && stored.job_id === scope.jobId
}

export function useChatRun(scope: ChatScope = { kind: 'global' }, navigation?: ConversationNavigation | null, onOpened?: (requestId: string) => void) {
  const [sessions, setSessions] = useState<ConversationSummary[]>([])
  // 列表在途时给「正在读取」而不是空态：慢请求与"没有历史"必须可区分
  const [sessionsLoading, setSessionsLoading] = useState(false)
  const [conversation, setConversation] = useState<ChatConversation>({ id: null, title: '新对话', mode: 'general', model: '', llm_channel: '', permission_mode: 'autonomous' })
  const [messages, setMessages] = useState<StoredMessage[]>([])
  const [scopeActions, setScopeActions] = useState<ScopeExpansion[]>([])
  const [lineage, setLineage] = useState<ConversationDetail['lineage']>(null)
  const [run, setRun] = useState<RunState | null>(null)
  const [error, setError] = useState('')
  const [failedMessage, setFailedMessage] = useState<StoredMessage | null>(null)
  const [conditions, setConditions] = useState<ConversationDetail['conditions']>()
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
  const resumeQueueRef = useRef<() => Promise<void>>(async () => {})
  const scopeRef = useRef(scope)
  scopeRef.current = scope
  const scopeKey = scope.kind === 'global' ? 'global' : scope.kind === 'job' ? `job:${scope.jobId}` : `company:${scope.company}`
  const storageKeyRef = useRef('')
  storageKeyRef.current = `jobscope-conversation:${scopeKey}`
  const [queue, setQueue] = useState<QueuedMessage[]>([])
  const queueRef = useRef<QueuedMessage[]>([])
  const queueKeyRef = useRef('')
  queueKeyRef.current = `jobscope-chat-queue:${scopeKey}`

  conversationIdRef.current = conversation.id

  function closeStream() {
    sourceRef.current?.close()
    sourceRef.current = null
    runIdRef.current = null
  }

  // 队列与会话绑定落 sessionStorage：切视图/刷新回来只要还是同一会话就恢复排队消息，换会话即丢弃。
  function persistQueue() {
    const items = queueRef.current
    if (!items.length) { sessionStorage.removeItem(queueKeyRef.current); return }
    sessionStorage.setItem(queueKeyRef.current, JSON.stringify({ conversation: conversationIdRef.current, items }))
  }

  function applyQueue(updater: (current: QueuedMessage[]) => QueuedMessage[]) {
    queueRef.current = updater(queueRef.current)
    setQueue(queueRef.current)
    persistQueue()
  }

  function syncQueueToConversation(id: string | null) {
    let parsed: { conversation: string | null; items?: QueuedMessage[] } | null = null
    try { parsed = JSON.parse(sessionStorage.getItem(queueKeyRef.current) || 'null') } catch { parsed = null }
    const items = parsed && parsed.conversation === id && Array.isArray(parsed.items)
      ? parsed.items.filter(item => typeof item?.content === 'string' && item.content.trim())
      : []
    queueRef.current = items
    setQueue(items)
    if (!items.length) sessionStorage.removeItem(queueKeyRef.current)
  }

  const refreshSessions = useCallback(async () => {
    const current = scopeRef.current
    if (current.kind === 'company' && !current.company) { setSessions([]); return }
    setSessionsLoading(true)
    try { setSessions((await listConversations(current)).conversations) } catch { /* 会话列表失败不阻塞对话 */ }
    finally { setSessionsLoading(false) }
  }, [scopeKey])

  useEffect(() => { void refreshSessions(); return closeStream }, [refreshSessions])

  const attach = useCallback((runId: string) => {
    closeStream()
    runIdRef.current = runId
    setRun({ id: runId, steps: [], answer: '', reasoning: '', segments: [] })
    const source = openRunStream(runId, (event, data) => {
      if (conversationIdRef.current === null || runIdRef.current !== runId) return // 会话已切换，静默丢弃迟到事件
      if (event === 'start') {
        setRun(current => current && current.id === data.run_id ? { ...current, steps: [], answer: '', reasoning: '', segments: [], approval: undefined, error: undefined, notice: undefined } : current)
        return
      }
      if (event === 'resumed') {
        // 审批通过后沿原流续传：只清掉确认卡，保留已有时间线与流式内容，避免整段重放闪空。
        setRun(current => current && current.id === data.run_id ? { ...current, approval: undefined, error: undefined, notice: undefined } : current)
        return
      }
      if (event === 'step') {
        setRun(current => current ? { ...current, steps: [...current.steps, data as unknown as ToolStep], segments: [...current.segments, { kind: 'step', step: data as unknown as ToolStep }] } : current)
        return
      }
      if (event === 'delta') {
        const piece = String(data.text ?? '')
        setRun(current => current ? { ...current, answer: current.answer + piece, segments: appendSegment(current.segments, 'text', piece) } : current)
        return
      }
      if (event === 'reasoning_delta') {
        const piece = String(data.text ?? '')
        setRun(current => current ? { ...current, reasoning: current.reasoning + piece, segments: appendSegment(current.segments, 'reasoning', piece) } : current)
        return
      }
      if (event === 'approval_required') {
        setRun(current => current ? { ...current, approval: data as unknown as ApprovalRequest } : current)
        return
      }
      if (event === 'error') {
        closeStream()
        setRun(null)
        setError(String(data.message ?? '生成失败'))
        void reloadRef.current(conversationIdRef.current)
        void refreshSessions()
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
        // 本轮正常完成：稍等历史落库刷新先走一步，再自动发送队首排队消息（如有）。
        const completedSelection = selectionRef.current
        const completedConversation = conversationIdRef.current
        window.setTimeout(() => {
          if (selectionRef.current === completedSelection && conversationIdRef.current === completedConversation) void resumeQueueRef.current()
        }, 400)
        return
      }
      if (event === 'end') {
        // 流终止于 error 时（done 路径已提前 closeStream），这里补解锁发送：不清理会让会话永远锁在“生成中”。
        closeStream()
        setRun(null)
        setError('连接已结束，请重试本轮问题')
        void reloadRef.current(conversationIdRef.current)
      }
    })
    source.onerror = () => {
      if (runIdRef.current !== runId) return
      const id = conversationIdRef.current
      if (!id) return
      // 短暂网络故障交由 EventSource 自动续传；只在服务端确认已结束时解锁。
      void getConversation(id).then(detail => {
        if (runIdRef.current !== runId || conversationIdRef.current !== id) return
        if (detail.active_run && !detail.active_run.done) return
        closeStream(); setRun(null); setError('生成连接已结束，请重试本轮问题'); void reloadRef.current(id)
      }).catch(() => { /* 离线时保留自动重连，避免重复启动后台仍在运行的请求。 */ })
    }
    sourceRef.current = source
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshSessions])

  const applyDetail = useCallback((detail: ConversationDetail) => {
    conversationIdRef.current = detail.id
    setMessages(detail.messages)
    setConditions(detail.conditions)
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
    setFailedMessage(null)
    setConditions(undefined)
    setRun(null)
    setMessages([])
    setScopeActions([])
    setLineage(null)
    setConversation({ id: null, title: '新对话', mode: 'general', model: '', llm_channel: '', permission_mode: 'autonomous' })
    if (!id) { sessionStorage.removeItem(storedKey); syncQueueToConversation(null); setOpening(false); openingRef.current = false; return true }
    setOpening(true)
    openingRef.current = true
    try {
      const detail = await getConversation(id)
      if (selection !== selectionRef.current || storedKey !== storageKeyRef.current) return false
      if (!scopeMatches(detail.scope, scopeRef.current)) throw new Error('该会话属于其他对话范围，已改为新会话')
      sessionStorage.setItem(storedKey, id)
      applyDetail(detail)
      syncQueueToConversation(id)
      return true
    } catch (err) {
      if (selection !== selectionRef.current || storedKey !== storageKeyRef.current) return false
      sessionStorage.removeItem(storedKey)
      syncQueueToConversation(null)
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
    setFailedMessage(null)
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
      setFailedMessage({ id: started.user_message_id, ts: '', role: 'user', content })
      sessionStorage.setItem(storageKeyRef.current, started.conversation_id)
      // 新会话刚拿到 id：把排队消息的绑定从 null 刷成该会话，切走再回来不丢。
      persistQueue()
      setMessages(current => {
        const withoutTemp = current.filter(item => item.id !== tempId)
        const entry: StoredMessage = { id: started.user_message_id, ts: '', role: 'user', content }
        if (options?.editMessageId) {
          const index = withoutTemp.findIndex(item => item.id === options.editMessageId)
          // 后端创建历史分支；原会话完整保留，当前视图切换到分支。
          if (index >= 0) return [...withoutTemp.slice(0, index), entry]
        }
        return [...withoutTemp, entry]
      })
      attach(started.run_id)
      void reloadRef.current(started.conversation_id)
      void refreshSessions()
      return true
    } catch (err) {
      setMessages(current => current.filter(item => item.id !== tempId))
      setError((err as Error).message)
      return false
    } finally { startingRef.current = false; setStarting(false) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attach, conversation, refreshSessions])

  // 排队消息的写入/移除/清空：任何变更同步 ref、state 与 sessionStorage。
  const enqueue = useCallback((content: string) => {
    const text = content.trim()
    if (!text) return
    applyQueue(current => [...current, { id: `q-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`, content: text }])
  }, [])

  const removeQueued = useCallback((id: string) => {
    applyQueue(current => current.filter(item => item.id !== id))
  }, [])

  const clearQueue = useCallback(() => { applyQueue(() => []) }, [])

  // 发送队首排队消息（done 自动触发或空闲时手动触发）：启动失败（被瞬态锁挡下）则塞回队首，等下次触发。
  const resumeQueue = useCallback(async () => {
    const first = queueRef.current[0]
    if (!first || runIdRef.current || startingRef.current || openingRef.current) return
    applyQueue(current => current.filter(item => item.id !== first.id))
    const ok = await send(first.content)
    if (!ok) applyQueue(current => current.some(item => item.id === first.id) ? current : [first, ...current])
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [send])
  resumeQueueRef.current = resumeQueue

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

  const approve = useCallback(async (approved: boolean, grant = false) => {
    const runId = runIdRef.current
    const approval = run?.approval
    if (!runId || !approval) return
    try {
      await approveChatRun(runId, approved, approval.approval_id, grant)
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
    sessions, sessionsLoading, conversation, messages, scopeActions, lineage, conditions, failedMessage, run, error, opening, starting, queue,
    openConversation, send, stop, approve, removeMessage, renameSession, removeSession, changeMode, changeModel, changePermissionMode, compress, refreshSessions, clearError,
    enqueue, removeQueued, clearQueue, resumeQueue,
  }
}
