import { useEffect, useMemo, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, TouchEvent as ReactTouchEvent, WheelEvent as ReactWheelEvent } from 'react'
import { estimateTokens, getChannels, getModelSettings, listLlmChannels } from '../api'
import type { ChatMode, ChatScope, LlmChannel, StoredMessage } from '../api'
import { toast } from '../toast'
import { ChatComposer } from './ChatComposer'
import type { ComposerChannel, EditingTarget } from './ChatComposer'
import { ChatMessageItem, ReasoningBlock } from './ChatMessageItem'
import { SessionList } from './SessionList'
import { ToolTimeline } from './ToolTimeline'
import { Markdown } from './Markdown'
import { useChatRun } from './useChatRun'

const SUGGESTIONS: Record<ChatMode, string[]> = {
  research: ['哪些岗位提到了 RAG 或 Agent？', '比较已保存岗位的招聘薪资', '哪些公司仍缺少独立资料？'],
  general: ['结合我的简历，制定 Agent 岗位面试计划', '检索 Python 岗位并比较薪资', '解释 MCP 与 Skills 的区别'],
}

const COMPANY_SUGGESTIONS = ['这家公司有哪些岗位要求？', '公司经营情况有哪些证据？', '薪资还有哪些口径需要核实？']
const JOB_SUGGESTIONS = ['这份岗位有哪些要求？', '公司有哪些可核实的信息？', '面试时还应该问清什么？']

// 距底部超过该阈值视为用户正在回看，暂停自动吸底；翻回阈值内自动恢复。
const STICK_THRESHOLD = 80
// 上下文估算基数：system 提示 + 工具声明，不随会话变化。
const CONTEXT_BASE_TOKENS = 600

export function ChatWorkspace({ scope, modelConfigured, onManageExtensions }: { scope: ChatScope; modelConfigured: boolean; onManageExtensions?: () => void }) {
  const isGlobal = scope.kind === 'global'
  const isJob = scope.kind === 'job'
  const company = scope.kind === 'company' ? scope.company.trim() : ''
  const jobCompany = scope.kind === 'job' ? scope.company : ''
  const {
    sessions, conversation, messages, run, error,
    openConversation, send, stop, approve, removeMessage, renameSession, removeSession, changeMode, changeModel, compress, clearError,
  } = useChatRun(scope)
  const [input, setInput] = useState('')
  const [editing, setEditing] = useState<EditingTarget>(null)
  const [sessionsCollapsed, setSessionsCollapsed] = useState(isJob)
  const [globalModels, setGlobalModels] = useState<string[]>([])
  const [modelChannels, setModelChannels] = useState<ComposerChannel[]>([])
  const [contextWindow, setContextWindow] = useState(128000)
  const [showReasoning, setShowReasoning] = useState(() => localStorage.getItem('jobscope-reasoning') !== 'off')
  const [showJump, setShowJump] = useState(false)
  const [compressing, setCompressing] = useState(false)
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
  }, [messages.length, run?.answer, run?.reasoning, run?.steps.length, run?.approval])

  // 切换会话时恢复吸底并直接落到最新消息。
  useEffect(() => { stickRef.current = true; setShowJump(false); scrollToBottom() }, [conversation.id])

  function jumpToBottom() {
    stickRef.current = true
    setShowJump(false)
    scrollToBottom()
  }

  function toggleReasoning(value: boolean) {
    setShowReasoning(value)
    localStorage.setItem('jobscope-reasoning', value ? 'on' : 'off')
  }

  const running = Boolean(run)
  const canMutate = conversation.id !== null && !running
  const mode: ChatMode = isGlobal ? conversation.mode : 'research'

  // 用量只统计未折叠消息（含摘要检查点）：与模型实际可见的上下文口径一致；流式增量也计入。
  const contextTokens = useMemo(() => (
    CONTEXT_BASE_TOKENS + messages.reduce((sum, item) => sum + (item.compacted ? 0 : estimateTokens(item.content)), 0)
      + (run?.answer ? estimateTokens(run.answer) : 0)
  ), [messages, run?.answer])

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
    setInput(message.content)
  }

  function cancelEdit() {
    setEditing(null)
    setInput('')
  }

  async function submit() {
    const text = input.trim()
    if (!text) return
    stickRef.current = true
    setShowJump(false)
    const ok = await send(text, editing ? { editMessageId: editing.id } : undefined)
    if (ok) {
      setInput('')
      setEditing(null)
    }
  }

  function resend(message: StoredMessage) {
    if (running) return
    if (message.role === 'user') {
      setEditing({ id: message.id, original: message.content })
      setInput(message.content)
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

  const suggestions = isGlobal ? SUGGESTIONS[mode] : isJob ? JOB_SUGGESTIONS : COMPANY_SUGGESTIONS
  const welcome = !isGlobal && !isJob && !company
    ? { title: '先确认公司主体，再开始对话', text: '在上方输入资料库中的精确公司名称。对话只检索这一主体的岗位与公司资料，其他公司和全局对话历史不会进来。' }
    : mode === 'general'
      ? { title: '让工具参与完成任务', text: 'Agent 会按需检索项目、读取简历、加载 Skill 或调用 MCP；外部工具会先向你确认。' }
      : isGlobal
        ? { title: '从所有已保存资料中查找线索', text: '检索本项目保存的全部岗位和公司证据，回答会标注公司与出处。' }
        : isJob
          ? { title: '围绕这份岗位提问', text: '检索该岗位的职责原文、详情快照与同一公司的补充资料，回答标注出处；资料缺失的部分保持未知。' }
          : { title: `围绕 ${company} 提问`, text: '询问招聘要求、公司披露、员工情况或仍待核实的问题；回答只引用该公司已保存的资料。' }

  const channelName = modelChannels.find(item => item.id === conversation.llm_channel)?.name
  const modelSuffix = conversation.llm_channel
    ? ` · ${channelName || '渠道已删除'} · ${conversation.model || '默认模型'}`
    : conversation.model ? ` · ${conversation.model}` : ''

  return <div className={`chat-page chat-${scope.kind} ${sessionsCollapsed ? 'sessions-collapsed' : ''}`}>
    <aside className="chat-sidebar">
      <div className="chat-sidebar-top">
        <button type="button" className="button primary small chat-new-button" onClick={() => { void openConversation(null); cancelEdit() }} disabled={running || (scope.kind === 'company' && !company)}>
          {sessionsCollapsed ? '＋' : '＋ 新对话'}
        </button>
        <button type="button" className="icon-button" aria-label={sessionsCollapsed ? '展开会话列表' : '收起会话列表'} onClick={toggleSessions}>
          {sessionsCollapsed ? '»' : '«'}
        </button>
      </div>
      <SessionList
        sessions={sessions}
        activeId={conversation.id}
        collapsed={sessionsCollapsed}
        onOpen={id => { if (!running) { cancelEdit(); void openConversation(id) } }}
        onRename={(id, title) => void renameSession(id, title)}
        onDelete={id => void removeSession(id)}
      />
    </aside>

    <section className="chat-main">
      <header className="chat-main-head">
        <div className="chat-main-title">
          <h2>{isGlobal ? conversation.title : scope.kind === 'job' ? (scope.title || scope.company) : `${company || '未选定公司'} · 对话`}</h2>
          <span className="chat-main-sub">
            {isGlobal
              ? (mode === 'general' ? '通用 Agent · 工具调用 / MCP / Skills' : '资料研究 · 检索与引用校验')
              : isJob ? '资料研究 · 只检索该岗位与同公司资料' : '资料研究 · 只检索该公司的已保存资料'}
            {modelSuffix}
          </span>
        </div>
        <span className={`chat-state ${run ? 'busy' : ''}`}>{run ? (run.approval ? '等待工具确认' : '生成中…') : '空闲'}</span>
      </header>

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
            message={message}
            showReasoning={showReasoning}
            editing={editing?.id === message.id}
            canMutate={canMutate}
            onStartEdit={startEdit}
            onEditSubmit={(target, content) => { setEditing(null); stickRef.current = true; setShowJump(false); void send(content, { editMessageId: target.id }) }}
            onEditCancel={cancelEdit}
            onResend={resend}
            onDelete={target => void removeMessage(target.id)}
          />)}

          {run && <article className="chat-bubble assistant live-bubble">
            <div className="chat-bubble-head">
              <span className="chat-avatar" aria-hidden="true">知</span>
              <span className="message-author">{run.approval ? '知根 Agent · 等待确认' : '知根 Agent · 正在工作'}</span>
            </div>
            {showReasoning && run.reasoning && <ReasoningBlock text={run.reasoning} live={!run.approval} />}
            <ToolTimeline steps={run.steps} running />
            {run.answer && <div className="message-content"><Markdown content={run.answer} /></div>}
            {run.error && <div className="run-error">{run.error}</div>}
          </article>}
        </div>
        {showJump && <button type="button" className="jump-bottom" onClick={jumpToBottom}>↓ 回到底部</button>}
      </div>

      {run?.approval && <section className="agent-approval" aria-label="待确认工具调用">
        <h3>确认工具调用</h3>
        <p>{run.approval.tool}</p>
        <pre>{JSON.stringify(run.approval.arguments, null, 2)}</pre>
        <p>{run.approval.reason}。确认后仅执行这一次调用。</p>
        <div>
          <button type="button" className="button secondary" onClick={() => void approve(false)}>拒绝并继续</button>
          <button type="button" className="button primary" onClick={() => void approve(true)}>确认执行</button>
        </div>
      </section>}

      {error && <div className="alert error chat-alert" role="alert"><span>{error}</span><button aria-label="关闭错误提示" onClick={clearError}>×</button></div>}

      <ChatComposer
        input={input}
        onInputChange={setInput}
        mode={mode}
        onModeChange={mode => void changeMode(mode)}
        model={conversation.model}
        llmChannel={conversation.llm_channel}
        modelChannels={modelChannels}
        globalModels={globalModels}
        onModelChange={(model, channelId) => void changeModel(model, channelId)}
        running={running}
        awaiting={Boolean(run?.approval)}
        onStop={() => void stop()}
        onSend={() => void submit()}
        modelConfigured={modelConfigured}
        showReasoning={showReasoning}
        onToggleReasoning={toggleReasoning}
        editing={editing}
        onCancelEdit={cancelEdit}
        onManageExtensions={onManageExtensions}
        showMode={isGlobal}
        modeLabel={isJob ? '岗位资料研究' : '公司资料研究'}
        locked={isGlobal || isJob || company ? '' : '请先在上方确认公司主体'}
        scopeName={isGlobal ? '全项目资料' : isJob ? `${jobCompany}的岗位资料` : company || '未选定公司'}
        contextTokens={contextTokens}
        contextWindow={activeWindow}
        compressing={compressing}
        hasConversation={conversation.id !== null}
        canCompress={canCompress}
        onCompress={() => void compressContext()}
      />
    </section>
  </div>
}

function toComposerChannel(item: LlmChannel): ComposerChannel {
  const models = item.models.filter(Boolean)
  if (item.model && !models.includes(item.model)) models.unshift(item.model)
  return { id: item.id, name: item.name, model: item.model, models, context_tokens: item.context_tokens ?? null }
}
