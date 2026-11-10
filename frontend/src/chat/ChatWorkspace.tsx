import { useEffect, useRef, useState } from 'react'
import { getChannels, getModelSettings } from '../api'
import type { ChatMode, ChatScope, StoredMessage } from '../api'
import { ChatComposer } from './ChatComposer'
import type { EditingTarget } from './ChatComposer'
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

export function ChatWorkspace({ scope, modelConfigured, onManageExtensions, onCollapse }: { scope: ChatScope; modelConfigured: boolean; onManageExtensions?: () => void; onCollapse?: () => void }) {
  const isGlobal = scope.kind === 'global'
  const isJob = scope.kind === 'job'
  const company = scope.kind === 'company' ? scope.company.trim() : ''
  const jobCompany = scope.kind === 'job' ? scope.company : ''
  const {
    sessions, conversation, messages, run, error,
    openConversation, send, stop, approve, removeMessage, renameSession, removeSession, changeMode, changeModel, clearError,
  } = useChatRun(scope)
  const [input, setInput] = useState('')
  const [editing, setEditing] = useState<EditingTarget>(null)
  const [sessionsCollapsed, setSessionsCollapsed] = useState(isJob)
  const [modelOptions, setModelOptions] = useState<string[]>([])
  const [showReasoning, setShowReasoning] = useState(() => localStorage.getItem('jobscope-reasoning') !== 'off')
  const threadBottom = useRef<HTMLDivElement>(null)

  useEffect(() => {
    Promise.all([getChannels(), getModelSettings()])
      .then(([channels, settings]) => {
        const channel = channels.channels.find(item => item.id === settings.llm.channel)
        setModelOptions(channel?.models ?? [])
      })
      .catch(() => { /* 模型选项获取失败时保持“跟随全局设置” */ })
  }, [])

  useEffect(() => {
    // 只滚线程自己的容器：scrollIntoView 会连带滚动详情面板等祖先，选中岗位时会把面板滚过标题区。
    const thread = threadBottom.current?.parentElement
    if (thread) thread.scrollTop = thread.scrollHeight
  }, [messages.length, run?.answer, run?.reasoning, run?.steps.length, run?.approval])

  function toggleReasoning(value: boolean) {
    setShowReasoning(value)
    localStorage.setItem('jobscope-reasoning', value ? 'on' : 'off')
  }

  const running = Boolean(run)
  const canMutate = conversation.id !== null && !running
  const mode: ChatMode = isGlobal ? conversation.mode : 'research'

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
    if (previous) void send(previous.content, { editMessageId: previous.id })
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
            {conversation.model ? ` · ${conversation.model}` : ''}
          </span>
        </div>
        <span className={`chat-state ${run ? 'busy' : ''}`}>{run ? (run.approval ? '等待工具确认' : '生成中…') : '空闲'}</span>
        {onCollapse && <button type="button" className="text-button chat-collapse" onClick={onCollapse}>收起</button>}
      </header>

      <div className="chat-thread" aria-live="polite">
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
          onEditSubmit={(target, content) => { setEditing(null); void send(content, { editMessageId: target.id }) }}
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

        <div ref={threadBottom} />
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
        modelOptions={modelOptions}
        onModelChange={model => void changeModel(model)}
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
      />
    </section>
  </div>
}
