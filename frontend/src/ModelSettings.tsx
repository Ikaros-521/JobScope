import { useEffect, useState } from 'react'
import { api, post } from './api'
import type { ModelProvider } from './api'
import { toast } from './toast'
import { navigateTabs } from './tabNavigation'
import { LlmChannelManager } from './LlmChannelManager'

type Protocol = 'openai' | 'anthropic'
type Provider = { mode: 'disabled' | 'remote' | 'local'; channel: string; protocol: Protocol; url: string; model: string; local_model: string; key_set: boolean; source: string; context_tokens?: number | null; saved_channel_id?: string }
type Draft = Omit<Provider, 'context_tokens'> & { key: string; clear_key: boolean; context_tokens: number | '' }
type Target = 'llm' | 'embedding'
type Settings = Record<Target, Provider>
type Channel = { id: string; name: string; url: string; protocol: Protocol; models: string[]; embeddings: string[] | null; note?: string }
type ModelList = { models: string[]; source: 'live'; message: string }
type ActionStatus = { kind: 'success' | 'error'; message: string }
const targets: Target[] = ['llm', 'embedding']
const labels = { llm: '对话与分析模型', embedding: '语义检索模型' }
const descriptions = { llm: '问题规划、资料问答与简历适配分析', embedding: '向量召回，与全文检索融合' }
const draftOf = (value: Provider): Draft => ({ ...value, key: '', clear_key: false, context_tokens: value.context_tokens ?? '' })
const payload = (value: Draft) => ({ mode: value.mode, channel: value.channel, protocol: value.protocol, url: value.url.trim(), model: value.model.trim(), local_model: value.local_model.trim(), key: value.key.trim(), clear_key: value.clear_key, context_tokens: value.context_tokens === '' ? null : value.context_tokens })
function resolvedUrl(value: Draft, target: Target) {
  const url = value.url.trim().replace(/\/$/, '')
  const explicitEndpoint = /\/(chat\/completions|messages|embeddings|models)$/.test(url)
  const root = url.replace(/\/(chat\/completions|messages|embeddings|models)$/, '')
  if (!root) return ''
  try {
    const base = new URL(root)
    if ((!base.pathname || base.pathname === '/') && !explicitEndpoint) base.pathname = '/v1'
    return base.toString().replace(/\/$/, '') + (target === 'embedding' ? '/embeddings' : value.protocol === 'anthropic' ? '/messages' : '/chat/completions')
  } catch { return value.url.trim() }
}

export function ModelSettings({ onSaved }: { onSaved: () => Promise<void> }) {
  const [saved, setSaved] = useState<Settings | null>(null)
  const [drafts, setDrafts] = useState<Record<Target, Draft> | null>(null)
  const [channels, setChannels] = useState<Channel[]>([])
  const [lists, setLists] = useState<Partial<Record<Target, ModelList>>>({})
  const [busy, setBusy] = useState('')
  const [selectedTarget, setSelectedTarget] = useState<Target>('llm')
  const [status, setStatus] = useState<Partial<Record<Target, ActionStatus>>>({})
  const [loadError, setLoadError] = useState<string | null>(null)
  const [loadAttempt, setLoadAttempt] = useState(0)
  useEffect(() => {
    let active = true
    void Promise.all([api<Settings>('/settings/models'), api<{ channels: Channel[] }>('/settings/channels')]).then(([value, catalog]) => {
      if (active) { setSaved(value); setDrafts({ llm: draftOf(value.llm), embedding: draftOf(value.embedding) }); setChannels(catalog.channels) }
    }).catch(err => {
      if (active) {
        const message = (err as Error).message || '读取模型配置失败，请重试。'
        setLoadError(message)
        toast(message, 'error')
      }
    })
    return () => { active = false }
  }, [loadAttempt])
  function edit(target: Target, changes: Partial<Draft>) {
    setDrafts(current => current ? { ...current, [target]: { ...current[target], ...changes } } : current)
    setStatus(current => ({ ...current, [target]: undefined }))
    if (['channel', 'protocol', 'url', 'key', 'clear_key', 'mode'].some(key => key in changes)) setLists(current => ({ ...current, [target]: undefined }))
  }
  function feedback(target: Target, message: string, kind: ActionStatus['kind'] = 'success') {
    setStatus(current => ({ ...current, [target]: { kind, message } }))
    toast(message, kind === 'error' ? 'error' : undefined)
  }
  async function applyActivated(settings: Record<'llm' | 'embedding', ModelProvider>) {
    const normalise = (value: ModelProvider): Provider => ({ ...value, mode: value.mode as Provider['mode'], protocol: value.protocol })
    const next = { llm: normalise(settings.llm), embedding: normalise(settings.embedding) }
    setSaved(next)
    setDrafts({ llm: draftOf(next.llm), embedding: draftOf(next.embedding) })
    await onSaved()
  }
  function chooseChannel(target: Target, channel: Channel) {
    edit(target, { channel: channel.id, protocol: channel.protocol, url: channel.url, model: '', key: '', clear_key: false })
  }
  async function action(target: Target, operation: 'save' | 'test' | 'discover') {
    if (!drafts || busy) return
    setBusy(`${target}-${operation}`)
    setStatus(current => ({ ...current, [target]: undefined }))
    try {
      if (operation === 'discover') {
        const result = await post<ModelList>('/settings/models/discover', { target, config: payload(drafts[target]) })
        setLists(current => ({ ...current, [target]: result }))
        feedback(target, result.models.length ? `已获取 ${result.models.length} 个模型，输入名称即可筛选。` : '渠道返回空列表，可手动输入模型 ID 后测试。')
      } else if (operation === 'test') {
        const result = await post<{ duration_ms: number; dimensions: number | null }>('/settings/models/test', { target, config: payload(drafts[target]) })
        feedback(target, `连接成功 · ${result.duration_ms} ms${result.dimensions ? ` · ${result.dimensions} 维` : ''}。测试未保存配置。`)
      } else {
        const result = await api<Settings>('/settings/models', { method: 'PUT', body: JSON.stringify({ [target]: payload(drafts[target]) }) })
        setSaved(result); setDrafts(current => current ? { ...current, [target]: draftOf(result[target]) } : current)
        feedback(target, '已保存，后续请求立即使用此配置。')
        await onSaved()
      }
    } catch (err) { feedback(target, (err as Error).message || '操作失败，请重试。', 'error') }
    finally { setBusy('') }
  }
  return <><section className="page-intro"><div><h1>模型设置</h1><p>选择渠道，填入密钥，找到要使用的模型。</p></div><span className="revision-label">保存在本机</span></section>
    {!drafts || !saved ? <section className="paper-panel model-settings-loading" aria-busy={!loadError}>
      {loadError ? <><div role="alert"><h2>暂时无法读取模型配置</h2><p>{loadError}</p></div><button className="button secondary" type="button" onClick={() => { setLoadError(null); setLoadAttempt(current => current + 1) }}>重新加载</button></> : <p role="status"><span className="spinner" aria-hidden="true" />正在读取模型配置与服务渠道…</p>}
    </section> : <div className="model-settings-layout">
      <aside className="model-settings-sidebar">
        <div className="model-settings-nav" role="tablist" aria-label="模型用途" aria-orientation="vertical" onKeyDown={navigateTabs}>
          {targets.map(target => {
            const stored = saved[target]
            return <button className="model-target" type="button" key={target} id={`${target}-settings-tab`} role="tab" aria-controls={`${target}-settings-panel`} aria-selected={selectedTarget === target} tabIndex={selectedTarget === target ? 0 : -1} onClick={() => setSelectedTarget(target)}>
              <span className="model-target-title">{labels[target]}</span>
              <span className="model-target-description">{descriptions[target]}</span>
              <span className="model-target-summary">{stored.mode === 'disabled' ? '未启用' : `${stored.mode === 'local' ? '本地模型' : 'API 服务'} · ${(stored.mode === 'local' ? stored.local_model : stored.model) || '未选择模型'}`}</span>
            </button>
          })}
        </div>
        <details className="model-settings-guide"><summary>连接帮助与使用说明</summary><h3>三个步骤即可连接</h3><ol><li>选择服务渠道与协议</li><li>填入该渠道的 API 密钥</li><li>获取模型，搜索选择后测试</li></ol><p>测试成功后保存，后续问答和分析立即使用新配置。测试发送固定文本，不包含岗位或简历。</p><h3>自定义渠道</h3><p>中转平台、企业网关和本机服务均可接入。选择实际支持的 OpenAI 或 Anthropic 协议，填入服务地址。</p><h3>模型列表</h3><p>在线列表取决于渠道支持与账户权限。预设只作参考；列表不可用时，直接输入控制台的模型 ID 后测试。</p><h3>语义检索</h3><p>保存向量模型后，到“数据与采集”构建语义向量。未构建或服务不可用时回退全文检索。</p></details>
        <p className="model-settings-warning"><strong>数据与密钥</strong><br />外部模型会接收问题及相关材料。密钥存于本机数据库，未加密，页面不读回密钥。</p>
      </aside>
      <div className="model-settings-forms">{targets.map(target => {
      const draft = drafts[target], stored = saved[target]
      const channel = channels.find(item => item.id === draft.channel)
      const dirty = JSON.stringify(payload(draft)) !== JSON.stringify(payload(draftOf(stored)))
      const keyChangedHost = stored.key_set && resolvedUrl(draft, target) !== resolvedUrl(draftOf(stored), target) && !draft.key && !draft.clear_key
      const validConnection = Boolean(draft.url.trim() && !keyChangedHost)
      const valid = draft.mode === 'disabled' || (draft.mode === 'local' ? Boolean(draft.local_model.trim()) : validConnection && Boolean(draft.model.trim()))
      const modelList = lists[target]
      const models = modelList?.models ?? (target === 'llm' ? channel?.models : channel?.embeddings) ?? []
      const matches = models.filter(model => model.toLowerCase().includes(draft.model.trim().toLowerCase()))
      const currentStatus = status[target]
      return <form className="paper-panel model-settings-card" key={target} id={`${target}-settings-panel`} role="tabpanel" aria-labelledby={`${target}-settings-tab`} hidden={selectedTarget !== target} tabIndex={0} aria-busy={busy.startsWith(`${target}-`)} onSubmit={event => { event.preventDefault(); void action(target, 'save') }}>
        <div className="section-heading"><div><h2>{labels[target]}</h2><p className="body-small muted">{descriptions[target]}</p></div><span className={`badge ${stored.mode !== 'disabled' ? 'good' : ''}`}><span />{stored.mode === 'disabled' ? '未启用' : stored.mode === 'local' ? '本地模型' : '已配置'}</span></div>
        {target === 'llm' && <LlmChannelManager activeLlm={stored} onActivated={applyActivated} />}
        <fieldset disabled={Boolean(busy)}>
          <section className="model-form-section" aria-labelledby={`${target}-connection-heading`}>
            <h3 className="model-section-heading" id={`${target}-connection-heading`}><span>01</span> 服务连接</h3>
            <label className="field-label" htmlFor={`${target}-mode`}>连接方式</label><select id={`${target}-mode`} value={draft.mode} onChange={event => edit(target, { mode: event.target.value as Draft['mode'] })}><option value="disabled">{target === 'llm' ? '不启用 · 仅检索摘录' : '不启用 · 使用 BM25'}</option><option value="remote">API 服务 · 选择渠道</option>{target === 'embedding' && <option value="local">本地缓存模型 · 离线运行</option>}</select>
            {draft.mode === 'disabled' && <div className="model-disabled-note"><strong>无需连接，也能检索资料</strong><p>{target === 'llm' ? '不启用对话与分析模型时，仍可检索资料并查看原文摘录；不会生成模型回答。' : '不启用语义检索时，仍可使用 BM25 全文检索，无需向量模型。'}选择连接方式后即可继续配置；停用已有配置需保存。</p></div>}
            {draft.mode === 'remote' && <>
              <div className="field-label" id={`${target}-channel-label`}>服务渠道</div>
              <div className="model-channels" role="group" aria-labelledby={`${target}-channel-label`}>{channels.filter(item => target === 'llm' || item.embeddings !== null).map(item => <button key={item.id} type="button" aria-pressed={draft.channel === item.id} className="model-channel" onClick={() => chooseChannel(target, item)}>{item.name}<span aria-hidden="true">{draft.channel === item.id ? '✓' : ''}</span></button>)}</div>
              <div className="model-connection-grid">
                <div><label className="field-label" htmlFor={`${target}-protocol`}>接口协议</label><select id={`${target}-protocol`} value={draft.protocol} onChange={event => edit(target, { protocol: event.target.value as Protocol })}><option value="openai">OpenAI 兼容 · Chat Completions{target === 'embedding' ? ' / Embeddings' : ''}</option>{target === 'llm' && <option value="anthropic">Anthropic · Messages（A 社）</option>}</select><p className="field-help">已按渠道预选协议。中转或私有网关请选择实际支持的协议。</p></div>
                <details className="model-advanced" key={draft.channel} open={draft.channel === 'custom' ? true : undefined}><summary>{draft.channel === 'custom' ? '自定义服务地址' : '高级设置 · 服务地址'}</summary><label className="field-label" htmlFor={`${target}-url`}>服务地址</label><input id={`${target}-url`} type="url" value={draft.url} onChange={event => edit(target, { url: event.target.value })} placeholder="https://api.example.com/v1" maxLength={2000} required autoComplete="off" spellCheck={false} /><p className="field-help">支持 Base URL 或完整接口地址；自动补齐对应协议的端点。本机服务支持 HTTP。</p></details>
              </div>
              <label className="field-label" htmlFor={`${target}-key`}>API 密钥 <span className="muted">{stored.key_set && !keyChangedHost ? '· 留空保留已保存密钥' : '· 本机免认证服务可留空'}</span></label><input id={`${target}-key`} type={'pass' + 'word'} value={draft.key} onChange={event => edit(target, { key: event.target.value, clear_key: false })} placeholder="填写渠道的 API Key" maxLength={4000} autoComplete={'new-pass' + 'word'} spellCheck={false} />
              {stored.key_set && <label className="model-key-clear"><input type="checkbox" checked={draft.clear_key} onChange={event => edit(target, { clear_key: event.target.checked, key: '' })} />清除已保存密钥</label>}{keyChangedHost && <p className="field-help key-warning">已更换服务地址或协议，请重新填写密钥，或选择清除密钥。</p>}
              {target === 'llm' && <>
                <label className="field-label" htmlFor={`${target}-context`}>上下文窗口 tokens <span className="muted">· 仅用于对话页的上下文用量估算，留空按 128000 估算</span></label>
                <input id={`${target}-context`} type="number" min={4000} step={1000} value={draft.context_tokens} onChange={event => edit(target, { context_tokens: event.target.value === '' ? '' : Number(event.target.value) })} placeholder="128000" />
              </>}
            </>}
          </section>
          {draft.mode === 'remote' && <section className="model-form-section" aria-labelledby={`${target}-model-heading`}>
            <h3 className="model-section-heading" id={`${target}-model-heading`}><span>02</span> 选择模型</h3>
            <div className="model-picker-heading"><label className="field-label" htmlFor={`${target}-model`}>搜索模型或输入 ID</label><button className="button secondary" type="button" disabled={!validConnection} onClick={() => void action(target, 'discover')}>{busy === `${target}-discover` && <span className="spinner" aria-hidden="true" />}{busy === `${target}-discover` ? '正在获取…' : '获取模型列表'}</button></div>
            <input id={`${target}-model`} value={draft.model} onChange={event => edit(target, { model: event.target.value })} placeholder="输入名称筛选，或直接填写完整模型 ID" required maxLength={300} autoComplete="off" spellCheck={false} aria-describedby={`${target}-model-help`} />
            <div className="model-list-label">{modelList ? `账户在线列表 · ${models.length} 个` : '渠道预设 · 仅供参考，非账户权限列表'}<span>{matches.length} 个匹配</span></div>
            {matches.length > 0 && <div className="model-options" role="group" aria-label="模型搜索结果">{matches.slice(0, 100).map(model => <button type="button" key={model} aria-pressed={draft.model === model} onClick={() => edit(target, { model })}><span>{model}</span><span aria-hidden="true">{draft.model === model ? '✓' : '选择'}</span></button>)}</div>}
            <p className="field-help" id={`${target}-model-help`}>{matches.length === 0 ? '没有匹配结果。可直接使用输入的模型 ID，点击测试确认。' : matches.length > 100 ? '仅展示前 100 个结果，请继续输入名称缩小范围。' : '点击结果选用；也可以直接使用输入的模型 ID。'}{target === 'embedding' && modelList ? '在线列表可能包含对话模型，请选择向量模型并测试。' : ''}{channel?.note}</p>
          </section>}
          {draft.mode === 'local' && <section className="model-form-section model-local-fields" aria-labelledby={`${target}-local-heading`}>
            <h3 className="model-section-heading" id={`${target}-local-heading`}><span>02</span> 本地模型</h3>
            <label className="field-label" htmlFor={`${target}-local`}>已缓存模型名称或路径</label><input id={`${target}-local`} value={draft.local_model} onChange={event => edit(target, { local_model: event.target.value })} placeholder="BAAI/bge-small-zh-v1.5" required maxLength={1000} spellCheck={false} /><p className="field-help">读取本机已有缓存。测试会加载模型，首次可能稍慢。</p>
          </section>}
          <section className="model-form-section model-settings-footer" aria-labelledby={`${target}-save-heading`}>
            <h3 className="model-section-heading" id={`${target}-save-heading`}><span>{draft.mode === 'disabled' ? '02' : '03'}</span> {draft.mode === 'disabled' ? '保存设置' : '测试与保存'}</h3>
            {draft.mode !== 'disabled' && <p className="field-help">测试仅发送固定文本，不包含岗位或简历；测试成功后仍需保存。</p>}
            {currentStatus && <p className={`model-settings-status ${currentStatus.kind}`} role={currentStatus.kind === 'error' ? 'alert' : 'status'} aria-atomic="true">{currentStatus.message}</p>}
            <div className="model-settings-actions"><span className="muted body-small">{dirty ? '有未保存修改' : stored.source === 'environment' ? '当前来自启动环境' : stored.source === 'saved' ? '当前使用本机保存配置' : '尚未设置'}</span><button className="button secondary" type="button" disabled={!valid || draft.mode === 'disabled'} onClick={() => void action(target, 'test')}>{busy === `${target}-test` && <span className="spinner" aria-hidden="true" />}{busy === `${target}-test` ? '正在测试…' : '测试连接'}</button><button className="button primary" type="submit" disabled={!valid || !dirty}>{busy === `${target}-save` && <span className="spinner" aria-hidden="true" />}{busy === `${target}-save` ? '正在保存…' : '保存设置'}</button></div>
          </section>
        </fieldset>
      </form>
    })}</div></div>}
  </>
}
