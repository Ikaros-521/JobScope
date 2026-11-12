import { useEffect, useRef, useState } from 'react'
import { activateLlmChannel, deleteLlmChannel, getBindings, getChannels, listLlmChannels, post, saveBinding, saveLlmChannel } from './api'
import type { BindingEntry, BindingsPayload, Channel, LlmChannel, LlmChannelDraft, ModelPurpose } from './api'
import { toast } from './toast'
import { navigateTabs } from './tabNavigation'

type Protocol = 'openai' | 'anthropic'
type Draft = { id?: string; name: string; channel: string; protocol: Protocol; url: string; model: string; modelsText: string; embeddingsText: string; key: string; clear_key: boolean; context_tokens: number | '' }
const emptyDraft: Draft = { name: '', channel: 'custom', protocol: 'openai', url: '', model: '', modelsText: '', embeddingsText: '', key: '', clear_key: false, context_tokens: '' }

const PURPOSE_META: Record<ModelPurpose, { label: string; description: string; allow: ('inherit' | 'channel' | 'local' | 'disabled')[] }> = {
  agent: { label: '对话 Agent', description: '岗位 / 公司 / 全局对话与上下文压缩使用的默认模型', allow: ['channel'] },
  embedding: { label: '语义检索', description: '向量召回；关闭后回退 BM25 全文检索', allow: ['channel', 'local', 'disabled'] },
  extraction: { label: '可靠性信号抽取', description: '从公司资料中抽取结构化经营信号', allow: ['inherit', 'channel', 'disabled'] },
  analysis: { label: '简历适配分析', description: '简历与岗位要求的逐项映射；关闭后走本地规则对照', allow: ['inherit', 'channel', 'disabled'] },
}

function hostOf(url: string) { try { return new URL(url).host } catch { return url } }
function splitModels(text: string) { return text.split('\n').map(item => item.trim()).filter(Boolean).slice(0, 200) }

export function ModelSettings({ onSaved }: { onSaved: () => Promise<void> }) {
  const [tab, setTab] = useState<'channels' | 'purposes'>('channels')
  const [channels, setChannels] = useState<LlmChannel[] | null>(null)
  const [catalog, setCatalog] = useState<Channel[]>([])
  const [bindings, setBindings] = useState<BindingsPayload | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [lists, setLists] = useState<{ models: string[]; source: string } | null>(null)
  const [busy, setBusy] = useState('')
  const [confirmId, setConfirmId] = useState('')
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [loadError, setLoadError] = useState<string | null>(null)
  const inFlight = useRef(false)

  useEffect(() => {
    let active = true
    void Promise.all([listLlmChannels(), getChannels(), getBindings()]).then(([saved, catalog, bindings]) => {
      if (!active) return
      setChannels(saved.channels); setCatalog(catalog.channels); setBindings(bindings)
      setSelectedId(current => current ?? saved.channels[0]?.id ?? null)
    }).catch(err => { if (active) setLoadError((err as Error).message || '读取模型配置失败，请重试。') })
    return () => { active = false }
  }, [loadAttempt])

  function refreshBindings() { return getBindings().then(setBindings) }

  function channelById(id: string | null | undefined) { return (channels ?? []).find(item => item.id === id) ?? null }
  const selected = channelById(selectedId)
  const defaultChannelId = bindings?.agent.binding?.kind === 'channel' ? bindings.agent.binding.channel_id : null

  function edit(value: LlmChannel) {
    setSelectedId(value.id)
    setDraft({ id: value.id, name: value.name, channel: value.channel, protocol: value.protocol, url: value.url, model: value.model, modelsText: value.models.join('\n'), embeddingsText: (value.embeddings ?? []).join('\n'), key: '', clear_key: false, context_tokens: value.context_tokens ?? '' })
    setLists(value.models.length ? { models: value.models, source: 'saved' } : null)
  }

  function startCreate() {
    setSelectedId(null)
    setDraft({ ...emptyDraft })
    setLists(null)
  }

  function update(changes: Partial<Draft>) { setDraft(current => current ? { ...current, ...changes } : current) }

  function draftPayload() {
    if (!draft) return null
    return { mode: 'remote' as const, channel: draft.channel, protocol: draft.protocol, url: draft.url.trim(), model: draft.model.trim(), key: draft.key.trim(), clear_key: draft.clear_key, context_tokens: draft.context_tokens === '' ? null : draft.context_tokens }
  }

  const savedChannel = channelById(draft?.id)
  const keyBindingChanged = Boolean(savedChannel && draft && (draft.url.trim() !== savedChannel.url || draft.protocol !== savedChannel.protocol))
  const needsKey = keyBindingChanged && Boolean(savedChannel?.key_set) && !draft?.key.trim() && !draft?.clear_key

  async function action(operation: 'test' | 'discover') {
    if (!draft || inFlight.current || needsKey) return
    const config = draftPayload()
    if (!config) return
    setBusy(operation)
    try {
      const body = { target: 'llm' as const, channel_id: draft.id, config }
      if (operation === 'discover') {
        const result = await post<{ models: string[]; message: string }>('/settings/models/discover', body)
        setLists({ models: result.models, source: 'live' })
        update({ modelsText: result.models.slice(0, 200).join('\n') })
        toast(result.models.length ? `已获取 ${result.models.length} 个模型，保存渠道后保留列表。` : '渠道返回空列表，可手动输入模型 ID。')
      } else {
        const result = await post<{ duration_ms: number }>('/settings/models/test', body)
        toast(`连接成功 · ${result.duration_ms} ms。测试未保存配置。`)
      }
    } catch (err) { toast((err as Error).message || '操作失败', 'error') }
    finally { setBusy('') }
  }

  async function save() {
    if (!draft || inFlight.current || needsKey) return
    inFlight.current = true
    setBusy('save')
    try {
      const payload: LlmChannelDraft = { id: draft.id, name: draft.name.trim(), channel: draft.channel, protocol: draft.protocol, url: draft.url.trim(), model: draft.model.trim(), models: splitModels(draft.modelsText), embeddings: splitModels(draft.embeddingsText), key: draft.key.trim(), clear_key: draft.clear_key, context_tokens: draft.context_tokens === '' ? null : draft.context_tokens }
      const result = await saveLlmChannel(payload)
      setChannels(result.channels)
      await refreshBindings()
      setDraft(null)
      setSelectedId(result.channel.id)
      toast(`渠道「${payload.name}」已保存`)
    } catch (err) { toast((err as Error).message || '保存失败', 'error') }
    finally { inFlight.current = false; setBusy('') }
  }

  async function remove(value: LlmChannel) {
    if (inFlight.current) return
    if (confirmId !== value.id) { setConfirmId(value.id); window.setTimeout(() => setConfirmId(''), 3000); return }
    inFlight.current = true
    setBusy(`delete-${value.id}`)
    try {
      const result = await deleteLlmChannel(value.id)
      setChannels(result.channels)
      if (result.bindings) setBindings(result.bindings)
      if (selectedId === value.id) { setSelectedId(result.channels[0]?.id ?? null); setDraft(null) }
      toast(`渠道「${value.name}」已删除`)
    } catch (err) { toast((err as Error).message || '删除失败', 'error') }
    finally { inFlight.current = false; setBusy(''); setConfirmId('') }
  }

  async function setAsDefault(value: LlmChannel) {
    if (inFlight.current) return
    if (!value.model) { toast('请先给这条渠道填写默认模型，再设为对话默认', 'error'); return }
    inFlight.current = true
    setBusy(`activate-${value.id}`)
    try {
      const result = await activateLlmChannel(value.id)
      setBindings(result.bindings)
      await onSaved()
      toast(`已将「${value.name} · ${value.model}」设为对话默认`)
    } catch (err) { toast((err as Error).message || '设置失败', 'error') }
    finally { inFlight.current = false; setBusy('') }
  }

  const preset = catalog.find(item => item.id === draft?.channel)
  const candidates = lists?.models ?? preset?.models ?? []
  const matches = draft ? candidates.filter(item => item.toLowerCase().includes(draft.model.trim().toLowerCase())) : []

  return <><section className="page-intro"><div><h1>模型设置</h1><p>渠道保存地址与密钥；每个功能单独决定用哪个渠道的哪个模型。</p></div><span className="revision-label">保存在本机</span></section>
    {loadError ? <section className="paper-panel"><div role="alert"><h2>暂时无法读取模型配置</h2><p>{loadError}</p></div><button className="button secondary" type="button" onClick={() => { setLoadError(null); setLoadAttempt(current => current + 1) }}>重新加载</button></section>
      : <div className="model-settings-shell">
        <div className="management-tabs model-settings-tabs" role="tablist" aria-label="模型设置分区" onKeyDown={navigateTabs}>
          <button id="channels-tab" role="tab" aria-controls="channels-panel" aria-selected={tab === 'channels'} tabIndex={tab === 'channels' ? 0 : -1} onClick={() => setTab('channels')}>模型渠道 <span>{channels ? channels.length : '—'}</span></button>
          <button id="purposes-tab" role="tab" aria-controls="purposes-panel" aria-selected={tab === 'purposes'} tabIndex={tab === 'purposes' ? 0 : -1} onClick={() => setTab('purposes')}>功能与模型</button>
        </div>

        {tab === 'channels' && <section id="channels-panel" role="tabpanel" aria-labelledby="channels-tab" className="channels-layout">
          <aside className="channels-list" aria-label="已保存渠道">
            <button type="button" className="button secondary small channels-new" onClick={startCreate}>＋ 新建渠道</button>
            {channels === null && <p className="body-small muted" role="status">正在读取渠道配置…</p>}
            {channels?.map(item => {
              const isDefault = defaultChannelId === item.id
              return <button type="button" key={item.id} className={`channel-item ${selectedId === item.id && !draft ? 'selected' : ''} ${isDefault ? 'default' : ''}`} onClick={() => { setDraft(null); setSelectedId(item.id) }}>
                <strong>{item.name}{isDefault && <span className="badge good channel-badge"><span />对话默认</span>}</strong>
                <span>{hostOf(item.url)}</span>
                <span>{item.model || '未设默认模型'} · {item.models.length} 个模型{item.key_set ? ' · 密钥已存' : ''}</span>
              </button>
            })}
            {channels !== null && channels.length === 0 && !draft && <p className="body-small muted">还没有渠道。新建一条，填入服务商地址与密钥，再把它的模型设为各功能的默认。</p>}
          </aside>

          {draft ? <section className="paper-panel channel-editor" aria-label={draft.id ? '编辑渠道' : '新建渠道'}>
            <div className="section-heading"><div><h2>{draft.id ? '编辑渠道' : '新建渠道'}</h2><p className="body-small muted">密钥只存在这条渠道里；功能绑定指向它，改密钥即刻全局生效。</p></div><button type="button" className="text-button" onClick={() => { setDraft(null); setSelectedId(channels?.[0]?.id ?? null) }}>取消</button></div>
            <div className="channel-editor-form" onKeyDown={event => {
              const target = event.target as HTMLElement
              if (event.key === 'Enter' && target.tagName !== 'TEXTAREA') event.preventDefault()
            }}>
              <div className="channel-grid">
                <div><label className="field-label" htmlFor="channel-name">渠道名称</label><input id="channel-name" value={draft.name} onChange={event => update({ name: event.target.value })} placeholder="例如 工作流·DeepSeek" maxLength={100} autoComplete="off" /></div>
                <div><label className="field-label" htmlFor="channel-preset">服务商预设</label><select id="channel-preset" value={draft.channel} onChange={event => {
                  const item = catalog.find(entry => entry.id === event.target.value)
                  update({ channel: event.target.value, protocol: item?.protocol ?? draft.protocol, url: item?.url ?? draft.url })
                }}>{catalog.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div>
                <div><label className="field-label" htmlFor="channel-protocol">接口协议</label><select id="channel-protocol" value={draft.protocol} onChange={event => update({ protocol: event.target.value as Protocol })}><option value="openai">OpenAI 兼容</option><option value="anthropic">Anthropic</option></select></div>
              </div>
              <label className="field-label" htmlFor="channel-url">服务地址</label><input id="channel-url" type="url" value={draft.url} onChange={event => update({ url: event.target.value })} placeholder="https://api.example.com/v1" maxLength={2000} autoComplete="off" spellCheck={false} />
              <label className="field-label" htmlFor="channel-key">API 密钥 <span className="muted">{draft.id ? '· 留空保留已保存密钥' : ''}</span></label><input id="channel-key" type={'pass' + 'word'} value={draft.key} onChange={event => update({ key: event.target.value, clear_key: false })} placeholder="填写该渠道的 API Key" maxLength={4000} autoComplete={'new-pass' + 'word'} spellCheck={false} />
              {draft.id && <label className="model-key-clear"><input type="checkbox" checked={draft.clear_key} onChange={event => update({ clear_key: event.target.checked, key: '' })} />清除该渠道已保存的密钥</label>}
              {needsKey && <p className="field-help key-warning">已更换服务地址或协议，请重新填写密钥，或勾选清除密钥后再保存。</p>}
              <div className="channel-grid">
                <div><label className="field-label" htmlFor="channel-model">默认模型 ID</label><input id="channel-model" value={draft.model} onChange={event => update({ model: event.target.value })} placeholder="设为对话默认时使用" maxLength={300} autoComplete="off" spellCheck={false} /></div>
                <div><label className="field-label" htmlFor="channel-context">上下文窗口 tokens <span className="muted">· 用量估算用</span></label><input id="channel-context" type="number" min={4000} step={1000} value={draft.context_tokens} onChange={event => update({ context_tokens: event.target.value === '' ? '' : Number(event.target.value) })} placeholder="128000" /></div>
              </div>
              <div className="model-picker-heading"><span className="field-label">对话模型列表{lists?.source === 'live' ? ' · 账户在线列表' : lists?.source === 'saved' ? ' · 已保存列表' : ' · 渠道预设'}</span><button type="button" className="button secondary" disabled={!draft.url.trim() || Boolean(busy) || needsKey} onClick={() => void action('discover')}>{busy === 'discover' && <span className="spinner" aria-hidden="true" />}{busy === 'discover' ? '正在获取…' : '获取模型列表'}</button></div>
              {matches.length > 0 && <div className="model-options" role="group" aria-label="模型搜索结果">{matches.slice(0, 60).map(item => <button type="button" key={item} aria-pressed={draft.model === item} onClick={() => update({ model: item })}><span>{item}</span><span aria-hidden="true">{draft.model === item ? '✓' : '选择'}</span></button>)}</div>}
              <label className="field-label" htmlFor="channel-models">对话模型（每行一个，可手动粘贴）</label>
              <textarea id="channel-models" rows={4} value={draft.modelsText} onChange={event => update({ modelsText: event.target.value })} placeholder="渠道不提供列表时，在这里粘贴模型 ID，每行一个" spellCheck={false} />
              <label className="field-label" htmlFor="channel-embeddings">向量模型（每行一个；供「语义检索」功能选用）</label>
              <textarea id="channel-embeddings" rows={2} value={draft.embeddingsText} onChange={event => update({ embeddingsText: event.target.value })} placeholder={(preset?.embeddings ?? []).join('\n') || '例如 BAAI/bge-m3'} spellCheck={false} />
              <div className="model-settings-actions">
                <button type="button" className="button secondary" disabled={!draft.url.trim() || !draft.model.trim() || Boolean(busy) || needsKey} onClick={() => void action('test')}>{busy === 'test' && <span className="spinner" aria-hidden="true" />}{busy === 'test' ? '正在测试…' : '测试连接'}</button>
                <button type="button" className="button primary" disabled={!draft.name.trim() || !draft.url.trim() || Boolean(busy) || needsKey} onClick={() => void save()}>{busy === 'save' && <span className="spinner" aria-hidden="true" />}{busy === 'save' ? '正在保存…' : '保存渠道'}</button>
              </div>
            </div>
          </section>
            : selected ? <section className="paper-panel channel-editor" aria-label={`渠道 ${selected.name}`}>
              <div className="section-heading"><div><h2>{selected.name}</h2><p className="body-small muted">{hostOf(selected.url)} · {selected.protocol === 'anthropic' ? 'Anthropic' : 'OpenAI 兼容'}</p></div>
                {defaultChannelId === selected.id && <span className="badge good"><span />对话默认</span>}</div>
              <dl className="channel-summary">
                <div><dt>默认模型</dt><dd>{selected.model || '未设置'}</dd></div>
                <div><dt>对话模型</dt><dd>{selected.models.length ? `${selected.models.length} 个` : '未登记'}</dd></div>
                <div><dt>向量模型</dt><dd>{(selected.embeddings ?? []).length ? `${selected.embeddings!.length} 个` : '未登记'}</dd></div>
                <div><dt>上下文窗口</dt><dd>{selected.context_tokens ? `${Math.round(selected.context_tokens / 1000)}k` : '按 128k 估算'}</dd></div>
                <div><dt>密钥</dt><dd>{selected.key_set ? '已保存（不回显）' : '未保存'}</dd></div>
              </dl>
              <p className="body-small muted">功能绑定直接使用这条渠道的密钥；轮换密钥后无需改任何功能。</p>
              <div className="model-settings-actions">
                <button type="button" className="button primary" disabled={Boolean(busy) || defaultChannelId === selected.id} onClick={() => void setAsDefault(selected)}>{busy === `activate-${selected.id}` ? '设置中…' : defaultChannelId === selected.id ? '当前对话默认' : '设为对话默认'}</button>
                <button type="button" className="button secondary" onClick={() => edit(selected)}>编辑</button>
                <button type="button" className="button secondary danger" disabled={Boolean(busy)} onClick={() => void remove(selected)}>{confirmId === selected.id ? '确认删除' : busy === `delete-${selected.id}` ? '删除中…' : '删除'}</button>
              </div>
            </section>
              : <section className="paper-panel channel-editor empty"><h3>选择或新建一条渠道</h3><p className="body-small muted">渠道保存服务地址、密钥与模型列表；「功能与模型」页把每个功能指向某条渠道的某个模型。</p></section>}
        </section>}

        {tab === 'purposes' && <section id="purposes-panel" role="tabpanel" aria-labelledby="purposes-tab" className="purposes-layout" aria-label="功能与模型">
          {bindings === null && <div className="paper-panel"><p className="body-small muted" role="status">正在读取功能绑定…</p></div>}
          {bindings && (Object.keys(PURPOSE_META) as ModelPurpose[]).map(purpose => <PurposeCard
            key={purpose} purpose={purpose} meta={PURPOSE_META[purpose]} entry={bindings[purpose]} channels={channels ?? []} inFlight={inFlight.current}
            onChange={async binding => {
              if (inFlight.current) return
              inFlight.current = true
              try { setBindings(await saveBinding(purpose, binding)); await onSaved(); toast('已保存，立即生效') }
              catch (err) { toast((err as Error).message || '保存失败', 'error') }
              finally { inFlight.current = false }
            }} />)}
        </section>}
      </div>}
  </>
}

function PurposeCard({ purpose, meta, entry, channels, inFlight, onChange }: {
  purpose: ModelPurpose
  meta: { label: string; description: string; allow: ('inherit' | 'channel' | 'local' | 'disabled')[] }
  entry: BindingEntry
  channels: LlmChannel[]
  inFlight: boolean
  onChange: (binding: { kind: 'channel'; channel_id: string; model: string } | { kind: 'local'; local_model: string } | { kind: 'disabled' } | { kind: 'inherit' }) => Promise<void>
}) {
  const [mode, setMode] = useState<string>(() => entry.binding?.kind ?? (meta.allow.includes('inherit') ? 'inherit' : 'channel'))
  const [channelId, setChannelId] = useState(entry.binding?.channel_id ?? '')
  const [model, setModel] = useState(entry.binding?.model ?? '')
  const [localModel, setLocalModel] = useState(entry.binding?.local_model ?? '')
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')
  const firstRender = useRef(true)
  useEffect(() => {
    if (firstRender.current) { firstRender.current = false; return }
    setMode(entry.binding?.kind ?? (meta.allow.includes('inherit') ? 'inherit' : 'channel'))
    setChannelId(entry.binding?.channel_id ?? '')
    setModel(entry.binding?.model ?? '')
    setLocalModel(entry.binding?.local_model ?? '')
    setDirty(false)
  }, [entry.binding])

  function summary() {
    if (entry.channel_missing) return '渠道已删除：正在回退默认配置，请重新绑定'
    if (entry.resolved.mode === 'disabled') return '已关闭'
    if (entry.resolved.mode === 'local') return `本地模型 · ${entry.resolved.model || entry.binding?.local_model || '未设置'}`
    const name = entry.channel_name || (entry.binding?.channel_id ? '已选渠道' : '全局默认配置')
    return entry.resolved.model ? `${name} · ${entry.resolved.model}` : `${name} · 未选择模型`
  }

  async function commit() {
    setError('')
    if (mode === 'inherit') await onChange({ kind: 'inherit' })
    else if (mode === 'disabled') await onChange({ kind: 'disabled' })
    else if (mode === 'local') { if (!localModel.trim()) { setError('请填写本地缓存模型'); return } await onChange({ kind: 'local', local_model: localModel.trim() }) }
    else { if (!channelId || !model.trim()) { setError('请选择渠道与模型'); return } await onChange({ kind: 'channel', channel_id: channelId, model: model.trim() }) }
    setDirty(false)
  }

  const canEmbed = (id: string) => channels.find(item => item.id === id)?.protocol !== 'anthropic'
  const selectable = channels.filter(item => purpose !== 'embedding' || canEmbed(item.id))

  return <section className="paper-panel purpose-card" aria-label={meta.label}>
    <div className="section-heading"><div><h2>{meta.label}</h2><p className="body-small muted">{meta.description}</p></div>
      <span className={`badge ${entry.resolved.configured ? 'good' : ''}`}><span />{entry.resolved.configured ? '已配置' : entry.resolved.mode === 'disabled' ? '已关闭' : '未配置'}</span></div>
    <p className="body-small purpose-summary"><strong>当前生效：</strong>{summary()}</p>
    <div className="purpose-controls">
      <label className="field-label" htmlFor={`purpose-${purpose}-mode`}>模型来源</label>
      <select id={`purpose-${purpose}-mode`} value={mode} onChange={event => { setMode(event.target.value); setDirty(true) }} disabled={inFlight}>
        {meta.allow.map(kind => <option key={kind} value={kind}>{kind === 'channel' ? '使用渠道模型' : kind === 'local' ? '本地缓存模型' : kind === 'disabled' ? '关闭此功能' : '跟随对话默认'}</option>)}
      </select>
      {mode === 'channel' && <>
        <label className="field-label" htmlFor={`purpose-${purpose}-channel`}>渠道</label>
        <select id={`purpose-${purpose}-channel`} value={channelId} onChange={event => { setChannelId(event.target.value); const next = channels.find(item => item.id === event.target.value); setModel(next?.model ?? ''); setDirty(true) }} disabled={inFlight}>
          <option value="">{channelId && !selectable.some(item => item.id === channelId) ? '渠道已删除，请重新选择' : '请选择渠道'}</option>
          {selectable.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
        <label className="field-label" htmlFor={`purpose-${purpose}-model`}>模型</label>
        <select id={`purpose-${purpose}-model`} value={model} onChange={event => { setModel(event.target.value); setDirty(true) }} disabled={inFlight || !channelId}>
          <option value="">{model ? model : '请选择模型'}</option>
          {(channelById2(channels, channelId)?.models ?? []).map(item => <option key={item} value={item}>{item}</option>)}
          {model && !(channelById2(channels, channelId)?.models ?? []).includes(model) && <option value={model}>{model}</option>}
        </select>
        {purpose === 'embedding' && channelId && <p className="body-small muted">向量模型清单在渠道编辑里维护；此处列出该渠道已登记的向量与对话模型。</p>}
      </>}
      {mode === 'local' && <>
        <label className="field-label" htmlFor={`purpose-${purpose}-local`}>本地缓存模型</label>
        <input id={`purpose-${purpose}-local`} value={localModel} onChange={event => { setLocalModel(event.target.value); setDirty(true) }} placeholder="BAAI/bge-small-zh-v1.5" maxLength={1000} spellCheck={false} disabled={inFlight} />
      </>}
    </div>
    {purpose === 'embedding' && mode === 'channel' && <div className="model-options purpose-embedding-picks" role="group" aria-label="该渠道可用模型">
      {(channelById2(channels, channelId)?.embeddings ?? []).slice(0, 12).map(item => <button type="button" key={item} aria-pressed={model === item} disabled={inFlight} onClick={() => { setModel(item); setDirty(true) }}><span>{item}</span><span aria-hidden="true">{model === item ? '✓' : '选择'}</span></button>)}
    </div>}
    {error && <p className="field-help key-warning" role="alert">{error}</p>}
    <div className="model-settings-actions">
      <button type="button" className="button primary" disabled={!dirty || inFlight} onClick={() => void commit().catch(err => setError((err as Error).message || '保存失败'))}>{inFlight ? '保存中…' : '保存此功能'}</button>
      {dirty && <button type="button" className="button secondary" disabled={inFlight} onClick={() => {
        setMode(entry.binding?.kind ?? (meta.allow.includes('inherit') ? 'inherit' : 'channel')); setChannelId(entry.binding?.channel_id ?? ''); setModel(entry.binding?.model ?? ''); setLocalModel(entry.binding?.local_model ?? ''); setDirty(false); setError('')
      }}>还原</button>}
    </div>
  </section>
}

function channelById2(channels: LlmChannel[], id: string) { return channels.find(item => item.id === id) }
