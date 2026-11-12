import { useEffect, useRef } from 'react'
import type { PermissionMode } from '../api'

export type EditingTarget = { id: string; original: string } | null
export type ComposerChannel = { id: string; name: string; model: string; models: string[]; context_tokens?: number | null }

// 三档工具权限：切换到完全访问由工作区弹确认框，这里只负责展示与回调。
const PERMISSION_MODES: { value: PermissionMode; label: string; title: string }[] = [
  { value: 'confirm', label: '确认', title: '变更前确认：所有外部工具（MCP）调用都先展示参数，确认后才执行；在确认卡上选「本会话始终允许」可给单个工具放行' },
  { value: 'autonomous', label: '自主', title: '自主模式：只读、已勾选「自动执行」或本会话已授权的工具直接执行，其余逐次确认（确认时可选「本会话始终允许」）' },
  { value: 'full', label: '完全访问', title: '完全访问：外部工具全部自动执行，不再逐次确认（读取简历后的外发调用仍会确认）' },
]

const MODE_HINTS: Record<PermissionMode, string> = {
  confirm: '外部工具逐次确认',
  autonomous: '只读/自动执行/已授权工具免确认',
  full: '外部工具自动执行',
}

function tokensLabel(value: number) { return value >= 10000 ? `${Math.round(value / 1000)}k` : value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value) }

// 下拉选项值编码：''=跟随全局；'|model'=覆盖全局配置上的模型；'channelId|model'=指定渠道的模型；'channelId|'=渠道默认模型
function selectValue(model: string, channelId: string) { return channelId ? `${channelId}|${model}` : model ? `|${model}` : '' }

export function ChatComposer({ input, onInputChange, model, llmChannel = '', modelChannels = [], globalModels = [], onModelChange, permissionMode = 'autonomous', onPermissionModeChange, running, awaiting, onStop, onSend, modelConfigured, showReasoning, onToggleReasoning, editing, onCancelEdit, onManageExtensions, scopeLabel = '全项目', onExpandScope, locked = '', scopeName = '全项目资料', contextTokens = 0, contextWindow = 128000, compressing = false, hasConversation = false, canCompress = true, onCompress }: {
  input: string
  onInputChange: (value: string) => void
  model: string
  llmChannel?: string
  modelChannels?: ComposerChannel[]
  globalModels?: string[]
  onModelChange: (model: string, channelId: string) => void
  permissionMode?: PermissionMode
  onPermissionModeChange: (mode: PermissionMode) => void
  running: boolean
  awaiting: boolean
  onStop: () => void
  onSend: () => void
  modelConfigured: boolean
  showReasoning: boolean
  onToggleReasoning: (value: boolean) => void
  editing: EditingTarget
  onCancelEdit: () => void
  onManageExtensions?: () => void
  scopeLabel?: string
  onExpandScope?: () => void
  locked?: string
  scopeName?: string
  contextTokens?: number
  contextWindow?: number
  compressing?: boolean
  hasConversation?: boolean
  canCompress?: boolean
  onCompress?: () => void
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  useEffect(() => {
    const node = textareaRef.current
    if (!node) return
    node.style.height = 'auto'
    node.style.height = `${Math.min(200, Math.max(48, node.scrollHeight))}px`
  }, [input])

  const blocked = Boolean(locked) || awaiting || compressing || !modelConfigured
  const placeholder = locked || (compressing ? '正在压缩上下文…' : awaiting ? '等待工具确认后再继续对话…'
    : !modelConfigured ? '请先在模型设置中配置对话模型' : `围绕${scopeName}提问，Agent 会按需调用工具…`)

  function trySend() {
    if (running || blocked || !input.trim()) return
    onSend()
  }

  function pickModel(value: string) {
    const sep = value.indexOf('|')
    if (sep < 0) { onModelChange(value, ''); return }
    onModelChange(value.slice(sep + 1), value.slice(0, sep))
  }

  const percent = contextWindow > 0 ? Math.min(100, Math.round((contextTokens / contextWindow) * 100)) : 0
  const level = percent >= 85 ? 'danger' : percent >= 60 ? 'warning' : ''

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
        aria-label={`围绕${scopeName}的对话输入框`}
        onChange={event => onInputChange(event.target.value)}
        onKeyDown={event => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); trySend() } }}
      />
      {running
        ? <button type="button" className="composer-stop" onClick={onStop} aria-label="停止生成"><span aria-hidden="true">■</span>停止</button>
        : <button type="button" className="composer-send" onClick={trySend} disabled={blocked || !input.trim()} aria-label="发送消息">发送</button>}
    </div>
    <div className="composer-toolbar">
      <div className="composer-scope" aria-label="资料检索范围">
        <span className="composer-scope-tag" title={scopeLabel}>{scopeLabel}</span>
        {onExpandScope && <button type="button" className="composer-expand" disabled={running || awaiting || compressing || !hasConversation || Boolean(locked)} onClick={onExpandScope}>扩大范围 ↗</button>}
      </div>
      <label className="composer-model">
        <span>模型</span>
        <select value={selectValue(model, llmChannel)} onChange={event => pickModel(event.target.value)} disabled={running || awaiting}>
          <option value="">跟随全局设置</option>
          <optgroup label="全局默认渠道">
            {globalModels.map(item => <option key={`global-${item}`} value={`|${item}`}>{item}</option>)}
            {model && !llmChannel && !globalModels.includes(model) && <option value={`|${model}`}>{model}</option>}
          </optgroup>
          {modelChannels.map(channel => <optgroup key={channel.id} label={channel.name}>
            <option value={`${channel.id}|`}>{channel.model ? `默认 · ${channel.model}` : '渠道默认模型（未设置）'}</option>
            {channel.models.map(item => <option key={`${channel.id}-${item}`} value={`${channel.id}|${item}`}>{item}</option>)}
            {llmChannel === channel.id && model && !channel.models.includes(model) && model !== channel.model && <option value={`${channel.id}|${model}`}>{model}</option>}
          </optgroup>)}
          {llmChannel && !modelChannels.some(channel => channel.id === llmChannel) && <option value={selectValue(model, llmChannel)}>{`渠道已删除 · ${model || '默认模型'}`}</option>}
        </select>
      </label>
      <div className="composer-mode" role="group" aria-label="外部工具权限模式">
        <span>模式</span>
        <div className="composer-mode-seg">
          {PERMISSION_MODES.map(item => (
            <button key={item.value} type="button" className={permissionMode === item.value ? 'active' : ''}
              title={item.title} disabled={running || awaiting}
              aria-pressed={permissionMode === item.value}
              onClick={() => { if (permissionMode !== item.value) onPermissionModeChange(item.value) }}>
              {item.label}
            </button>
          ))}
        </div>
      </div>
      <label className="composer-switch">
        <input type="checkbox" checked={showReasoning} onChange={event => onToggleReasoning(event.target.checked)} />
        <span>思维链</span>
      </label>
      {onManageExtensions && <button type="button" className="composer-extensions" onClick={onManageExtensions}>MCP 与 Skills</button>}
      {onCompress && <div className="context-meter-group">
        <div className={`context-meter ${level}`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}
          aria-label="当前会话上下文估算用量" title={`当前会话上下文估算 ${tokensLabel(contextTokens)} / ${tokensLabel(contextWindow)} tokens（${percent}%，按字符数估算）`}>
          <span className="context-meter-bar" aria-hidden="true"><span className="context-meter-fill" style={{ width: `${percent}%` }} /></span>
          <span className="context-meter-text">{tokensLabel(contextTokens)} / {tokensLabel(contextWindow)}</span>
        </div>
        <button type="button" className="composer-compress" onClick={onCompress} disabled={running || awaiting || compressing || !hasConversation || !canCompress}
          title={canCompress ? '把较早的历史折叠成一条摘要检查点，模型上下文随之精简；原文仍保留可查看' : '历史消息较少，暂无需压缩'}>
          {compressing ? '压缩中…' : '压缩上下文'}
        </button>
      </div>}
      <span className="composer-hint">Enter 发送 · Shift + Enter 换行 · {MODE_HINTS[permissionMode]}</span>
    </div>
  </div>
}
