import { useCallback, useEffect, useRef, useState } from 'react'
import { api, date, getPipelineReview, patchJobs, post, startDetailBatch } from './api'
import type { Job, OfferComparison, PageInfo, PipelineFunnel, PipelineFunnelBucket, PipelineReview, Task } from './api'
import { emptyPage, Pagination } from './Pagination'
import { toast } from './toast'
import { addCompareCompany } from './compareSelectionStore'
import { JobCard } from './JobCard'
import { Icon } from './Icon'
import { PipelineFollowEditor } from './PipelineFollowEditor'
import { OutreachEditor } from './OutreachEditor'

// 任务状态中文名（局部子集即可；档案页的完整表随来源注册表动态组装，这里不必复用）。
const taskStatusNames: Record<string, string> = {
  running: '反查中', pending: '等待中', completed: '已完成', failed: '失败',
  needs_login: '需要登录', needs_verification: '需要验证', backoff: '退避等待',
}

// 机会管线（2026-11-17）：收藏篮子上的投递跟进状态，顺序即推进方向。
// 与收藏共用一个篮子：给未收藏岗位设状态会自动收藏；取消收藏即移出管线（后端语义）。
// 岗位详情页（App.tsx）与今日概览共用这套状态字典。
export const PIPELINE_ORDER = ['interested', 'applied', 'interviewing', 'offer', 'archived'] as const
export const PIPELINE_NAMES: Record<string, string> = {
  interested: '意向', applied: '已投递', interviewing: '面试中', offer: 'Offer', archived: '归档',
}
// 截止日逾期按本地日历日判断（用户视角的「今天」），与后端 UTC 口径只在跨日零点附近有差异。
export function isDueOverdue(due: string | null | undefined) {
  if (!due) return false
  const now = new Date()
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`
  return due < today
}
type PipelineFilter = '' | 'tracked' | (typeof PIPELINE_ORDER)[number]
const PIPELINE_FILTERS: { id: PipelineFilter; label: string }[] = [
  { id: '', label: '全部收藏' },
  { id: 'tracked', label: '管线中' },
  ...PIPELINE_ORDER.map(status => ({ id: status as PipelineFilter, label: PIPELINE_NAMES[status] })),
]

// 漏斗转化与归因（2026-11-22）：曾到达口径的转化率一行 + 三组归因切片。
// 这里只呈现数据不给结论——卡点解读交给「投递复盘」卡（Agent 用 pipeline_funnel 工具取同一份数据）。
function FunnelPanel({ funnel }: { funnel: PipelineFunnel }) {
  if (!funnel.steps.some(step => step.rate !== null)) return null
  const groups: { title: string; hint: string; buckets: PipelineFunnelBucket[] }[] = [
    { title: '按来源', hint: '各渠道岗位的约面率对比', buckets: funnel.attribution.by_source },
    { title: '按简历', hint: '定制简历与通用简历的约面率对比', buckets: funnel.attribution.by_resume },
    { title: '按可靠度分带', hint: '约面率与分数高低序明显相悖时，评分规则该调了', buckets: funnel.attribution.by_reliability },
  ]
  return <>
    <div className="pipeline-funnel">
      {funnel.steps.map(step => <span key={step.to} className="funnel-step"
        title={`累计到达口径（事件流∪当前状态）；${step.cycle_n ? `按 ${step.cycle_n} 条推进样本计算` : '尚无可算周期的样本'}`}>
        {PIPELINE_NAMES[step.from]}→{PIPELINE_NAMES[step.to]} <b>{step.rate === null ? '数据不足' : `${Math.round(step.rate * 100)}%`}</b>
        {step.cycle_days !== null && <span className="tiny-label"> · 平均 {step.cycle_days} 天</span>}
      </span>)}
      {funnel.archived_untracked > 0 && <span className="tiny-label" title="事件流上线前的归档岗位没有历史，不计入转化分母">{funnel.archived_untracked} 条归档无历史</span>}
    </div>
    <div className="pipeline-attribution">
      {groups.filter(group => group.buckets.some(item => item.applied > 0)).map(group => <div key={group.title} className="attribution-group">
        <h4 title={group.hint}>{group.title}</h4>
        <table>
          <thead><tr><th>分组</th><th>已投</th><th>约面</th><th>约面率</th></tr></thead>
          <tbody>{group.buckets.filter(item => item.applied > 0).map(item => <tr key={item.key}>
            <td>{item.label}</td><td>{item.applied}</td><td>{item.interviewing}</td>
            <td><b>{item.interview_rate === null ? '—' : `${Math.round(item.interview_rate * 100)}%`}</b></td>
          </tr>)}</tbody>
        </table>
      </div>)}
    </div>
  </>
}

// Offer 对比（2026-11-22 P1）：Offer 状态岗位并排——薪资、可靠度分带、与本地薪资基线的差值。
// 基线差值只对「城市+角色可比」的岗位有值，是参考不是该不该去的结论。
function OffersPanel({ offers, onOpenJob }: { offers: OfferComparison[]; onOpenJob: (id: string) => void }) {
  const salaryText = (item: OfferComparison) =>
    item.salary_monthly_min && item.salary_monthly_max
      ? `${Math.round(item.salary_monthly_min / 1000)}–${Math.round(item.salary_monthly_max / 1000)}K`
      : (item.salary || '—')
  return <div className="pipeline-offers">
    <h4 title="Offer 状态的岗位并排；基线差值只对城市+角色可比的岗位计算，是参考不是结论">Offer 对比</h4>
    <table>
      <thead><tr><th>岗位</th><th>月薪</th><th>可靠度</th><th>vs 薪资基线</th></tr></thead>
      <tbody>{offers.map(item => {
        const delta = item.baseline_delta === null || item.baseline_delta === undefined ? null
          : `${item.baseline_delta >= 0 ? '+' : ''}${Math.round(item.baseline_delta * 100)}%`
        return <tr key={item.id}>
          <td><button type="button" className="text-button" onClick={() => onOpenJob(item.id)}>{item.title} · {item.company}</button></td>
          <td>{salaryText(item)}</td>
          <td>{item.reliability_band || '未评估'}</td>
          <td><b title={item.baseline_note ? `${item.baseline_note}（中位 ${item.baseline_median}）` : undefined}>{delta || '—'}</b></td>
        </tr>
      })}</tbody>
    </table>
  </div>
}

// 收藏夹：标星岗位的集中视图。只做轻列表（筛选/勾选/批量反查/横评/取消收藏），
// 点卡片跳回岗位研究看完整详情——详情工作台（求职参考、评估适配）不在这里重复。
// 反查走既有详情采集管道：单条同步接口，多条走批量任务+轮询；未登录/需验证原样透出。
export function FavoritesView({ onOpenJob, onOpenCompare, onChange }: {
  onOpenJob: (jobId: string) => void
  onOpenCompare: () => void
  onChange: () => void
}) {
  const [jobs, setJobs] = useState<Job[]>([])
  const [info, setInfo] = useState<PageInfo>(emptyPage)
  const [loading, setLoading] = useState(true)
  const [draft, setDraft] = useState({ q: '', source: '', pipeline: '' })
  const filters = useRef({ q: '', source: '', pipeline: '' })
  const requestToken = useRef(0)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  // 保存已见岗位的公司快照：分页不会丢失选中岗位所属公司。
  const selectedCompanies = useRef(new Map<string, string>())
  const [selectionNotice, setSelectionNotice] = useState('')
  const [busy, setBusy] = useState('')
  const [recheckTask, setRecheckTask] = useState<Task | null>(null)
  const recheckTimer = useRef(0)
  const [review, setReview] = useState<PipelineReview | null>(null)
  // 跟进编辑弹窗的目标岗位（2026-11-18）：下一步行动 + 截止日。
  const [followJob, setFollowJob] = useState<Job | null>(null)
  // 触达话术弹窗的目标岗位（2026-11-22）：打招呼/跟进消息生成与版本 A/B。
  const [outreachJob, setOutreachJob] = useState<Job | null>(null)

  const load = useCallback(async (next = filters.current, page = 1) => {
    if (next.q !== filters.current.q || next.source !== filters.current.source || next.pipeline !== filters.current.pipeline) {
      setSelectedIds(new Set()); selectedCompanies.current.clear(); setSelectionNotice('筛选已变更，已清空此前跨页选择。')
    }
    filters.current = next
    const params = new URLSearchParams({ starred: 'true', q: next.q, source: next.source, pipeline_status: next.pipeline, page_size: '100' })
    params.set('page', String(page))
    const token = ++requestToken.current
    const data = await api<PageInfo & { jobs: Job[] }>(`/jobs?${params.toString()}`)
    if (token === requestToken.current) {
      data.jobs.forEach(item => selectedCompanies.current.set(item.id, item.company))
      setJobs(data.jobs); setInfo(data)
    }
    return data.jobs
  }, [])

  const loadReview = useCallback(async () => {
    try { setReview(await getPipelineReview()) } catch { /* 回顾是辅助信息，失败不打断收藏夹主列表 */ }
  }, [])

  useEffect(() => {
    void load().finally(() => setLoading(false))
    void loadReview()
    return () => window.clearInterval(recheckTimer.current)
  }, [load, loadReview])

  function run(key: string, action: () => Promise<void>) {
    setBusy(key)
    void action().catch(err => toast((err as Error).message, 'error')).finally(() => setBusy(''))
  }

  function watchRecheck(taskId: string) {
    window.clearInterval(recheckTimer.current)
    let tries = 0
    recheckTimer.current = window.setInterval(() => {
      tries += 1
      void api<{ task: Task }>(`/tasks/${encodeURIComponent(taskId)}`).then(data => {
        setRecheckTask(data.task)
        if (!['running', 'pending'].includes(data.task.status)) {
          window.clearInterval(recheckTimer.current)
          toast(`${taskStatusNames[data.task.status] || data.task.status}：${data.task.message || '反查结束'}`, data.task.status === 'completed' ? 'notice' : 'error')
          void load(); onChange()
        } else if (tries > 900) {
          window.clearInterval(recheckTimer.current)
          toast('反查等待超时，请到「数据与采集 → 任务记录」查看进展', 'error')
        }
      }).catch(() => window.clearInterval(recheckTimer.current))
    }, 2000)
  }

  function toggleSelection(id: string) {
    setSelectionNotice('')
    setSelectedIds(previous => { const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next })
  }
  const allPageSelected = jobs.length > 0 && jobs.every(item => selectedIds.has(item.id))
  function togglePageSelection() {
    setSelectionNotice('')
    setSelectedIds(previous => {
      const next = new Set(previous)
      if (jobs.length > 0 && jobs.every(item => next.has(item.id))) jobs.forEach(item => next.delete(item.id))
      else jobs.forEach(item => next.add(item.id))
      return next
    })
  }

  function joinCompare(item: Job) {
    const result = addCompareCompany(item.company)
    if (result === 'exists') { toast('这家公司已在横评列表里。'); return }
    if (result === 'full') { toast('横评最多 8 家，先在数据分析页移除几家再加入。', 'error'); return }
    toast('已加入横评，可继续评估其他公司', 'notice', false, { label: '去横评', onClick: onOpenCompare })
  }

  function compareSelectedCompanies() {
    // 横评比的是公司：选中岗位的公司去重后逐个加入，满了如实告知几条没进。
    const companies = [...new Set([...selectedIds].map(id => (selectedCompanies.current.get(id) || '').trim()).filter(Boolean))]
    if (!companies.length) { toast('先勾选要横评的岗位'); return }
    let added = 0, exists = 0, full = 0
    for (const company of companies) {
      const result = addCompareCompany(company)
      if (result === 'added') added += 1
      else if (result === 'exists') exists += 1
      else full += 1
    }
    const parts = [`已加入 ${added} 家`]
    if (exists) parts.push(`${exists} 家已在列表`)
    if (full) parts.push(`${full} 家超出 8 家上限未加入`)
    toast(parts.join('，'), 'notice', false, { label: '去横评', onClick: onOpenCompare })
  }

  const recheckSelected = () => run('recheck', async () => {
    const result = await startDetailBatch([...selectedIds])
    setRecheckTask(result.task)
    watchRecheck(result.task.id)
  })
  const unstarSelected = () => run('unstar', async () => {
    const result = await patchJobs([...selectedIds], { starred: false })
    toast(`已取消收藏 ${result.updated} 份岗位`)
    setSelectedIds(new Set())
    await load(); onChange()
  })
  const toggleStar = (item: Job) => run(`star-${item.id}`, async () => {
    await patchJobs([item.id], { starred: !item.starred })
    if (item.starred) setSelectedIds(previous => { const next = new Set(previous); next.delete(item.id); return next })
    await load(); onChange()
  })
  const recheckOne = (item: Job) => run(`recheck-${item.id}`, async () => {
    const result = await post<{ task: Task; job: Job }>(`/jobs/${encodeURIComponent(item.id)}/detail`)
    toast(result.task.message, result.task.status === 'completed' ? 'notice' : 'error')
    await load(); onChange()
  })

  const rechecking = Boolean(recheckTask && ['running', 'pending'].includes(recheckTask.status))
  return <section className="favorites-view" aria-label="收藏夹">
    <section className="paper-panel favorites-panel">
      {/* 机会管线回顾：纯本地统计，指路不施压——待跟进只是「看一眼进展」的提醒，不评分不排名。 */}
      {review && Object.keys(review.totals).length ? <div className="pipeline-review">
        <div className="pipeline-review-totals">
          {PIPELINE_ORDER.map(status => <span key={status} className="pipeline-chip-stat"><b>{review.totals[status] || 0}</b>{PIPELINE_NAMES[status]}</span>)}
        </div>
        {review.funnel && <FunnelPanel funnel={review.funnel} />}
        {review.offers && review.offers.length > 0 && <OffersPanel offers={review.offers} onOpenJob={onOpenJob} />}
        {review.follow_ups.length > 0 ? <div className="pipeline-followups">
          <strong>超 {review.stale_days} 天未跟进</strong>
          <ul>{review.follow_ups.map(item => <li key={item.id}>
            <button type="button" className="text-button" onClick={() => onOpenJob(item.id)}>{item.title} · {item.company}</button>
            <span className="tiny-label">「{PIPELINE_NAMES[item.status]}」自 {date(item.changed_at)}</span>
          </li>)}</ul>
        </div> : null}
      </div> : null}
      <div className="favorites-toolbar">
        <div className="pipeline-filter" role="group" aria-label="管线筛选">
          {PIPELINE_FILTERS.map(item => <button key={item.id} type="button"
            className={`pipeline-filter-chip${draft.pipeline === item.id ? ' active' : ''}`}
            onClick={() => run('pipeline', async () => { setDraft({ ...draft, pipeline: item.id }); await load({ ...filters.current, q: draft.q, source: draft.source, pipeline: item.id }, 1) })}>{item.label}</button>)}
        </div>
        <form className="favorites-filters" onSubmit={event => { event.preventDefault(); run('filter', async () => { await load({ ...draft }, 1) }) }}>
          <label className="search-input"><Icon name="search" size={16} /><span className="sr-only">收藏内搜索</span><input value={draft.q} onChange={event => setDraft({ ...draft, q: event.target.value })} placeholder="在收藏的岗位中搜索" maxLength={300} /></label>
          <button className="button secondary small" type="submit" disabled={busy === 'filter'}>筛选</button>
        </form>
        <div className="favorites-batch">
          <label className="agent-checkbox"><input type="checkbox" checked={allPageSelected} disabled={Boolean(busy)} onChange={togglePageSelection} />本页全选</label>
          {selectedIds.size > 0 && <>
            <span className="tiny-label">跨页已选 {selectedIds.size} 项（本页 {jobs.filter(item => selectedIds.has(item.id)).length} 项）</span>
            <button className="button secondary small" disabled={Boolean(busy) || rechecking} title="用项目浏览器重读源站原帖，刷新薪资与完整JD；未登录时会提示先登录" onClick={recheckSelected}>反查选中</button>
            <button className="button secondary small" disabled={Boolean(busy)} onClick={compareSelectedCompanies}>横评选中公司</button>
            <button className="button secondary small danger-text" disabled={Boolean(busy)} onClick={unstarSelected}>取消收藏</button>
            <button className="text-button" onClick={() => setSelectedIds(new Set())}>取消</button>
          </>}
        </div>
      </div>
      {selectionNotice && <p role="status" className="tiny-label">{selectionNotice}</p>}
      {rechecking && <p className="batch-message" role="status"><span className="spinner" /> {recheckTask?.message}{recheckTask?.total ? ` · 已处理 ${recheckTask.processed || 0}/${recheckTask.total}` : ''}</p>}
      {loading ? <div className="loading-state"><span className="spinner" />正在读取收藏…</div>
        : jobs.length === 0 ? <div className="empty"><div className="empty-icon"><Icon name="star" /></div><h3>{draft.q || filters.current.q ? '收藏中没有符合条件的岗位' : '还没有收藏的岗位'}</h3><p>在岗位档案列表点卡片右侧的星标，或岗位详情页点「收藏」。收藏不会因为重新采集而丢失。</p></div>
        : <div className={`job-list${busy === 'filter' ? ' refreshing' : ''}`}>{jobs.map(item => <JobCard key={item.id} item={item} selected={false} checked={selectedIds.has(item.id)}
          busy={busy.startsWith(`star-${item.id}`) || busy.startsWith(`recheck-${item.id}`)}
          onToggleSelection={id => { if (!busy) toggleSelection(id) }} onOpen={onOpenJob} onToggleStar={toggleStar}
          actions={<span className="job-card-actions">
            {/* 管线状态下拉：给未收藏岗位设状态会自动收藏（后端语义），换状态即盖新时间戳。 */}
            <select className="pipeline-select" value={item.pipeline_status || ''} disabled={Boolean(busy)}
              title="机会管线状态：标记投递进展；给未收藏的岗位设状态会自动收藏"
              onChange={event => run(`pipe-${item.id}`, async () => {
                const value = event.target.value as 'interested' | 'applied' | 'interviewing' | 'offer' | 'archived' | ''
                await patchJobs([item.id], { pipeline_status: value })
                toast(value ? `已标记为「${PIPELINE_NAMES[value]}」` : '已移出管线')
                await load(); await loadReview(); onChange()
              })}>
              <option value="">未跟踪</option>
              {PIPELINE_ORDER.map(status => <option key={status} value={status}>{PIPELINE_NAMES[status]}</option>)}
            </select>
            <button className="text-button" disabled={Boolean(busy)} title="设置下一步行动与截止日；到期的会出现在「今日概览」待办" onClick={() => setFollowJob(item)}>{item.pipeline_due ? '改跟进' : '跟进'}</button>
            <button className="text-button" disabled={Boolean(busy)} title="生成打招呼/跟进话术并记录版本；发送仍在招聘站手动完成" onClick={() => setOutreachJob(item)}>话术</button>
            <button className="text-button" disabled={Boolean(busy)} title="重读源站原帖，刷新薪资与完整JD" onClick={() => void recheckOne(item)}>反查</button>
            <button className="text-button" onClick={() => joinCompare(item)}>加入横评</button>
          </span>}
          footer={<span className="tiny-label job-follow-footer">
            {item.pipeline_due && <b className={`follow-due${isDueOverdue(item.pipeline_due) ? ' overdue' : ''}`} title="跟进截止日；逾期条目同时出现在「今日概览」待办">截止 {item.pipeline_due}{isDueOverdue(item.pipeline_due) ? '（逾期）' : ''}</b>}
            {item.pipeline_note && <span className="follow-note" title="下一步行动">{item.pipeline_note}</span>}
            {item.detail_collected_at ? `反查于 ${date(item.detail_collected_at)}` : '尚未反查过详情'}
          </span>} />)}</div>}
      <Pagination info={info} label="收藏夹" busy={Boolean(busy)} onPage={page => run('page', async () => { await load(undefined, page) })} />
      {followJob && <PipelineFollowEditor job={followJob} onClose={() => setFollowJob(null)}
        onSaved={() => { void run('follow', async () => { await load() }) }} />}
      {outreachJob && <OutreachEditor job={outreachJob} onClose={() => setOutreachJob(null)} />}
    </section>
  </section>
}
