import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent, ReactNode } from 'react'
import { api, post, readable, plain, date, isStale, safeUrl, computeReliability, collectCompanySignals, extractCompanySignals, getReliability, getReliabilityHistory, getSourceHealth, patchJobs, startCompanyResearch } from './api'
import type { ConversationNavigation, SourceHealth } from './api'
import { SignalSourceSettings } from './SignalSourceSettings'
import { signalTaskNames } from './SignalSources'
import { toast, dismissToast, failedTaskToast, taskSucceeded } from './toast'
import { Pagination, emptyPage } from './Pagination'
import { EvidenceWorkspace } from './EvidenceWorkspace'
import { EvidenceCard } from './EvidenceCard'
import { DetailBatch } from './DetailBatch'
import { CompanyLookup } from './CompanyLookup'
import { CompanyCompare } from './CompanyCompare'
import { CityInput } from './CityInput'
import type { SavedLocation } from './CityInput'
import { ResearchIndex } from './ResearchIndex'
import { AgentExtensions } from './AgentExtensions'
import { ModelSettings } from './ModelSettings'
import { SystemSettings } from './SystemSettings'
import { CompanyReference } from './CompanyReference'
import { ResumeImport } from './ResumeImport'
import { ResumeWorkshop } from './ResumeWorkshop'
import type { WorkshopPrefill } from './ResumeWorkshop'
import { KnowledgeView } from './KnowledgeView'
import { navigateTabs } from './tabNavigation'
import { useCompareSelection } from './compareSelectionStore'
import { ChatWorkspace } from './chat/ChatWorkspace'
import { Icon } from './Icon'
import { JobCard } from './JobCard'
import { FavoritesView } from './FavoritesView'
import { ManualJobDescription } from './ManualJobDescription'
import { BossImport } from './BossImport'
import type { Analysis, BrowserStatus, Evidence, Job, PageInfo, ReliabilityHistory, ReliabilityReport, Source, Task } from './api'

const transportNames: Record<string, string> = { http_api: '接口', browser_page: '需浏览器', company_web: '网页快查', manual: '手动' }
const statusNames: Record<string, string> = { completed: '已完成', success: '已完成', running: '采集中', pending: '等待中', cancelled: '已停止', failed: '失败', needs_login: '需要登录', needing_login: '需要登录', login_required: '需要登录', needs_verification: '需要验证', verification_required: '需要验证', requires_verification: '需要验证', unsupported: '暂不支持', available: '可用', ready: '可用', not_configured: '未配置', blocked: '访问受限', backoff: '退避等待', opt_in: '需你自行开启', skipped: '已跳过', degraded: '已降级留档' }
const dimensionNames: Record<string, string> = { skill: '技能', skills: '技能', experience: '经历与职责', domain: '行业与业务', industry: '行业与业务', preference: '求职偏好', preferences: '求职偏好' }
const requirementNames: Record<string, string> = { met: '满足', matched: '满足', satisfied: '满足', partial: '部分满足', not_met: '未满足', unmet: '未满足', missing: '未满足', unknown: '待确认', confirmed: '满足' }
function needsFullJd(job: Job) {
  return job.description_status === 'listing' || (['boss', 'zhaopin'].includes(job.source) && job.description_status !== 'complete')
}
function jobQuery(filters: { q: string; location: string; source: string; salaryMin: string; salaryMax: string; starred?: boolean }) {
  // 薪资输入单位是 K/月，后端按元比较；留空表示不限制。
  const params = new URLSearchParams({ q: filters.q, location: filters.location, source: filters.source, page_size: '100' })
  if (filters.salaryMin) params.set('salary_min', String(Math.round(Number(filters.salaryMin) * 1000)))
  if (filters.salaryMax) params.set('salary_max', String(Math.round(Number(filters.salaryMax) * 1000)))
  if (filters.starred) params.set('starred', 'true')
  return params
}
function Button({ children, busy, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { busy?: boolean }) { return <button {...props} disabled={props.disabled || busy}>{busy ? <span className="spinner" /> : null}{children}</button> }
function Empty({ title, children, icon = 'book' }: { title: string; children: ReactNode; icon?: string }) { return <div className="empty"><div className="empty-icon"><Icon name={icon} /></div><h3>{title}</h3><p>{children}</p></div> }
function Badge({ value }: { value: string }) { return <span className={`badge ${['completed', 'success', 'available', 'ready'].includes(value) ? 'good' : ['failed', 'blocked'].includes(value) ? 'bad' : ''}`}><span />{statusNames[value] || value}</span> }
function External({ url, children }: { url: string; children: ReactNode }) { const href = safeUrl(url); return href ? <a className="external-link" href={href} target="_blank" rel="noreferrer">{children}<Icon name="external" size={13} /></a> : <span className="muted">无来源链接</span> }

type ViewId = 'jobs' | 'favorites' | 'resume' | 'workshop' | 'sources' | 'evidence' | 'compare' | 'global' | 'knowledge' | 'settings'
type SettingsTab = 'model' | 'system' | 'extensions'
// 导航二级菜单：按工作流分四组；组可折叠，展开状态持久化，切换视图自动展开所在组。
const NAV_GROUPS: { id: string; label: string; items: { id: ViewId; icon: string; label: string }[] }[] = [
  { id: 'research', label: '岗位研究', items: [
    { id: 'jobs', icon: 'jobs', label: '岗位研究' },
    { id: 'favorites', icon: 'star', label: '收藏' },
    { id: 'evidence', icon: 'book', label: '公司资料库' },
    { id: 'compare', icon: 'chart', label: '数据分析' },
  ] },
  { id: 'dialogue', label: '对话与知识', items: [
    { id: 'global', icon: 'chat', label: '全局对话' },
    { id: 'knowledge', icon: 'knowledge', label: '知识库' },
  ] },
  { id: 'profile', label: '个人材料', items: [
    { id: 'resume', icon: 'resume', label: '我的简历' },
    { id: 'workshop', icon: 'wand', label: '简历工坊' },
  ] },
  { id: 'manage', label: '系统管理', items: [
    { id: 'sources', icon: 'source', label: '数据与采集' },
    { id: 'settings', icon: 'settings', label: '设置中心' },
  ] },
]
const NAV_STORAGE_KEY = 'jobscope-nav-expanded'

export default function App() {
  const [view, setView] = useState<ViewId>('jobs')
  // 导航组展开状态：默认全展开（首次访问能看到全部功能）；切换视图自动展开所在组，从不自动收起。
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(() => {
    try {
      const stored = localStorage.getItem(NAV_STORAGE_KEY)
      return stored ? new Set(JSON.parse(stored) as string[]) : new Set(NAV_GROUPS.map(group => group.id))
    } catch { return new Set(NAV_GROUPS.map(group => group.id)) }
  })
  // 知识库侧栏角标：轻请求取 total，增删改经 KnowledgeView 回写。
  const [knowledgeTotal, setKnowledgeTotal] = useState(0)
  function toggleGroup(id: string) {
    setExpandedGroups(previous => {
      const next = new Set(previous)
      if (next.has(id)) next.delete(id); else next.add(id)
      try { localStorage.setItem(NAV_STORAGE_KEY, JSON.stringify([...next])) } catch { /* 存储不可用时仅本次会话生效 */ }
      return next
    })
  }
  useEffect(() => {
    const group = NAV_GROUPS.find(item => item.items.some(nav => nav.id === view))
    if (!group) return
    setExpandedGroups(previous => {
      if (previous.has(group.id)) return previous
      const next = new Set([...previous, group.id])
      try { localStorage.setItem(NAV_STORAGE_KEY, JSON.stringify([...next])) } catch { /* 同上 */ }
      return next
    })
  }, [view])
  // 岗位详情「优化简历」跳转工坊时的预填（工坊消费后清空，避免切回时重复生效）。
  const [workshopPrefill, setWorkshopPrefill] = useState<WorkshopPrefill | null>(null)
  // 横评已选列表（共享 store）：侧栏徽标用；资料库/岗位详情「加入横评」直接写这份 store。
  const compareSelected = useCompareSelection()
  const [navigationOpen, setNavigationOpen] = useState(false)
  const [chatNavigation, setChatNavigation] = useState<ConversationNavigation | null>(null)
  // 全局对话的预填指令（如求职参考的「让 Agent 协同补齐」）：token 保证同一段指令可重复触发。
  const [chatPrompt, setChatPrompt] = useState<{ text: string; token: string } | null>(null)
  const openGlobalAgent = useCallback((prompt: string) => {
    setChatPrompt({ text: prompt, token: `${Date.now()}-${Math.random().toString(36).slice(2)}` })
    setView('global')
  }, [])
  const navigateConversation = useCallback((request: ConversationNavigation) => {
    if (request.scope.kind === 'job') throw new Error('扩大范围只能前往公司或全局对话')
    setChatNavigation(request)
    setView(request.scope.kind === 'global' ? 'global' : 'evidence')
    setNavigationOpen(false)
  }, [])
  const chatOpened = useCallback((requestId: string) => {
    setChatNavigation(current => current?.requestId === requestId ? null : current)
    toast('范围已扩大，可继续提问')
  }, [])
  const [sourcesTab, setSourcesTab] = useState<'collect' | 'lookup' | 'catalog' | 'history' | 'health'>('collect')
  // 来源健康度面板：首次切到该页时拉一次，之后手动刷新（不自动轮询，与「任务记录」同一节奏）。
  const [sourceHealth, setSourceHealth] = useState<SourceHealth[] | null>(null)
  useEffect(() => {
    if (view !== 'sources' || sourcesTab !== 'health' || sourceHealth !== null) return
    void getSourceHealth().then(data => setSourceHealth(data.sources)).catch(err => toast((err as Error).message, 'error'))
  }, [view, sourcesTab, sourceHealth])
  // 公司对话入口前移（2026-11-17）：岗位详情直接跳公司对话；token 变化驱动 EvidenceWorkspace 锁主体。
  const [companyChatRequest, setCompanyChatRequest] = useState<{ company: string; token: number } | null>(null)
  function openCompanyChat(company: string) {
    setCompanyChatRequest({ company, token: Date.now() })
    setView('evidence')
  }
  const [jobs, setJobs] = useState<Job[]>([])
  const [jobsInfo, setJobsInfo] = useState<PageInfo>(emptyPage)
  const [jobsTotal, setJobsTotal] = useState(0)
  // 收藏数只做侧栏角标：独立轻请求取 total，不与档案列表混用（收藏变动后单独刷新）。
  const [favTotal, setFavTotal] = useState(0)
  const [tasksInfo, setTasksInfo] = useState<PageInfo>(emptyPage)
  const jobFilters = useRef({ q: '', location: '', source: '', salaryMin: '', salaryMax: '', starred: false })
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
  const [archiveDraft, setArchiveDraft] = useState({ q: '', source: '', location: '', salaryMin: '', salaryMax: '', starred: false })
  const [batchEdit, setBatchEdit] = useState<{ ids: string[] } | null>(null)
  const [tagsChanged, setTagsChanged] = useState(false)
  const [batchDraft, setBatchDraft] = useState({ location: '', salary: '', tags: '' })
  const [confirmDelete, setConfirmDelete] = useState<{ ids: string[]; label: string } | null>(null)
  const [detail, setDetail] = useState<{ job: Job; evidence: Evidence[]; company_evidence: Evidence[]; evidence_total: number } | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const scrollToSelectedDetail = useRef(false)
  const [tab, setTab] = useState<'overview' | 'analysis' | 'evidence'>('overview')
  const [analysis, setAnalysis] = useState<Analysis | null>(null)
  const [reliability, setReliability] = useState<ReliabilityReport | null>(null)
  const [reliabilityHistory, setReliabilityHistory] = useState<ReliabilityHistory | null>(null)
  // 求职参考是详情页里的慢请求：读取中与「还没生成过」必须可区分
  const [reliabilityLoading, setReliabilityLoading] = useState(false)
  const [reliabilityAction, setReliabilityAction] = useState<{ label: string; status: string; message: string } | null>(null)
  const [evidenceFocus, setEvidenceFocus] = useState('')
  const [descExpanded, setDescExpanded] = useState(false)
  const [companyPage, setCompanyPage] = useState(0)
  const [paneFocus, setPaneFocus] = useState<'archive' | 'info' | 'chat'>('info')
  const [discoveryOpen, setDiscoveryOpen] = useState(false)
  const [browserCollecting, setBrowserCollecting] = useState(false)
  const jobScroll = useRef({ window: 0, detail: 0, list: 0 })
  const [settingsReturn, setSettingsReturn] = useState<typeof view>('jobs')
  // 设置中心（2026-11-17）：模型/系统/Agent 扩展三页合一，视图内分标签。原独立视图的跳转点
  // 统一改走 openSettings(tab)，深链行为不变（记录来源工作区，返回键原路回跳）。
  const [settingsTab, setSettingsTab] = useState<SettingsTab>('model')
  function openSettings(tab: SettingsTab) { setSettingsReturn(view); setSettingsTab(tab); setView('settings') }
  function configureModel() { openSettings('model') }
  function openJobChat() { jobScroll.current = { window: window.scrollY, detail: document.querySelector('.detail-panel')?.scrollTop || 0, list: document.querySelector('.job-list-panel')?.scrollTop || 0 }; setPaneFocus('chat') }
  function returnToJob() { setPaneFocus('info'); requestAnimationFrame(() => { window.scrollTo({ top: jobScroll.current.window, behavior: 'instant' }); const detailPane = document.querySelector('.detail-panel'); const listPane = document.querySelector('.job-list-panel'); if (detailPane) detailPane.scrollTop = jobScroll.current.detail; if (listPane) listPane.scrollTop = jobScroll.current.list }) }
  const [resumeText, setResumeText] = useState('')
  const [resumeFormat, setResumeFormat] = useState('text')
  const [resumeRevision, setResumeRevision] = useState<number | null>(null)
  const [resumeSaved, setResumeSaved] = useState(false)
  const [browser, setBrowser] = useState<BrowserStatus | null>(null)
  const [modal, setModal] = useState<'job' | 'evidence' | 'boss-import' | 'batch-edit' | 'confirm-delete' | null>(null)
  const detailRequest = useRef(0)
  const activeJobId = useRef<string | null>(null)
  const jobGeneration = useRef(0)
  const job = detail?.job
  const modalOpener = useRef<HTMLElement | null>(null)
  function openModal(value: 'job' | 'evidence' | 'boss-import' | 'batch-edit' | 'confirm-delete') {
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
    if (token === jobsRequest.current) { setJobs(data.jobs); setJobsInfo(data); const active = filters; if (!active.q && !active.location && !active.source && !active.salaryMin && !active.salaryMax && !active.starred) setJobsTotal(data.total) }
    return data.jobs
  }, [])
  const refreshFavCount = useCallback(async () => {
    // 收藏角标的唯一数据源；失败静默——角标缺一个数字不值得打断主流程。
    try { setFavTotal((await api<PageInfo>('/jobs?starred=true&page_size=1')).total) } catch { /* 角标静默 */ }
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
      api<PageInfo>('/knowledge?page_size=1'),
    ])
    if (results[0].status === 'fulfilled') { setOnline(true); setModelConfigured(results[0].value.model_configured) } else setOnline(false)
    if (results[1].status === 'fulfilled') setSources(results[1].value.sources)
    if (results[2].status === 'fulfilled') { setTasks(results[2].value.tasks); setTasksInfo(results[2].value) }
    if (results[3].status === 'fulfilled') setBrowser(results[3].value)
    if (results[4].status === 'fulfilled') setLocations(results[4].value.locations)
    if (results[5].status === 'fulfilled') setCityPinyin(results[5].value.pinyin)
    if (results[6].status === 'fulfilled') setKnowledgeTotal(results[6].value.total)
  }, [])
  useEffect(() => {
    let active = true
    async function init() {
      try {
        await Promise.all([refreshMetadata(), loadJobs(), refreshFavCount()])
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
        setReliabilityLoading(true)
        void Promise.allSettled([
          getReliability(company).then(result => {
            if (token === detailRequest.current && data.job.id === activeJobId.current) setReliability(result.report)
            return result.report // 带出去给自动重算做「输入有没有变」的对照；条件不满足时 undefined 同样按「没有已存报告」处理
          }), // 没有生成过参考时这里什么都不显示；失败同样只结束 loading，不算「已生成」
          getReliabilityHistory(company).then(result => {
            if (token === detailRequest.current && data.job.id === activeJobId.current) setReliabilityHistory(result.history)
          }), // 没有历史快照时趋势区不显示
        ]).then(async settled => {
          // 打开详情即自动重算：打分全程是本地代码（本地统计 → 规则打分 → 措辞模板），不联网、
          // 不用模型；输入没变时信号与报告落库都是幂等写入，不产生重复历史快照。采集完「还得
          // 手动点一下生成」的点击就省在这里；「生成参考」按钮保留给手动刷新与失败重试。
          if (token !== detailRequest.current || data.job.id !== activeJobId.current) return
          const stored = settled[0].status === 'fulfilled' ? settled[0].value : null
          try {
            const report = await computeReliability(company)
            if (token !== detailRequest.current || data.job.id !== activeJobId.current) return
            setReliability(report)
            // 输入没变就收掉状态行，别在每次打开岗位时留常驻提示；变了才说明本地统计刷新了什么。
            const changed = !stored || stored.input_hash !== report.input_hash
              || stored.rubric_version !== report.rubric_version || stored.wording_version !== report.wording_version
            setReliabilityAction(changed && report.collection
              ? { label: '已按已存数据自动重算', status: report.collection.status, message: report.collection.message }
              : null)
            const history = await getReliabilityHistory(company)
            if (token === detailRequest.current && data.job.id === activeJobId.current) setReliabilityHistory(history.history)
          } catch (err) {
            if (token !== detailRequest.current || data.job.id !== activeJobId.current) return
            // 自动路径失败不弹 toast：已有报告就照常显示上次结果，只留一行可行动的失败说明。
            setReliabilityAction({ label: '自动重算未完成', status: 'failed',
              message: `${(err as Error).message}；${stored ? '仍显示上次报告，可点「重新计算」重试。' : '可点「生成参考」重试。'}` })
          } finally {
            // 首次生成期间保持「正在读取」占位；结束后才让「暂不出分 / 还没生成过」的兜底文案露面。
            if (token === detailRequest.current && data.job.id === activeJobId.current) setReliabilityLoading(false)
          }
        })
      }
    } catch (err) { if (token === detailRequest.current) toast((err as Error).message, 'error') }
    finally { if (token === detailRequest.current) setDetailLoading(false) }
  }, [])
  function selectJob(id: string) {
    setPaneFocus('info'); setDiscoveryOpen(false); jobGeneration.current++; activeJobId.current = id; setSelectedId(id); setDetail(null); setAnalysis(null); setReliability(null); setReliabilityHistory(null); setReliabilityLoading(false); setReliabilityAction(null); setEvidenceFocus(''); setDescExpanded(false); setCompanyPage(0); setTab('overview')
    const panel = document.querySelector<HTMLElement>('.detail-panel')
    panel?.scrollTo({ top: 0 })
    scrollToSelectedDetail.current = window.matchMedia('(max-width: 1040px)').matches
    void loadDetail(id)
  }
  useEffect(() => {
    if (!scrollToSelectedDetail.current) return
    const panel = document.querySelector<HTMLElement>('.detail-panel')
    panel?.scrollIntoView({ block: 'start' })
    panel?.focus({ preventScroll: true })
    if (!detailLoading) scrollToSelectedDetail.current = false
  }, [selectedId, detailLoading])
  function openReferenceEvidence(evidenceId: string) { setEvidenceFocus(evidenceId); setTab('evidence') }
  async function recomputeReference() {
    if (!job) return
    const company = job.company, id = job.id, generation = jobGeneration.current
    await perform('reliability', async () => {
      const result = await computeReliability(company)
      if (generation === jobGeneration.current && id === activeJobId.current) {
        setReliability(result)
        // 重算会自动刷新本地统计；把这段反馈亮出来，用户才知道分数用上了哪些已存数据。
        setReliabilityAction(result.collection
          ? { label: '已自动刷新本地统计', status: result.collection.status, message: result.collection.message }
          : null)
        try {
          const history = await getReliabilityHistory(company)
          if (generation === jobGeneration.current && id === activeJobId.current) setReliabilityHistory(history.history)
        } catch { toast('求职参考已重算，历史记录暂时读取失败。', 'error') }
      }
    })
  }
  async function runReferenceAction(kind: 'coverage' | 'extract') {
    if (!job) return
    const company = job.company, id = job.id, generation = jobGeneration.current
    const label = kind === 'coverage' ? '盘点已有资料' : '提取资料数字'
    await perform('reliability', async () => {
      // 采集与抽取的 status、message 一律照后端原文显示，不改写、不美化。
      const result = kind === 'coverage' ? await collectCompanySignals(company, 'jobscope_jobs') : await extractCompanySignals(company)
      if (generation !== jobGeneration.current || id !== activeJobId.current) return
      setReliabilityAction({ label, status: result.status, message: result.message })
      if (kind === 'extract' && result.status === 'skipped') return
      const report = await computeReliability(company)
      if (generation !== jobGeneration.current || id !== activeJobId.current) return
      setReliability(report)
      try {
        const history = await getReliabilityHistory(company)
        if (generation === jobGeneration.current && id === activeJobId.current) setReliabilityHistory(history.history)
      } catch { toast('求职参考已重算，历史记录暂时读取失败。', 'error') }
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
      const name = sources.find(item => item.id === source)?.name || source
      if (taskSucceeded(data.task)) toast(data.task.message || `采集结束，新增或更新 ${data.task.count ?? data.jobs?.length ?? 0} 条资料。`)
      else if (data.task.status === 'needs_login' || data.task.status === 'needs_verification') {
        // 接口直连撞登录墙（boss 等）：写明发生了什么与下一步去哪，红色 20 秒长提示并给出跳转按钮，不能一闪而过。
        toast(`${name}需要登录或人工验证，本次未取得岗位。请到「数据与采集 → 浏览器采集」打开专用浏览器，登录后用浏览器入口采集。`,
          'error', false, { label: '去浏览器采集', onClick: () => { setView('sources'); setSourcesTab('collect') } }, 20000)
      }
      else failedTaskToast(data.task, name)
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
    if (browserCollecting) return
    setBrowserCollecting(true)
    const progress = toast('已开始后台采集，可继续处理其他工作', 'notice', true)
    try {
      const started = await post<{ task: Task }>('/browser/collect', { source: browserSource, query, location, limit: browserLimit })
      const task = await watchTask(started.task.id)
      await Promise.all([loadJobs(), refreshMetadata()])
      const name = sources.find(item => item.id === browserSource)?.name || browserSource
      if (taskSucceeded(task)) {
        toast(task.count > 0 ? `已保存或更新 ${task.count} 条岗位。${task.message || ''}` : task.message || '本次未保存岗位', 'notice', false, {
          label: '查看岗位', onClick: () => { setView('jobs'); setPaneFocus('info') },
        })
      } else if (task.status === 'needs_login' || task.status === 'needs_verification') {
        // 登录/验证要用户在浏览器窗口里动手：后端会把页面留在前台。提示写清平台与两步动作，
        // 红色 20 秒长提示；后端的技术细节留在任务记录里，不堆进这条 toast。
        const what = task.status === 'needs_login' ? '登录' : '人工验证'
        toast(`${name}采集未完成：需要${what}。请在弹出的项目浏览器窗口里完成${what}（浏览器没开就先点「打开专用浏览器」），完成后回到这里再点一次「开始采集」。`, 'error', false, undefined, 20000)
      }
      else failedTaskToast(task, name)
    } catch (error) { toast((error as Error).message, 'error') }
    finally { setBrowserCollecting(false); dismissToast(progress) }
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
    const company = job.company
    await perform('company', async () => {
      const started = await startCompanyResearch(company)
      // 一键完备的进度聚合到一条常驻提示：每完成一步更新计数，失败的步骤单独弹出原因。
      let done = 0
      let progress = toast(`一键完备「${company}」：0/${started.tasks.length} 步完成`, 'notice', true)
      try {
        await Promise.all(started.tasks.map(async initial => {
          for (let poll = 0; poll < 300; poll++) {
            await new Promise(resolve => setTimeout(resolve, 2000))
            const { task } = await api<{ task: Task }>(`/tasks/${encodeURIComponent(initial.id)}`)
            if (task.status !== 'running' && task.status !== 'pending') {
              done += 1
              dismissToast(progress)
              progress = toast(`一键完备「${company}」：${done}/${started.tasks.length} 步完成`, 'notice', true)
              const name = sourceNames[task.source] || task.source
              if (!taskSucceeded(task) && task.status !== 'skipped') failedTaskToast(task, name)
              return
            }
          }
          toast(`${sourceNames[initial.source] || initial.source}：等待超时，请到采集记录查看`, 'error')
        }))
      } finally {
        dismissToast(progress)
        if (done >= started.tasks.length) toast(`一键完备完成，求职参考已按最新材料重算。`, 'notice', false)
        await Promise.all([job.id === activeJobId.current ? loadDetail(job.id) : Promise.resolve(), refreshMetadata()])
      }
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
    setTagsChanged(false)
    setBatchDraft(targets.length === 1 ? { location: targets[0].location || '', salary: targets[0].salary || '', tags: (targets[0].personal_tags ?? targets[0].tags ?? []).join(', ') } : { location: '', salary: '', tags: '' })
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
    if (tagsChanged) payload.tags = tags
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
  const sourceNames: Record<string, string> = { ...Object.fromEntries(sources.map(item => [item.id, item.name])), ...signalTaskNames, knowledge_import: '知识库导入' }
  const jobSources = sources.filter(item => item.kind === 'jobs')
  const browserSources = sources.filter(item => item.browser_collectable)
  const browserSourceItem = browserSources.find(item => item.id === browserSource)
  const browserCities = browserSourceItem?.cities || []
  const hasArchiveFilters = Object.values(jobFilters.current).some(Boolean)
  const emptyLibrary = !loading && jobsTotal === 0 && !hasArchiveFilters && !job
  const extraFilterCount = [archiveDraft.source, archiveDraft.location, archiveDraft.salaryMin || archiveDraft.salaryMax].filter(Boolean).length
  function clearArchiveFilters() {
    const cleared = { q: '', source: '', location: '', salaryMin: '', salaryMax: '', starred: false }
    setArchiveDraft(cleared)
    document.querySelector<HTMLInputElement>('.job-archive-filters .search-input input')?.focus({ preventScroll: true })
    void perform('filter', async () => { await loadJobs(cleared, 1) })
  }
  // 星标切换：本地先改列表/详情状态再刷角标，PATCH 很快，无需整页重载。
  async function toggleStar(item: Job) {
    await perform('star', async () => {
      const result = await patchJobs([item.id], { starred: !item.starred })
      if (!result.updated) return
      const starred = !item.starred
      setJobs(list => list.map(entry => entry.id === item.id ? { ...entry, starred, starred_at: starred ? new Date().toISOString() : undefined } : entry))
      setDetail(current => current?.job.id === item.id ? { ...current, job: { ...current.job, starred, starred_at: starred ? new Date().toISOString() : undefined } } : current)
      void refreshFavCount()
      toast(starred ? '已收藏，可在收藏夹反查与横评' : '已取消收藏')
    })
  }
  async function setStarredSelected(starred: boolean) {
    await perform('star-batch', async () => {
      const result = await patchJobs([...selectedIds], { starred })
      toast(`${starred ? '已收藏' : '已取消收藏'} ${result.updated} 份岗位`)
      setSelectedIds(new Set())
      await loadJobs(undefined, jobsInfo.page)
      if (selectedId) await loadDetail(selectedId)
      void refreshFavCount()
    })
  }

  return <div className="app-shell">
    <a className="skip-link" href="#workspace-main">跳到工作区</a>
    <aside className={`sidebar${navigationOpen ? ' nav-open' : ''}`}>
      <a className="brand" href="#" onClick={event => { event.preventDefault(); setView('jobs') }}><span className="brand-symbol" aria-hidden="true"><i /><i /></span><span>知根<span className="brand-english">JobScope</span></span></a>
      <button className="mobile-nav-toggle" aria-expanded={navigationOpen} aria-controls="workspace-navigation" onClick={() => setNavigationOpen(value => !value)}><Icon name={navigationOpen ? 'close' : 'menu'} />{navigationOpen ? '收起导航' : '工作区导航'}</button>
      <nav id="workspace-navigation" aria-label="主导航" onClick={event => {
        // 组头切换不收起移动端导航：用户只是折叠分组，不是完成了页面跳转。
        const button = (event.target as HTMLElement).closest('button')
        if (!button || button.classList.contains('nav-group-toggle')) return
        setNavigationOpen(false)
        if (window.matchMedia('(max-width: 800px)').matches) document.getElementById('workspace-main')?.focus()
      }}>
        {NAV_GROUPS.map(group => {
          const expanded = expandedGroups.has(group.id)
          return <div key={group.id} className={`nav-group${expanded ? ' expanded' : ''}`}>
            <button type="button" className="nav-group-toggle" aria-expanded={expanded} aria-controls={`nav-group-${group.id}`} onClick={() => toggleGroup(group.id)}>
              {group.label}<Icon name="chevron" size={13} />
            </button>
            <div id={`nav-group-${group.id}`} className="nav-group-items">
              {group.items.map(item => {
                const badge = item.id === 'jobs' ? jobsTotal : item.id === 'favorites' ? favTotal : item.id === 'compare' ? compareSelected.length : item.id === 'knowledge' ? knowledgeTotal : 0
                return <button key={item.id} aria-current={view === item.id ? 'page' : undefined} className={view === item.id ? 'active' : ''} onClick={() => setView(item.id)}>
                  <Icon name={item.icon} />{item.label}
                  {item.id === 'resume' ? (resumeSaved && <span className="nav-dot" />) : badge > 0 && <span className="nav-count">{badge}</span>}
                </button>
              })}
            </div>
          </div>
        })}
      </nav>

      <div className="sidebar-bottom"><span className={`connection-dot ${online ? 'connected' : ''}`} />{online ? '本地工作区已连接' : '等待本地服务'}<span className="version">v0.1</span></div>
    </aside>

    <main id="workspace-main" tabIndex={-1} className={view === 'jobs' || view === 'evidence' ? 'main main-workbench' : view === 'sources' || view === 'settings' || view === 'compare' || view === 'favorites' || view === 'knowledge' ? 'main main-compact' : 'main'}>
      <header className="topbar"><div className="breadcrumb">工作区<span>/</span>{view === 'jobs' ? '岗位研究' : view === 'favorites' ? '收藏' : view === 'resume' ? '我的简历' : view === 'workshop' ? '简历工坊' : view === 'evidence' ? '公司资料库' : view === 'compare' ? '数据分析' : view === 'global' ? '全局对话' : view === 'knowledge' ? '知识库' : view === 'settings' ? '设置中心' : '数据与采集'}</div><div className="topbar-right"><span className="local-tag"><span />本地资料库</span><button className="icon-button" aria-label="刷新工作区" onClick={() => void perform('refresh', async () => { await Promise.all([loadJobs(), refreshMetadata(), refreshFavCount()]); if (selectedId) await loadDetail(selectedId) })}><Icon name="source" size={17} /></button></div></header>
      <div hidden={view !== 'evidence'} className="evidence-view"><EvidenceWorkspace active={view === 'evidence'} refreshMetadata={refreshMetadata} modelConfigured={modelConfigured} onConfigureModel={configureModel} navigation={chatNavigation} onNavigateConversation={navigateConversation} onOpened={chatOpened} onManageExtensions={() => openSettings('extensions')} onOpenCompare={() => setView('compare')} onOpenKnowledge={() => setView('knowledge')} openCompanyChat={companyChatRequest} /></div>

      {view === 'knowledge' && <KnowledgeView onCountChange={setKnowledgeTotal} />}

      {view === 'global' && <><section className="page-intro chat-intro"><div><h1>全局对话</h1><p>跨岗位、跨公司检索项目资料与知识库，回答标注公司与出处</p></div><span className="revision-label">会话保存在本机</span></section><ChatWorkspace scope={{ kind: 'global' }} modelConfigured={modelConfigured} onConfigureModel={configureModel} onManageExtensions={() => openSettings('extensions')} onOpenKnowledge={() => setView('knowledge')} navigation={chatNavigation?.scope.kind === 'global' ? chatNavigation : null} onNavigateConversation={navigateConversation} onOpened={chatOpened} prefill={chatPrompt} /></>}

      {view === 'compare' && <CompanyCompare />}

      {view === 'favorites' && <FavoritesView onOpenJob={id => { setView('jobs'); selectJob(id) }} onOpenCompare={() => setView('compare')} onChange={() => {
        // 收藏变动会同时影响档案页的「只看收藏」过滤结果，一并刷新。
        void refreshFavCount()
        if (jobFilters.current.starred) void perform('filter', async () => { await loadJobs(undefined, 1) })
      }} />}

      {view === 'jobs' && <>
        <section className="page-intro"><div><h1>岗位研究</h1><p>把岗位要求、公司证据和你的经历逐项核对</p></div></section>
        {emptyLibrary && <section className="empty-library-intro"><h2>从一个机会开始</h2><p>粘贴你正在考虑的岗位，或查找外部机会。每份档案保留出处。</p></section>}
        {paneFocus !== 'chat' && <div className="job-entry-actions"><button className="button primary" onClick={() => openModal('boss-import')} title="捕获项目浏览器里看到的 Boss 页面、粘贴分享链接，批量导入岗位">从 Boss 导入</button><button className="button secondary" onClick={() => openModal('job')}>粘贴已有岗位 JD</button><button className="button secondary" onClick={() => setDiscoveryOpen(!discoveryOpen)} aria-expanded={discoveryOpen}>查找外部岗位</button></div>}
        <section className="search-section" aria-labelledby="collect-heading" hidden={!discoveryOpen || paneFocus === 'chat'}>
          <div className="search-section-heading"><h2 id="collect-heading"><Icon name="globe" size={16} />发现新岗位</h2><span>从外部来源采集，保存到本地档案</span><button className="text-button" onClick={() => setView('sources')}>管理数据源<Icon name="arrow" size={13} /></button></div>
          <form className="search-form" onSubmit={collect}>
            <label className="collect-field collect-keyword"><span>岗位关键词</span><span className="collect-control"><Icon name="search" size={17} /><input value={query} onChange={event => setQuery(event.target.value)} placeholder="岗位、技能或公司" required maxLength={200} /></span></label>
            <div className="collect-field collect-location"><label htmlFor="collect-location">工作地点</label><div className="collect-control"><Icon name="pin" size={17} /><CityInput id="collect-location" cities={selectedSource?.cities || []} hot={selectedSource?.hot || []} pinyin={cityPinyin} locations={locations} value={location} onChange={setLocation} placeholder="不限地点" maxLength={100} /></div></div>
            <label className="collect-field collect-source"><span>岗位数据源</span><select value={source} onChange={event => setSource(event.target.value)}>{jobSources.map(item => <option key={item.id} value={item.id}>{item.name} · {transportNames[item.transport_mode] || item.transport_mode}</option>)}</select></label>
            <label className="collect-field collect-count"><span>采集数量</span><span className="collect-control"><input type="number" min={1} max={1000} value={collectLimit} onChange={event => setCollectLimit(Math.max(1, Math.min(1000, Number(event.target.value) || 20)))} inputMode="numeric" aria-label="采集数量" /><span>条</span></span></label>
            <Button className="button primary" type="submit" busy={busy === 'collect'} disabled={Boolean(busy)}>检索岗位<Icon name="arrow" size={17} /></Button>
          </form>
          {/* 需要登录的平台在提交前就亮明前置条件（独立元素，避免被矮屏下 footnote 的折叠规则藏掉）。 */}
          {selectedSource?.login === 'required' && <p className="collect-login-note" role="note">「{selectedSource.name}」需要登录后才能返回岗位：请先到「数据与采集 → 浏览器采集」打开专用浏览器并登录，再回到这里用浏览器入口采集；未登录直接检索只会得到登录或验证提示。</p>}
          <div className="search-footnote"><span><span className="small-dot" />{selectedSource?.description || '标注“需浏览器”的来源，需要先在项目 Chrome 中登录。'}</span></div>
        </section>

        {job && paneFocus !== 'chat' && <div className="job-reading-actions"><span>{job.title}</span><button className="button primary small" onClick={openJobChat}>问这份岗位</button></div>}
        {!emptyLibrary && <div className={`research-layout focus-${paneFocus}${job ? '' : ' no-chat'}`}>
          <section className="job-list-panel" aria-labelledby="archive-heading"><div className="panel-title"><h2 id="archive-heading">岗位档案 <span>{jobsInfo.total.toLocaleString()}</span></h2>{hasArchiveFilters ? <button className="text-button archive-reset" disabled={Boolean(busy)} onClick={clearArchiveFilters}>清除筛选</button> : <span className="archive-scope">本地资料库</span>}</div>
            <form className="job-archive-filters" aria-label="岗位档案筛选" onSubmit={event => { event.preventDefault(); void perform('filter', async () => { await loadJobs({ ...archiveDraft }, 1) }) }}>
              <label className="search-input"><Icon name="search" size={16} /><span className="sr-only">档案关键词</span><input value={archiveDraft.q} onChange={event => setArchiveDraft({ ...archiveDraft, q: event.target.value })} placeholder="在已存档案中搜索" maxLength={300} /></label>
              <Button className="button secondary small archive-submit" type="submit" busy={busy === 'filter'} disabled={Boolean(busy)}>筛选</Button>
              <label className="agent-checkbox archive-starred" title="只显示收藏过的岗位；收藏不会因重新采集而丢失"><input type="checkbox" checked={Boolean(archiveDraft.starred)} onChange={event => { const next = { ...archiveDraft, starred: event.target.checked }; setArchiveDraft(next); void perform('filter', async () => { await loadJobs(next, 1) }) }} />只看收藏</label>
              <details className="archive-filter-options">
                <summary><Icon name="filter" size={16} />筛选条件{extraFilterCount > 0 && <span className="filter-count">{extraFilterCount}</span>}<Icon name="chevron" size={14} /></summary>
                <div className="archive-filter-fields">
                  <label className="archive-field"><span>信息源</span><select value={archiveDraft.source} onChange={event => setArchiveDraft({ ...archiveDraft, source: event.target.value })}><option value="">全部来源</option>{jobSources.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}<option value="user_import">用户导入</option></select></label>
                  <div className="archive-field"><label htmlFor="archive-location">地点</label><CityInput id="archive-location" cities={[]} hot={[]} pinyin={cityPinyin} locations={locations} value={archiveDraft.location} onChange={value => setArchiveDraft({ ...archiveDraft, location: value })} placeholder="不限地点" maxLength={100} /></div>
                  <div className="archive-field archive-salary"><span>月薪范围 <span className="muted">· K/月</span></span><div className="salary-range"><input aria-label="月薪下限" type="number" min={0} step="0.5" placeholder="最低" inputMode="decimal" value={archiveDraft.salaryMin} onChange={event => setArchiveDraft({ ...archiveDraft, salaryMin: event.target.value })} /><span>–</span><input aria-label="月薪上限" type="number" min={0} step="0.5" placeholder="最高" inputMode="decimal" value={archiveDraft.salaryMax} onChange={event => setArchiveDraft({ ...archiveDraft, salaryMax: event.target.value })} /></div></div>
                </div>
                <div className="filter-actions"><span>仅筛选本地档案</span><button type="button" className="text-button" disabled={Boolean(busy)} onClick={clearArchiveFilters}>清除</button><Button className="button secondary small" type="submit" busy={busy === 'filter'} disabled={Boolean(busy)}>应用条件</Button></div>
              </details>
            </form>
            {jobs.length > 0 && <div className="job-select-bar"><label className="agent-checkbox"><input type="checkbox" checked={allPageSelected} onChange={togglePageSelection} />本页全选</label><button className="text-button" disabled={Boolean(busy)} onClick={() => void selectAllFiltered()}>选择全部 {jobsInfo.total} 条</button>{selectedIds.size > 0 && <div className="selection-actions"><span>已选 {selectedIds.size} 项</span><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => void setStarredSelected(true)}>收藏</Button><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => void setStarredSelected(false)}>取消收藏</Button><Button className="button secondary small" disabled={Boolean(busy)} onClick={openBatchEditForSelection}>编辑</Button><Button className="button secondary small danger-text" disabled={Boolean(busy)} onClick={() => askDelete([...selectedIds], `已选的 ${selectedIds.size} 份岗位档案`)}>删除</Button><button className="text-button" onClick={() => setSelectedIds(new Set())}>取消</button></div>}</div>}
            {loading ? <div className="loading-state"><span className="spinner" />正在连接资料库…</div> : jobs.length === 0 ? <Empty title={hasArchiveFilters ? '没有符合条件的岗位' : '从一个机会开始'} icon="search">{hasArchiveFilters ? <>试试其他关键词，或<button className="inline-button" disabled={Boolean(busy)} onClick={clearArchiveFilters}>清除筛选</button>查看全部档案。</> : '检索真实岗位，或导入你正在考虑的职位。采集后的岗位会保存在这里。'}</Empty> : <div className={`job-list${busy === 'filter' || busy === 'page-jobs' ? ' refreshing' : ''}`}>{jobs.map(item => <JobCard key={item.id} item={item} selected={selectedId === item.id} checked={selectedIds.has(item.id)} onToggleSelection={toggleJobSelection} onOpen={selectJob} onToggleStar={item => void toggleStar(item)} />)}</div>}
            <Pagination info={jobsInfo} label="岗位档案" busy={Boolean(busy)} onPage={page => void perform('page-jobs', async () => { await loadJobs(undefined, page) })} /><div className="list-bottom">资料以采集时的页面为准，投递前请再次核实。</div>
          </section>

          <section className="detail-panel" aria-label="岗位详情" tabIndex={-1}>
            {selectedId && <button className="mobile-back text-button" onClick={() => { document.querySelector('.job-list-panel')?.scrollIntoView({ block: 'start' }); document.querySelector<HTMLElement>('.job-card-body[aria-pressed="true"]')?.focus({ preventScroll: true }) }}><Icon name="arrow" size={16} />返回岗位列表</button>}
            {detailLoading ? <div className="loading-state"><span className="spinner" />正在读取岗位档案…</div> : !job ? <div className="welcome-panel"><div className="welcome-heading"><Icon name="book" /><span>岗位调查档案</span></div><h2>先选一份岗位，再逐项核对</h2><p>检索岗位或导入已有 JD。选择左侧档案，查看要求、公司资料与简历依据。</p><div className="welcome-checklist"><div><span>01</span><section><h3>岗位要求</h3><p>职责、技能、地点和薪酬口径</p></section></div><div><span>02</span><section><h3>公司证据</h3><p>资料原文、披露主体和发布时间</p></section></div><div><span>03</span><section><h3>个人适配</h3><p>已有依据、硬性条件与待核实项</p></section></div></div><div className="welcome-source-note">每份资料都保留出处，缺少证据时保持未知</div></div> : <>
              <div className="detail-heading"><div className="detail-kicker"><span>{sourceNames[job.source] || job.source}</span><External url={job.url}>查看原始岗位</External></div><h2>{job.title}</h2><div className="detail-company">{job.company}<span>·</span><Icon name="pin" size={14} />{job.location || '地点待确认'}</div><div className="detail-heading-bottom"><strong>{job.salary || '薪资未披露'}</strong>{isStale(job.updated_at, 30) && <span className="stale-hint" title="这份档案已超过 30 天未更新，薪资与职责可能已变化；可在收藏夹「反查」重读源站，或按原条件重新采集。">数据较旧 · 建议反查</span>}<Button className="button secondary small" disabled={Boolean(busy)} title={job.starred ? '取消收藏；收藏不会因重新采集而丢失' : '收藏岗位，可在收藏夹反查与横评'} onClick={() => job && void toggleStar(job)}><Icon name="star" size={15} />{job.starred ? '已收藏' : '收藏'}</Button><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => openBatchEdit([job])}>编辑档案</Button><Button className="button secondary small" busy={busy === 'analyze'} disabled={Boolean(busy) || needsFullJd(job)} onClick={() => void analyze()}><Icon name="check" size={16} />评估适配</Button><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => { setWorkshopPrefill({ jobId: job.id, company: job.company, jobTitle: job.title }); setView('workshop') }}><Icon name="wand" size={16} />优化简历</Button></div></div>
              <ManualJobDescription key={job.id} job={job} onSaved={async saved => { if (activeJobId.current === saved.id) { await loadDetail(saved.id); setAnalysis(null) }; await loadJobs() }} /><div className="jd-state"><div><strong>{job.description_status === 'complete' ? '完整JD已保存' : needsFullJd(job) ? '仅列表摘要 · 待补全' : '岗位材料已保存'}</strong><p>{job.detail_collected_at ? `正文读取于 ${date(job.detail_collected_at)}` : needsFullJd(job) ? ((job.source === 'boss' || job.source === 'zhaopin') ? '需要完整职责与任职要求，才能评估是否适合投递' : '该平台列表未给出完整职责，可在岗位档案补充后再评估') : '请核对岗位材料完整性及原始出处'}</p></div>{(job.source === 'boss' || job.source === 'zhaopin') && <Button className="button secondary small" busy={busy === 'job-detail'} disabled={Boolean(busy)} onClick={() => void perform('job-detail', async () => { const id = job.id, generation = jobGeneration.current; const result = await post<{ task: Task; job: Job }>(`/jobs/${encodeURIComponent(id)}/detail`); if (taskSucceeded(result.task)) toast(result.task.message); else failedTaskToast(result.task, '完整职责读取'); await Promise.all([generation === jobGeneration.current ? loadDetail(id) : Promise.resolve(), loadJobs(), refreshMetadata()]); if (generation === jobGeneration.current) setAnalysis(null) })}>{job.description_status === 'complete' ? '更新完整职责' : '读取完整职责'}</Button>}</div><div className="detail-tabs" role="tablist" aria-label="岗位档案内容" onKeyDown={navigateTabs}>{(['overview', 'analysis', 'evidence'] as const).map(item => <button key={item} id={`job-${item}-tab`} role="tab" aria-controls="job-detail-panel" tabIndex={tab === item ? 0 : -1} aria-selected={tab === item} className={tab === item ? 'active' : ''} onClick={() => setTab(item)}>{item === 'overview' ? '岗位与公司' : item === 'analysis' ? '适配分析' : '证据资料'}{item === 'evidence' && <span>{evidenceTotal}</span>}</button>)}</div>
              <div id="job-detail-panel" className="detail-content" role="tabpanel" aria-labelledby={`job-${tab}-tab`}>
                {tab === 'overview' && <><div className="section-label"><span className="label-line" />岗位描述</div><div className={`description-text${descExpanded ? '' : ' collapsed'}`}>{plain(job.description) || '尚无岗位描述，请补充原始岗位资料。'}</div>{job.description.length > 600 && <button className="text-button" onClick={() => setDescExpanded(value => !value)}>{descExpanded ? '收起描述' : '展开全文'}</button>}<div className="company-section"><div className="section-heading"><div><span className="section-label">公司情报</span><h3>了解 {job.company}</h3></div><span className="purpose-heading-actions"><Button className="button secondary small" title="围绕这家公司已保存的全部岗位与资料发起对话（在公司资料库的「公司对话」页打开）" onClick={() => openCompanyChat(job.company)}>公司对话<Icon name="chat" size={14} /></Button><Button className="button secondary small" busy={busy === 'company'} disabled={Boolean(busy)} onClick={() => void collectCompany()}>一键完备<Icon name="arrow" size={14} /></Button></span></div><p className="muted body-small">一键串完单岗完整参考路径：多平台检索岗位与公告 → 快查公开页 → 补读完整职责 → 采集已启用信号源 → 抽取材料数字 → AI 行业判断 → 重算报告。官方页面和付费 API 可在下方「按来源补数据」中选择；遇登录/验证会停下来等你处理。</p>{detail.company_evidence?.length ? <>{detail.company_evidence.length > 4 && <div className="company-pager"><button className="text-button" disabled={companyPage === 0} onClick={() => setCompanyPage(page => page - 1)}>上一页</button><span className="tiny-label">{companyPage + 1} / {Math.ceil(detail.company_evidence.length / 4)} 页 · 共 {detail.company_evidence.length} 条</span><button className="text-button" disabled={companyPage >= Math.ceil(detail.company_evidence.length / 4) - 1} onClick={() => setCompanyPage(page => page + 1)}>下一页</button></div>}<div className="company-evidence-list">{detail.company_evidence.slice(companyPage * 4, companyPage * 4 + 4).map(e => <EvidenceCard key={e.id} evidence={e} />)}</div></> : <div className="inline-empty">尚未收集到该公司的资料。你可以多平台检索公开岗位与公告，或补充已有材料。</div>}<button className="text-button" onClick={() => openModal('evidence')}><Icon name="plus" size={14} />补充公司资料</button><CompanyReference key={job.id} onSignalBusy={value => setBusy(value ? 'reliability' : '')} onSignalCollected={(report, history) => { if (job.id !== activeJobId.current) return; setReliability(report); setReliabilityHistory(history); setReliabilityAction(null); void refreshMetadata() }} onConfigureSources={() => { setView('sources'); setSourcesTab('catalog') }} onAgentCollect={openGlobalAgent} report={reliability} history={reliabilityHistory} busy={Boolean(busy)} loading={reliabilityLoading} action={reliabilityAction} company={job.company} onOpenCompare={() => setView('compare')} onCompute={() => void recomputeReference()} onCoverage={() => void runReferenceAction('coverage')} onExtract={() => void runReferenceAction('extract')} onOpenEvidence={openReferenceEvidence} onOpenEvidenceModal={() => openModal('evidence')} /></div></>}
                {tab === 'analysis' && (!analysis ? <Empty title={busy === 'analyze' ? '正在对应岗位要求…' : '让经历与要求逐项对照'} icon="resume">{busy === 'analyze' ? '分析完成后，将展示匹配依据、未知项与面试核实问题。' : <>{resumeSaved ? '点击“评估适配”，查看有依据的匹配与缺口。' : <>先在<button className="inline-button" onClick={() => setView('resume')}>我的简历</button>保存个人经历，再生成适配分析。</>}分数仅供辅助比较，不代表录用概率。</>}</Empty> : <AnalysisView analysis={analysis} />)}
                {tab === 'evidence' && <><div className="section-heading"><div><h3>证据与出处</h3></div><button className="text-button" onClick={() => openModal('evidence')}><Icon name="plus" size={14} />补充资料</button></div>{evidenceTotal ? <JobEvidence key={job.id} jobId={job.id} focus={evidenceFocus} /> : <Empty title="尚无证据资料">采集岗位或补充公司材料后，原文与来源会保存在这里。</Empty>}</>}
              </div>
            </>}
          </section>
          {job && <section className="chat-section job-chat"><button className="button secondary small job-return" onClick={returnToJob}>返回岗位</button><ChatWorkspace key={job.id} scope={{ kind: 'job', jobId: job.id, company: job.company, title: job.title }} modelConfigured={modelConfigured} onConfigureModel={configureModel} onNavigateConversation={navigateConversation} onOpened={chatOpened} onManageExtensions={() => openSettings('extensions')} onOpenKnowledge={() => setView('knowledge')} /></section>}
        </div>}
      </>}

      {view === 'resume' && <><section className="page-intro"><div><h1>我的简历</h1><p>你的简历是适配分析的依据，保存在当前本地工作区。</p></div>{resumeRevision != null && <span className="revision-label">版本 {resumeRevision}</span>}</section><div className="resume-layout"><form className="paper-panel resume-editor" onSubmit={saveResume}><div className="section-heading"><h2>简历与事实档案</h2><label className="format-select"><span className="sr-only">简历格式</span><select value={resumeFormat} onChange={event => { setResumeFormat(event.target.value); setResumeSaved(false) }}><option value="text">简历纯文本 / Markdown</option><option value="facts_yaml">ResumeLoom facts.yaml</option></select></label></div><ResumeImport disabled={Boolean(busy) || loading} hasText={Boolean(resumeText.trim())} onApply={(text, format) => { setResumeText(text); setResumeFormat(format); setResumeSaved(false); toast('已提取并填入编辑区，请核对内容后保存。') }} /><label htmlFor="resume-text" className="field-label">简历正文 · 可直接编辑或粘贴</label><textarea className="resume-textarea" id="resume-text" value={resumeText} onChange={event => { setResumeText(event.target.value); setResumeSaved(false) }} required maxLength={100000} spellCheck={false} placeholder={resumeFormat === 'facts_yaml' ? 'schema_version: 1\nfacts:\n  ...' : '填写技能、工作经历、项目经验，以及地点和薪资等求职偏好。\n\n建议保留具体职责、项目背景和经过确认的成果。'} /><div className="editor-footer"><span className={resumeSaved ? '' : 'editor-unsaved'}>{resumeText.length.toLocaleString()} 字符 · {resumeSaved ? '已保存' : '未保存的修改'}</span><Button className="button primary" busy={busy === 'resume'} disabled={Boolean(busy)} type="submit">保存到本地<Icon name="check" size={17} /></Button></div></form><aside className="paper-panel resume-guide"><span className="section-label">整理建议</span><h2>整理可核对的经历</h2><div className="guide-item"><b>01</b><div><h3>保留具体经历</h3><p>技能、职责和项目事实，比笼统的自我评价更有帮助。</p></div></div><div className="guide-item"><b>02</b><div><h3>未知就留作未知</h3><p>未提到的技能不能自动认定为不会，也不能当作已经掌握。</p></div></div><div className="guide-item"><b>03</b><div><h3>复用 ResumeLoom</h3><p>可导入 PDF、Word .docx 和文本文件，也可使用 facts.yaml。扫描件需先 OCR；旧版 .doc 请另存为 .docx。</p></div></div><div className="privacy-note"><Icon name="book" size={18} /><p>文件在本机提取文字，确认保存后仅保留文本。启用外部模型分析时，相关资料可能发送至你配置的模型服务。</p></div></aside></div></>}

      {view === 'workshop' && <ResumeWorkshop prefill={workshopPrefill} onPrefillConsumed={() => setWorkshopPrefill(null)} onOpenResume={() => setView('resume')} />}

      {view === 'sources' && <div className="sources-workspace">
        <section className="page-intro"><div><h1>数据与采集</h1><p>采集岗位、补全职责，让每份资料有据可查。</p></div><Button className="button secondary" busy={busy === 'metadata'} disabled={Boolean(busy)} onClick={() => void perform('metadata', refreshMetadata)}><Icon name="source" size={16} />刷新状态</Button></section>
        <div className="management-overview" aria-label="数据概况">
          <div><span className="overview-icon"><Icon name="jobs" /></span><div><span>本地岗位</span><strong>{jobsTotal.toLocaleString()}<small>份档案</small></strong></div></div>
          <div><span className="overview-icon"><Icon name="globe" /></span><div><span>专用浏览器</span><strong>{browser ? browser.running ? '运行中' : '未启动' : '状态未知'}<small>登录后采集</small></strong></div></div>
          <div><span className="overview-icon"><Icon name="book" /></span><div><span>采集记录</span><strong>{tasksInfo.total.toLocaleString()}<small>次任务</small></strong></div></div>
        </div>
        <div className="management-tabs" role="tablist" aria-label="数据管理分区" onKeyDown={navigateTabs}>{([{ id: 'collect', label: '采集工作台' }, { id: 'lookup', label: '公司快查' }, { id: 'catalog', label: '数据源' }, { id: 'health', label: '来源健康' }, { id: 'history', label: '任务记录' }] as const).map(item => <button key={item.id} id={`sources-${item.id}-tab`} role="tab" aria-controls={`sources-${item.id}-panel`} aria-selected={sourcesTab === item.id} tabIndex={sourcesTab === item.id ? 0 : -1} onClick={() => setSourcesTab(item.id)}>{item.label}{item.id === 'catalog' && <span>{sources.length}</span>}</button>)}</div>
        <div id="sources-collect-panel" role="tabpanel" aria-labelledby="sources-collect-tab" hidden={sourcesTab !== 'collect'}>
          <section className="browser-panel paper-panel">
            <div className="browser-guide"><span className="section-label">浏览器采集</span><h2>从已登录的平台<br />带回真实岗位</h2><p>使用项目专属 Chrome，登录状态与日常浏览器分开保存。</p><ol className="collection-steps"><li><span>1</span><div><strong>选择平台</strong><p>在右侧填写关键词与城市</p></div></li><li><span>2</span><div><strong>打开浏览器并登录</strong><p>遇到验证时，请在浏览器内手动完成</p></div></li><li><span>3</span><div><strong>开始采集</strong><p>结果自动存档，任务记录保留真实状态</p></div></li></ol><div className="browser-status"><span className={`connection-dot ${browser?.running ? 'connected' : ''}`} />{browser?.message || '浏览器状态未知'}</div></div>
            <div className="browser-actions"><form onSubmit={event => { event.preventDefault(); void collectBrowser() }}>
              <div className="browser-form-heading"><h3>采集条件</h3><span>仅采集公开可见的资料</span></div>
              <div className="browser-fields"><div><label className="field-label" htmlFor="browser-source">采集平台</label><select id="browser-source" value={browserSource} onChange={event => setBrowserSource(event.target.value)}>{browserSources.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div><div><label className="field-label" htmlFor="browser-query">岗位关键词</label><input id="browser-query" value={query} onChange={event => setQuery(event.target.value)} placeholder="例如：Python 后端" required maxLength={200} /></div><div><label className="field-label" htmlFor="browser-location">城市（可选）</label><CityInput id="browser-location" cities={browserCities} hot={browserSourceItem?.hot || []} pinyin={cityPinyin} locations={locations} value={location} onChange={setLocation} placeholder={browserCities.length ? '输入或点选城市' : '例如：北京'} maxLength={100} /></div><div><label className="field-label" htmlFor="browser-limit">采集数量 <span>· 条</span></label><input id="browser-limit" type="number" min={1} max={1000} value={browserLimit} onChange={event => setBrowserLimit(Math.max(1, Math.min(1000, Number(event.target.value) || 20)))} inputMode="numeric" /></div></div>
              <div className="browser-form-actions"><Button className="button secondary" type="button" title="启动/连接项目专属 Chrome（登录态与日常浏览器隔离）并打开所选平台首页；适合平时先登录、过验证" busy={busy === 'browser-start'} disabled={Boolean(busy) || !browserSources.length} onClick={() => void perform('browser-start', async () => { const result = await post<BrowserStatus>(`/browser/start?source=${encodeURIComponent(browserSource)}`); setBrowser(result); toast(result.message); await refreshMetadata() })}><Icon name="globe" size={17} />打开专用浏览器</Button><Button className="button primary" title="在项目浏览器会话里采集当前平台；浏览器没开会自动拉起——未登录时在弹出的窗口里完成登录/验证，再点一次即可" busy={browserCollecting} disabled={browserCollecting || !browserSources.length} type="submit">开始采集<Icon name="arrow" size={16} /></Button></div>
              <p className="browser-form-note">点「开始采集」没开浏览器会自动拉起；实际数量以平台可返回的结果为准。</p>
              {/* 登录前置说明随所选平台切换：需要登录的平台未登录采集必失败，提交前就写明白。 */}
              {browserSourceItem?.login === 'required' && <p className="browser-form-note login-required"><strong>「{browserSourceItem.name}」需要登录：</strong>未登录采集拿不到岗位。先点「打开专用浏览器」，在弹出的窗口里登录（遇到滑块、扫码就手动完成），再点「开始采集」。</p>}
              {browserSourceItem?.login === 'recommended' && <p className="browser-form-note login-recommended"><strong>「{browserSourceItem.name}」建议先登录：</strong>未登录可能遇到人工验证或结果受限，遇到时在弹出的窗口里完成后重试。</p>}
            </form></div>
          </section>
          <div className="collection-maintenance"><DetailBatch onUpdate={refreshMetadata} /><ResearchIndex /></div>
        </div>
        <div id="sources-lookup-panel" role="tabpanel" aria-labelledby="sources-lookup-tab" hidden={sourcesTab !== 'lookup'}>
          <CompanyLookup refreshMetadata={refreshMetadata} />
        </div>
        <div id="sources-catalog-panel" role="tabpanel" aria-labelledby="sources-catalog-tab" hidden={sourcesTab !== 'catalog'}>
          <div className="management-section-heading"><div><h2>岗位与公司数据源</h2><p>接口优先，浏览器补充；可用状态不代表已登录。</p></div></div>
          <div className="sources-grid">{loading ? <div className="paper-panel"><div className="loading-state"><span className="spinner" />正在读取数据源状态…</div></div> : sources.length ? sources.map(item => <article className="source-card paper-panel" key={item.id}><div className="source-card-heading"><span className="source-icon"><Icon name={item.id === 'cninfo' ? 'book' : 'globe'} /></span><h3>{item.name}</h3><Badge value={item.status} /></div><p>{item.description}</p><div className="source-card-footer"><span>{{ http_api: 'HTTP 接口', browser_page: '浏览器会话', company_web: '网页快查', manual: '用户材料' }[item.transport_mode] || item.transport_mode}</span><span>{item.id}</span></div></article>) : <div className="paper-panel"><Empty title="等待数据源状态">请确认本地后端服务已启动，然后刷新状态。</Empty></div>}</div>
          <SignalSourceSettings />
        </div>
        <div id="sources-health-panel" role="tabpanel" aria-labelledby="sources-health-tab" hidden={sourcesTab !== 'health'}>
          {/* 来源健康度：成败分布只是任务记录的聚合视图，消息引用任务原文，不做任何新的诊断结论。 */}
          <section className="paper-panel tasks-panel">
            <div className="section-heading">
              <div><h2>来源健康度</h2><p className="body-small muted">按最近 100 次任务统计每个来源的成功 / 未完成 / 失败分布。某来源连续失败通常是站点改版或登录态失效——先去「采集工作台」处理，不要把零条结果当作采集成功。</p></div>
              <Button className="button secondary small" busy={busy === 'health'} disabled={Boolean(busy)} onClick={() => void perform('health', async () => {
                const data = await getSourceHealth()
                setSourceHealth(data.sources)
              })}>刷新</Button>
            </div>
            {sourceHealth === null
              ? <div className="loading-state"><span className="spinner" />正在读取来源健康度…</div>
              : sourceHealth.length
                ? <div className="task-table-wrap"><table>
                  <thead><tr><th scope="col">来源</th><th scope="col">成功</th><th scope="col">未完成</th><th scope="col">失败</th><th scope="col">最近成功</th><th scope="col">最近结果</th></tr></thead>
                  <tbody>{sourceHealth.map(item => <tr key={item.source}>
                    <td data-label="来源"><strong>{sourceNames[item.source] || item.source}</strong><small>{item.source}</small></td>
                    <td data-label="成功" className="nowrap">{item.ok}</td>
                    <td data-label="未完成" className="nowrap">{item.attention}</td>
                    <td data-label="失败" className="nowrap">{item.failed}</td>
                    <td data-label="最近成功" className="nowrap">{item.last_ok_at ? date(item.last_ok_at) : '从未成功'}</td>
                    <td data-label="最近结果"><Badge value={item.last_status} /><span className="task-message">{item.last_message || '—'}</span></td>
                  </tr>)}</tbody>
                </table></div>
                : <Empty title="还没有可统计的任务">发起一次岗位或公司采集后，各来源的健康度会出现在这里。</Empty>}
            <p className="tiny-label">「未完成」指需要登录/人工验证、浏览器正忙、部分成功或被跳过的任务；判断口径与采集记录的状态完全一致。没有任何任务的来源不会出现在表里。</p>
          </section>
        </div>
        <div id="sources-history-panel" role="tabpanel" aria-labelledby="sources-history-tab" hidden={sourcesTab !== 'history'}>
          <section className="paper-panel tasks-panel"><div className="section-heading"><div><h2>采集记录</h2><p className="body-small muted">成功、失败与验证请求都留有记录。</p></div><span className="muted body-small">{tasksInfo.total} 次任务</span></div>{loading ? <div className="loading-state"><span className="spinner" />正在读取采集记录…</div> : tasks.length ? <div className="task-table-wrap"><table><thead><tr><th scope="col">查询 / 来源</th><th scope="col">状态</th><th scope="col">资料数</th><th scope="col">时间</th><th scope="col">说明</th></tr></thead><tbody>{tasks.map(task => <tr key={task.id}><td data-label="查询 / 来源"><strong>{task.query || '未指定'}</strong><small>{sourceNames[task.source] || task.source}</small></td><td data-label="状态"><Badge value={task.status} /></td><td data-label="资料数">{task.count ?? '—'}</td><td data-label="时间" className="nowrap">{date(task.created_at)}</td><td data-label="说明" className="task-message">{task.message || '—'}</td></tr>)}</tbody></table></div> : <Empty title="还没有采集记录">发起一次岗位或公司检索，结果与失败原因都会记录在这里。</Empty>}<Pagination info={tasksInfo} label="采集记录" busy={Boolean(busy)} onPage={page => void perform('page-tasks', () => loadTasks(page))} /></section>
        </div>
      </div>}

      {view === 'settings' && <>
        <button className="button secondary settings-return" onClick={() => setView(settingsReturn)}>返回原工作区</button>
        {/* 设置中心：模型 / 系统 / Agent 扩展三页合一。标签沿用 management-tabs 的键盘导航。 */}
        <div className="management-tabs" role="tablist" aria-label="设置分区" onKeyDown={navigateTabs}>{([['model', '模型设置'], ['system', '系统设置'], ['extensions', 'Agent 扩展']] as const).map(([id, label]) => <button key={id} id={`settings-${id}-tab`} role="tab" aria-controls={`settings-${id}-panel`} aria-selected={settingsTab === id} tabIndex={settingsTab === id ? 0 : -1} onClick={() => setSettingsTab(id)}>{label}</button>)}</div>
        <div id="settings-model-panel" role="tabpanel" aria-labelledby="settings-model-tab" hidden={settingsTab !== 'model'}><ModelSettings onSaved={refreshMetadata} /></div>
        <div id="settings-system-panel" role="tabpanel" aria-labelledby="settings-system-tab" hidden={settingsTab !== 'system'}><SystemSettings /></div>
        <div id="settings-extensions-panel" role="tabpanel" aria-labelledby="settings-extensions-tab" hidden={settingsTab !== 'extensions'} className="extensions-view"><AgentExtensions /></div>
      </>}
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
      }}><div className="section-heading"><div><h2 id="modal-title">{modal === 'job' ? '导入岗位' : modal === 'evidence' ? '补充公司资料' : modal === 'boss-import' ? '从 Boss 导入' : modal === 'batch-edit' ? '批量编辑档案' : '确认删除岗位'}</h2></div><button className="icon-button" aria-label="关闭弹窗" onClick={() => setModal(null)} disabled={Boolean(busy)}><Icon name="close" /></button></div><p className="muted body-small">{modal === 'job' ? '粘贴原始岗位信息，来源将标记为“用户导入”。' : modal === 'evidence' ? '保留原文和来源，帮助后续核实。请勿填写 Cookie 或访问令牌。' : modal === 'boss-import' ? '你在项目浏览器里看到的 Boss 页面就是导入源：捕获当前页、粘贴分享链接，重复岗位自动归并。' : modal === 'batch-edit' ? `将应用到已选的 ${batchEdit?.ids.length ?? 0} 份档案；留空的字段保持不变，薪资按原文覆盖（如 15–25K·13薪）。` : `将删除${confirmDelete?.label || '所选岗位'}，连同其岗位证据、适配报告与岗位对话，此操作不可恢复。`}</p>{(modal === 'job' || modal === 'evidence') && <><form onSubmit={importRecord}><label className="field-label" htmlFor="import-title">{modal === 'job' ? '岗位名称' : '资料标题'} <span>*</span></label><input id="import-title" name="title" required maxLength={300} autoFocus /><label className="field-label" htmlFor="import-company">公司名称 <span>*</span></label><input id="import-company" name="company" required maxLength={300} defaultValue={modal === 'evidence' ? job?.company : ''} />{modal === 'job' ? <div className="form-two"><div><label className="field-label" htmlFor="import-location">工作地点</label><input id="import-location" name="location" maxLength={200} /></div><div><label className="field-label" htmlFor="import-salary">原始薪资描述</label><input id="import-salary" name="salary" maxLength={300} placeholder="保留原文，如 20–30K·13薪" /></div></div> : <><label className="field-label" htmlFor="import-kind">资料类型</label><select id="import-kind" name="kind" defaultValue="user_material"><option value="user_material">用户提供材料</option><option value="employee_statement">员工自述</option><option value="company_statement">企业自述</option><option value="disclosure">正式披露</option><option value="media_report">媒体报道</option></select></>}<label className="field-label" htmlFor="import-url">原始链接（可选）</label><input id="import-url" name="url" type="url" maxLength={2000} placeholder="https://" /><label className="field-label" htmlFor="import-body">{modal === 'job' ? '岗位描述' : '资料原文'} <span>*</span></label><textarea id="import-body" name={modal === 'job' ? 'description' : 'text'} required rows={7} maxLength={100000} /><div className="modal-actions"><button type="button" className="button secondary" onClick={() => setModal(null)} disabled={Boolean(busy)}>取消</button><Button className="button primary" type="submit" busy={busy === 'import'} disabled={Boolean(busy)}>保存资料<Icon name="check" size={16} /></Button></div></form></>}
              {modal === 'boss-import' && <BossImport onClose={() => setModal(null)} onImported={() => { void loadJobs(); void refreshFavCount(); void refreshMetadata() }} />}
              {modal === 'batch-edit' && batchEdit && <form onSubmit={submitBatchEdit}><label className="field-label" htmlFor="batch-location">工作地点</label><input id="batch-location" value={batchDraft.location} onChange={event => setBatchDraft({ ...batchDraft, location: event.target.value })} maxLength={300} autoFocus /><label className="field-label" htmlFor="batch-salary">薪资描述</label><input id="batch-salary" value={batchDraft.salary} onChange={event => setBatchDraft({ ...batchDraft, salary: event.target.value })} maxLength={300} placeholder="15–25K·13薪" /><label className="field-label" htmlFor="batch-tags">个人标签（逗号分隔，可清空；平台标签保留）</label><input id="batch-tags" value={batchDraft.tags} onChange={event => { setTagsChanged(true); setBatchDraft({ ...batchDraft, tags: event.target.value }) }} maxLength={500} placeholder="已核实, 优先投递" /><div className="modal-actions"><button type="button" className="button secondary" onClick={() => setModal(null)} disabled={Boolean(busy)}>取消</button><Button className="button primary" type="submit" busy={busy === 'batch-edit'} disabled={Boolean(busy)}>保存修改<Icon name="check" size={16} /></Button></div></form>}
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
