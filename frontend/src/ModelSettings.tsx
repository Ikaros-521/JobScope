import { useEffect, useRef, useState } from 'react'
import { activateLlmChannel, deleteLlmChannel, getBindings, getChannels, listLlmChannels, post, saveBinding, saveLlmChannel } from './api'
import type { BindingEntry, BindingsPayload, Channel, LlmChannel, LlmChannelDraft, ModelMetadata, ModelPurpose } from './api'
import { toast } from './toast'
import { navigateTabs } from './tabNavigation'

type Protocol = 'openai' | 'anthropic'
type Draft = { id?: string; name: string; channel: string; protocol: Protocol; url: string; model: string; modelsText: string; embeddingsText: string; key: string; clear_key: boolean; context_tokens: number | '' }
type BindingDraft = { kind: 'channel'; channel_id: string; model: string } | { kind: 'local'; local_model: string } | { kind: 'disabled' } | { kind: 'inherit' }
type TestRecord = { duration_ms: number; channel: string; at: string }
const emptyDraft: Draft = { name: '', channel: 'custom', protocol: 'openai', url: '', model: '', modelsText: '', embeddingsText: '', key: '', clear_key: false, context_tokens: '' }

const PURPOSE_META: Record<ModelPurpose, { label: string; description: string; allow: ('inherit' | 'channel' | 'local' | 'disabled')[] }> = {
  agent: { label: '对话 Agent', description: '岗位 / 公司 / 全局对话与上下文压缩使用的默认模型', allow: ['channel'] },
  embedding: { label: '语义检索', description: '向量召回；关闭后回退 BM25 全文检索', allow: ['channel', 'local', 'disabled'] },
  extraction: { label: '可靠性信号抽取', description: '从公司资料中抽取结构化经营信号', allow: ['inherit', 'channel', 'disabled'] },
  analysis: { label: '简历适配分析', description: '简历与岗位要求的逐项映射；关闭后走本地规则对照', allow: ['inherit', 'channel', 'disabled'] },
}
const PURPOSE_ORDER = Object.keys(PURPOSE_META) as ModelPurpose[]
const STEPS: { id: number; label: string; target: string }[] = [
  { id: 1, label: '选择服务商', target: 'provider-heading' },
  { id: 2, label: '连接信息', target: 'connection-heading' },
  { id: 3, label: '选择默认模型', target: 'models-heading' },
]

function hostOf(url: string) { try { return new URL(url).host } catch { return url } }
function splitModels(text: string) { return [...new Set(text.split(/[\n,，;；]+/).map(item => item.trim()).filter(Boolean))].slice(0, 200) }
function contextLabel(tokens: number) { return tokens >= 1_000_000 ? `${(tokens / 1_000_000).toLocaleString('zh-CN', { maximumFractionDigits: 6 })}M` : `${(tokens / 1000).toLocaleString('zh-CN', { maximumFractionDigits: 3 })}K` }
function purposeSource(kind: string | undefined) {
  return kind === 'channel' ? '使用渠道模型' : kind === 'local' ? '本地缓存模型' : kind === 'disabled' ? '关闭此功能' : '跟随对话默认'
}

export function ModelSettings({ onSaved }: { onSaved: () => Promise<void> }) {
  const [tab, setTab] = useState<'channels' | 'purposes'>('channels')
  const [channels, setChannels] = useState<LlmChannel[] | null>(null)
  const [catalog, setCatalog] = useState<Channel[]>([])
  const [metadata, setMetadata] = useState<Record<string, ModelMetadata>>({})
  const [modelQuery, setModelQuery] = useState('')
  const [showKey, setShowKey] = useState(false)
  const [formError, setFormError] = useState('')
  const [bindings, setBindings] = useState<BindingsPayload | null>(null)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [lists, setLists] = useState<{ models: string[]; source: string } | null>(null)
  const [busy, setBusy] = useState('')
  const [confirmId, setConfirmId] = useState('')
  const [loadAttempt, setLoadAttempt] = useState(0)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [lastTest, setLastTest] = useState<TestRecord | null>(null)
  const [testedIds, setTestedIds] = useState<Set<string>>(new Set())
  const [expandedPurpose, setExpandedPurpose] = useState<ModelPurpose | null>(null)
  const [formDirty, setFormDirty] = useState(false)
  const [activeStep, setActiveStep] = useState(1)
  const inFlight = useRef(false)

  useEffect(() => {
    let active = true
    void Promise.all([listLlmChannels(), getChannels(), getBindings()]).then(([saved, catalog, bindings]) => {
      if (!active) return
      setChannels(saved.channels); setCatalog(catalog.channels); setMetadata(catalog.model_metadata ?? {}); setBindings(bindings)
      setSelectedId(current => current ?? saved.channels[0]?.id ?? null)
    }).catch(err => { if (active) setLoadError((err as Error).message || '读取模型配置失败，请重试。') })
    return () => { active = false }
  }, [loadAttempt])

  function refreshBindings() { return getBindings().then(setBindings) }

  function channelById(id: string | null | undefined) { return (channels ?? []).find(item => item.id === id) ?? null }
  const selected = channelById(selectedId)
  const defaultChannelId = bindings?.agent.binding?.kind === 'channel' ? bindings.agent.binding.channel_id : null
  const configuredPurposes = bindings ? PURPOSE_ORDER.filter(purpose => bindings[purpose].resolved.configured).length : 0
  const defaultChannel = channelById(defaultChannelId)
  const defaultModel = bindings?.agent.resolved.model || defaultChannel?.model || '未设置'

  function edit(value: LlmChannel) {
    setSelectedId(value.id)
    setDraft({ id: value.id, name: value.name, channel: value.channel, protocol: value.protocol, url: value.url, model: value.model, modelsText: value.models.join('\n'), embeddingsText: (value.embeddings ?? []).join('\n'), key: '', clear_key: false, context_tokens: value.context_tokens ?? '' })
    setLists(value.models.length ? { models: value.models, source: 'saved' } : null)
    setModelQuery(''); setShowKey(false); setFormError(''); setConfirmId(''); setFormDirty(false); setActiveStep(1)
  }

  function startCreate() {
    setSelectedId(null)
    setDraft({ ...emptyDraft })
    setLists(null)
    setModelQuery(''); setShowKey(false); setFormError(''); setConfirmId(''); setFormDirty(false); setActiveStep(1)
  }

  function update(changes: Partial<Draft>) { setDraft(current => current ? { ...current, ...changes } : current); setFormError(''); setFormDirty(true) }

  function chooseProvider(id: string) {
    const item = catalog.find(entry => entry.id === id)
    if (!item || !draft) return
    const previous = catalog.find(entry => entry.id === draft.channel)
    update({ channel: id, key: '', clear_key: false, name: !draft.name || draft.name === previous?.name ? item.name : draft.name, protocol: item.protocol, url: item.url, model: '', context_tokens: '', modelsText: item.models.join('\n'), embeddingsText: (item.embeddings ?? []).join('\n') })
    setLists(null); setModelQuery(''); setShowKey(false)
  }

  function chooseModel(model: string) {
    update({ model, context_tokens: metadata[model.trim()]?.context_tokens ?? '' })
  }

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
    inFlight.current = true
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
        const testedId = draft.id
        const testedName = draft.name.trim() || draft.url.trim()
        setLastTest({ duration_ms: result.duration_ms, channel: testedName, at: new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) })
        if (testedId) setTestedIds(current => { const next = new Set(current); next.add(testedId); return next })
        toast(`连接成功 · ${result.duration_ms} ms。测试未保存配置。`)
      }
    } catch (err) { toast((err as Error).message || '操作失败', 'error') }
    finally { inFlight.current = false; setBusy('') }
  }

  async function save() {
    if (!draft || inFlight.current || needsKey) return
    if (draft.context_tokens !== '' && (!Number.isInteger(draft.context_tokens) || draft.context_tokens < 4000 || draft.context_tokens > 2_000_000)) {
      setFormError('上下文窗口请输入 4,000 至 2,000,000 之间的整数。')
      document.getElementById('channel-context')?.focus()
      return
    }
    inFlight.current = true
    setBusy('save')
    try {
      const payload: LlmChannelDraft = { id: draft.id, name: draft.name.trim(), channel: draft.channel, protocol: draft.protocol, url: draft.url.trim(), model: draft.model.trim(), models: splitModels([draft.model.trim(), draft.modelsText].filter(Boolean).join('\n')), embeddings: splitModels(draft.embeddingsText), key: draft.key.trim(), clear_key: draft.clear_key, context_tokens: draft.context_tokens === '' ? null : draft.context_tokens }
      const result = await saveLlmChannel(payload)
      setChannels(result.channels)
      await refreshBindings()
      setDraft(null)
      setSelectedId(result.channel.id)
      setFormDirty(false)
      toast(`渠道「${payload.name}」已保存`)
    } catch (err) { toast((err as Error).message || '保存失败', 'error') }
    finally { inFlight.current = false; setBusy('') }
  }

  function requestRemove(value: LlmChannel) {
    setConfirmId(current => current === value.id ? '' : value.id)
  }

  async function remove(value: LlmChannel) {
    if (inFlight.current) return
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

  async function updatePurpose(purpose: ModelPurpose, binding: BindingDraft) {
    if (inFlight.current) return
    inFlight.current = true
    try { setBindings(await saveBinding(purpose, binding)); await onSaved(); toast('已保存，立即生效') }
    catch (err) { toast((err as Error).message || '保存失败', 'error') }
    finally { inFlight.current = false }
  }

  function goToStep(step: number, target: string) {
    setActiveStep(step)
    document.getElementById(target)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }

  const preset = catalog.find(item => item.id === draft?.channel)
  const candidates = [...new Set([...(lists?.source === 'live' ? lists.models : [...(lists?.models ?? []), ...(preset?.models ?? [])]), ...splitModels(draft?.modelsText ?? '')])]
  const matches = candidates.filter(item => item.toLowerCase().includes(modelQuery.trim().toLowerCase()))
  const orderedMatches = draft?.model ? [...matches].sort((a, b) => a === draft.model ? -1 : b === draft.model ? 1 : 0) : matches
  const modelInfo = metadata[draft?.model.trim() ?? '']
  const canSave = Boolean(draft?.name.trim() && draft.url.trim() && !busy && !needsKey)

  return <><section className="page-intro"><div><h1>模型设置</h1><p>渠道保存地址与密钥；每个功能单独决定用哪个渠道的哪个模型。</p></div>
    <div className="model-security"><span className="revision-label">保存在本机</span><small>密钥不回显、不上传</small></div></section>
    {loadError ? <section className="paper-panel"><div role="alert"><h2>暂时无法读取模型配置</h2><p>{loadError}</p></div><button className="button secondary" type="button" onClick={() => { setLoadError(null); setLoadAttempt(current => current + 1) }}>重新加载</button></section>
      : <div className="model-settings-shell">
        <section className="model-status-strip" aria-label="配置状态总览">
          <div className="model-metric primary">
            <span className="metric-label">模型渠道</span>
            <strong className="metric-value">{channels ? channels.length : '—'} <small>条已保存</small></strong>
            <span className="metric-foot">{defaultChannelId ? '1 条设为对话默认' : '尚未设置对话默认'}</span>
          </div>
          <div className="model-metric">
            <span className="metric-label">对话默认</span>
            <strong className="metric-value">{defaultModel}</strong>
            <span className="metric-foot">{defaultChannel?.name || '全局默认配置'}{bindings?.agent.resolved.context_tokens ? ` · ${contextLabel(bindings.agent.resolved.context_tokens)} 上下文` : ''}</span>
          </div>
          <div className="model-metric">
            <span className="metric-label">功能绑定</span>
            <strong className="metric-value">{configuredPurposes} <small>/ {PURPOSE_ORDER.length} 已配置</small></strong>
            <span className="metric-foot">{bindings ? PURPOSE_ORDER.filter(purpose => !bindings[purpose].resolved.configured).map(purpose => PURPOSE_META[purpose].label).join('、') || '全部已配置' : '读取中'}</span>
          </div>
          <div className="model-metric">
            <span className="metric-label">连接状态</span>
            <strong className="metric-value">{lastTest ? <><span className="status-dot ok" aria-hidden="true" />{lastTest.duration_ms} <small>ms</small></> : '尚未测试'}</strong>
            <span className="metric-foot">{lastTest ? `${lastTest.channel} · ${lastTest.at} · 本次会话` : '在渠道编辑里点「测试连接」'}</span>
          </div>
        </section>

        <div className="management-tabs model-settings-tabs" role="tablist" aria-label="模型设置分区" onKeyDown={navigateTabs}>
          <button id="channels-tab" role="tab" aria-controls="channels-panel" aria-selected={tab === 'channels'} tabIndex={tab === 'channels' ? 0 : -1} onClick={() => setTab('channels')}>模型渠道 <span>{channels ? channels.length : '—'}</span></button>
          <button id="purposes-tab" role="tab" aria-controls="purposes-panel" aria-selected={tab === 'purposes'} tabIndex={tab === 'purposes' ? 0 : -1} onClick={() => setTab('purposes')}>功能与模型</button>
        </div>

        {tab === 'channels' && <section id="channels-panel" role="tabpanel" aria-labelledby="channels-tab" className="channels-layout">
          <aside className="channels-list" aria-label="已保存渠道">
            <button type="button" className="button secondary small channels-new" disabled={Boolean(busy)} onClick={startCreate}>＋ 新建渠道</button>
            {channels === null && <p className="body-small muted" role="status">正在读取渠道配置…</p>}
            {channels?.map(item => {
              const isDefault = defaultChannelId === item.id
              const tested = testedIds.has(item.id)
              return <button type="button" key={item.id} disabled={Boolean(busy)} className={`channel-item ${selectedId === item.id && !draft ? 'selected' : ''} ${isDefault ? 'default' : ''}`} onClick={() => { setDraft(null); setSelectedId(item.id); setConfirmId('') }}>
                <strong><span className={`status-dot ${tested ? 'ok' : 'off'}`} aria-hidden="true" />{item.name}{isDefault && <span className="badge good channel-badge"><span />对话默认</span>}</strong>
                <span>{hostOf(item.url)}</span>
                <span>{item.model || '未设默认模型'} · {item.models.length} 个模型{item.key_set ? ' · 密钥已存' : ''} · {tested ? '已测试' : '未测试'}</span>
              </button>
            })}
            {channels !== null && channels.length === 0 && !draft && <p className="body-small muted">还没有渠道。新建一条，填入服务商地址与密钥，再把它的模型设为各功能的默认。</p>}
          </aside>

          {draft ? <section className="paper-panel channel-editor" aria-label={draft.id ? '编辑渠道' : '新建渠道'}>
            <div className="section-heading"><div><h2>{draft.id ? '编辑渠道' : '新建渠道'}</h2><p className="body-small muted">选服务商、填密钥、选模型，即可完成配置。</p></div><button type="button" className="text-button" disabled={Boolean(busy)} onClick={() => { setDraft(null); setSelectedId(channels?.[0]?.id ?? null); setFormDirty(false) }}>取消</button></div>
            <div className="channel-stepnav" role="group" aria-label="配置步骤">
              {STEPS.map(step => <button type="button" key={step.id} className={activeStep === step.id ? 'active' : ''} aria-current={activeStep === step.id ? 'step' : undefined} onClick={() => goToStep(step.id, step.target)}><span>{step.id}</span>{step.label}</button>)}
            </div>
            <fieldset className="channel-editor-form" disabled={Boolean(busy)}>
              <section className="channel-form-section" aria-labelledby="provider-heading">
                <div className="channel-step-heading"><span aria-hidden="true">1</span><h3 id="provider-heading">选择服务商</h3><span className="body-small muted">自动填入地址与协议</span></div>
                <div className="provider-picks" role="group" aria-label="服务商预设">{catalog.map(item => <button type="button" key={item.id} aria-pressed={draft.channel === item.id} onClick={() => chooseProvider(item.id)}>{item.name}</button>)}</div>
                {preset?.note && <p className="field-help">{preset.note}</p>}
              </section>
              <section className="channel-form-section" aria-labelledby="connection-heading">
                <div className="channel-step-heading"><span aria-hidden="true">2</span><h3 id="connection-heading">连接信息</h3><span className="body-small muted">密钥仅保存在本机</span></div>
                <div className="channel-grid">
                  <div><label className="field-label" htmlFor="channel-name">渠道名称 <span className="muted">必填</span></label><input id="channel-name" value={draft.name} onChange={event => update({ name: event.target.value })} placeholder="例如 工作专用 · DeepSeek" maxLength={100} autoComplete="off" /></div>
                  <div><label className="field-label" htmlFor="channel-protocol">接口协议</label><select id="channel-protocol" value={draft.protocol} onChange={event => update({ protocol: event.target.value as Protocol })}><option value="openai">OpenAI 兼容</option><option value="anthropic">Anthropic Messages</option></select></div>
                </div>
                <label className="field-label" htmlFor="channel-url">服务地址 <span className="muted">必填 · 支持 Base URL 或完整端点</span></label><input id="channel-url" type="url" value={draft.url} onChange={event => update({ url: event.target.value })} placeholder="https://api.example.com/v1" maxLength={2000} autoComplete="off" spellCheck={false} />
                <label className="field-label" htmlFor="channel-key">API 密钥 <span className="muted">{draft.id && savedChannel?.key_set ? '· 已保存，留空保留' : '· 本地无鉴权服务可留空'}</span></label>
                <div className="channel-key-input"><input id="channel-key" type={showKey ? 'text' : 'pass' + 'word'} value={draft.key} onChange={event => update({ key: event.target.value, clear_key: false })} placeholder="填写该渠道的 API Key" maxLength={4000} autoComplete={'new-pass' + 'word'} spellCheck={false} /><button type="button" className="button secondary" aria-controls="channel-key" aria-pressed={showKey} onClick={() => setShowKey(current => !current)}>{showKey ? '隐藏' : '显示'}</button></div>
                {draft.id && <label className="model-key-clear"><input type="checkbox" checked={draft.clear_key} onChange={event => update({ clear_key: event.target.checked, key: '' })} />清除已保存的密钥</label>}
                {needsKey && <p className="field-help key-warning" role="alert">已更换服务地址或协议，请重新填写密钥，或勾选清除密钥后再保存。</p>}
              </section>
              <section className="channel-form-section" aria-labelledby="models-heading">
                <div className="channel-step-heading"><span aria-hidden="true">3</span><h3 id="models-heading">选择默认模型</h3><span className="body-small muted">也可直接输入模型 ID</span></div>
                <div className="model-picker-heading"><span className="body-small muted">{lists?.source === 'live' ? '账户在线清单' : lists?.source === 'saved' ? '已保存清单与官方参考' : '官方参考清单'} · {candidates.length} 个</span><button type="button" className="button secondary" disabled={!draft.url.trim() || needsKey} onClick={() => void action('discover')}>{busy === 'discover' && <span className="spinner" aria-hidden="true" />}{busy === 'discover' ? '正在获取…' : '获取账户模型'}</button></div>
                <label className="field-label" htmlFor="channel-model-search">搜索模型</label><input id="channel-model-search" type="search" value={modelQuery} onChange={event => setModelQuery(event.target.value)} placeholder="输入关键词筛选，不改变已选模型" autoComplete="off" />
                {orderedMatches.length > 0 ? <div className="model-options channel-model-options" role="group" aria-label="模型搜索结果">{orderedMatches.slice(0, 60).map(item => <button type="button" key={item} aria-pressed={draft.model === item} onClick={() => chooseModel(item)}><span>{item}</span><span>{metadata[item] ? `${contextLabel(metadata[item].context_tokens)} 上下文 · ` : ''}{draft.model === item ? '已选择' : '选择'}</span></button>)}</div> : <p className="channel-model-empty">{candidates.length ? '没有匹配项，可在下方直接填写模型 ID。' : '暂无模型清单。填入地址与密钥后获取，或直接填写模型 ID。'}</p>}
                {matches.length > 60 && <p className="field-help">显示前 60 / {matches.length} 个结果，请输入关键词缩小范围。</p>}
                <div className="channel-grid">
                  <div><label className="field-label" htmlFor="channel-model">默认模型 ID</label><input id="channel-model" value={draft.model} onChange={event => chooseModel(event.target.value)} placeholder="点击上方模型，或直接粘贴 ID" maxLength={300} autoComplete="off" spellCheck={false} /></div>
                  <div><label className="field-label" htmlFor="channel-context">上下文窗口 <span className="muted">tokens</span></label><input id="channel-context" type="number" min={4000} max={2000000} step={1} aria-describedby="channel-context-help" aria-invalid={Boolean(formError)} value={draft.context_tokens} onChange={event => update({ context_tokens: event.target.value === '' ? '' : Number(event.target.value) })} placeholder="未填写时按 128,000 估算" /></div>
                </div>
                <p id="channel-context-help" className="field-help">上下文用于对话用量估算，不是单次输出上限；未填写时按 128,000 估算。</p>
                <details className="channel-help-fold">
                  <summary>什么是上下文窗口 · 模型清单说明</summary>
                  <div className="channel-help-body">
                    <p className="field-help">{modelInfo ? <>官方参考 {contextLabel(modelInfo.context_tokens)} tokens · <a href={modelInfo.source} target="_blank" rel="noreferrer">查看来源</a> · 核对于 {modelInfo.checked_at}。可按渠道实际限制调整。</> : '暂无该模型的已核实上下文，请按服务商说明填写；留空使用 128K 估算。'} 上下文用于对话用量估算，不是单次输出上限。</p>
                    <p className="field-help">参考清单不代表账户有调用权限；以获取结果和连接测试为准。切换模型会重新匹配上下文。</p>
                  </div>
                </details>
              </section>
              <details className="channel-advanced">
                <summary>高级配置 <span>批量模型清单 · 向量模型</span></summary>
                <div className="channel-advanced-body">
                  <div className="model-picker-heading"><label className="field-label" htmlFor="channel-models">对话模型 · {splitModels(draft.modelsText).length} 个</label><button type="button" className="text-button" disabled={!preset?.models.length} onClick={() => { update({ modelsText: (preset?.models ?? []).join('\n') }); setLists(null) }}>填入服务商预设</button></div>
                  <textarea id="channel-models" rows={4} value={draft.modelsText} onChange={event => update({ modelsText: event.target.value })} placeholder="支持每行一个、逗号或分号分隔，自动去重" spellCheck={false} />
                  <p className="field-help">保存时自动加入默认模型，最多保留 200 个模型。</p>
                  <label className="field-label" htmlFor="channel-embeddings">向量模型 <span className="muted">供语义检索选用</span></label>
                  <textarea id="channel-embeddings" rows={2} value={draft.embeddingsText} onChange={event => update({ embeddingsText: event.target.value })} placeholder={(preset?.embeddings ?? []).join('\n') || '例如 BAAI/bge-m3'} spellCheck={false} />
                </div>
              </details>
              {formError && <p className="field-help key-warning" role="alert">{formError}</p>}
              <div className="model-settings-actions channel-save-bar">
                <span className="body-small muted save-state"><span className={`status-dot ${formDirty ? 'warn' : 'ok'}`} aria-hidden="true" />{draft.id ? (formDirty ? '有未保存的更改' : '所有更改已保存') : '新建草稿，尚未保存'} · {draft.model ? `${draft.model} · ${draft.context_tokens ? contextLabel(draft.context_tokens) : '128K 估算'}` : '可先保存渠道，稍后配置模型'}</span>
                <button type="button" className="button secondary" disabled={!draft.url.trim() || !draft.model.trim() || needsKey} onClick={() => void action('test')}>{busy === 'test' && <span className="spinner" aria-hidden="true" />}{busy === 'test' ? '正在测试…' : '测试连接'}</button>
                <button type="button" className="button primary" disabled={!canSave} onClick={() => void save()}>{busy === 'save' && <span className="spinner" aria-hidden="true" />}{busy === 'save' ? '正在保存…' : '保存渠道'}</button>
              </div>
            </fieldset>
          </section>
            : selected ? <section className="paper-panel channel-editor" aria-label={`渠道 ${selected.name}`}>
              <div className="section-heading"><div><h2>{selected.name}</h2><p className="body-small muted">{hostOf(selected.url)} · {selected.protocol === 'anthropic' ? 'Anthropic' : 'OpenAI 兼容'}</p></div>
                {defaultChannelId === selected.id && <span className="badge good"><span />对话默认</span>}</div>
              <dl className="channel-summary">
                <div><dt>默认模型</dt><dd>{selected.model || '未设置'}</dd></div>
                <div><dt>对话模型</dt><dd>{selected.models.length ? `${selected.models.length} 个` : '未登记'}</dd></div>
                <div><dt>向量模型</dt><dd>{(selected.embeddings ?? []).length ? `${selected.embeddings!.length} 个` : '未登记'}</dd></div>
                <div><dt>上下文窗口</dt><dd>{selected.context_tokens ? contextLabel(selected.context_tokens) : '按 128K 估算'}</dd></div>
                <div><dt>密钥</dt><dd>{selected.key_set ? '已保存（不回显）' : '未保存'}</dd></div>
                <div><dt>连接测试</dt><dd>{testedIds.has(selected.id) ? '本次会话已通过' : '尚未测试'}</dd></div>
              </dl>
              <p className="body-small muted">功能绑定直接使用这条渠道的密钥；轮换密钥后无需改任何功能。</p>
              <div className="model-settings-actions">
                <button type="button" className="button primary" disabled={Boolean(busy) || defaultChannelId === selected.id} onClick={() => void setAsDefault(selected)}>{busy === `activate-${selected.id}` ? '设置中…' : defaultChannelId === selected.id ? '当前对话默认' : '设为对话默认'}</button>
                <button type="button" className="button secondary" onClick={() => edit(selected)}>编辑</button>
              </div>
              <div className="channel-danger-zone">
                <span className="body-small muted">危险操作</span>
                <button type="button" className="button secondary danger" disabled={Boolean(busy)} onClick={() => requestRemove(selected)}>删除渠道</button>
                {confirmId === selected.id && <div className="inline-confirm" role="alert">
                  <p><strong>删除后，4 个功能的绑定将回退默认配置。</strong> 此操作不可撤销。</p>
                  <div className="inline-confirm-actions">
                    <button type="button" className="button secondary small" onClick={() => setConfirmId('')}>取消</button>
                    <button type="button" className="button danger small" disabled={Boolean(busy)} onClick={() => void remove(selected)}>{busy === `delete-${selected.id}` ? '删除中…' : '确认删除'}</button>
                  </div>
                </div>}
              </div>
            </section>
              : <section className="paper-panel channel-editor empty"><h3>选择或新建一条渠道</h3><p className="body-small muted">渠道保存服务地址、密钥与模型列表；「功能与模型」页把每个功能指向某条渠道的某个模型。</p></section>}
        </section>}

        {tab === 'purposes' && <section id="purposes-panel" role="tabpanel" aria-labelledby="purposes-tab" className="purposes-layout" aria-label="功能与模型">
          {bindings === null && <div className="paper-panel"><p className="body-small muted" role="status">正在读取功能绑定…</p></div>}
          {bindings && <>
            <div className="paper-panel binding-matrix">
              <div className="section-heading"><div><h2>功能与模型</h2><p className="body-small muted">一屏总览 4 个功能各自使用哪条渠道的哪个模型，点击「调整」只展开当前一项。</p></div><span className="badge good"><span />{configuredPurposes} / {PURPOSE_ORDER.length} 已配置</span></div>
              <div className="binding-matrix-scroll">
                <table>
                  <thead><tr><th scope="col">功能</th><th scope="col">模型来源</th><th scope="col">渠道</th><th scope="col">模型</th><th scope="col">状态</th><th scope="col">操作</th></tr></thead>
                  <tbody>{PURPOSE_ORDER.map(purpose => {
                    const entry = bindings[purpose]
                    const configured = entry.resolved.configured
                    const channelLabel = entry.channel_missing ? '渠道已删除' : entry.channel_name || (entry.resolved.mode === 'local' ? '本机' : entry.binding?.channel_id ? '已选渠道' : '全局默认配置')
                    return <tr key={purpose}>
                      <td data-label="功能"><strong>{PURPOSE_META[purpose].label}</strong></td>
                      <td data-label="模型来源">{purposeSource(entry.binding?.kind)}</td>
                      <td data-label="渠道">{channelLabel}</td>
                      <td data-label="模型">{entry.resolved.model || '未设置'}</td>
                      <td data-label="状态"><span className={`badge ${configured ? 'good' : ''}`}><span />{configured ? '已配置' : entry.resolved.mode === 'disabled' ? '已关闭' : '未配置'}</span></td>
                      <td data-label="操作"><button type="button" className="text-button" aria-expanded={expandedPurpose === purpose} onClick={() => setExpandedPurpose(current => current === purpose ? null : purpose)}>{expandedPurpose === purpose ? '收起' : '调整'}</button></td>
                    </tr>
                  })}</tbody>
                </table>
              </div>
            </div>
            {expandedPurpose ? <PurposeCard
              key={expandedPurpose}
              purpose={expandedPurpose}
              meta={PURPOSE_META[expandedPurpose]}
              entry={bindings[expandedPurpose]}
              channels={channels ?? []}
              metadata={metadata}
              inFlight={inFlight.current}
              onCollapse={() => setExpandedPurpose(null)}
              onChange={binding => updatePurpose(expandedPurpose, binding)} />
              : <p className="binding-placeholder"><span className="status-dot off" aria-hidden="true" />4 个功能配置均已收起，点击上表「调整」只展开对应一项，再点一次可收起。</p>}
          </>}
        </section>}
        <p className="model-footer-note">功能绑定直接使用渠道的密钥；轮换密钥后无需改任何功能。密钥仅保存在本机，不回显。上下文窗口用于对话用量估算，不是单次输出上限。</p>
      </div>}
  </>
}

function PurposeCard({ purpose, meta, entry, channels, metadata, inFlight, onChange, onCollapse }: {
  purpose: ModelPurpose
  meta: { label: string; description: string; allow: ('inherit' | 'channel' | 'local' | 'disabled')[] }
  entry: BindingEntry
  channels: LlmChannel[]
  metadata: Record<string, ModelMetadata>
  inFlight: boolean
  onChange: (binding: BindingDraft) => Promise<void>
  onCollapse: () => void
}) {
  const [mode, setMode] = useState<string>(() => entry.binding?.kind ?? (meta.allow.includes('inherit') ? 'inherit' : 'channel'))
  const [channelId, setChannelId] = useState(entry.binding?.channel_id ?? '')
  const [model, setModel] = useState(entry.binding?.model ?? '')
  const [localModel, setLocalModel] = useState(entry.binding?.local_model ?? '')
  const [modelQuery, setModelQuery] = useState('')
  const [dirty, setDirty] = useState(false)
  const [error, setError] = useState('')
  const firstRender = useRef(true)
  useEffect(() => {
    if (firstRender.current) { firstRender.current = false; return }
    setMode(entry.binding?.kind ?? (meta.allow.includes('inherit') ? 'inherit' : 'channel'))
    setChannelId(entry.binding?.channel_id ?? '')
    setModel(entry.binding?.model ?? '')
    setLocalModel(entry.binding?.local_model ?? '')
    setModelQuery('')
    setDirty(false)
  }, [entry.binding])

  function summary() {
    if (entry.channel_missing) return '渠道已删除：正在回退默认配置，请重新绑定'
    if (entry.resolved.mode === 'disabled') return '已关闭'
    if (entry.resolved.mode === 'local') return `本地模型 · ${entry.resolved.model || entry.binding?.local_model || '未设置'}`
    const name = entry.channel_name || (entry.binding?.channel_id ? '已选渠道' : '全局默认配置')
    const context = entry.resolved.context_tokens ? ` · ${contextLabel(entry.resolved.context_tokens)}` : ''
    return entry.resolved.model ? `${name} · ${entry.resolved.model}${context}` : `${name} · 未选择模型`
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
  const activeChannel = channelById2(channels, channelId)
  const candidates = [...new Set(purpose === 'embedding' ? [...(activeChannel?.embeddings ?? []), ...(activeChannel?.models ?? [])] : (activeChannel?.models ?? []))]
  const isVector = (id: string) => purpose === 'embedding' && (activeChannel?.embeddings ?? []).includes(id) && !(activeChannel?.models ?? []).includes(id)
  const matches = candidates.filter(item => item.toLowerCase().includes(modelQuery.trim().toLowerCase()))
  const current = model.trim()
  const pinned = Boolean(current) && !candidates.includes(current)
  const customId = modelQuery.trim() !== '' && !candidates.some(item => item.toLowerCase() === modelQuery.trim().toLowerCase())
  const modelInfo = metadata[current]
  const modeLabel = (kind: string) => kind === 'channel' ? '使用渠道模型' : kind === 'local' ? '本地缓存模型' : kind === 'disabled' ? '关闭此功能' : '跟随对话默认'

  function pickModel(value: string) { setModel(value); setDirty(true) }

  return <section className="paper-panel purpose-card" aria-label={meta.label}>
    <div className="section-heading"><div><h2>{meta.label}</h2><p className="body-small muted">{meta.description}</p></div>
      <div className="purpose-heading-actions">
        <span className={`badge ${entry.resolved.configured ? 'good' : ''}`}><span />{entry.resolved.configured ? '已配置' : entry.resolved.mode === 'disabled' ? '已关闭' : '未配置'}</span>
        <button type="button" className="text-button" onClick={onCollapse}>收起</button>
      </div></div>
    <p className="body-small purpose-summary"><strong>当前生效：</strong>{summary()}{entry.resolved.configured && !entry.resolved.context_tokens ? ' · 按 128K 估算' : ''}</p>
    <div className="purpose-controls">
      <label className="field-label" id={`purpose-${purpose}-mode-label`}>模型来源</label>
      <div className="mode-picks" role="group" aria-labelledby={`purpose-${purpose}-mode-label`}>
        {meta.allow.map(kind => <button type="button" key={kind} aria-pressed={mode === kind} disabled={inFlight} onClick={() => { setMode(kind); setDirty(true) }}>{modeLabel(kind)}</button>)}
      </div>
      {mode === 'channel' && <>
        <label className="field-label" htmlFor={`purpose-${purpose}-channel`}>渠道</label>
        <select id={`purpose-${purpose}-channel`} value={channelId} onChange={event => { setChannelId(event.target.value); const next = channels.find(item => item.id === event.target.value); setModel(next?.model ?? ''); setModelQuery(''); setDirty(true) }} disabled={inFlight}>
          <option value="">{channelId && !selectable.some(item => item.id === channelId) ? '渠道已删除，请重新选择' : '请选择渠道'}</option>
          {selectable.map(item => <option key={item.id} value={item.id}>{item.name} · {item.models.length} 个模型</option>)}
        </select>
        {channelId ? <>
          <div className="model-picker-heading"><span className="body-small muted">{candidates.length ? `${candidates.length} 个可选模型` : '该渠道还没有登记模型'}</span></div>
          {candidates.length > 0 && <>
            <label className="field-label" htmlFor={`purpose-${purpose}-search`}>搜索模型</label>
            <input id={`purpose-${purpose}-search`} type="search" value={modelQuery} onChange={event => setModelQuery(event.target.value)} placeholder="输入关键词筛选，不改变已选模型" autoComplete="off" />
          </>}
          {matches.length > 0 || pinned || customId ? <div className="model-options purpose-model-options" role="group" aria-label="可选模型">
            {pinned && <button type="button" aria-pressed="true" disabled={inFlight} onClick={() => pickModel(current)}><span>{current}</span><span>已保存 · 当前使用</span></button>}
            {matches.slice(0, 60).map(item => <button type="button" key={item} aria-pressed={current === item} disabled={inFlight} onClick={() => pickModel(item)}><span>{item}</span><span>{isVector(item) ? '向量 · ' : ''}{metadata[item] ? `${contextLabel(metadata[item].context_tokens)} · ` : ''}{current === item ? '已选择' : '选择'}</span></button>)}
            {customId && <button type="button" aria-pressed={current === modelQuery.trim()} disabled={inFlight} onClick={() => pickModel(modelQuery.trim())}><span>使用「{modelQuery.trim()}」</span><span>手动指定</span></button>}
          </div> : <p className="channel-model-empty">{candidates.length ? '没有匹配项，可输入完整 ID 后点「使用」直接指定。' : '先到「模型渠道」给这条渠道登记模型，或输入完整 ID 直接指定。'}</p>}
          {matches.length > 60 && <p className="field-help">显示前 60 / {matches.length} 个结果，请输入关键词缩小范围。</p>}
          <label className="field-label" htmlFor={`purpose-${purpose}-model`}>模型 ID</label>
          <input id={`purpose-${purpose}-model`} value={model} onChange={event => pickModel(event.target.value)} placeholder="点击上方模型，或直接输入 ID" maxLength={1000} autoComplete="off" spellCheck={false} disabled={inFlight} />
          {modelInfo && <p className="field-help">官方参考上下文 {contextLabel(modelInfo.context_tokens)} tokens，核对于 {modelInfo.checked_at}。</p>}
          {purpose === 'embedding' && <p className="body-small muted">清单合并了这条渠道登记的向量与对话模型，向量条目已标注。</p>}
        </> : <p className="body-small muted">{selectable.length ? '先选择一条渠道，再挑模型。' : '还没有可用渠道，先到「模型渠道」新建一条。'}</p>}
      </>}
      {mode === 'local' && <>
        <label className="field-label" htmlFor={`purpose-${purpose}-local`}>本地缓存模型</label>
        <input id={`purpose-${purpose}-local`} value={localModel} onChange={event => { setLocalModel(event.target.value); setDirty(true) }} placeholder="BAAI/bge-small-zh-v1.5" maxLength={1000} spellCheck={false} disabled={inFlight} />
      </>}
    </div>
    {error && <p className="field-help key-warning" role="alert">{error}</p>}
    <div className="model-settings-actions">
      <button type="button" className="button primary" disabled={!dirty || inFlight} onClick={() => void commit().catch(err => setError((err as Error).message || '保存失败'))}>{inFlight ? '保存中…' : '保存此功能'}</button>
      {dirty && <button type="button" className="button secondary" disabled={inFlight} onClick={() => {
        setMode(entry.binding?.kind ?? (meta.allow.includes('inherit') ? 'inherit' : 'channel')); setChannelId(entry.binding?.channel_id ?? ''); setModel(entry.binding?.model ?? ''); setLocalModel(entry.binding?.local_model ?? ''); setModelQuery(''); setDirty(false); setError('')
      }}>还原</button>}
    </div>
  </section>
}

function channelById2(channels: LlmChannel[], id: string) { return channels.find(item => item.id === id) }
