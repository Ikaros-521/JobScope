import { useEffect, useState } from 'react'
import { acceptMergeProposal, api, dismissMergeProposal, getMergeOverview, mergeCompanies, splitCompanyAlias, startMergeScan } from './api'
import type { MergeOverview, MergeProposal } from './api'
import { toast, dismissToast } from './toast'

// 公司主体归并面板：招聘平台上同一家公司写法不一（缩写/全称/带城市前缀），归并后数据集中复用。
// 三层：规则（后缀/城市前缀/子串）+ LLM 判定产候选 → 这里人工确认；手动合并与拆分兜底。
// 子公司/关联公司刻意隔离不并——错合并比漏合并更糟，拆分按钮是错合并的退路。

function proposalJobs(proposal: MergeProposal, entities: MergeOverview['entities']) {
  const jobs = new Map(entities.map(item => [item.name, item.jobs_count]))
  return { a: jobs.get(proposal.company_a) ?? 0, b: jobs.get(proposal.company_b) ?? 0 }
}

export function CompanyMerge({ data, onUpdated }: { data: MergeOverview | null; onUpdated: (value: MergeOverview) => void }) {
  const [busy, setBusy] = useState('')
  const [flip, setFlip] = useState<Record<string, 'a' | 'b'>>({})
  const [manualA, setManualA] = useState('')
  const [manualB, setManualB] = useState('')
  const [manualConfirm, setManualConfirm] = useState(false)
  const [splitTarget, setSplitTarget] = useState<{ id: string; name: string; alias: string } | null>(null)

  useEffect(() => { if (!data) void getMergeOverview().then(onUpdated).catch(() => {}) }, [data]) // eslint-disable-line react-hooks/exhaustive-deps

  async function run(key: string, action: () => Promise<void>) {
    setBusy(key)
    try { await action() } catch (err) { toast((err as Error).message, 'error') } finally { setBusy('') }
  }

  function scan() {
    if (busy) return
    void run('scan', async () => {
      const started = await startMergeScan()
      const progress = toast('正在扫描合并候选（规则比对 + 模型判定，稍等片刻）…', 'notice', true)
      try {
        for (let poll = 0; poll < 150; poll++) {
          await new Promise(resolve => setTimeout(resolve, 2000))
          const { task } = await api<{ task: { status: string; message: string; count: number } }>(`/tasks/${encodeURIComponent(started.task.id)}`)
          if (task.status !== 'running' && task.status !== 'pending') {
            if (task.status === 'completed') toast(task.message)
            else toast(task.message || '归并扫描未完成，请稍后重试', 'error')
            break
          }
        }
      } finally {
        dismissToast(progress)
        onUpdated(await getMergeOverview())
      }
    })
  }

  function accept(id: string) {
    if (busy) return
    void run(id, async () => {
      const result = await acceptMergeProposal(id, flip[id] ?? 'auto')
      onUpdated({ entities: result.entities, proposals: result.proposals })
      toast(`已合并：${result.company.name}（岗位与资料已并入，参考报告已重算）`)
    })
  }

  function dismiss(id: string) {
    if (busy) return
    void run(id, async () => {
      const result = await dismissMergeProposal(id)
      onUpdated({ entities: data?.entities ?? [], proposals: result.proposals })
    })
  }

  function submitManual() {
    const a = manualA.trim(), b = manualB.trim()
    if (!a || !b || a === b) { toast('两家公司的名称都要填，且不能相同。'); return }
    void run('manual', async () => {
      const result = await mergeCompanies(a, b)
      onUpdated({ entities: result.entities, proposals: result.proposals })
      toast(`已合并：${result.company.name}（吸收 ${a === result.company.name ? b : a}，参考报告已重算）`)
      setManualA(''); setManualB(''); setManualConfirm(false)
    })
  }

  function submitSplit() {
    if (!splitTarget) return
    void run(`split:${splitTarget.alias}`, async () => {
      const result = await splitCompanyAlias(splitTarget.id, splitTarget.alias)
      onUpdated({ entities: result.entities, proposals: data?.proposals ?? [] })
      toast(`已把「${result.company.name}」拆成独立主体；原主体的报告已按剩余口径重算。`)
      setSplitTarget(null)
    })
  }

  const pending = (data?.proposals ?? []).filter(item => item.status === 'pending')
  const resolved = (data?.proposals ?? []).filter(item => item.status !== 'pending').slice(0, 8)
  const entities = data?.entities ?? []

  return <section className="paper-panel merge-panel">
    <div className="section-heading">
      <div><h2>主体归并</h2><p className="body-small muted">同一家公司的不同写法合并后，岗位与资料集中复用；子公司、关联公司保持隔离。模型判定「同一主体」且高置信的已在扫描时自动合并，其余进这里等确认。</p></div>
      <button type="button" className="button primary" disabled={Boolean(busy)} onClick={scan}>
        {busy === 'scan' ? <span className="spinner" /> : null}扫描合并候选
      </button>
    </div>

    {pending.length ? <div className="merge-proposals">{pending.map(item => {
      const jobs = proposalJobs(item, entities)
      const primaryIsB = flip[item.id] === 'b' || (!flip[item.id] && jobs.b > jobs.a)
      const primary = primaryIsB ? item.company_b : item.company_a
      const secondary = primaryIsB ? item.company_a : item.company_b
      return <div className="merge-proposal" key={item.id}>
        <div className="merge-proposal-pair">
          <span className="merge-pair-name"><i className="merge-primary-tag">主</i>{primary}</span>
          <span className="muted">吸收</span>
          <span className="merge-pair-name">{secondary}</span>
          <button type="button" className="text-button" disabled={Boolean(busy)}
                  title="对调合并方向（谁做主名）" aria-label="对调合并方向"
                  onClick={() => setFlip(prev => ({ ...prev, [item.id]: primaryIsB ? 'a' : 'b' }))}>对调</button>
        </div>
        <p className="merge-proposal-reason">{item.reason}</p>
        <div className="merge-proposal-actions">
          <span className="tiny-label">{jobs.a} / {jobs.b} 条岗位</span>
          <button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={() => accept(item.id)}>
            {busy === item.id ? <span className="spinner" /> : null}合并
          </button>
          <button type="button" className="text-button" disabled={Boolean(busy)} onClick={() => dismiss(item.id)}>忽略</button>
        </div>
      </div>})}</div>
      : <p className="tiny-label">没有待确认的合并候选。点「扫描合并候选」跑一轮规则比对 + 模型判定。</p>}

    <div className="merge-manual">
      <div className="section-label"><span className="label-line" />手动合并（左边为主名，吸收右边）</div>
      <div className="merge-manual-row">
        <input list="merge-entity-names" value={manualA} onChange={event => { setManualA(event.target.value); setManualConfirm(false) }}
               placeholder="主名（保留的一方）" maxLength={300} aria-label="合并主名" disabled={manualConfirm} />
        <input list="merge-entity-names" value={manualB} onChange={event => { setManualB(event.target.value); setManualConfirm(false) }}
               placeholder="被并入的一方" maxLength={300} aria-label="被并入的公司" disabled={manualConfirm} />
        <datalist id="merge-entity-names">{entities.map(item => <option value={item.name} key={item.id}>{item.jobs_count} 条岗位</option>)}</datalist>
        {manualConfirm
          ? <div className="inline-confirm">
              <span>合并后「{manualB.trim()}」实体消失，岗位资料并入「{manualA.trim()}」，可再拆分。</span>
              <button type="button" className="button danger small" disabled={Boolean(busy)} onClick={submitManual}>{busy === 'manual' ? <span className="spinner" /> : null}确认合并</button>
              <button type="button" className="text-button" onClick={() => setManualConfirm(false)}>取消</button>
            </div>
          : <button type="button" className="button secondary" disabled={Boolean(busy) || !manualA.trim() || !manualB.trim() || manualA.trim() === manualB.trim()} onClick={() => setManualConfirm(true)}>合并主体</button>}
      </div>
    </div>

    <div className="merge-entities">
      <div className="section-label"><span className="label-line" />已登记主体（{entities.length}）</div>
      {splitTarget ? <div className="inline-confirm merge-split-confirm">
        <span>把「{splitTarget.alias}」从「{splitTarget.name}」拆成独立主体？拆分后原主体报告按剩余口径重算。</span>
        <button type="button" className="button danger small" disabled={Boolean(busy)} onClick={submitSplit}>{busy === `split:${splitTarget.alias}` ? <span className="spinner" /> : null}确认拆分</button>
        <button type="button" className="text-button" onClick={() => setSplitTarget(null)}>取消</button>
      </div> : null}
      {entities.map(item => <div className="merge-entity" key={item.id}>
        <div className="merge-entity-head">
          <strong>{item.name}</strong>
          <span className="tiny-label">{item.jobs_count} 条岗位{item.credit_code ? ` · 信用代码已登记` : ''}</span>
        </div>
        {item.aliases.length ? <div className="merge-aliases">{item.aliases.map(alias => <span className="merge-alias" key={alias}>
          {alias}
          <button type="button" aria-label={`拆分 ${alias}`} title="拆成独立主体" disabled={Boolean(busy)}
                  onClick={() => setSplitTarget({ id: item.id, name: item.name, alias })}>×</button>
        </span>)}</div> : null}
      </div>)}
    </div>

    {resolved.length ? <details className="merge-resolved">
      <summary className="tiny-label">最近处理过的候选（{resolved.length}）</summary>
      {resolved.map(item => <p className="tiny-label" key={item.id}>{item.company_a} × {item.company_b} —— {item.status === 'accepted' ? '已合并' : '已忽略'} · {item.reason}</p>)}
    </details> : null}
  </section>
}
