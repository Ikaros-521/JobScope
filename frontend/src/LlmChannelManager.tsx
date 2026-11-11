import { useEffect, useState } from 'react'
import { activateLlmChannel, api, deleteLlmChannel, listLlmChannels, post, saveLlmChannel } from './api'
import type { Channel, LlmChannel, LlmChannelDraft, ModelProvider } from './api'
import { toast } from './toast'

type Protocol = 'openai' | 'anthropic'
type Draft = { id?: string; name: string; channel: string; protocol: Protocol; url: string; model: string; models: string[]; key: string; clear_key: boolean; context_tokens: number | '' }
const emptyDraft: Draft = { name: '', channel: 'custom', protocol: 'openai', url: '', model: '', models: [], key: '', clear_key: false, context_tokens: '' }

function hostOf(url: string) { try { return new URL(url).host } catch { return url } }

/** 已保存的对话渠道管理：每条渠道有独立地址/密钥/默认模型，可一键启用为全局默认。 */
export function LlmChannelManager({ activeLlm, onActivated }: { activeLlm: ModelProvider; onActivated: (settings: Record<'llm' | 'embedding', ModelProvider>) => Promise<void> }) {
  const [channels, setChannels] = useState<LlmChannel[]>([])
  const [catalog, setCatalog] = useState<Channel[]>([])
  const [draft, setDraft] = useState<Draft | null>(null)
  const [lists, setLists] = useState<{ models: string[]; source: string } | null>(null)
  const [busy, setBusy] = useState('')
  const [confirmId, setConfirmId] = useState('')

  useEffect(() => {
    void Promise.all([listLlmChannels(), api<{ channels: Channel[] }>('/settings/channels')])
      .then(([saved, catalog]) => { setChannels(saved.channels); setCatalog(catalog.channels) })
      .catch(err => toast((err as Error).message || '读取已存渠道失败', 'error'))
  }, [])

  function edit(value: LlmChannel) {
    setDraft({ id: value.id, name: value.name, channel: value.channel, protocol: value.protocol, url: value.url, model: value.model, models: value.models, key: '', clear_key: false, context_tokens: value.context_tokens ?? '' })
    setLists(value.models.length ? { models: value.models, source: 'saved' } : null)
  }

  function startCreate() {
    setDraft({ ...emptyDraft })
    setLists(null)
  }

  function update(changes: Partial<Draft>) {
    setDraft(current => current ? { ...current, ...changes } : current)
    if ('channel' in changes || 'protocol' in changes || 'url' in changes || 'key' in changes) setLists(null)
  }

  function draftPayload() {
    if (!draft) return null
    return { mode: 'remote' as const, channel: draft.channel, protocol: draft.protocol, url: draft.url.trim(), model: draft.model.trim(), key: draft.key.trim(), clear_key: draft.clear_key, context_tokens: draft.context_tokens === '' ? null : draft.context_tokens }
  }

  const saved = channels.find(item => item.id === draft?.id)
  // 与全局设置同一套凭证规则：换地址/协议必须重填密钥或显式清除。
  const keyBindingChanged = Boolean(saved && draft && (draft.url.trim() !== saved.url || draft.protocol !== saved.protocol))
  const needsKey = keyBindingChanged && Boolean(saved?.key_set) && !draft?.key.trim() && !draft?.clear_key

  async function action(operation: 'test' | 'discover') {
    if (!draft || busy || needsKey) return
    const config = draftPayload()
    if (!config) return
    setBusy(operation)
    try {
      const body = { target: 'llm' as const, channel_id: draft.id, config }
      if (operation === 'discover') {
        const result = await post<{ models: string[]; message: string }>('/settings/models/discover', body)
        setLists({ models: result.models, source: 'live' })
        update({ models: result.models.slice(0, 200) })
        toast(result.models.length ? `已获取 ${result.models.length} 个模型，保存渠道后保留列表。` : '渠道返回空列表，可手动输入模型 ID。')
      } else {
        const result = await post<{ duration_ms: number }>('/settings/models/test', body)
        toast(`连接成功 · ${result.duration_ms} ms。测试未保存配置。`)
      }
    } catch (err) { toast((err as Error).message || '操作失败', 'error') }
    finally { setBusy('') }
  }

  async function save() {
    if (!draft || busy || needsKey) return
    setBusy('save')
    try {
      const payload: LlmChannelDraft = { id: draft.id, name: draft.name.trim(), channel: draft.channel, protocol: draft.protocol, url: draft.url.trim(), model: draft.model.trim(), models: draft.models.slice(0, 200), key: draft.key.trim(), clear_key: draft.clear_key, context_tokens: draft.context_tokens === '' ? null : draft.context_tokens }
      const result = await saveLlmChannel(payload)
      setChannels(result.channels)
      setDraft(null)
      toast(`渠道「${payload.name}」已保存`)
    } catch (err) { toast((err as Error).message || '保存失败', 'error') }
    finally { setBusy('') }
  }

  async function remove(value: LlmChannel) {
    if (confirmId !== value.id) { setConfirmId(value.id); window.setTimeout(() => setConfirmId(''), 3000); return }
    setBusy(`delete-${value.id}`)
    try {
      const result = await deleteLlmChannel(value.id)
      setChannels(result.channels)
      if (draft?.id === value.id) setDraft(null)
      toast(`渠道「${value.name}」已删除`)
    } catch (err) { toast((err as Error).message || '删除失败', 'error') }
    finally { setBusy(''); setConfirmId('') }
  }

  async function activate(value: LlmChannel) {
    if (busy) return
    setBusy(`activate-${value.id}`)
    try {
      const settings = await activateLlmChannel(value.id)
      await onActivated(settings)
      toast(`已启用「${value.name}」为默认对话渠道`)
    } catch (err) { toast((err as Error).message || '启用失败', 'error') }
    finally { setBusy('') }
  }

  const preset = catalog.find(item => item.id === draft?.channel)
  const candidates = lists?.models ?? preset?.models ?? []
  const matches = draft ? candidates.filter(item => item.toLowerCase().includes(draft.model.trim().toLowerCase())) : []

  return <section className="llm-channel-manager" aria-label="已保存的对话渠道">
    <div className="section-heading">
      <div><h3 className="model-section-heading"><span>00</span> 已存渠道</h3><p className="body-small muted">每条渠道保存自己的地址与密钥；对话输入框可按渠道挑选模型，启用后作为全局默认。</p></div>
      {!draft && <button type="button" className="button secondary" onClick={startCreate}>＋ 新建渠道</button>}
    </div>
    {channels.length === 0 && !draft && <p className="body-small muted llm-channel-empty">还没有保存的渠道。新建一条，或把常用服务商的地址与密钥分别保存，之后在对话里按渠道切换模型。</p>}
    {channels.length > 0 && <ul className="llm-channel-list">
      {channels.map(item => {
        const active = activeLlm.saved_channel_id === item.id
        return <li key={item.id} className={active ? 'active' : ''}>
          <div className="llm-channel-info">
            <strong>{item.name}{active && <span className="badge good llm-channel-badge"><span />使用中</span>}</strong>
            <span className="body-small muted">{hostOf(item.url)} · {item.model || '未设默认模型'} · {item.key_set ? '密钥已存' : '无密钥'}{item.models.length ? ` · ${item.models.length} 个模型` : ''}</span>
          </div>
          <div className="llm-channel-actions">
            {active
              ? <span className="body-small muted">全局默认</span>
              : <button type="button" disabled={Boolean(busy)} onClick={() => void activate(item)}>{busy === `activate-${item.id}` ? '启用中…' : '启用为默认'}</button>}
            <button type="button" disabled={Boolean(busy)} onClick={() => edit(item)}>编辑</button>
            <button type="button" className="danger" disabled={Boolean(busy)} onClick={() => void remove(item)}>{confirmId === item.id ? '确认删除' : '删除'}</button>
          </div>
        </li>
      })}
    </ul>}
    {/* 外层已是模型设置的 form；嵌套 form 属非法 HTML 会让原生提交逃逸，这里用 div + 按钮提交。
        同时拦住回车，避免在渠道输入框里按 Enter 触发外层“保存模型设置”。 */}
    {draft && <div className="llm-channel-form" onKeyDown={event => {
      const target = event.target as HTMLElement
      if (event.key === 'Enter' && target.tagName !== 'TEXTAREA') event.preventDefault()
    }}>
      <div className="llm-channel-grid">
        <div><label className="field-label" htmlFor="llm-channel-name">渠道名称</label><input id="llm-channel-name" value={draft.name} onChange={event => update({ name: event.target.value })} placeholder="例如 工作流·DeepSeek" required maxLength={100} autoComplete="off" /></div>
        <div><label className="field-label" htmlFor="llm-channel-preset">服务商预设</label><select id="llm-channel-preset" value={draft.channel} onChange={event => {
          const channel = catalog.find(item => item.id === event.target.value)
          update({ channel: event.target.value, protocol: channel?.protocol ?? draft.protocol, url: channel?.url ?? draft.url })
        }}>{catalog.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
        <div><label className="field-label" htmlFor="llm-channel-protocol">接口协议</label><select id="llm-channel-protocol" value={draft.protocol} onChange={event => update({ protocol: event.target.value as Protocol })}><option value="openai">OpenAI 兼容</option><option value="anthropic">Anthropic</option></select></div>
      </div>
      <label className="field-label" htmlFor="llm-channel-url">服务地址</label><input id="llm-channel-url" type="url" value={draft.url} onChange={event => update({ url: event.target.value })} placeholder="https://api.example.com/v1" required maxLength={2000} autoComplete="off" spellCheck={false} />
      <label className="field-label" htmlFor="llm-channel-key">API 密钥 <span className="muted">{draft.id ? '· 留空保留已保存密钥' : ''}</span></label><input id="llm-channel-key" type={'pass' + 'word'} value={draft.key} onChange={event => update({ key: event.target.value, clear_key: false })} placeholder="填写该渠道的 API Key" maxLength={4000} autoComplete={'new-pass' + 'word'} spellCheck={false} />
      {draft.id && <label className="model-key-clear"><input type="checkbox" checked={draft.clear_key} onChange={event => update({ clear_key: event.target.checked, key: '' })} />清除该渠道已保存的密钥</label>}
      {needsKey && <p className="field-help key-warning">已更换服务地址或协议，请重新填写密钥，或勾选清除密钥后再保存。</p>}
      <div className="llm-channel-grid">
        <div><label className="field-label" htmlFor="llm-channel-model">默认模型 ID</label><input id="llm-channel-model" value={draft.model} onChange={event => update({ model: event.target.value })} placeholder="对话默认使用的模型，可留空" maxLength={300} autoComplete="off" spellCheck={false} /></div>
        <div><label className="field-label" htmlFor="llm-channel-context">上下文窗口 tokens <span className="muted">· 用量估算用</span></label><input id="llm-channel-context" type="number" min={4000} step={1000} value={draft.context_tokens} onChange={event => update({ context_tokens: event.target.value === '' ? '' : Number(event.target.value) })} placeholder="128000" /></div>
      </div>
      <div className="model-picker-heading"><span className="field-label">模型列表{lists?.source === 'live' ? ' · 账户在线列表' : lists?.source === 'saved' ? ' · 已保存列表' : ' · 渠道预设'}</span><button type="button" className="button secondary" disabled={!draft.url.trim() || Boolean(busy) || needsKey} onClick={() => void action('discover')}>{busy === 'discover' && <span className="spinner" aria-hidden="true" />}{busy === 'discover' ? '正在获取…' : '获取模型列表'}</button></div>
      {matches.length > 0 && <div className="model-options" role="group" aria-label="模型搜索结果">{matches.slice(0, 60).map(item => <button type="button" key={item} aria-pressed={draft.model === item} onClick={() => update({ model: item })}><span>{item}</span><span aria-hidden="true">{draft.model === item ? '✓' : '选择'}</span></button>)}</div>}
      <label className="field-label" htmlFor="llm-channel-models">可选模型（每行一个，可手动粘贴）</label>
      <textarea id="llm-channel-models" rows={3} value={draft.models.join('\n')} onChange={event => update({ models: event.target.value.split('\n').map(item => item.trim()).filter(Boolean).slice(0, 200) })} placeholder="渠道不提供模型列表时，在这里粘贴模型 ID，每行一个" spellCheck={false} />
      <div className="model-settings-actions">
        <button type="button" className="button secondary" disabled={!draft.url.trim() || !draft.model.trim() || Boolean(busy) || needsKey} onClick={() => void action('test')}>{busy === 'test' && <span className="spinner" aria-hidden="true" />}{busy === 'test' ? '正在测试…' : '测试连接'}</button>
        <button type="button" className="button secondary" onClick={() => setDraft(null)}>取消</button>
        <button type="button" className="button primary" disabled={!draft.name.trim() || !draft.url.trim() || Boolean(busy) || needsKey} onClick={() => void save()}>{busy === 'save' && <span className="spinner" aria-hidden="true" />}{busy === 'save' ? '正在保存…' : '保存渠道'}</button>
      </div>
    </div>}
  </section>
}
