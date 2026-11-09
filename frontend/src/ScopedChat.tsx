import { useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { post, safeUrl } from './api'
import type { ChatMessage, ChatResult } from './api'
import { ResearchTrace } from './ResearchTrace'

export function ScopedChat({ company, global = false, modelConfigured, onManageExtensions }: { company?: string; global?: boolean; modelConfigured: boolean; onManageExtensions?: () => void }) {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [mode, setMode] = useState<'research' | 'general'>('research')
  const [pending, setPending] = useState<ChatResult | null>(null)
  const conversation = useRef<string | null>(null)
  const generation = useRef(0)
  const enabled = global || Boolean(company)
  const inputId = global ? 'global-chat-input' : 'company-chat-input'
  const scopeName = global ? '全项目资料' : company || '尚未选择公司'
  function clear() { generation.current++; conversation.current = null; setMessages([]); setInput(''); setError(''); setBusy(false); setPending(null) }
  async function approve(approved: boolean) {
    if (!pending || busy) return
    const token = generation.current
    setBusy(true); setError('')
    try {
      const result = await post<ChatResult>(`/agent/runs/${pending.trace?.id}/approval`, { approved, approval_id: pending.pending_approval?.approval_id })
      if (token !== generation.current) return
      setPending(result.pending_approval ? result : null)
      setMessages(current => [...current.slice(0, -1), { role: 'assistant', content: result.answer, mode: result.mode, citations: result.citations, trace: result.trace, metrics: result.metrics }])
    } catch (err) { if (token === generation.current) { setError((err as Error).message); setPending(null) } }
    finally { if (token === generation.current) setBusy(false) }
  }
  async function send(event: FormEvent) {
    event.preventDefault()
    if (!enabled || !input.trim() || busy || pending) return
    const token = generation.current
    const text = input.trim()
    const previous = messages
    setBusy(true); setError(''); setInput(''); setMessages([...previous, { role: 'user', content: text }])
    try {
      const result = await post<ChatResult>('/chat', { message: text, ...(global ? { scope: 'global', agent_mode: mode } : { company }), conversation_id: conversation.current })
      if (token !== generation.current) return
      conversation.current = result.conversation_id
      setPending(result.pending_approval ? result : null)
      setMessages(current => [...current, { role: 'assistant', content: result.answer, mode: result.mode, citations: result.citations, trace: result.trace, metrics: result.metrics }])
    } catch (err) {
      if (token === generation.current) { setError((err as Error).message); setInput(text); setMessages(previous) }
    } finally { if (token === generation.current) setBusy(false) }
  }
  return <section className={`paper-panel scoped-chat ${global ? 'global-chat-panel' : 'company-chat-panel'}`}>
    <div className="section-heading"><div><h2>{global ? mode === 'general' ? '通用 Agent' : '跨公司资料问答' : '公司对话'}</h2><p className="body-small muted">{modelConfigured ? mode === 'general' ? '自主选择工具 · 按需加载 Skills' : '模型综合问答 · 引用可核实' : '资料检索模式 · 未配置模型'}</p></div><button className="button secondary small" onClick={clear} disabled={busy || (!messages.length && !input)}>新建对话</button></div>
    {global && <><div className="agent-mode-select"><label htmlFor="global-agent-mode">对话模式</label><select id="global-agent-mode" value={mode} disabled={busy || Boolean(pending)} onChange={event => { clear(); setMode(event.target.value as 'research' | 'general') }}><option value="research">资料研究 · 固定检索与引用校验</option><option value="general">通用 Agent · 工具调用 / MCP / Skills</option></select></div><button type="button" className="button secondary manage-extensions" onClick={onManageExtensions}>管理 MCP 与 Skills <span aria-hidden="true">→</span></button></>}
    <div className="scope-banner"><span>{global ? '可用能力' : '已锁定公司'}</span><strong>{mode === 'general' ? '通用问答 + 全项目资料 + 已启用扩展' : scopeName}</strong><p>{global ? mode === 'general' ? '模型按需检索项目、读取简历、加载 Skill 或调用 MCP。外部工具按配置确认，项目引用仅来自本轮实际读取的证据。' : '检索本项目保存的全部岗位和公司证据，回答会标注公司与出处' : '只检索这个精确主体的岗位和公司资料，其他公司及全局对话历史不会进入此会话'}</p></div>
    {error && <div className="alert error" role="alert">{error}</div>}
    {!messages.length && <div className="chat-empty"><h3>{global ? mode === 'general' ? '让工具参与完成任务' : '从所有已保存资料中查找线索' : company ? `围绕 ${company} 提问` : '先选择公司，再开始对话'}</h3><p>{global ? mode === 'general' ? '可以准备面试、制定学习计划，或结合项目资料做多步研究' : '可以比较岗位要求、搜索薪资口径，或查找某家公司的证据' : '询问招聘要求、公司披露、员工情况或仍待核实的问题'}</p>{enabled && <div className="chat-suggestions">{(global ? mode === 'general' ? ['结合我的简历，制定 Agent 岗位面试计划', '检索 Python 岗位并比较薪资', '解释 MCP 与 Skills 的区别'] : ['哪些岗位提到了 RAG 或 Agent？', '比较已保存岗位的招聘薪资', '哪些公司仍缺少独立资料？'] : ['这家公司有哪些岗位要求？', '公司经营情况有哪些证据？', '薪资还有哪些口径需要核实？']).map(text => <button key={text} onClick={() => setInput(text)}>{text}</button>)}</div>}</div>}
    <div className="chat-messages" aria-live="polite">{messages.map((message, index) => <article className={`chat-message ${message.role}`} key={index}><span className="message-author">{message.role === 'user' ? '我' : message.mode === 'evidence_search' ? '资料检索' : '知根'}</span>{message.role === 'assistant' && <ResearchTrace run={message.trace} metrics={message.metrics} />}<div className="message-content">{message.content}</div>{message.citations?.map(citation => <details className="citation" key={citation.id}><summary>{citation.company && <span className="citation-company">{citation.company} · </span>}{citation.title}</summary><p>{citation.excerpt}</p>{citation.locators?.map((locator, i) => <p className="tiny-label" key={i}>{locator.page || '原文'} · 字符 {locator.start}–{locator.end}</p>)}{safeUrl(citation.url) ? <a className="external-link" href={safeUrl(citation.url)} target="_blank" rel="noreferrer">原始来源</a> : <span className="tiny-label">未提供原始链接</span>}</details>)}</article>)}{busy && <div className="chat-thinking" role="status"><span className="spinner" />正在检索{scopeName}…</div>}</div>
    {pending?.pending_approval && <section className="agent-approval" aria-label="待确认工具调用"><h3>确认工具调用</h3><p>{pending.pending_approval.tool}</p><pre>{JSON.stringify(pending.pending_approval.arguments, null, 2)}</pre><p>{pending.pending_approval.reason}。确认后仅执行这一次调用。</p><div><button className="button secondary" disabled={busy} onClick={() => void approve(false)}>拒绝并继续</button><button className="button primary" disabled={busy} onClick={() => void approve(true)}>确认执行</button></div></section>}
    <form className="chat-form" onSubmit={send}><label className="sr-only" htmlFor={inputId}>{global ? '围绕全项目资料提问' : '围绕当前公司提问'}</label><textarea id={inputId} rows={3} disabled={!enabled || Boolean(pending)} value={input} onChange={event => setInput(event.target.value)} maxLength={8000} placeholder={enabled ? mode === 'general' ? '描述要完成的任务…' : `向${scopeName}提问…` : '请先选择公司'} onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); event.currentTarget.form?.requestSubmit() } }} /><button className="button primary" disabled={!enabled || !input.trim() || busy || Boolean(pending) || (mode === 'general' && !modelConfigured)} aria-label={global ? '发送全局问题' : '发送公司问题'}>{busy ? <span className="spinner" /> : '发送'}</button></form>
    <p className="chat-footer">Enter 发送 · Shift + Enter 换行 · {mode === 'general' ? '工具过程可查看 · 外部结果需核实' : modelConfigured ? '资料缺失时保持未知' : '当前返回证据摘录，未执行模型综合分析'}</p>
  </section>
}
