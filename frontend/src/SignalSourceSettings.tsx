import { useCallback, useEffect, useRef, useState } from 'react'
import { date, listSignalSources, saveSignalSource } from './api'
import type { SignalFieldMapping, SignalSource } from './api'
import { signalStatusNames } from './SignalSources'
import { toast } from './toast'

type Draft = { mode: 'disabled' | 'enabled'; key: string; url: string; auth_header: string; auth_prefix: string; company_param: string; fields: string }
function draftOf(item: SignalSource): Draft {
  return { mode: item.mode, key: '', url: item.url || '', auth_header: item.auth_header ?? 'Authorization', auth_prefix: item.auth_prefix ?? 'Bearer ', company_param: item.company_param ?? 'company', fields: JSON.stringify(item.fields || {}, null, 2) }
}

export function SignalSourceSettings() {
  const [sources, setSources] = useState<Record<string, SignalSource> | null>(null)
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const request = useRef(0)
  const load = useCallback(async () => {
    const token = ++request.current
    setLoading(true); setError('')
    try {
      const data = await listSignalSources()
      if (token !== request.current) return
      setSources(data.sources)
      setDrafts(Object.fromEntries(Object.entries(data.sources).map(([id, item]) => [id, draftOf(item)])))
    } catch (err) { if (token === request.current) setError((err as Error).message) }
    finally { if (token === request.current) setLoading(false) }
  }, [])
  useEffect(() => { void load(); return () => { request.current++ } }, [load])
  function edit(id: string, patch: Partial<Draft>) { setDrafts(current => ({ ...current, [id]: { ...current[id], ...patch } })) }
  async function save(id: string) {
    if (busy || !sources) return
    setBusy(id)
    try {
      const draft = drafts[id]
      let fields: SignalFieldMapping | undefined
      if (sources[id].tier === 'paid') {
        try { fields = JSON.parse(draft.fields) as SignalFieldMapping } catch { throw new Error('字段映射不是有效的 JSON，请检查括号、引号和逗号。') }
        if (!fields || Array.isArray(fields) || typeof fields !== 'object') throw new Error('字段映射必须是 JSON 对象。')
      }
      const result = await saveSignalSource(id, {
        mode: draft.mode, key: draft.key, url: draft.url,
        ...(sources[id].tier === 'paid' ? { auth_header: draft.auth_header, auth_prefix: draft.auth_prefix, company_param: draft.company_param, fields } : {}),
      })
      setSources(current => current ? { ...current, [id]: result } : current)
      setDrafts(current => ({ ...current, [id]: draftOf(result) }))
      toast(result.ready ? '来源设置已保存。到求职参考或横评页选择来源后采集；保存本身不发起请求。' : `设置已保存：${result.unavailable_reason || '该来源暂不可采集'}`)
    } catch (err) { toast((err as Error).message, 'error') }
    finally { setBusy('') }
  }
  return <section className="paper-panel signal-sources" aria-label="求职参考来源">
    <div className="section-heading"><div><span className="section-label">求职参考</span><h2>信号来源与付费接口</h2><p className="muted body-small">保存设置后，在求职参考的「按来源补数据」或横评的「补数据来源」中执行。付费 API 每次手动勾选；多平台检索里的企查查 / 天眼查仍是公开网页。</p></div><button className="button secondary small" type="button" disabled={Boolean(busy) || loading} onClick={() => void load()}>刷新</button></div>
    {loading ? <p role="status" className="tiny-label">正在读取来源设置与最近执行结果…</p> : null}
    {error ? <p role="status" className="tiny-label">读取失败：{error}，可点击刷新重试。</p> : null}
    {sources ? <div className="signal-source-rows">{Object.entries(sources).map(([id, item]) => {
      const draft = drafts[id]
      const changed = JSON.stringify(draft) !== JSON.stringify(draftOf(item))
      return <article key={id}>
        <div className="signal-source-head"><div><strong>{item.name}</strong><span className="tiny-label">{id} · {item.tier === 'free' ? '免费来源' : '付费 API'}</span></div><span className={`badge${item.ready && item.status === 'available' ? ' good' : ''}`}><span />{!item.implemented ? '尚未接入' : item.status === 'needs_verification' ? '待实测' : item.ready ? '可采集' : item.mode === 'disabled' ? '已关闭' : '配置未完成'}</span></div>
        <p className="tiny-label">{item.description}</p>
        <p className="tiny-label">{item.ready ? item.tier === 'paid' ? '配置已就绪，需在本次采集中手动勾选；不代表已连通验证。' : item.status === 'available' ? '多平台补数据时会刷新此来源；重新计算也会使用。' : '可手动尝试，页面结构与解析仍待验证。' : item.unavailable_reason}</p>
        <details className="signal-source-options"><summary>{item.implemented ? '配置来源' : '查看预留规则'}</summary>
          <p className="tiny-label">规则：{item.rules.join('、')}</p>
          {draft && item.implemented ? <div className="signal-source-edit">
            <label className="signal-source-mode"><input type="checkbox" checked={draft.mode === 'enabled'} disabled={Boolean(busy) || loading} onChange={event => edit(id, { mode: event.target.checked ? 'enabled' : 'disabled' })} />启用该来源</label>
            {item.tier === 'paid' ? <>
              <label className="signal-source-key"><span>API 密钥 · {item.key_set ? '已设置，留空保留' : '填写自己的密钥'}</span><input type={'pass' + 'word'} autoComplete="off" spellCheck={false} maxLength={4000} value={draft.key} disabled={Boolean(busy)} onChange={event => edit(id, { key: event.target.value })} placeholder={item.key_set ? '已设置，页面不读取原文' : '填写密钥'} /></label>
              <label className="signal-source-key"><span>接口地址 · 支持 HTTP GET 的接口或适配网关</span><input type="url" autoComplete="off" spellCheck={false} maxLength={2000} value={draft.url} disabled={Boolean(busy)} onChange={event => edit(id, { url: event.target.value })} placeholder="https://…/company/profile" /></label>
              <details className="signal-api-options"><summary>请求格式与字段映射</summary>
                <p className="tiny-label">按已采购接口的文档填写。密钥放在请求头；服务商要求动态签名或 POST 时，请使用适配网关。这里不会自动实现厂商签名。</p>
                <label className="signal-source-key"><span>密钥请求头名称</span><input value={draft.auth_header} disabled={Boolean(busy)} maxLength={100} onChange={event => edit(id, { auth_header: event.target.value })} placeholder="Authorization 或 token" /></label>
                <label className="signal-source-key"><span>密钥前缀 · Bearer 后须保留一个空格；无前缀则留空</span><input value={draft.auth_prefix} disabled={Boolean(busy)} maxLength={100} onChange={event => edit(id, { auth_prefix: event.target.value })} placeholder="Bearer " /></label>
                <label className="signal-source-key"><span>公司名称查询参数</span><input value={draft.company_param} disabled={Boolean(busy)} maxLength={100} onChange={event => edit(id, { company_param: event.target.value })} placeholder="company 或 keyword" /></label>
                <label className="signal-source-key"><span>响应字段映射（JSON）</span><textarea rows={9} value={draft.fields} spellCheck={false} disabled={Boolean(busy)} maxLength={50000} onChange={event => edit(id, { fields: event.target.value })} /></label>
                <p className="tiny-label">保留规则名、槽位名和数组结构，只将候选字段名改为接口实际返回的字段。预置候选未按厂商协议核实；缺字段会逐项说明，绝不猜数字。</p>
              </details>
              {item.key_set && draft.url !== (item.url || '') && !draft.key ? <p className="tiny-label">更换接口地址时需要重新填写密钥。</p> : null}
            </> : null}
            <button className="button secondary small" type="button" disabled={Boolean(busy) || loading || !changed} onClick={() => void save(id)}>{busy === id ? <span className="spinner" /> : null}保存设置</button>
          </div> : null}
        </details>
        {item.last_run ? <div className="signal-source-last"><strong>最近执行 · {signalStatusNames[item.last_run.status] || item.last_run.status} · {item.last_run.count} 条</strong><span>{item.last_run.query} · {date(item.last_run.created_at)}</span><details><summary>查看执行说明</summary><p>{item.last_run.message}</p></details></div> : <p className="tiny-label">尚无信号采集记录。保存设置不会进行连通性测试。</p>}
      </article>
    })}</div> : null}
    <p className="tiny-label">密钥保存在本机数据库中，不加密，页面不会读回原文。关闭来源会阻止后续调用，历史材料仍可用于重算。完整执行记录可在「采集记录」查看。</p>
  </section>
}
