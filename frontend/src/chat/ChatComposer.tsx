import { useEffect, useRef } from 'react'
import type { ChatMode } from '../api'

export type EditingTarget = { id: string; original: string } | null

export function ChatComposer({ input, onInputChange, mode, onModeChange, model, modelOptions, onModelChange, running, awaiting, onStop, onSend, modelConfigured, showReasoning, onToggleReasoning, editing, onCancelEdit, onManageExtensions }: {
  input: string
  onInputChange: (value: string) => void
  mode: ChatMode
  onModeChange: (mode: ChatMode) => void
  model: string
  modelOptions: string[]
  onModelChange: (model: string) => void
  running: boolean
  awaiting: boolean
  onStop: () => void
  onSend: () => void
  modelConfigured: boolean
  showReasoning: boolean
  onToggleReasoning: (value: boolean) => void
  editing: EditingTarget
  onCancelEdit: () => void
  onManageExtensions: () => void
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const node = textareaRef.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(200, Math.max(48, node.scrollHeight))}px`
  }, [input])

  const blocked = awaiting || (mode === 'general' && !modelConfigured)
  const placeholder = blocked
    ? awaiting ? '等待工具确认后再继续对话…' : '通用 Agent 需要先在模型设置中配置对话模型'
    : mode === 'general' ? '描述要完成的任务，Agent 会按需调用工具…' : '检索全项目资料，支持引用与出处…'

  function trySend() {
    if (running || blocked || !input.trim()) return
    onSend()
  }

  return <div className="chat-composer">
    {editing && <div className="composer-editing-note">
      <span>正在编辑历史消息并重新生成之后的回答</span>
      <button type="button" onClick={onCancelEdit}>取消编辑</button>
    </div>}
    <div className={`composer-box ${blocked ? 'disabled' : ''}`}>
      <textarea
        ref={textareaRef}
        value={input}
        rows={1}
        disabled={blocked}
        maxLength={8000}
        placeholder={placeholder}
        aria-label="全局对话输入框"
        onChange={event => onInputChange(event.target.value)}
        onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); trySend() } }}
      />
      {running
        ? <button type="button" className="composer-stop" onClick={onStop} aria-label="停止生成"><span aria-hidden="true">■</span>停止</button>
        : <button type="button" className="composer-send" onClick={trySend} disabled={blocked || !input.trim()} aria-label="发送消息">发送</button>}
    </div>
    <div className="composer-toolbar">
      <div className="composer-mode" role="group" aria-label="工作模式">
        <button type="button" aria-pressed={mode === 'research'} onClick={() => onModeChange('research')} disabled={running || awaiting}>资料研究</button>
        <button type="button" aria-pressed={mode === 'general'} onClick={() => onModeChange('general')} disabled={running || awaiting} title={modelConfigured ? '通用 Agent · 工具调用 / MCP / Skills' : '需要先配置对话模型'}>通用 Agent</button>
      </div>
      <label className="composer-model">
        <span>模型</span>
        <select value={model} onChange={event => onModelChange(event.target.value)} disabled={running || awaiting}>
          <option value="">跟随全局设置</option>
          {modelOptions.filter(option => option !== model).map(option => <option key={option} value={option}>{option}</option>)}
          {model && !modelOptions.includes(model) && <option value={model}>{model}</option>}
        </select>
      </label>
      <label className="composer-switch">
        <input type="checkbox" checked={showReasoning} onChange={event => onToggleReasoning(event.target.checked)} />
        <span>思维链</span>
      </label>
      <button type="button" className="composer-extensions" onClick={onManageExtensions}>MCP 与 Skills</button>
      <span className="composer-hint">Enter 发送 · Shift + Enter 换行{mode === 'general' ? ' · 外部工具按配置确认' : ' · 回答标注证据出处'}</span>
    </div>
  </div>
}
