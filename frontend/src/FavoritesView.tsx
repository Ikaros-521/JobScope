import { useCallback, useEffect, useRef, useState } from 'react'
import { api, date, patchJobs, post, startDetailBatch } from './api'
import type { Job, PageInfo, Task } from './api'
import { emptyPage, Pagination } from './Pagination'
import { toast } from './toast'
import { addCompareCompany } from './compareSelectionStore'
import { JobCard } from './JobCard'
import { Icon } from './Icon'

// 任务状态中文名（局部子集即可；档案页的完整表随来源注册表动态组装，这里不必复用）。
const taskStatusNames: Record<string, string> = {
  running: '反查中', pending: '等待中', completed: '已完成', failed: '失败',
  needs_login: '需要登录', needs_verification: '需要验证', backoff: '退避等待',
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
  const [draft, setDraft] = useState({ q: '', source: '' })
  const filters = useRef({ q: '', source: '' })
  const requestToken = useRef(0)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  // 保存已见岗位的公司快照：分页不会丢失选中岗位所属公司。
  const selectedCompanies = useRef(new Map<string, string>())
  const [selectionNotice, setSelectionNotice] = useState('')
  const [busy, setBusy] = useState('')
  const [recheckTask, setRecheckTask] = useState<Task | null>(null)
  const recheckTimer = useRef(0)

  const load = useCallback(async (next = filters.current, page = 1) => {
    if (next.q !== filters.current.q || next.source !== filters.current.source) {
      setSelectedIds(new Set()); selectedCompanies.current.clear(); setSelectionNotice('筛选已变更，已清空此前跨页选择。')
    }
    filters.current = next
    const params = new URLSearchParams({ starred: 'true', q: next.q, source: next.source, page_size: '100' })
    params.set('page', String(page))
    const token = ++requestToken.current
    const data = await api<PageInfo & { jobs: Job[] }>(`/jobs?${params.toString()}`)
    if (token === requestToken.current) {
      data.jobs.forEach(item => selectedCompanies.current.set(item.id, item.company))
      setJobs(data.jobs); setInfo(data)
    }
    return data.jobs
  }, [])

  useEffect(() => { void load().finally(() => setLoading(false)); return () => window.clearInterval(recheckTimer.current) }, [load])

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
    <section className="page-intro"><div><h1>收藏夹</h1><p>标过星的岗位集中在这里：反查最新情况、直接横评公司、直达源帖</p></div><span className="revision-label">收藏随岗位档案保存在本机</span></section>
    <section className="paper-panel favorites-panel">
      <div className="favorites-toolbar">
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
            <button className="text-button" disabled={Boolean(busy)} title="重读源站原帖，刷新薪资与完整JD" onClick={() => void recheckOne(item)}>反查</button>
            <button className="text-button" onClick={() => joinCompare(item)}>加入横评</button>
          </span>}
          footer={<span className="tiny-label">{item.detail_collected_at ? `反查于 ${date(item.detail_collected_at)}` : '尚未反查过详情'}</span>} />)}</div>}
      <Pagination info={info} label="收藏夹" busy={Boolean(busy)} onPage={page => run('page', async () => { await load(undefined, page) })} />
      <div className="list-bottom">反查会用项目浏览器重新读取源站原帖；投递前请以平台页面为准。</div>
    </section>
  </section>
}
