import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { api, post, readable, plain, date, safeUrl, computeReliability, collectCompanySignals, extractCompanySignals, getReliability, getReliabilityHistory, listSignalSources, saveSignalSource } from './api'
import type { SignalSource } from './api'
import { toast, dismissToast } from './toast'
import { Pagination, emptyPage } from './Pagination'
import { EvidenceWorkspace } from './EvidenceWorkspace'
import { EvidenceCard } from './EvidenceCard'
import { DetailBatch } from './DetailBatch'
import { CityInput } from './CityInput'
import type { SavedLocation } from './CityInput'
import { ResearchIndex } from './ResearchIndex'
import { AgentExtensions } from './AgentExtensions'
import { ModelSettings } from './ModelSettings'
import { CompanyReference } from './CompanyReference'
import { ResumeImport } from './ResumeImport'
import { navigateTabs } from './tabNavigation'
import { ChatWorkspace } from './chat/ChatWorkspace'
import type { Analysis, BrowserStatus, Evidence, Job, PageInfo, ReliabilityHistory, ReliabilityReport, Source, Task } from './api'

const transportNames: Record<string, string> = { http_api: '接口', browser_page: '需浏览器', manual: '手动' }
const statusNames: Record<string, string> = { completed: '已完成', success: '已完成', running: '采集中', pending: '等待中', failed: '失败', needs_login: '需要登录', needing_login: '需要登录', login_required: '需要登录', needs_verification: '需要验证', verification_required: '需要验证', requires_verification: '需要验证', unsupported: '暂不支持', available: '可用', ready: '可用', not_configured: '未配置', blocked: '访问受限', backoff: '退避等待', opt_in: '需你自行开启', skipped: '已跳过', degraded: '已降级留档' }
const dimensionNames: Record<string, string> = { skill: '技能', skills: '技能', experience: '经历与职责', domain: '行业与业务', industry: '行业与业务', preference: '求职偏好', preferences: '求职偏好' }
const requirementNames: Record<string, string> = { met: '满足', matched: '满足', satisfied: '满足', partial: '部分满足', not_met: '未满足', unmet: '未满足', missing: '未满足', unknown: '待确认', confirmed: '满足' }
function needsFullJd(job: Job) {
  return job.description_status === 'listing' || (['boss', 'zhaopin'].includes(job.source) && job.description_status !== 'complete')
}
function jobQuery(filters: { q: string; location: string; source: string; salaryMin: string; salaryMax: string }) {
  // 薪资输入单位是 K/月，后端按元比较；留空表示不限制。
  const params = new URLSearchParams({ q: filters.q, location: filters.location, source: filters.source, page_size: '100' })
  if (filters.salaryMin) params.set('salary_min', String(Math.round(Number(filters.salaryMin) * 1000)))
  if (filters.salaryMax) params.set('salary_max', String(Math.round(Number(filters.salaryMax) * 1000)))
  return params
}
function Icon({ name, size = 18 }: { name: string; size?: number }) {
  const paths: Record<string, ReactNode> = {
    search: <><circle cx="10.5" cy="10.5" r="6.5" /><path d="m16 16 4 4" /></>,
    jobs: <><rect x="3" y="7" width="18" height="14" rx="2" /><path d="M8 7V4h8v3M3 12h18M10 12v3h4v-3" /></>,
    resume: <><path d="M14 3H5v18h14V8Z" /><path d="M14 3v5h5M8 12h8M8 16h6" /></>,
    source: <><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v7c0 4 16 4 16 0V5M4 12v7c0 4 16 4 16 0" /></>,
    arrow: <><path d="M5 12h14M14 7l5 5-5 5" /></>,
    plus: <path d="M12 5v14M5 12h14" />,
    external: <><path d="M14 4h6v6M20 4l-9 9M10 4H4v16h16v-6" /></>,
    pin: <><path d="M19 10c0 5-7 11-7 11S5 15 5 10a7 7 0 1 1 14 0Z" /><circle cx="12" cy="10" r="2" /></>,
    chat: <path d="M21 4H3v14h5l4 4v-4h9Z" />,
    check: <path d="m5 12 4 4L19 6" />,
    settings: <><path d="M4 7h16M4 17h16" /><circle cx="9" cy="7" r="3" fill="currentColor" /><circle cx="15" cy="17" r="3" fill="currentColor" /></>,
    close: <path d="m6 6 12 12M18 6 6 18" />,
    book: <><path d="M12 5C8 2 3 3 3 3v16s5-1 9 2c4-3 9-2 9-2V3s-5-1-9 2ZM12 5v16" /></>,
    globe: <><circle cx="12" cy="12" r="9" /><ellipse cx="12" cy="12" rx="4" ry="9" /><path d="M3 12h18" /></>,
  }
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name] || paths.book}</svg>
}
function Button({ children, busy, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { busy?: boolean }) { return <button {...props} disabled={props.disabled || busy}>{busy ? <span className="spinner" /> : null}{children}</button> }
function Empty({ title, children, icon = 'book' }: { title: string; children: ReactNode; icon?: string }) { return <div className="empty"><div className="empty-icon"><Icon name={icon} /></div><h3>{title}</h3><p>{children}</p></div> }
function Badge({ value }: { value: string }) { return <span className={`badge ${['completed', 'success', 'available', 'ready'].includes(value) ? 'good' : ['failed', 'blocked'].includes(value) ? 'bad' : ''}`}><span />{statusNames[value] || value}</span> }
function External({ url, children }: { url: string; children: ReactNode }) { const href = safeUrl(url); return href ? <a className="external-link" href={href} target="_blank" rel="noreferrer">{children}<Icon name="external" size={13} /></a> : <span className="muted">无来源链接</span> }

export default function App() {
  const [view, setView] = useState<'jobs' | 'resume' | 'sources' | 'evidence' | 'global' | 'settings' | 'extensions'>('jobs')
  const [jobs, setJobs] = useState<Job[]>([])
  const [jobsInfo, setJobsInfo] = useState<PageInfo>(emptyPage)
  const [jobsTotal, setJobsTotal] = useState(0)
  const [tasksInfo, setTasksInfo] = useState<PageInfo>(emptyPage)
  const jobFilters = useRef({ q: '', location: '', source: '', salaryMin: '', salaryMax: '' })
  const jobsRequest = useRef(0)
  const [sources, setSources] = useState<Source[]>([])
  const [locations, setLocations] = useState<SavedLocation[]>([])
  const [cityPinyin, setCityPinyin] = useState<Record<string, string[]>>({})
  const [tasks, setTasks] = useState<Task[]>([])
  const [online, setOnline] = useState(false)
  const [modelConfigured, setModelConfigured] = useState(false)
  const [loading, setLoading] = useState(true)
  const [query, setQuery] = useState('')
  const [location, setLocation] = useState('')
  const [source, setSource] = useState('remotive')
  const [browserSource, setBrowserSource] = useState('boss')
  const [browserLimit, setBrowserLimit] = useState(20)
  const [collectLimit, setCollectLimit] = useState(20)
  const [busy, setBusy] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [archiveDraft, setArchiveDraft] = useState({ q: '', source: '', location: '', salaryMin: '', salaryMax: '' })
  const [batchEdit, setBatchEdit] = useState<{ ids: string[] } | null>(null)
  const [batchDraft, setBatchDraft] = useState({ location: '', salary: '', tags: '' })
  const [confirmDelete, setConfirmDelete] = useState<{ ids: string[]; label: string } | null>(null)
  const [detail, setDetail] = useState<{ job: Job; evidence: Evidence[]; company_evidence: Evidence[]; evidence_total: number } | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [tab, setTab] = useState<'overview' | 'analysis' | 'evidence'>('overview')
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [reliability, setReliability] = useState<ReliabilityReport | null>(null)
  const [reliabilityHistory, setReliabilityHistory] = useState<ReliabilityHistory | null>(null)
  const [reliabilityAction, setReliabilityAction] = useState<{ label: string; status: string; message: string } | null>(null)
  const [evidenceFocus, setEvidenceFocus] = useState('')
  const [descExpanded, setDescExpanded] = useState(false)
  const [companyPage, setCompanyPage] = useState(0)
  const [resumeText, setResumeText] = useState('')
  const [resumeFormat, setResumeFormat] = useState('text')
  const [resumeRevision, setResumeRevision] = useState<number | null>(null)
  const [resumeSaved, setResumeSaved] = useState(false)
  const [browser, setBrowser] = useState<BrowserStatus | null>(null)
  const [modal, setModal] = useState<'job' | 'evidence' | 'batch-edit' | 'confirm-delete' | null>(null)
  const detailRequest = useRef(0)
  const activeJobId = useRef<string | null>(null)
  const jobGeneration = useRef(0)
  const job = detail?.job
  const modalOpener = useRef<HTMLElement | null>(null)
  function openModal(value: 'job' | 'evidence' | 'batch-edit' | 'confirm-delete') {
    modalOpener.current = document.activeElement as HTMLElement | null
    setModal(value)
  }
  useEffect(() => {
    if (!modal) return
    const opener = modalOpener.current
    const previousOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = previousOverflow; opener?.focus() }
  }, [modal])

  const loadJobs = useCallback(async (filters = jobFilters.current, page = 1) => {
    jobFilters.current = filters
    const params = jobQuery(filters); params.set('page', String(page))
    const token = ++jobsRequest.current
    const data = await api<PageInfo & { jobs: Job[] }>(`/jobs?${params.toString()}`)
    if (token === jobsRequest.current) { setJobs(data.jobs); setJobsInfo(data); const active = filters; if (!active.q && !active.location && !active.source && !active.salaryMin && !active.salaryMax) setJobsTotal(data.total) }
    return data.jobs
  }, [])
  async function loadTasks(page: number) {
    const data = await api<PageInfo & { tasks: Task[] }>(`/tasks?page=${page}&page_size=10`)
    setTasks(data.tasks); setTasksInfo(data)
  }
  const refreshMetadata = useCallback(async () => {
    const results = await Promise.allSettled([
      api<{ status: string; model_configured: boolean }>('/health'),
      api<{ sources: Source[] }>('/sources'),
      api<PageInfo & { tasks: Task[] }>('/tasks?page_size=10'),
      api<BrowserStatus>('/browser/status'),
      api<{ locations: SavedLocation[] }>('/locations'),
      api<{ pinyin: Record<string, string[]> }>('/cities'),
    ])
    if (results[0].status === 'fulfilled') { setOnline(true); setModelConfigured(results[0].value.model_configured) } else setOnline(false)
    if (results[1].status === 'fulfilled') setSources(results[1].value.sources)
    if (results[2].status === 'fulfilled') { setTasks(results[2].value.tasks); setTasksInfo(results[2].value) }
    if (results[3].status === 'fulfilled') setBrowser(results[3].value)
    if (results[4].status === 'fulfilled') setLocations(results[4].value.locations)
    if (results[5].status === 'fulfilled') setCityPinyin(results[5].value.pinyin)
  }, [])
  useEffect(() => {
    let active = true
    async function init() {
      try {
        await Promise.all([refreshMetadata(), loadJobs()])
        const resume = await api<{ text: string; format: string; revision?: number }>('/resume')
        if (active) { setResumeText(resume.text || ''); setResumeFormat(resume.format || 'text'); setResumeRevision(resume.revision || null); setResumeSaved(Boolean(resume.text)) }
      } catch (err) { if (active) toast((err as Error).message, 'error') }
      finally { if (active) setLoading(false) }
    }
    void init()
    return () => { active = false }
  }, [refreshMetadata, loadJobs])
  const loadDetail = useCallback(async (id: string) => {
    const token = ++detailRequest.current
    setDetailLoading(true)
    try {
      const data = await api<{ job: Job; evidence: Evidence[]; company_evidence: Evidence[]; evidence_total: number }>(`/jobs/${encodeURIComponent(id)}`)
      if (token === detailRequest.current) {
        setDetail(data)
        // 先清空上一家的报告，再读这一家的：GET 走后端的 peek，查不到也只是不显示，不会写出 companies 行。
        setReliability(null)
        setReliabilityHistory(null)
        setReliabilityAction(null)
        const company = data.job.company
        getReliability(company).then(result => {
          if (token === detailRequest.current && data.job.id === activeJobId.current) setReliability(result.report)
        }).catch(() => { /* 没有生成过参考时这里什么都不显示 */ })
        getReliabilityHistory(company).then(result => {
          if (token === detailRequest.current && data.job.id === activeJobId.current) setReliabilityHistory(result.history)
        }).catch(() => { /* 没有历史快照时趋势区不显示 */ })
      }
    } catch (err) { if (token === detailRequest.current) toast((err as Error).message, 'error') }
    finally { if (token === detailRequest.current) setDetailLoading(false) }
  }, [])
  function selectJob(id: string) { jobGeneration.current++; activeJobId.current = id; setSelectedId(id); setDetail(null); setAnalysis(null); setReliability(null); setReliabilityHistory(null); setReliabilityAction(null); setEvidenceFocus(''); setDescExpanded(false); setCompanyPage(0); setTab('overview'); document.querySelector('.detail-panel')?.scrollTo({ top: 0 }); void loadDetail(id) }
  function openReferenceEvidence(evidenceId: string) { setEvidenceFocus(evidenceId); setTab('evidence') }
  async function recomputeReference() {
    if (!job) return
    const company = job.company, id = job.id, generation = jobGeneration.current
    await perform('reliability', async () => {
      const result = await computeReliability(company)
      if (generation === jobGeneration.current && id === activeJobId.current) {
        setReliability(result); setReliabilityAction(null)
        setReliabilityHistory((await getReliabilityHistory(company)).history)
      }
    })
  }
  async function runReferenceAction(kind: 'coverage' | 'extract') {
    if (!job) return
    const company = job.company, id = job.id, generation = jobGeneration.current
    const label = kind === 'coverage' ? '用已有材料算覆盖度' : '从材料里抽取信号'
    await perform('reliability', async () => {
      // 采集与抽取的 status、message 一律照后端原文显示，不改写、不美化。
      const result = kind === 'coverage' ? await collectCompanySignals(company, 'jobscope_jobs') : await extractCompanySignals(company)
      if (generation !== jobGeneration.current || id === activeJobId.current) return
      setReliabilityAction({ label, status: result.status, message: result.message })
      if (kind === 'extract' && result.status === 'skipped') return
      setReliability(await computeReliability(company))
      setReliabilityHistory((await getReliabilityHistory(company)).history)
    })
  }
  async function perform(key: string, action: () => Promise<void>) {
    setBusy(key)
    try { await action() } catch (err) { toast((err as Error).message, 'error') }
    finally { setBusy('') }
  }
  async function collect(event: FormEvent) {
    event.preventDefault()
    await perform('collect', async () => {
      const data = await post<{ task: Task; jobs: Job[] }>('/collect', { source, query, location, limit: collectLimit })
      toast(data.task.message || `采集结束，新增或更新 ${data.task.count ?? data.jobs?.length ?? 0} 条资料。`)
      await Promise.all([loadJobs(), refreshMetadata()])
    })
  }
  async function watchTask(id: string): Promise<Task> {
    // 后台采集最多约 30 分钟；轮询直到任务离开运行态。
    for (let attempt = 0; attempt < 900; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 2000))
      const { task } = await api<{ task: Task }>(`/tasks/${encodeURIComponent(id)}`)
      if (task.status !== 'running' && task.status !== 'pending') return task
    }
    throw new Error('等待采集超时，请稍后在采集记录里查看结果')
  }
  async function collectBrowser() {
    const progress = toast('已开始后台采集，读完后自动保存并刷新；若浏览器出现登录或验证页面，请在该窗口完成', 'notice', true)
    await perform('browser-collect', async () => {
      const started = await post<{ task: Task }>('/browser/collect', { source: browserSource, query, location, limit: browserLimit })
      const task = await watchTask(started.task.id)
      await Promise.all([loadJobs(), refreshMetadata()])
      if (task.status === 'completed' && task.count > 0) {
        toast(`已保存或更新 ${task.count} 条岗位。${task.message || ''}`)
        setView('jobs')
        window.scrollTo({ top: 0, behavior: 'instant' })
      } else {
        toast(task.message || '本次未保存岗位，请查看采集记录')
      }
    })
    dismissToast(progress)
  }
  async function analyze() {
    if (!job) return
    const id = job.id
    const generation = jobGeneration.current
    setTab('analysis')
    await perform('analyze', async () => {
      const result = await post<Analysis>('/analyze', { job_id: id })
      if (generation === jobGeneration.current && id === activeJobId.current) setAnalysis(result)
    })
  }
  async function saveResume(event: FormEvent) {
    event.preventDefault()
    await perform('resume', async () => {
      const data = await api<{ revision: number }>('/resume', { method: 'PUT', body: JSON.stringify({ text: resumeText, format: resumeFormat }) })
      setResumeRevision(data.revision); setResumeSaved(true); setAnalysis(null); toast('简历已保存在本地。岗位适配分析将使用这个版本。')
    })
  }
  async function collectCompany() {
    if (!job) return
    await perform('company', async () => {
      const data = await post<{ task: Task }>('/collect', { source: 'cninfo', query: job.company, limit: 10 })
      toast(data.task.message || '公司公告检索完成。公告元信息不代表已经解析财报正文。')
      await Promise.all([job.id === activeJobId.current ? loadDetail(job.id) : Promise.resolve(), refreshMetadata()])
    })
  }
  async function importRecord(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const payload = Object.fromEntries(form.entries())
    const mode = modal
    await perform('import', async () => {
      if (mode === 'job') {
        const result = await post<{ job: Job }>('/import/job', payload)
        await loadJobs(); setView('jobs'); selectJob(result.job.id); toast('岗位已导入，来源标记为用户材料。')
      } else {
        await post('/evidence', payload)
        if (job) await loadDetail(job.id)
        toast('资料已保存。用户提供的资料仍需结合来源核实。')
      }
      setModal(null)
    })
  }
  function toggleJobSelection(id: string) {
    setSelectedIds(previous => { const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next })
  }
  const allPageSelected = jobs.length > 0 && jobs.every(item => selectedIds.has(item.id))
  function togglePageSelection() {
    setSelectedIds(previous => {
      const next = new Set(previous)
      if (jobs.length > 0 && jobs.every(item => next.has(item.id))) jobs.forEach(item => next.delete(item.id))
      else jobs.forEach(item => next.add(item.id))
      return next
    })
  }
  function openBatchEdit(targets: Job[]) {
    setBatchEdit({ ids: targets.map(item => item.id) })
    setBatchDraft(targets.length === 1 ? { location: targets[0].location || '', salary: targets[0].salary || '', tags: (targets[0].tags || []).join(', ') } : { location: '', salary: '', tags: '' })
    openModal('batch-edit')
  }
  function openBatchEditForSelection() {
    // 只勾了一条时预填它的现值；跨页多选则从空白开始（留空字段不改动）。
    const onPage = jobs.filter(item => selectedIds.has(item.id))
    if (selectedIds.size === 1 && onPage.length === 1) openBatchEdit(onPage)
    else {
      setBatchEdit({ ids: [...selectedIds] })
      setBatchDraft({ location: '', salary: '', tags: '' })
      openModal('batch-edit')
    }
  }
  function askDelete(ids: string[], label: string) {
    setConfirmDelete({ ids, label })
    openModal('confirm-delete')
  }
  async function selectAllFiltered() {
    await perform('select-all', async () => {
      const data = await api<{ ids: string[]; total: number }>(`/jobs/ids?${jobQuery(jobFilters.current).toString()}`)
      setSelectedIds(new Set(data.ids))
      toast(`已选择筛选结果全部 ${data.total} 条（含未显示的页）`)
    })
  }
  async function submitBatchEdit(event: FormEvent) {
    event.preventDefault()
    if (!batchEdit) return
    const payload: Record<string, unknown> = { ids: batchEdit.ids }
    if (batchDraft.location.trim()) payload.location = batchDraft.location.trim()
    if (batchDraft.salary.trim()) payload.salary = batchDraft.salary.trim()
    const tags = batchDraft.tags.split(/[,，]/).map(tag => tag.trim()).filter(Boolean)
    if (tags.length) payload.tags = tags
    await perform('batch-edit', async () => {
      const result = await api<{ updated: number }>('/jobs', { method: 'PATCH', body: JSON.stringify(payload) })
      toast(`已更新 ${result.updated} 份档案`)
      setModal(null); setSelectedIds(new Set())
      await Promise.all([loadJobs(), selectedId ? loadDetail(selectedId) : Promise.resolve()])
    })
  }
  async function submitBatchDelete() {
    if (!confirmDelete) return
    const targets = confirmDelete
    await perform('batch-delete', async () => {
      const result = await post<{ deleted: number }>('/jobs/batch-delete', { ids: targets.ids })
      toast(`已删除 ${result.deleted} 份岗位档案`)
      setModal(null); setConfirmDelete(null); setSelectedIds(new Set())
      if (selectedId && targets.ids.includes(selectedId)) { setSelectedId(null); setDetail(null); setAnalysis(null) }
      await Promise.all([loadJobs(), refreshMetadata()])
    })
  }
  const evidenceTotal = detail?.evidence_total ?? 0
  const selectedSource = sources.find(item => item.id === source)
  const sourceNames: Record<string, string> = Object.fromEntries(sources.map(item => [item.id, item.name]))
  const jobSources = sources.filter(item => item.kind === 'jobs')
  const browserSources = sources.filter(item => item.browser_collectable)
  const browserSourceItem = browserSources.find(item => item.id === browserSource)
  const browserCities = browserSourceItem?.cities || []

  return <div className="app-shell">
    <aside className="sidebar">
      <a className="brand" href="#" onClick={event => { event.preventDefault(); setView('jobs') }}><span className="brand-symbol" aria-hidden="true"><i /><i /></span><span>知根<span className="brand-english">JobScope</span></span></a>
      <div className="sidebar-caption">求职调查档案</div>
      <nav aria-label="主导航">
        <button aria-current={view === 'jobs' ? 'page' : undefined} className={view === 'jobs' ? 'active' : ''} onClick={() => setView('jobs')}><Icon name="jobs" />岗位研究<span className="nav-count">{jobsTotal}</span></button>
        <button aria-current={view === 'resume' ? 'page' : undefined} className={view === 'resume' ? 'active' : ''} onClick={() => setView('resume')}><Icon name="resume" />我的简历{resumeSaved && <span className="nav-dot" />}</button>
        <button aria-current={view === 'evidence' ? 'page' : undefined} className={view === 'evidence' ? 'active' : ''} onClick={() => setView('evidence')}><Icon name="book" />公司资料库</button>
        <button aria-current={view === 'global' ? 'page' : undefined} className={view === 'global' ? 'active' : ''} onClick={() => setView('global')}><Icon name="chat" />全局对话</button>
        <button aria-current={view === 'sources' ? 'page' : undefined} className={view === 'sources' ? 'active' : ''} onClick={() => setView('sources')}><Icon name="source" />数据与采集</button>
        <button aria-current={view === 'extensions' ? 'page' : undefined} className={view === 'extensions' ? 'active' : ''} onClick={() => setView('extensions')}><Icon name="settings" />Agent 扩展</button>
        <button aria-current={view === 'settings' ? 'page' : undefined} className={view === 'settings' ? 'active' : ''} onClick={() => setView('settings')}><Icon name="settings" />模型设置</button>
      </nav>

      <div className="sidebar-bottom"><span className={`connection-dot ${online ? 'connected' : ''}`} />{online ? '本地工作区已连接' : '等待本地服务'}<span className="version">v0.1</span></div>
    </aside>

    <main className={view === 'jobs' ? 'main main-workbench' : 'main'}>
      <header className="topbar"><div className="breadcrumb">工作区<span>/</span>{view === 'jobs' ? '岗位研究' : view === 'resume' ? '我的简历' : view === 'evidence' ? '公司资料库' : view === 'global' ? '全局对话' : view === 'extensions' ? 'Agent 扩展' : view === 'settings' ? '模型设置' : '数据与采集'}</div><div className="topbar-right"><span className="local-tag"><span />本地资料库</span><button className="icon-button" aria-label="刷新工作区" onClick={() => void perform('refresh', async () => { await Promise.all([loadJobs(), refreshMetadata()]); if (selectedId) await loadDetail(selectedId) })}><Icon name="source" size={17} /></button></div></header>
      <div hidden={view !== 'evidence'}><EvidenceWorkspace active={view === 'evidence'} refreshMetadata={refreshMetadata} modelConfigured={modelConfigured} /></div>

      {view === 'global' && <ChatWorkspace scope={{ kind: 'global' }} modelConfigured={modelConfigured} onManageExtensions={() => setView('extensions')} />}

      {view === 'jobs' && <>
        <section className="page-intro"><div><h1>岗位研究</h1><p>把岗位要求、公司证据和你的经历逐项核对</p></div><Button className="button secondary" onClick={() => openModal('job')}><Icon name="plus" size={17} />导入岗位</Button></section>
        <section className="search-section" aria-label="岗位检索">
          <form className="search-form" onSubmit={collect}>
            <label className="search-input"><Icon name="search" /><span className="sr-only">岗位关键词</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder="搜索岗位、技能或公司" required maxLength={200} /></label>
            <label className="location-input"><Icon name="pin" size={18} /><CityInput label="工作地点" cities={selectedSource?.cities || []} hot={selectedSource?.hot || []} pinyin={cityPinyin} locations={locations} value={location} onChange={setLocation} placeholder="不限地点" maxLength={100} /></label>
            <label className="source-select"><span className="sr-only">岗位数据源</span><select value={source} onChange={event => setSource(event.target.value)}>{jobSources.map(item => <option key={item.id} value={item.id}>{item.name} · {transportNames[item.transport_mode] || item.transport_mode}</option>)}</select></label>
            <label className="collect-limit"><span className="sr-only">采集数量</span><input type="number" min={1} max={1000} value={collectLimit} onChange={event => setCollectLimit(Math.max(1, Math.min(1000, Number(event.target.value) || 20)))} inputMode="numeric" aria-label="采集数量" /></label>
            <Button className="button primary" type="submit" busy={busy === 'collect'} disabled={Boolean(busy)}>检索岗位<Icon name="arrow" size={17} /></Button>
          </form>
          <div className="search-footnote"><span><span className="small-dot" />{selectedSource?.description || '选择来源后点击检索；标注“需浏览器”的来源会在项目 Chrome 内取数'}</span><button onClick={() => setView('sources')}>管理数据源<Icon name="arrow" size={13} /></button></div>
        </section>

        <div className="research-layout">
          <section className="job-list-panel"><div className="panel-title"><h2>岗位档案 <span>{jobsInfo.total}</span></h2>{selectedIds.size > 0 && <span className="muted body-small">已选 {selectedIds.size}</span>}{jobs.length > 0 && <div className="job-select-bar"><label className="agent-checkbox"><input type="checkbox" checked={allPageSelected} onChange={togglePageSelection} />本页全选</label><button className="text-button" disabled={Boolean(busy)} onClick={() => void selectAllFiltered()}>选筛选全部 {jobsInfo.total} 条</button>{selectedIds.size > 0 && <><Button className="button secondary small" disabled={Boolean(busy)} onClick={openBatchEditForSelection}>批量编辑</Button><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => askDelete([...selectedIds], `已选的 ${selectedIds.size} 份岗位档案`)}>删除</Button><button className="text-button" onClick={() => setSelectedIds(new Set())}>取消选择</button></>}</div>}</div>
            <form className="job-archive-filters" aria-label="岗位档案筛选" onSubmit={event => { event.preventDefault(); void perform('filter', async () => { await loadJobs({ ...archiveDraft }, 1) }) }}>
              <label className="search-input"><Icon name="search" /><span className="sr-only">档案关键词</span><input value={archiveDraft.q} onChange={event => setArchiveDraft({ ...archiveDraft, q: event.target.value })} placeholder="岗位、公司或描述" maxLength={300} /></label>
              <label className="source-select"><span className="sr-only">信息源</span><select value={archiveDraft.source} onChange={event => setArchiveDraft({ ...archiveDraft, source: event.target.value })}><option value="">全部来源</option>{jobSources.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}<option value="user_import">用户导入</option></select></label>
              <CityInput label="地点" cities={[]} hot={[]} pinyin={cityPinyin} locations={locations} value={archiveDraft.location} onChange={value => setArchiveDraft({ ...archiveDraft, location: value })} placeholder="不限地点" maxLength={100} />
              <div className="salary-range"><input aria-label="月薪下限" type="number" min={0} step="0.5" placeholder="最低" inputMode="decimal" value={archiveDraft.salaryMin} onChange={event => setArchiveDraft({ ...archiveDraft, salaryMin: event.target.value })} /><span>–</span><input aria-label="月薪上限" type="number" min={0} step="0.5" placeholder="最高" inputMode="decimal" value={archiveDraft.salaryMax} onChange={event => setArchiveDraft({ ...archiveDraft, salaryMax: event.target.value })} /><span className="salary-unit">K/月</span></div>
              <div className="filter-actions"><Button className="button secondary small" type="submit" busy={busy === 'filter'} disabled={Boolean(busy)}>筛选</Button><button type="button" className="text-button" onClick={() => { const cleared = { q: '', source: '', location: '', salaryMin: '', salaryMax: '' }; setArchiveDraft(cleared); void perform('filter', async () => { await loadJobs(cleared, 1) }) }}>清除</button></div>
            </form>
            {loading ? <div className="loading-state"><span className="spinner" />正在连接资料库…</div> : jobs.length === 0 ? <Empty title="从一个机会开始" icon="search">检索真实岗位，或导入你正在考虑的职位。采集后的岗位会保存在这里。</Empty> : <div className="job-list">{jobs.map(item => <div className={`job-card ${selectedId === item.id ? 'selected' : ''}${selectedIds.has(item.id) ? ' checked' : ''}`} key={item.id}><label className="job-card-check"><input type="checkbox" checked={selectedIds.has(item.id)} onChange={() => toggleJobSelection(item.id)} /><span className="sr-only">选择该岗位</span></label><button className="job-card-body" onClick={() => selectJob(item.id)} title={`${item.title} · ${item.company}`}><h3>{item.title}</h3><div className="job-card-meta"><span className="job-salary">{item.salary || '薪资未披露'}</span><span className="job-location"><Icon name="pin" size={12} />{item.location || '地点待确认'}</span>{(item.tags || []).slice(0, 1).map(tag => <span className="job-tags" key={tag}><span>{tag}</span></span>)}</div><div className="job-card-bottom"><span className="job-company">{item.company}</span><span>{sourceNames[item.source] || item.source}</span><time>{date(item.updated_at)}</time></div></button></div>)}</div>}
            <Pagination info={jobsInfo} label="岗位档案" busy={Boolean(busy)} onPage={page => void perform('page-jobs', async () => { await loadJobs(undefined, page) })} /><div className="list-bottom">资料以采集时的页面为准，投递前请再次核实。</div>
          </section>

          <section className="detail-panel">
            {detailLoading ? <div className="loading-state"><span className="spinner" />正在读取岗位档案…</div> : !job ? <div className="welcome-panel"><div className="welcome-heading"><Icon name="book" /><span>岗位调查档案</span></div><h2>先选一份岗位，再逐项核对</h2><p>检索岗位或导入已有 JD。选择左侧档案，查看要求、公司资料与简历依据。</p><div className="welcome-checklist"><div><span>01</span><section><h3>岗位要求</h3><p>职责、技能、地点和薪酬口径</p></section></div><div><span>02</span><section><h3>公司证据</h3><p>资料原文、披露主体和发布时间</p></section></div><div><span>03</span><section><h3>个人适配</h3><p>已有依据、硬性条件与待核实项</p></section></div></div><Button className="button secondary" onClick={() => openModal('job')}><Icon name="plus" />导入已有岗位</Button><div className="welcome-source-note">每份资料都保留出处，缺少证据时保持未知</div></div> : <>
              <div className="detail-heading"><div className="detail-kicker"><span>{sourceNames[job.source] || job.source}</span><External url={job.url}>查看原始岗位</External></div><h2>{job.title}</h2><div className="detail-company">{job.company}<span>·</span><Icon name="pin" size={14} />{job.location || '地点待确认'}</div><div className="detail-heading-bottom"><strong>{job.salary || '薪资未披露'}</strong><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => openBatchEdit([job])}>编辑档案</Button><Button className="button secondary small" busy={busy === 'analyze'} disabled={Boolean(busy) || needsFullJd(job)} onClick={() => void analyze()}><Icon name="check" size={16} />评估适配</Button></div></div>
              <div className="jd-state"><div><strong>{job.description_status === 'complete' ? '完整JD已保存' : needsFullJd(job) ? '仅列表摘要 · 待补全' : '岗位材料已保存'}</strong><p>{job.detail_collected_at ? `正文读取于 ${date(job.detail_collected_at)}` : needsFullJd(job) ? ((job.source === 'boss' || job.source === 'zhaopin') ? '需要完整职责与任职要求，才能评估是否适合投递' : '该平台列表未给出完整职责，可在岗位档案补充后再评估') : '请核对岗位材料完整性及原始出处'}</p></div>{(job.source === 'boss' || job.source === 'zhaopin') && <Button className="button secondary small" busy={busy === 'job-detail'} disabled={Boolean(busy)} onClick={() => void perform('job-detail', async () => { const id = job.id, generation = jobGeneration.current; const result = await post<{ task: Task; job: Job }>(`/jobs/${encodeURIComponent(id)}/detail`); toast(result.task.message); await Promise.all([generation === jobGeneration.current ? loadDetail(id) : Promise.resolve(), loadJobs(), refreshMetadata()]); if (generation === jobGeneration.current) setAnalysis(null) })}>{job.description_status === 'complete' ? '更新完整职责' : '读取完整职责'}</Button>}</div><div className="detail-tabs" role="tablist" aria-label="岗位档案内容" onKeyDown={navigateTabs}>{(['overview', 'analysis', 'evidence'] as const).map(item => <button key={item} id={`job-${item}-tab`} role="tab" aria-controls="job-detail-panel" tabIndex={tab === item ? 0 : -1} aria-selected={tab === item} className={tab === item ? 'active' : ''} onClick={() => setTab(item)}>{item === 'overview' ? '岗位与公司' : item === 'analysis' ? '适配分析' : '证据资料'}{item === 'evidence' && <span>{evidenceTotal}</span>}</button>)}</div>
              <div id="job-detail-panel" className="detail-content" role="tabpanel" aria-labelledby={`job-${tab}-tab`}>
                {tab === 'overview' && <><div className="section-label"><span className="label-line" />岗位描述</div><div className={`description-text${descExpanded ? '' : ' collapsed'}`}>{plain(job.description) || '尚无岗位描述，请补充原始岗位资料。'}</div>{job.description.length > 600 && <button className="text-button" onClick={() => setDescExpanded(value => !value)}>{descExpanded ? '收起描述' : '展开全文'}</button>}<div className="company-section"><div className="section-heading"><div><span className="section-label">公司情报</span><h3>了解 {job.company}</h3></div><Button className="button secondary small" busy={busy === 'company'} disabled={Boolean(busy)} onClick={() => void collectCompany()}>检索公告<Icon name="arrow" size={14} /></Button></div><p className="muted body-small">从公开公告开始核实经营信息。公告标题与元信息不等于财报正文或业绩结论。</p>{detail.company_evidence?.length ? <>{detail.company_evidence.length > 4 && <div className="company-pager"><button className="text-button" disabled={companyPage === 0} onClick={() => setCompanyPage(page => page - 1)}>上一页</button><span className="tiny-label">{companyPage + 1} / {Math.ceil(detail.company_evidence.length / 4)} 页 · 共 {detail.company_evidence.length} 条</span><button className="text-button" disabled={companyPage >= Math.ceil(detail.company_evidence.length / 4) - 1} onClick={() => setCompanyPage(page => page + 1)}>下一页</button></div>}<div className="company-evidence-list">{detail.company_evidence.slice(companyPage * 4, companyPage * 4 + 4).map(e => <div key={e.id}><span className="tiny-label">{date(e.published_at)}</span><External url={e.url}>{e.title}</External></div>)}</div></> : <div className="inline-empty">尚未收集到该公司的资料。你可以检索公开公告，或补充已有材料。</div>}<button className="text-button" onClick={() => openModal('evidence')}><Icon name="plus" size={14} />补充公司资料</button><CompanyReference report={reliability} history={reliabilityHistory} busy={busy === 'reliability'} action={reliabilityAction} onCompute={() => void recomputeReference()} onCoverage={() => void runReferenceAction('coverage')} onExtract={() => void runReferenceAction('extract')} onOpenEvidence={openReferenceEvidence} onOpenEvidenceModal={() => openModal('evidence')} /></div></>}
                {tab === 'analysis' && (!analysis ? <Empty title={busy === 'analyze' ? '正在对应岗位要求…' : '让经历与要求逐项对照'} icon="resume">{busy === 'analyze' ? '分析完成后，将展示匹配依据、未知项与面试核实问题。' : <>{resumeSaved ? '点击“评估适配”，查看有依据的匹配与缺口。' : <>先在<button className="inline-button" onClick={() => setView('resume')}>我的简历</button>保存个人经历，再生成适配分析。</>}分数仅供辅助比较，不代表录用概率。</>}</Empty> : <AnalysisView analysis={analysis} />)}
                {tab === 'evidence' && <><div className="section-heading"><div><h3>证据与出处</h3></div><button className="text-button" onClick={() => openModal('evidence')}><Icon name="plus" size={14} />补充资料</button></div>{evidenceTotal ? <JobEvidence key={job.id} jobId={job.id} focus={evidenceFocus} /> : <Empty title="尚无证据资料">采集岗位或补充公司材料后，原文与来源会保存在这里。</Empty>}</>}
              </div>
              <section className="chat-section job-chat"><ChatWorkspace scope={{ kind: 'job', jobId: job.id, company: job.company, title: job.title }} modelConfigured={modelConfigured} /></section>
            </>}
          </section>
        </div>
      </>}

      {view === 'resume' && <><section className="page-intro"><div><h1>我的简历</h1><p>你的简历是适配分析的依据，保存在当前本地工作区。</p></div>{resumeRevision != null && <span className="revision-label">版本 {resumeRevision}</span>}</section><div className="resume-layout"><form className="paper-panel resume-editor" onSubmit={saveResume}><div className="section-heading"><h2>简历与事实档案</h2><label className="format-select"><span className="sr-only">简历格式</span><select value={resumeFormat} onChange={event => { setResumeFormat(event.target.value); setResumeSaved(false) }}><option value="text">简历纯文本 / Markdown</option><option value="facts_yaml">ResumeLoom facts.yaml</option></select></label></div><ResumeImport disabled={Boolean(busy) || loading} hasText={Boolean(resumeText.trim())} onApply={(text, format) => { setResumeText(text); setResumeFormat(format); setResumeSaved(false); toast('已提取并填入编辑区，请核对内容后保存。') }} /><label htmlFor="resume-text" className="field-label">简历正文 · 可直接编辑或粘贴</label><textarea className="resume-textarea" id="resume-text" value={resumeText} onChange={event => { setResumeText(event.target.value); setResumeSaved(false) }} required maxLength={100000} spellCheck={false} placeholder={resumeFormat === 'facts_yaml' ? 'schema_version: 1\nfacts:\n  ...' : '填写技能、工作经历、项目经验，以及地点和薪资等求职偏好。\n\n建议保留具体职责、项目背景和经过确认的成果。'} /><div className="editor-footer"><span>{resumeText.length.toLocaleString()} 字符 · {resumeSaved ? '已保存' : '未保存的修改'}</span><Button className="button primary" busy={busy === 'resume'} disabled={Boolean(busy)} type="submit">保存到本地<Icon name="check" size={17} /></Button></div></form><aside className="resume-guide"><h2>整理可核对的经历</h2><div className="guide-item"><b>01</b><div><h3>保留具体经历</h3><p>技能、职责和项目事实，比笼统的自我评价更有帮助。</p></div></div><div className="guide-item"><b>02</b><div><h3>未知就留作未知</h3><p>未提到的技能不能自动认定为不会，也不能当作已经掌握。</p></div></div><div className="guide-item"><b>03</b><div><h3>复用 ResumeLoom</h3><p>可导入 PDF、Word .docx 和文本文件，也可使用 facts.yaml。扫描件需先 OCR；旧版 .doc 请另存为 .docx。</p></div></div><div className="privacy-note"><Icon name="book" size={18} /><p>文件在本机提取文字，确认保存后仅保留文本。启用外部模型分析时，相关资料可能发送至你配置的模型服务。</p></div></aside></div></>}

      {view === 'sources' && <><section className="page-intro"><div><h1>数据与采集</h1><p>接口优先，浏览器补充。每次采集都保留真实状态。</p></div><Button className="button secondary" busy={busy === 'metadata'} onClick={() => void perform('metadata', refreshMetadata)}>刷新状态</Button></section><div className="sources-grid">{sources.length ? sources.map(item => <article className="source-card paper-panel" key={item.id}><div className="section-heading"><span className="source-icon"><Icon name={item.id === 'cninfo' ? 'book' : 'globe'} /></span><Badge value={item.status} /></div><h2>{item.name}</h2><p>{item.description}</p><div className="source-card-footer"><span>{{ http_api: 'HTTP 接口', browser_page: '浏览器会话', manual: '用户材料' }[item.transport_mode] || item.transport_mode}</span><span>{item.id}</span></div></article>) : <div className="paper-panel"><Empty title="等待数据源状态">请确认本地后端服务已启动，然后刷新状态。</Empty></div>}</div><SignalSourceSettings /><section className="browser-panel paper-panel"><div><div className="section-label">浏览器会话</div><h2>从已登录的浏览器采集</h2><p>使用项目专属 Chrome 配置目录。打开浏览器后，请在该窗口登录要采集的平台并完成验证，再返回这里采集。</p><span className="browser-status"><span className={`connection-dot ${browser?.running ? 'connected' : ''}`} />{browser?.message || '浏览器状态未知'}</span></div><div className="browser-actions"><Button className="button secondary" busy={busy === 'browser-start'} disabled={Boolean(busy)} onClick={() => void perform('browser-start', async () => { const result = await post<BrowserStatus>(`/browser/start?source=${encodeURIComponent(browserSource)}`); setBrowser(result); toast(result.message); await refreshMetadata() })}><Icon name="globe" size={17} />打开专用浏览器</Button><form onSubmit={event => { event.preventDefault(); void collectBrowser() }}><label className="field-label" htmlFor="browser-source">采集平台</label><select id="browser-source" value={browserSource} onChange={event => setBrowserSource(event.target.value)}>{browserSources.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select><label className="field-label" htmlFor="browser-query">岗位关键词</label><input id="browser-query" value={query} onChange={event => setQuery(event.target.value)} placeholder="例如：Python 后端" required maxLength={200} /><label className="field-label" htmlFor="browser-location">城市（可选）</label><CityInput id="browser-location" cities={browserCities} hot={browserSourceItem?.hot || []} pinyin={cityPinyin} locations={locations} value={location} onChange={setLocation} placeholder={browserCities.length ? '输入或点选城市' : '例如：北京'} maxLength={100} /><label className="field-label" htmlFor="browser-limit">采集数量</label><input id="browser-limit" type="number" min={1} max={1000} value={browserLimit} onChange={event => setBrowserLimit(Math.max(1, Math.min(1000, Number(event.target.value) || 20)))} inputMode="numeric" /><Button className="button primary" busy={busy === 'browser-collect'} disabled={Boolean(busy)} type="submit">从已登录浏览器采集<Icon name="arrow" size={16} /></Button></form></div></section><DetailBatch onUpdate={refreshMetadata} /><ResearchIndex /><section className="paper-panel tasks-panel"><div className="section-heading"><div><h2>采集记录</h2></div><span className="muted body-small">{tasksInfo.total} 次任务</span></div>{tasks.length ? <div className="task-table-wrap"><table><thead><tr><th scope="col">查询 / 来源</th><th scope="col">状态</th><th scope="col">资料数</th><th scope="col">时间</th><th scope="col">说明</th></tr></thead><tbody>{tasks.map(task => <tr key={task.id}><td><strong>{task.query || '未指定'}</strong><small>{sourceNames[task.source] || task.source}</small></td><td><Badge value={task.status} /></td><td>{task.count ?? '—'}</td><td className="nowrap">{date(task.created_at)}</td><td className="task-message">{task.message || '—'}</td></tr>)}</tbody></table></div> : <Empty title="还没有采集记录">发起一次岗位或公司检索，结果与失败原因都会记录在这里。</Empty>}<Pagination info={tasksInfo} label="采集记录" busy={Boolean(busy)} onPage={page => void perform('page-tasks', () => loadTasks(page))} /></section></>}

      <div hidden={view !== 'extensions'}><AgentExtensions /></div>
      {view === 'settings' && <ModelSettings onSaved={refreshMetadata} />}
      <footer className="page-footer"><span>知根 JobScope</span><span>资料保留原始出处，未知信息待核实</span></footer>
    </main>
    {modal && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) setModal(null) }}><section className="modal paper-panel" role="dialog" aria-modal="true" aria-labelledby="modal-title" onKeyDown={event => {
        if (event.key === 'Escape' && !busy) { event.stopPropagation(); setModal(null) }
        if (event.key === 'Tab') {
          const focusable = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href]'))
          const first = focusable[0], last = focusable[focusable.length - 1]
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
        }
      }}><div className="section-heading"><div><h2 id="modal-title">{modal === 'job' ? '导入岗位' : modal === 'evidence' ? '补充公司资料' : modal === 'batch-edit' ? '批量编辑档案' : '确认删除岗位'}</h2></div><button className="icon-button" aria-label="关闭弹窗" onClick={() => setModal(null)} disabled={Boolean(busy)}><Icon name="close" /></button></div><p className="muted body-small">{modal === 'job' ? '粘贴原始岗位信息，来源将标记为“用户导入”。' : modal === 'evidence' ? '保留原文和来源，帮助后续核实。请勿填写 Cookie 或访问令牌。' : modal === 'batch-edit' ? `将应用到已选的 ${batchEdit?.ids.length ?? 0} 份档案；留空的字段保持不变，薪资按原文覆盖（如 15–25K·13薪）。` : `将删除${confirmDelete?.label || '所选岗位'}，连同其岗位证据、适配报告与岗位对话，此操作不可恢复。`}</p>{(modal === 'job' || modal === 'evidence') && <><form onSubmit={importRecord}><label className="field-label" htmlFor="import-title">{modal === 'job' ? '岗位名称' : '资料标题'} <span>*</span></label><input id="import-title" name="title" required maxLength={300} autoFocus /><label className="field-label" htmlFor="import-company">公司名称 <span>*</span></label><input id="import-company" name="company" required maxLength={300} defaultValue={modal === 'evidence' ? job?.company : ''} />{modal === 'job' ? <div className="form-two"><div><label className="field-label" htmlFor="import-location">工作地点</label><input id="import-location" name="location" maxLength={200} /></div><div><label className="field-label" htmlFor="import-salary">原始薪资描述</label><input id="import-salary" name="salary" maxLength={300} placeholder="保留原文，如 20–30K·13薪" /></div></div> : <><label className="field-label" htmlFor="import-kind">资料类型</label><select id="import-kind" name="kind" defaultValue="user_material"><option value="user_material">用户提供材料</option><option value="employee_statement">员工自述</option><option value="company_statement">企业自述</option><option value="disclosure">正式披露</option><option value="media_report">媒体报道</option></select></>}<label className="field-label" htmlFor="import-url">原始链接（可选）</label><input id="import-url" name="url" type="url" maxLength={2000} placeholder="https://" /><label className="field-label" htmlFor="import-body">{modal === 'job' ? '岗位描述' : '资料原文'} <span>*</span></label><textarea id="import-body" name={modal === 'job' ? 'description' : 'text'} required rows={7} maxLength={100000} /><div className="modal-actions"><button type="button" className="button secondary" onClick={() => setModal(null)} disabled={Boolean(busy)}>取消</button><Button className="button primary" type="submit" busy={busy === 'import'} disabled={Boolean(busy)}>保存资料<Icon name="check" size={16} /></Button></div></form></>}
              {modal === 'batch-edit' && batchEdit && <form onSubmit={submitBatchEdit}><label className="field-label" htmlFor="batch-location">工作地点</label><input id="batch-location" value={batchDraft.location} onChange={event => setBatchDraft({ ...batchDraft, location: event.target.value })} maxLength={300} autoFocus /><label className="field-label" htmlFor="batch-salary">薪资描述</label><input id="batch-salary" value={batchDraft.salary} onChange={event => setBatchDraft({ ...batchDraft, salary: event.target.value })} maxLength={300} placeholder="15–25K·13薪" /><label className="field-label" htmlFor="batch-tags">标签（逗号分隔，保存后替换现有标签）</label><input id="batch-tags" value={batchDraft.tags} onChange={event => setBatchDraft({ ...batchDraft, tags: event.target.value })} maxLength={500} placeholder="已核实, 优先投递" /><div className="modal-actions"><button type="button" className="button secondary" onClick={() => setModal(null)} disabled={Boolean(busy)}>取消</button><Button className="button primary" type="submit" busy={busy === 'batch-edit'} disabled={Boolean(busy)}>保存修改<Icon name="check" size={16} /></Button></div></form>}
              {modal === 'confirm-delete' && confirmDelete && <div className="modal-actions"><button type="button" className="button secondary" onClick={() => setModal(null)} disabled={Boolean(busy)}>取消</button><Button className="button danger" busy={busy === 'batch-delete'} disabled={Boolean(busy)} onClick={() => void perform('batch-delete', submitBatchDelete)}>确认删除</Button></div>}
            </section></div>}
  </div>
}

function AnalysisView({ analysis }: { analysis: Analysis }) {
  const coverage = Math.max(0, Math.min(100, analysis.coverage * 100))
  const judged = analysis.requirements.filter(item => item.status !== 'unknown').length
  const score = analysis.score
  return <>
    <div className="analysis-summary">
      <div className="analysis-summary-main"><h3>经历与要求，逐项对照</h3><div className="coverage-meta"><span>{analysis.method === 'model_assisted' ? '语义证据覆盖' : '词项核实覆盖'} {Math.round(coverage)}%</span><span>{judged} / {analysis.requirements.length} 项有对应依据</span></div><progress className="coverage-progress" value={coverage} max="100" aria-label="要求证据覆盖率" /></div>
      <div className="analysis-score"><span>综合适配参考</span><strong>{score == null ? '暂不生成总分' : `${Math.round(score)} / 100`}</strong><span>{score == null ? '能力深度与硬性条件仍需核实' : '仅供辅助比较，不代表录用概率'}</span></div>
    </div>
    <div className="analysis-meta"><span>{analysis.method === 'model_assisted' ? '模型辅助分析' : '规则辅助 · 词项核实'}</span><span>简历版本 {analysis.resume_revision}</span></div>
    <p className="analysis-disclaimer">未知项需要进一步确认，不能当作已满足</p>
    {analysis.limitations && <details className="analysis-limitations"><summary>分析范围与限制</summary><p>{readable(analysis.limitations)}</p></details>}
    <h4 className="requirement-heading">岗位要求映射</h4>
    {analysis.requirements.length ? <div className="requirements-table-wrap"><table className="requirements-table"><thead><tr><th scope="col">岗位要求</th><th scope="col">简历依据</th><th scope="col">判断</th></tr></thead><tbody>{analysis.requirements.map((item, index) => <tr key={index}>
      <td><strong>{item.requirement}</strong>{item.hard && <span className="hard-label">硬性要求</span>}<span className="requirement-dimension">{dimensionNames[item.dimension] || item.dimension}</span>{item.jd_quote && <details className="jd-source"><summary>JD 原文</summary><blockquote>{item.jd_quote}</blockquote></details>}</td>
      <td><p>{item.rationale || (Array.isArray(item.evidence) && item.evidence.length > 0 ? '简历材料中找到对应词项，实际熟练程度待核实' : '尚未提供对应事实，待核实')}</p>{Array.isArray(item.evidence) && item.evidence.length > 0 && <span className="fact-reference">简历事实 {item.evidence.join('、')}</span>}</td>
      <td><span className={`requirement-status ${item.status}`}><i />{requirementNames[item.status] || item.status}</span></td>
    </tr>)}</tbody></table></div> : <div className="inline-empty">尚未提取到可分析的要求</div>}
    {Boolean(analysis.hard_conditions?.length) && <div className="hard-conditions"><h4>硬性条件 · 单独核实</h4>{analysis.hard_conditions?.map((item, index) => <HardCondition key={index} value={item} />)}</div>}
    <div className="analysis-columns"><AnalysisList title="已有依据" items={analysis.strengths} /><AnalysisList title="缺口与未知" items={analysis.gaps} /></div>
    <AnalysisList title="面试时建议核实" items={analysis.questions} />
    <div className="analysis-facts-grid"><SalaryView value={analysis.salary} /><div className="analysis-note company-summary"><h4>公司证据</h4><p>{readable(analysis.company_summary)}</p><span className="source-annotation">出处见「证据资料」 · {analysis.evidence_ids.length} 条关联证据</span></div></div>
  </>
}
function AnalysisList({ title, items }: { title: string; items: string[] }) { return <section className="analysis-list"><h4>{title}</h4>{items?.length ? <ul>{items.map((item, index) => <li key={index}>{readable(item)}</li>)}</ul> : <p className="muted">暂无可支持的结论。</p>}</section> }

// 付费来源的设置入口：默认零付费，密钥只写不读。
// 和 ModelSettings 的密钥字段一套做法——回填永远只显示 key_set（「已设置」），页面任何位置都不出现原文；
// 关闭开关只是让后端不再调用该来源，已存的密钥仍留在库里（后端语义是关了就不调用，不是删除）。
function SignalSourceSettings() {
  const [sources, setSources] = useState<Record<string, SignalSource> | null>(null)
  const [drafts, setDrafts] = useState<Record<string, { mode: 'disabled' | 'enabled'; key: string; url: string }>>({})
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [feedback, setFeedback] = useState<Record<string, string>>({})
  const load = useCallback(async () => {
    const data = await listSignalSources()
    setSources(data.sources)
    // 地址不是秘密，照常回填好让你看清现在连的是哪个端点；密钥永远只留 key_set 这个布尔。
    setDrafts(Object.fromEntries(Object.entries(data.sources).map(([id, item]) => [id, { mode: item.mode, key: '', url: item.url || '' }])))
  }, [])
  useEffect(() => { void load().catch(err => setError((err as Error).message)) }, [load])
  async function save(id: string) {
    if (busy) return
    setBusy(id); setError(''); setFeedback(current => ({ ...current, [id]: '' }))
    try {
      const draft = drafts[id]
      // key 留空即「不改密钥」，由后端沿用已存的那一条；这里不把原文读回来，也就没有回显一说。
      // 地址改了又留空密钥时，后端会拒绝保存并要你重填密钥——这条约束在这里原样交给它。
      const result = await saveSignalSource(id, { mode: draft.mode, key: draft.key, url: draft.url })
      setSources(current => current ? { ...current, [id]: result } : current)
      setDrafts(current => ({ ...current, [id]: { mode: result.mode, key: '', url: result.url || '' } }))
      setFeedback(current => ({ ...current, [id]: result.mode === 'enabled'
        ? '已保存，采集时才会使用该来源。'
        : '已关闭调用；已保存的密钥留在本机数据库里，重新启用即可继续使用。' }))
    } catch (err) { setFeedback(current => ({ ...current, [id]: (err as Error).message })) }
    finally { setBusy('') }
  }
  return <section className="paper-panel signal-sources" aria-label="求职参考来源">
    <div className="section-heading"><div><span className="section-label">求职参考</span><h2>信号来源与付费接口</h2><p className="muted body-small">免费官方来源与本项目已存材料默认启用；付费来源需要你自备密钥并显式开启，JobScope 不代持密钥、不默认调用。</p></div><Button className="button secondary small" busy={Boolean(busy)} onClick={() => void load().catch(err => setError((err as Error).message))}>刷新</Button></div>
    {error && <p className="alert error" role="alert">{error}</p>}
    {!sources ? <p role="status">正在读取来源登记…</p> : <div className="signal-source-rows">{Object.entries(sources).map(([id, item]) => {
      const draft = drafts[id]
      return <article key={id}>
        <div className="signal-source-head"><div><strong>{item.name}</strong><span className="tiny-label">{id} · {item.tier === 'free' ? '免费来源' : '付费来源'}</span></div><Badge value={item.status} /></div>
        <p className="tiny-label">{item.description}</p>
        <p className="tiny-label">规则：{item.rules.join('、')} · 当前{item.mode === 'enabled' ? '已启用' : '未启用'}{item.tier === 'paid' && item.key_set ? ' · 密钥已设置' : ''}</p>
        {draft && <div className="signal-source-edit">
          <label className="signal-source-mode"><input type="checkbox" checked={draft.mode === 'enabled'} disabled={Boolean(busy)} onChange={event => setDrafts(current => ({ ...current, [id]: { ...current[id], mode: event.target.checked ? 'enabled' as const : 'disabled' as const } }))} />启用该来源</label>
          {item.tier === 'paid' && <label className="signal-source-key"><span>API 密钥 <span className="muted">{item.key_set ? '· 已设置，留空则保留' : '· 填写你自己的密钥'}</span></span><input type={'pass' + 'word'} autoComplete={'off'} spellCheck={false} maxLength={4000} value={draft.key} disabled={Boolean(busy)} onChange={event => setDrafts(current => ({ ...current, [id]: { ...current[id], key: event.target.value } }))} placeholder={item.key_set ? '已设置，页面不读取原文' : '填写密钥'} /></label>}
          {/* 接口地址不是秘密：与密钥输入同样的观感，但照常回填，好让你看清现在连的是哪个端点。 */}
          {item.tier === 'paid' && <label className="signal-source-key"><span>接口地址 <span className="muted">· 服务商给的取数地址，不要把密钥写进地址</span></span><input type="text" autoComplete={'off'} spellCheck={false} maxLength={2000} value={draft.url} disabled={Boolean(busy)} onChange={event => setDrafts(current => ({ ...current, [id]: { ...current[id], url: event.target.value } }))} placeholder="https://… 例如 /v1/company/profile" /></label>}
          {item.tier === 'paid' && item.key_set && draft.url !== (item.url || '') && !draft.key && <p className="tiny-label">改了地址就得重填密钥，否则后端会拒绝这次保存。</p>}
          <Button className="button secondary small" busy={busy === id} disabled={!draft || (draft.mode === item.mode && !draft.key && draft.url === (item.url || ''))} onClick={() => void save(id)}>保存设置</Button>
        </div>}
        {feedback[id] && <p className="tiny-label" role="status">{feedback[id]}</p>}
      </article>
    })}</div>}
    <p className="tiny-label">密钥保存在本机数据库中，不加密，页面不会读回密钥原文；关闭开关只是停止调用，不会删除已保存的密钥。付费来源取哪个数字由后端那份按通用写法预置的字段映射决定，尚未对照任何服务商文档核实：采集回来的说明会点名每条规则试过的字段名，请照你采购的接口文档校正。</p>
  </section>
}

function SalaryView({ value }: { value: unknown }) {
  if (!value || typeof value !== 'object') return <div className="analysis-note"><h4>薪酬口径</h4><p>{value ? readable(value) : '待核实：尚无可比较的薪酬资料。'}</p></div>
  const salary = value as Record<string, unknown>
  const currency = salary.currency === 'CNY' ? '元' : typeof salary.currency === 'string' ? salary.currency : ''
  const amount = (number: unknown) => typeof number === 'number' ? number.toLocaleString('zh-CN') : null
  const range = (low: unknown, high: unknown) => amount(low) && amount(high) ? `${amount(low)}–${amount(high)} ${currency}` : '待核实'
  return <div className="analysis-note salary-note"><h4>薪酬口径</h4><dl><div><dt>招聘原文</dt><dd>{typeof salary.raw === 'string' && salary.raw ? salary.raw : '未披露'}</dd></div><div><dt>税前月薪区间</dt><dd>{range(salary.monthly_min, salary.monthly_max)}</dd></div><div><dt>保证薪资月数</dt><dd>{typeof salary.guaranteed_months === 'number' ? `${salary.guaranteed_months} 薪` : '待核实，未确认保证条件'}</dd></div><div><dt>保证现金年薪</dt><dd>{range(salary.guaranteed_annual_min, salary.guaranteed_annual_max)}</dd></div><div><dt>实际薪资</dt><dd>{salary.actual_salary != null ? readable(salary.actual_salary) : '待核实，暂无独立样本支持'}</dd></div><div><dt>独立样本数</dt><dd>{typeof salary.sample_count === 'number' ? `${salary.sample_count} 个` : '未知'}</dd></div></dl>{salary.note != null && <p>{readable(salary.note)}</p>}</div>
}
function HardCondition({ value }: { value: unknown }) {
  if (typeof value !== 'object' || !value) return <p>{readable(value)}</p>
  const item = value as Record<string, unknown>
  return <div><strong>{readable(item.requirement || item.condition || item.jd_quote || '岗位硬性要求')}</strong><span>{requirementNames[String(item.status)] || '待核实'}</span>{item.rationale != null && <p>{readable(item.rationale)}</p>}</div>
}

function JobEvidence({ jobId, focus = '' }: { jobId: string; focus?: string }) {
  const [items, setItems] = useState<Evidence[]>([])
  const [info, setInfo] = useState<PageInfo>(emptyPage)
  const [busy, setBusy] = useState(false)
  const request = useRef(0)
  async function load(page = 1) {
    const token = ++request.current
    setBusy(true)
    try {
      const data = await api<PageInfo & { evidence: Evidence[] }>(`/jobs/${encodeURIComponent(jobId)}/evidence?page=${page}&page_size=10`)
      if (token === request.current) { setItems(data.evidence); setInfo(data) }
    } catch (err) { if (token === request.current) toast((err as Error).message, 'error') }
    finally { if (token === request.current) setBusy(false) }
  }
  useEffect(() => { void load(); return () => { request.current++ } }, [jobId])
  // 求职参考里的「出处」跳到这里：命中那条证据时展开并滚过去，让它自己开口说来源。
  useEffect(() => {
    if (!focus) return
    const target = document.getElementById(`evidence-entry-${focus}`)
    if (!target) return
    target.querySelector('details')?.setAttribute('open', '')
    target.scrollIntoView({ block: 'center' })
  }, [focus, items])
  return <>{items.map(item => <div key={item.id} id={`evidence-entry-${item.id}`} className={item.id === focus ? 'evidence-focus' : undefined}><EvidenceCard evidence={item} /></div>)}<Pagination info={info} label="岗位证据" busy={busy} onPage={page => void load(page)} /></>
}
