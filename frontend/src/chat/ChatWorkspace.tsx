import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, TouchEvent as ReactTouchEvent, WheelEvent as ReactWheelEvent } from 'react'
import { acceptScopeExpansion, estimateTokens, getChannels, getModelSettings, listLlmChannels, proposeScopeExpansion } from '../api'
import type { ChatScope, ConversationNavigation, LlmChannel, PermissionMode, ScopeExpansion, StoredMessage } from '../api'
import { toast } from '../toast'
import { updateSystemSettings, useSystemSettings } from '../systemSettingsStore'
import { ChatComposer } from './ChatComposer'
import type { ComposerChannel, EditingTarget } from './ChatComposer'
import { ChatMessageItem, ReasoningBlock } from './ChatMessageItem'
import { ChatSegments } from './ChatSegments'
import { SessionList } from './SessionList'
import { ToolTimeline } from './ToolTimeline'
import { Markdown } from './Markdown'
import { useChatRun } from './useChatRun'
import { ScopeExpansionCard } from './ScopeExpansionCard'
import './chatRecovery.css'

const SUGGESTIONS = ['结合我的简历，制定 Agent 岗位面试计划', '检索 Python 岗位并比较薪资', '哪些公司仍缺少独立资料？']

const COMPANY_SUGGESTIONS = ['这家公司有哪些岗位要求？', '公司经营情况有哪些证据？', '薪资还有哪些口径需要核实？']
const JOB_SUGGESTIONS = ['这份岗位有哪些要求？', '公司有哪些可核实的信息？', '面试时还应该问清什么？']

// 距底部超过该阈值视为用户正在回看，暂停自动吸底；翻回阈值内自动恢复。
const STICK_THRESHOLD = 80
// 上下文估算基数：system 提示 + 工具声明，不随会话变化。
const CONTEXT_BASE_TOKENS = 600

export function ChatWorkspace({ scope, modelConfigured, onManageExtensions, onConfigureModel, navigation, onNavigateConversation, onOpened }: {
  scope: ChatScope; modelConfigured: boolean; onManageExtensions?: () => void
  onConfigureModel?: () => void
  navigation?: ConversationNavigation | null
  onNavigateConversation?: (navigation: ConversationNavigation) => void
  onOpened?: (requestId: string) => void
}) {
  const isGlobal = scope.kind === 'global'
  const isJob = scope.kind === 'job'
  const company = scope.kind === 'company' ? scope.company.trim() : ''
  const jobCompany = scope.kind === 'job' ? scope.company : ''
  const {
    sessions, sessionsLoading, conversation, messages, scopeActions, lineage, conditions, failedMessage, run, error, opening, starting, queue,
    openConversation, send, stop, approve, removeMessage, renameSession, removeSession, changeModel, changePermissionMode, compress, clearError,
    enqueue, removeQueued, clearQueue, resumeQueue,
  } = useChatRun(scope, navigation, onOpened)
  const [input, setInputState] = useState('')
  const draftScope = scope.kind === 'job' ? `job:${scope.jobId}` : scope.kind === 'company' ? `company:${scope.company}` : 'global'
  const draftKey = `jobscope-draft:${draftScope}:${conversation.id || 'new'}`
  const draftKeyRef = useRef(draftKey)
  draftKeyRef.current = draftKey
  function setInput(value: string) {
    setInputState(value)
    if (value) sessionStorage.setItem(draftKeyRef.current, value)
    else sessionStorage.removeItem(draftKeyRef.current)
  }
  useEffect(() => {
    if (!opening) { setInputState(sessionStorage.getItem(draftKey) || ''); setEditing(null) }
  }, [draftKey, opening])
  const [editing, setEditing] = useState<EditingTarget>(null)
  const [sessionsCollapsed, setSessionsCollapsed] = useState(isJob)
  const [globalModels, setGlobalModels] = useState<string[]>([])
  const [modelChannels, setModelChannels] = useState<ComposerChannel[]>([])
  const [contextWindow, setContextWindow] = useState(128000)
  // 思维链显隐与显示模式来自系统设置（服务端持久化），输入框开关只是快捷入口。
  const system = useSystemSettings()
  const showReasoning = system.show_reasoning
  const sequential = system.chat_display_mode === 'sequential'
  const [showJump, setShowJump] = useState(false)
  const [compressing, setCompressing] = useState(false)
  const [showScopeOptions, setShowScopeOptions] = useState(false)
  const [proposing, setProposing] = useState(false)
  const [cloning, setCloning] = useState(false)
  // 升档到「完全访问」前先弹确认：降档与自主档间切换不需要确认。
  const [modeDialog, setModeDialog] = useState(false)
  const cloningRef = useRef(false)
  const proposingRef = useRef(false)
  const conversationIdRef = useRef(conversation.id)
  conversationIdRef.current = conversation.id
  const [localActions, setLocalActions] = useState<ScopeExpansion[]>([])
  const [actionOverrides, setActionOverrides] = useState<Record<string, ScopeExpansion>>({})
  const threadRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)

  useEffect(() => {
    Promise.all([getChannels(), getModelSettings(), listLlmChannels()])
      .then(([channels, settings, saved]) => {
        const channel = channels.channels.find(item => item.id === settings.llm.channel)
        setGlobalModels(channel?.models ?? [])
        setContextWindow(settings.llm.context_tokens ?? 128000)
        setModelChannels(saved.channels.map(item => toComposerChannel(item)))
      })
      .catch(() => { /* 模型选项获取失败时保持“跟随全局设置” */ })
  }, [])

  function scrollToBottom() {
    const thread = threadRef.current
    // 只滚线程自己的容器：scrollIntoView 会连带滚动详情面板等祖先，选中岗位时会把面板滚过标题区。
    // 用即时滚动而非 smooth：内嵌浏览器后台面板会冻结动画，平滑滚动可能完全不动。
    if (thread) thread.scrollTop = thread.scrollHeight
  }

  // 滚轮/触控/键盘也补算一次：部分内嵌浏览器只更新 scrollTop 而不派发 scroll 事件。
  const measureStick = () => {
    const thread = threadRef.current
    if (!thread) return
    const distance = thread.scrollHeight - thread.scrollTop - thread.clientHeight
    const stick = distance <= STICK_THRESHOLD
    stickRef.current = stick
    setShowJump(!stick)
  }
  const measureStickDeferred = () => { window.setTimeout(measureStick, 60) }
  // 向上滚立即解除吸底：不依赖 scroll 事件与定时器（后台面板会节流），保证流式输出时不会被拽回底部。
  const pauseStick = () => { stickRef.current = false; setShowJump(true) }
  const onThreadWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    if (event.deltaY < 0) pauseStick()
    else measureStickDeferred()
  }
  const touchYRef = useRef<number | null>(null)
  const onThreadTouchStart = (event: ReactTouchEvent<HTMLDivElement>) => {
    touchYRef.current = event.touches[0]?.clientY ?? null
  }
  const onThreadTouchMove = (event: ReactTouchEvent<HTMLDivElement>) => {
    const current = event.touches[0]?.clientY ?? null
    if (touchYRef.current !== null && current !== null && current > touchYRef.current) pauseStick()
    touchYRef.current = current
    measureStickDeferred()
  }
  const onThreadKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (['PageUp', 'Home', 'ArrowUp'].includes(event.key)) pauseStick()
    else if (['PageDown', 'End', 'ArrowDown', ' '].includes(event.key)) measureStickDeferred()
  }

  useEffect(() => {
    if (!stickRef.current) return
    scrollToBottom()
    // 会话恢复时消息与布局可能分两拍到位：补一次延迟滚动，避免停在顶部。
    const timer = window.setTimeout(() => { if (stickRef.current) scrollToBottom() }, 150)
    return () => window.clearTimeout(timer)
  }, [messages.length, localActions.length, scopeActions.length, queue.length, run?.answer, run?.reasoning, run?.steps.length, run?.approval])

  // 切换会话时恢复吸底并直接落到最新消息。
  useEffect(() => { stickRef.current = true; setShowJump(false); scrollToBottom() }, [conversation.id])

  function jumpToBottom() {
    stickRef.current = true
    setShowJump(false)
    scrollToBottom()
  }

  function toggleReasoning(value: boolean) {
    void updateSystemSettings({ show_reasoning: value }).catch(err => toast((err as Error).message || '设置保存失败，请稍后重试', 'error'))
  }

  const running = Boolean(run)
  const busy = running || opening || starting || compressing || cloning || proposing
  // 生成中的 run 在服务端继续执行，切走只是断开 SSE、切回经 active_run 重挂；
  // 新建/切换会话只被在途的瞬态请求锁住（发送在途切走会把新 run 的流挂错会话）。
  const switching = opening || starting || compressing || cloning || proposing
  const canMutate = conversation.id !== null && !busy
  const selectedChannel = modelChannels.find(item => item.id === conversation.llm_channel)
  const effectiveConfigured = conversation.llm_channel ? Boolean(selectedChannel && (conversation.model || selectedChannel.model)) : modelConfigured
  const scopeLabel = isGlobal ? '全项目' : isJob ? `本岗位 · ${scope.title || jobCompany}` : `本公司 · ${company || '未选定'}`

  useEffect(() => { setLocalActions([]); setActionOverrides({}); setShowScopeOptions(false) }, [conversation.id])

  function resolveAction(action: ScopeExpansion) {
    return actionOverrides[action.id] || scopeActions.find(item => item.id === action.id) || action
  }

  async function requestExpansion(target: 'company' | 'global') {
    if (!conversation.id || busy || proposingRef.current) return
    const sourceId = conversation.id
    proposingRef.current = true
    setProposing(true)
    try {
      const action = await proposeScopeExpansion(sourceId, target)
      if (conversationIdRef.current !== sourceId) return
      setLocalActions(current => [...current.filter(item => item.id !== action.id), action])
      setShowScopeOptions(false)
      stickRef.current = true
    } catch (err) { toast((err as Error).message || '暂时无法扩大范围，请重试', 'error') }
    finally { proposingRef.current = false; setProposing(false) }
  }

  async function acceptExpansion(action: ScopeExpansion) {
    if (!conversation.id || busy) throw new Error('请等待当前会话完成后重试')
    if (!onNavigateConversation) throw new Error('此入口暂不支持跳转，请重新打开工作区')
    if (cloningRef.current) return
    cloningRef.current = true
    setCloning(true)
    try {
      const result = await acceptScopeExpansion(conversation.id, action.id)
      setActionOverrides(current => ({ ...current, [action.id]: { ...action, target_conversation_id: result.conversation_id } }))
      onNavigateConversation({ requestId: `${action.id}:${Date.now()}`, conversationId: result.conversation_id, scope: result.scope })
    } finally { cloningRef.current = false; setCloning(false) }
  }

  function actionInScope(action: ScopeExpansion) {
    const source = action.source_scope
    if (source.kind !== scope.kind) return false
    if (source.kind === 'job' && scope.kind === 'job') return source.job_id === scope.jobId
    if (source.kind === 'company' && scope.kind === 'company') return source.company === scope.company
    return source.kind === 'global'
  }

  const visibleMessageActions = new Set(messages.flatMap(message => (message.actions || []).filter(actionInScope).map(action => action.id)))
  const liveActions = (run?.steps || []).flatMap(step => step.action ? [step.action] : [])
  const floatingActions = Array.from(new Map([...scopeActions, ...localActions].filter(action => !visibleMessageActions.has(action.id)).map(action => [action.id, action])).values())

  // 对齐服务端初始输入窗口：最新摘要 + 最近六条未折叠普通消息。
  // 工具定义/动态工具结果无法在浏览器精确计量，因此持续标注为估算。
  const contextTokens = useMemo(() => {
    const checkpoints = messages.filter(item => item.kind === 'compaction')
    let history = messages.filter(item => item.kind !== 'compaction' && !item.compacted)
    let question = input
    if (run || starting) {
      let latestUser = history.length - 1
      while (latestUser >= 0 && history[latestUser].role !== 'user') latestUser--
      if (latestUser >= 0) { question = history[latestUser].content; history = history.slice(0, latestUser) }
    }
    const checkpoint = checkpoints.at(-1)?.content.slice(0, 8000) || ''
    return CONTEXT_BASE_TOKENS + estimateTokens(checkpoint)
      + history.slice(-6).reduce((sum, item) => sum + estimateTokens(item.content.slice(0, 3000)), 0)
      + estimateTokens(question) + estimateTokens(JSON.stringify(conditions || {}))
      + estimateTokens(run?.answer || '')
  }, [messages, conditions, input, run, starting])

  // 会话选了已存渠道且该渠道登记了窗口大小时，按渠道窗口估算，否则用全局设置。
  const activeWindow = useMemo(() => {
    const channel = modelChannels.find(item => item.id === conversation.llm_channel)
    return channel?.context_tokens || contextWindow
  }, [modelChannels, conversation.llm_channel, contextWindow])

  // 与后端一致的压缩门槛：保留最近 4 条，至少还要有 2 条可折叠。
  const canCompress = messages.filter(item => !item.compacted).length - 4 >= 2

  async function compressContext() {
    if (compressing || running) return
    setCompressing(true)
    const result = await compress()
    setCompressing(false)
    if (result.ok) toast(`已压缩 ${result.compacted_count} 条历史消息，上下文已精简`)
    else toast(result.message || '压缩失败，请稍后重试', 'error')
  }

  function startEdit(message: StoredMessage) {
    setEditing({ id: message.id, original: message.content })
  }

  function cancelEdit() {
    setEditing(null)
  }

  async function submit() {
    const text = input.trim()
    if (!text || !effectiveConfigured) return
    // 生成中（或本轮正在启动）发送 = 排队：不打断当前生成，本轮完成后自动发送。
    if (running || starting) {
      enqueue(text)
      setInput('')
      stickRef.current = true
      setShowJump(false)
      toast('已加入队列，本轮完成后自动发送')
      return
    }
    stickRef.current = true
    setShowJump(false)
    const sendingDraftKey = draftKeyRef.current
    const ok = await send(text)
    if (ok) {
      sessionStorage.removeItem(sendingDraftKey)
      setInput('')
      setEditing(null)
    }
  }

  function resend(message: StoredMessage) {
    if (running) return
    if (message.role === 'user') {
      setEditing({ id: message.id, original: message.content })
      return
    }
    const index = messages.findIndex(item => item.id === message.id)
    const previous = [...messages.slice(0, index)].reverse().find(item => item.role === 'user')
    if (previous) {
      stickRef.current = true
      setShowJump(false)
      void send(previous.content, { editMessageId: previous.id })
    }
  }

  function toggleSessions() { setSessionsCollapsed(value => !value) }

  const suggestions = isGlobal ? SUGGESTIONS : isJob ? JOB_SUGGESTIONS : COMPANY_SUGGESTIONS
  const welcome = !isGlobal && !isJob && !company
    ? { title: '先确认公司主体，再开始对话', text: '在上方输入资料库中的精确公司名称，Agent 将围绕这一主体检索。' }
    : isGlobal
      ? { title: '让 Agent 帮你查资料、做比较、准备面试', text: '可检索本项目全部岗位与公司资料，按需读取简历、Skills 或调用 MCP；回答保留来源。' }
      : isJob
        ? { title: '围绕这份岗位展开', text: 'Agent 可检索本岗位与同公司的公共资料。需要比较其他岗位或公司时，可复制对话并扩大范围。' }
        : { title: `围绕 ${company} 展开`, text: 'Agent 可检索该公司的岗位与资料。需要跨公司比较时，可复制对话进入全局范围。' }

  const channelName = modelChannels.find(item => item.id === conversation.llm_channel)?.name
  const modelSuffix = conversation.llm_channel
    ? ` · ${channelName || '渠道已删除'} · ${conversation.model || '默认模型'}`
    : conversation.model ? ` · ${conversation.model}` : ''

  function pickPermissionMode(mode: PermissionMode) {
    if (mode === conversation.permission_mode) return
    if (mode === 'full') { setModeDialog(true); return }
    void changePermissionMode(mode)
  }

  return <div className={`chat-page chat-${scope.kind} ${sessionsCollapsed ? 'sessions-collapsed' : ''}`}>
    <aside className="chat-sidebar">
      <div className="chat-sidebar-top">
        <button type="button" className="button primary small chat-new-button" onClick={() => { void openConversation(null); cancelEdit() }} disabled={switching || (scope.kind === 'company' && !company)}>
          {sessionsCollapsed ? '＋' : '＋ 新对话'}
        </button>
        <button type="button" className="icon-button" aria-label={sessionsCollapsed ? '展开会话列表' : '收起会话列表'} onClick={toggleSessions}>
          {sessionsCollapsed ? '»' : '«'}
        </button>
      </div>
      <SessionList
        sessions={sessions}
        loading={sessionsLoading}
        activeId={conversation.id}
        collapsed={sessionsCollapsed}
        onOpen={id => { if (!switching) { cancelEdit(); void openConversation(id) } }}
        onRename={(id, title) => void renameSession(id, title)}
        onDelete={id => void removeSession(id)}
      />
    </aside>

    <section className="chat-main">
      <header className="chat-main-head">
        <div className="chat-main-title">
          <h2>{isGlobal ? conversation.title : scope.kind === 'job' ? (scope.title || scope.company) : `${company || '未选定公司'} · 对话`}</h2>
          <span className="chat-main-sub">
            Agent · {isGlobal ? '全部岗位与公司资料' : isJob ? '本岗位与同公司公共资料' : '本公司岗位与资料'}
            {modelSuffix}
          </span>
        </div>
        <span className={`chat-state ${run ? 'busy' : ''}`}>{opening ? '正在打开会话…' : run ? (run.approval ? '等待工具确认' : '生成中…') : '空闲'}</span>
      </header>

      {lineage && <div className="chat-clone-notice" role="status">范围已扩大至{scopeLabel}；已复制的历史回答仍按原范围生成，可继续提问。</div>}

      {conditions && (conditions.location || conditions.threshold !== null || conditions.exclude_outsourcing || conditions.clarification) && <div className="chat-conditions" aria-label="当前会话条件">
        <strong>本会话条件</strong>
        {conditions.location && <span>{conditions.location}<button disabled={busy} onClick={() => setInput('不限地点')}>取消地点</button></span>}
        {conditions.threshold !== null && <span>薪资 {conditions.operator === 'ge' ? '至少' : conditions.operator} {conditions.threshold / 1000}K（{conditions.threshold_basis === 'monthly_min' ? '月薪下限' : conditions.threshold_basis}）<button disabled={busy} onClick={() => setInput('取消薪资限制')}>取消薪资</button></span>}
        {conditions.exclude_outsourcing && <span>不考虑外包<button disabled={busy} onClick={() => setInput('取消外包限制')}>取消</button></span>}
        {conditions.clarification && <span>{conditions.clarification}</span>}
        <button disabled={busy} onClick={() => setInput('修改条件：')}>修改条件</button><small>在输入框确认并发送；也可直接用自然语言更改。</small>
      </div>}
      <div className="chat-thread-wrap">
        <div className="chat-thread" ref={threadRef} aria-live="polite"
          onScroll={measureStick} onWheel={onThreadWheel} onTouchStart={onThreadTouchStart} onTouchMove={onThreadTouchMove} onKeyDown={onThreadKeyDown} tabIndex={-1}>
          {messages.length === 0 && !run && <div className="chat-welcome">
            <h3>{welcome.title}</h3>
            <p>{welcome.text}</p>
            {(isGlobal || isJob || company) && <div className="chat-suggestions">
              {suggestions.map(text => <button key={text} type="button" onClick={() => { setInput(text); }}>{text}</button>)}
            </div>}
          </div>}

          {messages.map(message => <ChatMessageItem
            key={message.id}
            message={{ ...message, actions: message.actions?.filter(actionInScope).map(resolveAction) }}
            actionsDisabled={busy}
            onAcceptScope={acceptExpansion}
            showReasoning={showReasoning}
            editing={editing?.id === message.id}
            canMutate={canMutate}
            onStartEdit={startEdit}
            onEditSubmit={(target, content) => { if (!effectiveConfigured) { toast('请先配置模型；编辑内容仍在当前消息中', 'error'); return }; stickRef.current = true; setShowJump(false); void send(content, { editMessageId: target.id }).then(ok => { if (ok) setEditing(null) }) }}
            onEditCancel={cancelEdit}
            onResend={resend}
            onDelete={target => void removeMessage(target.id)}
          />)}

          {run && <article className="chat-bubble assistant live-bubble">
            <div className="chat-bubble-head">
              <span className="chat-avatar" aria-hidden="true">知</span>
              <span className="message-author">{run.approval ? '知根 Agent · 等待确认' : '知根 Agent · 正在工作'}</span>
            </div>
            {sequential && run.segments.length ? <ChatSegments segments={run.segments} live={!run.approval} showReasoning={showReasoning} /> : <>
              {showReasoning && run.reasoning && <ReasoningBlock text={run.reasoning} live={!run.approval} />}
              <ToolTimeline steps={run.steps} running />
              {run.answer && <div className="message-content"><Markdown content={run.answer} /></div>}
            </>}
            {run.error && <div className="run-error">{run.error}</div>}
            {Array.from(new Map(liveActions.map(action => [action.id, action])).values()).map(action => <ScopeExpansionCard key={action.id} action={resolveAction(action)} disabled onAccept={acceptExpansion} />)}
          </article>}
          {floatingActions.map(action => <ScopeExpansionCard key={action.id} action={resolveAction(action)} disabled={busy} onAccept={acceptExpansion} />)}

          {queue.map((item, index) => <div className="queued-message" key={item.id} title={item.content}>
            <span className="queued-index">{running ? `第 ${index + 1} 位待发送` : '待发送'}</span>
            <span className="queued-text">{item.content}</span>
            <button type="button" className="queued-remove" aria-label="移除这条排队消息" onClick={() => removeQueued(item.id)}>×</button>
          </div>)}
        </div>
        {showJump && <button type="button" className="jump-bottom" onClick={jumpToBottom}>↓ 回到底部</button>}
      </div>

      {run?.approval && <section className="agent-approval" aria-label="待确认工具调用">
        <div className="agent-approval-head">
          <h3>确认工具调用</h3>
          <span className="agent-approval-tool">{run.approval.tool}</span>
        </div>
        {/* 单行紧凑 JSON：完整保留待确认内容（可滚动查看），但不逐字段换行撑高卡片。 */}
        <pre>{JSON.stringify(run.approval.arguments ?? {})}</pre>
        <p>{run.approval.reason}。确认后仅执行这一次调用；选择「本会话始终允许」后，本会话内该工具的后续调用不再确认。</p>
        <div>
          <button type="button" className="button secondary" onClick={() => void approve(false)}>拒绝并继续</button>
          <button type="button" className="button secondary" onClick={() => void approve(true, true)}>本会话始终允许</button>
          <button type="button" className="button primary" onClick={() => void approve(true)}>确认执行</button>
        </div>
      </section>}

      {error && <div className="alert error chat-alert" role="alert"><span>{error}</span>
        {failedMessage && <button type="button" disabled={busy || !effectiveConfigured} onClick={() => void send(failedMessage.content, { editMessageId: failedMessage.id })}>重试本轮问题</button>}
        {navigation && <button type="button" onClick={() => { void openConversation(navigation.conversationId).then(ok => { if (ok) onOpened?.(navigation.requestId) }) }}>重新打开目标对话</button>}
        <button aria-label="关闭错误提示" onClick={clearError}>×</button></div>}

      {showScopeOptions && !isGlobal && <div className="scope-expansion-options" aria-label="选择扩大后的范围">
        <span>将当前对话复制到：</span>
        {isJob && <button type="button" className="button secondary small" disabled={busy || proposing} onClick={() => void requestExpansion('company')}>所属公司</button>}
        <button type="button" className="button secondary small" disabled={busy || proposing} onClick={() => void requestExpansion('global')}>全项目</button>
        <button type="button" className="text-button" onClick={() => setShowScopeOptions(false)}>取消</button>
      </div>}

      {/* 空闲但队列还有剩（停止/出错/刷新恢复后）：手动续发或清空，不自动发。 */}
      {queue.length > 0 && !busy && <div className="queue-actions" role="status">
        <span>队列中还有 {queue.length} 条消息</span>
        <button type="button" className="button secondary small" onClick={() => void resumeQueue()}>发送队首消息</button>
        <button type="button" className="text-button" onClick={clearQueue}>清空队列</button>
      </div>}

      <ChatComposer
        input={input}
        onInputChange={setInput}
        onQueue={() => void submit()}
        queueCount={queue.length}
        model={conversation.model}
        llmChannel={conversation.llm_channel}
        modelChannels={modelChannels}
        globalModels={globalModels}
        onModelChange={(model, channelId) => void changeModel(model, channelId)}
        permissionMode={conversation.permission_mode}
        onPermissionModeChange={pickPermissionMode}
        running={running}
        awaiting={Boolean(run?.approval)}
        onStop={() => void stop()}
        onSend={() => void submit()}
        modelConfigured={effectiveConfigured}
        showReasoning={showReasoning}
        onToggleReasoning={toggleReasoning}
        editing={editing}
        onCancelEdit={cancelEdit}
        onManageExtensions={onManageExtensions}
        onConfigureModel={onConfigureModel}
        scopeLabel={scopeLabel}
        onExpandScope={!isGlobal && onNavigateConversation ? () => setShowScopeOptions(value => !value) : undefined}
        locked={opening ? '正在打开会话…' : starting ? '正在开始本轮对话…' : cloning ? '正在复制并打开对话…' : proposing ? '正在生成范围扩展卡片…' : isGlobal || isJob || company ? '' : '请先在上方确认公司主体'}
        scopeName={isGlobal ? '全项目资料' : isJob ? `${jobCompany}的岗位资料` : company || '未选定公司'}
        contextTokens={contextTokens}
        contextWindow={activeWindow}
        compressing={compressing}
        hasConversation={conversation.id !== null}
        canCompress={canCompress}
        onCompress={() => void compressContext()}
      />
    </section>

    {modeDialog && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget) setModeDialog(false) }}>
      <section className="modal paper-panel mode-modal" role="dialog" aria-modal="true" aria-labelledby="mode-dialog-title"
        onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); setModeDialog(false) } }}>
        <div className="section-heading"><div><h2 id="mode-dialog-title">切换到「完全访问」？</h2></div></div>
        <p className="muted body-small">此后本会话的外部工具（MCP）调用将不再逐次确认，直接自动执行；调用参数仍会发送到已启用的外部服务。读取简历后的外发调用仍会要求确认。</p>
        <div className="modal-actions">
          <button type="button" className="button secondary" onClick={() => setModeDialog(false)}>取消</button>
          <button type="button" className="button primary" autoFocus onClick={() => { setModeDialog(false); void changePermissionMode('full') }}>确认切换</button>
        </div>
      </section>
    </div>}
  </div>
}

function toComposerChannel(item: LlmChannel): ComposerChannel {
  const models = item.models.filter(Boolean)
  if (item.model && !models.includes(item.model)) models.unshift(item.model)
  return { id: item.id, name: item.name, model: item.model, models, context_tokens: item.context_tokens ?? null }
}
