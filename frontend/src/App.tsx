import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, post, readable, plain, date, isStale, computeReliability, collectCompanySignals, extractCompanySignals, getReliability, getReliabilityHistory, patchJobs, startCompanyResearch, startAnalyzeRun, cancelAnalyzeRun, openAnalyzeStream, listJobReports, getJobReport, updateAnalysisStatus, listResumeVersions, pinResumeVersion, getAnalyzeRun } from './api'
import type { ConversationNavigation } from './api'
import { signalTaskNames } from './SignalSources'
import { toast, dismissToast, failedTaskToast, taskSucceeded } from './toast'
import { Pagination, emptyPage } from './Pagination'
import { EvidenceWorkspace } from './EvidenceWorkspace'
import { EvidenceCard } from './EvidenceCard'
import { CompanyCompare } from './CompanyCompare'
import { CityInput } from './CityInput'
import type { SavedLocation } from './CityInput'
import { SourcesView } from './SourcesView'
import type { SourcesTab } from './SourcesView'
import { Button, Empty, External } from './ui'
import { AgentExtensions } from './AgentExtensions'
import { ModelSettings } from './ModelSettings'
import { SystemSettings } from './SystemSettings'
import { CompanyReference } from './CompanyReference'
import { ResumeImport } from './ResumeImport'
import { ResumeWorkshop } from './ResumeWorkshop'
import { ProfileView } from './ProfileView'
import type { WorkshopPrefill } from './ResumeWorkshop'
import { KnowledgeView } from './KnowledgeView'
import { LogView } from './LogView'
import { navigateTabs } from './tabNavigation'
import Tutorial, { tutorialSeenVersion, TUTORIAL_VERSION } from './Tutorial'
import { useCompareSelection } from './compareSelectionStore'
import { ChatWorkspace } from './chat/ChatWorkspace'
import { Icon } from './Icon'
import { JobCard } from './JobCard'
import { FavoritesView } from './FavoritesView'
import { ManualJobDescription } from './ManualJobDescription'
import { BossImport } from './BossImport'
import type { Analysis, AnalyzeRunStart, BrowserStatus, Evidence, Job, PageInfo, ReliabilityHistory, ReliabilityReport, ReportSummary, ResumeVersionSummary, Source, Task } from './api'

const transportNames: Record<string, string> = { http_api: '接口', browser_page: '需浏览器', company_web: '网页快查', manual: '手动' }
const dimensionNames: Record<string, string> = { skill: '技能', skills: '技能', experience: '经历与职责', domain: '行业与业务', industry: '行业与业务', preference: '求职偏好', preferences: '求职偏好' }
const requirementNames: Record<string, string> = { met: '满足', matched: '满足', satisfied: '满足', partial: '部分满足', not_met: '未满足', unmet: '未满足', missing: '未满足', unknown: '待确认', confirmed: '满足' }
function needsFullJd(job: Job) {
  return job.description_status === 'listing' || (['boss', 'zhaopin'].includes(job.source) && job.description_status !== 'complete')
}
// 评估适配流式运行的前端状态：阶段进展 + 已到达的映射条目（done 帧整体替换为正式报告）。
type AnalyzeStageStatus = 'pending' | 'running' | 'done'
type AnalyzeRunState = {
  id: string; startedAt: number
  extract: { status: AnalyzeStageStatus; total: number | null }
  match: { status: AnalyzeStageStatus; done: number; total: number | null }
  score: { status: AnalyzeStageStatus }
  items: Analysis['requirements']
}
function jobQuery(filters: { q: string; location: string; source: string; salaryMin: string; salaryMax: string; starred?: boolean }) {
  // 薪资输入单位是 K/月，后端按元比较；留空表示不限制。
  const params = new URLSearchParams({ q: filters.q, location: filters.location, source: filters.source, page_size: '100' })
  if (filters.salaryMin) params.set('salary_min', String(Math.round(Number(filters.salaryMin) * 1000)))
  if (filters.salaryMax) params.set('salary_max', String(Math.round(Number(filters.salaryMax) * 1000)))
  if (filters.starred) params.set('starred', 'true')
  return params
}
type ViewId = 'jobs' | 'favorites' | 'resume' | 'profile' | 'workshop' | 'sources' | 'evidence' | 'compare' | 'global' | 'knowledge' | 'logs' | 'settings'
type SettingsTab = 'model' | 'system' | 'extensions'
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
    { id: 'profile', icon: 'book', label: '个人资料库' },
    { id: 'workshop', icon: 'wand', label: '简历工坊' },
  ] },
  { id: 'manage', label: '系统管理', items: [
    { id: 'sources', icon: 'source', label: '数据与采集' },
    { id: 'logs', icon: 'menu', label: '运行日志' },
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
  const [sourcesTab, setSourcesTab] = useState<SourcesTab>('collect')
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
  // 使用教程（遮罩指引）：首次访问（以及教程改版后第一次）自动弹出，看完或跳过即按版本
  // 记住，不再自动出现；侧栏底部「使用教程」可随时重看。播放期间展开全部分组，保证步骤
  // 指向的导航元素可见。
  const [tutorialOpen, setTutorialOpen] = useState(false)
  useEffect(() => {
    if (!loading && tutorialSeenVersion() < TUTORIAL_VERSION) setTutorialOpen(true)
  }, [loading])
  useEffect(() => {
    if (!tutorialOpen) return
    setExpandedGroups(previous => {
      const all = new Set(NAV_GROUPS.map(group => group.id))
      if (NAV_GROUPS.every(group => previous.has(group.id))) return previous
      try { localStorage.setItem(NAV_STORAGE_KEY, JSON.stringify([...all])) } catch { /* 存储不可用时仅本次会话生效 */ }
      return all
    })
  }, [tutorialOpen])
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
  const [analyzeRun, setAnalyzeRun] = useState<AnalyzeRunState | null>(null)
  const analyzeSourceRef = useRef<EventSource | null>(null)
  const analyzeActiveRef = useRef(false)  // 流式 run 是否在途：后台细节刷新不能盖掉进行中的进度面板
  const [analysisHistory, setAnalysisHistory] = useState<ReportSummary[] | null>(null)
  const [activeReportId, setActiveReportId] = useState<string | null>(null)
  // 岗位绑定的存档版本：评估简历选择条的数据源（pin 的那份即「评估用」）。
  const [jobVersions, setJobVersions] = useState<ResumeVersionSummary[] | null>(null)
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
  // 评估报告回读：打开岗位即拉历史摘要与最近一份完整报告（历史数据不丢，刷新/切回都能看到）。
  // 流式 run 在途时只更新历史列表，不用旧报告盖掉进度面板；done 帧会带新报告并刷新此列表。
  function refreshReports(jobId: string) {
    const token = detailRequest.current
    void listJobReports(jobId).then(result => {
      if (token !== detailRequest.current || jobId !== activeJobId.current) return
      setAnalysisHistory(result.reports)
      setActiveReportId(result.reports[0]?.id ?? null)
      if (result.latest && !analyzeActiveRef.current) setAnalysis(result.latest)
    }).catch(() => { /* 回读失败不影响详情展示 */ })
  }
  async function loadHistoryReport(reportId: string) {
    const id = activeJobId.current
    if (!id) return
    try { setAnalysis(await getJobReport(id, reportId)); setActiveReportId(reportId) }
    catch (err) { toast((err as Error).message, 'error') }
  }
  // 需求行手动改判：后端按岗位记忆并重算所有已存报告；这里把返回的最新报告换上。
  async function changeRequirementStatus(jdQuote: string, status: string) {
    const id = activeJobId.current
    if (!id) return
    try {
      const result = await updateAnalysisStatus(id, jdQuote, status)
      if (result.report) setAnalysis(result.report)
      toast(result.message || '已按你的确认重算，重新评估时自动沿用。')
    } catch (err) { toast((err as Error).message, 'error') }
  }
  // 评估简历切换：选存档即 pin 到当前岗位（互斥），选「我的简历」解 pin 回退全局。
  const pinnedVersion = (jobVersions ?? []).find(version => version.pinned) ?? null
  async function switchEvalResume(value: string) {
    const id = activeJobId.current
    if (!id) return
    try {
      if (value) { await pinResumeVersion(Number(value), true); toast('已切换，该岗位评估适配将使用所选存档。') }
      else if (pinnedVersion) { await pinResumeVersion(pinnedVersion.id, false); toast('已解绑，该岗位评估将回退「我的简历」。') }
      const versions = await listResumeVersions(id)
      if (id === activeJobId.current) setJobVersions(versions.versions)
    } catch (err) { toast((err as Error).message, 'error') }
  }
  const loadDetail = useCallback(async (id: string) => {
    const token = ++detailRequest.current
    setDetailLoading(true)
    try {
      const data = await api<{ job: Job; evidence: Evidence[]; company_evidence: Evidence[]; evidence_total: number }>(`/jobs/${encodeURIComponent(id)}`)
      if (token === detailRequest.current) {
        setDetail(data)
        refreshReports(data.job.id)
        // 岗位绑定的存档版本（评估简历选择条数据源）；失败静默——选择条消失即可，不挡详情。
        void listResumeVersions(data.job.id).then(versions => {
          if (token === detailRequest.current && data.job.id === activeJobId.current) setJobVersions(versions.versions)
        }).catch(() => { if (token === detailRequest.current) setJobVersions([]) })
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
    closeAnalyzeStream(); setAnalyzeRun(null); analyzeActiveRef.current = false; setAnalysisHistory(null); setActiveReportId(null); setJobVersions(null)  // 切走岗位停止消费；后台 run 继续跑完并落库
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
  // 评估按钮禁用原因（hover 提示）：每个条件都说明人话，不再"灰了不说话"。
  function analyzeDisabledReason(j: Job): string {
    if (analyzeRun) return '流式分析进行中，进度见「适配分析」页签，可在进度面板取消'
    if (needsFullJd(j)) return '该岗位只有列表摘要：BOSS/智联需先补全完整职责才能评估（岗位卡片「读取完整职责」或详情页上方「补全未完成岗位」）'
    if (busy) return '有其他任务进行中，结束后可评估'
    return '按完整JD逐项对照简历，模型辅助、代码计分'
  }
  // 评估适配走流式运行：拆解/分组匹配/计分进度实时展示，完成时整体替换为正式报告。
  // 重复点击同一岗位时后端返回既有 run（reused），前端从头重放事件即可续接进度。
  async function analyze() {
    if (!job || analyzeRun) return
    const id = job.id
    const generation = jobGeneration.current
    setTab('analysis')
    let started: AnalyzeRunStart
    try { started = await startAnalyzeRun(id) } catch (err) { toast((err as Error).message, 'error'); return }
    if (generation !== jobGeneration.current) return
    if (started.report) { setAnalysis(started.report); refreshReports(id); toast('适配分析完成。'); return }  // 未绑定分析模型：规则分析即时返回
    setAnalyzeRun({ id: started.run_id, startedAt: Date.now(), extract: { status: 'pending', total: null }, match: { status: 'pending', done: 0, total: null }, score: { status: 'pending' }, items: [] })
    analyzeActiveRef.current = true
    attachAnalyzeStream(started.run_id)
  }
  function attachAnalyzeStream(runId: string) {
    closeAnalyzeStream()
    const generation = jobGeneration.current
    const source = openAnalyzeStream(runId, (event, data) => {
      // 仅当前代次消费事件；切走岗位后后台 run 照常执行，事件缓冲在服务端。
      if (generation !== jobGeneration.current) return
      if (event === 'stage') {
        const stage = String(data.stage)
        const status: AnalyzeStageStatus = data.status === 'done' ? 'done' : 'running'
        const total = typeof data.total === 'number' ? data.total : null
        setAnalyzeRun(cur => !cur ? cur : stage === 'extract' ? { ...cur, extract: { status, total } }
          : stage === 'match' ? { ...cur, match: { ...cur.match, status, total: total ?? cur.match.total } }
          : { ...cur, score: { status } })
      } else if (event === 'group') {
        const items = Array.isArray(data.items) ? data.items as Analysis['requirements'] : []
        setAnalyzeRun(cur => !cur ? cur : {
          ...cur, match: { ...cur.match, status: 'running', done: cur.match.done + 1, total: typeof data.total === 'number' ? data.total : cur.match.total },
          items: [...cur.items, ...items],
        })
      } else if (event === 'done') {
        closeAnalyzeStream(); setAnalyzeRun(null); analyzeActiveRef.current = false
        const report = data as unknown as Analysis
        setAnalysis(report)
        refreshReports(report.job_id)  // 历史列表刷新，新报告成为最新一份
        toast('适配分析完成。')
      } else if (event === 'error') {
        closeAnalyzeStream(); setAnalyzeRun(null); analyzeActiveRef.current = false
        toast(String(data.message || '评估适配失败'), 'error')
      } else if (event === 'cancelled' || event === 'end') {
        closeAnalyzeStream(); setAnalyzeRun(null); analyzeActiveRef.current = false
      }
    })
    source.onerror = () => {
      // 断连由 EventSource 自动重连；但后端重启会孤儿化 run（流 404、永不再来事件），
      // 探一次状态：run 已不存在就解灰评估按钮，避免卡死。
      getAnalyzeRun(runId).then(status => { if (status.done) { closeAnalyzeStream(); setAnalyzeRun(null); analyzeActiveRef.current = false; refreshReports(activeJobId.current || '') } }).catch(() => {
        if (generation !== jobGeneration.current) return
        closeAnalyzeStream(); setAnalyzeRun(null); analyzeActiveRef.current = false
        toast('后台评估运行已不存在（服务可能重启过），可重新评估。', 'error')
      })
    }
    analyzeSourceRef.current = source
  }
  function closeAnalyzeStream() {
    analyzeSourceRef.current?.close()
    analyzeSourceRef.current = null
  }
  async function cancelAnalyze() {
    if (!analyzeRun) return
    try { await cancelAnalyzeRun(analyzeRun.id) } catch { /* 已结束的 run 返回 404，等 done/error 帧收尾即可 */ }
    // 取消是合作式：保留进度面板直到 cancelled 帧到达（当前小组完成后停止）。
  }
  useEffect(() => closeAnalyzeStream, [])
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

      <div className="sidebar-bottom"><span className={`connection-dot ${online ? 'connected' : ''}`} />{online ? '本地工作区已连接' : '等待本地服务'}<button className="text-button sidebar-tutorial-button" onClick={() => setTutorialOpen(true)}>使用教程</button><span className="version">v0.1</span></div>
    </aside>

    <main id="workspace-main" tabIndex={-1} className={view === 'jobs' || view === 'evidence' ? 'main main-workbench' : view === 'sources' || view === 'settings' || view === 'compare' || view === 'favorites' || view === 'knowledge' || view === 'logs' || view === 'profile' ? 'main main-compact' : 'main'}>
      <header className="topbar"><div className="breadcrumb">工作区<span>/</span>{view === 'jobs' ? '岗位研究' : view === 'favorites' ? '收藏' : view === 'resume' ? '我的简历' : view === 'profile' ? '个人资料库' : view === 'workshop' ? '简历工坊' : view === 'evidence' ? '公司资料库' : view === 'compare' ? '数据分析' : view === 'global' ? '全局对话' : view === 'knowledge' ? '知识库' : view === 'logs' ? '运行日志' : view === 'settings' ? '设置中心' : '数据与采集'}</div><div className="topbar-right"><span className="local-tag"><span />本地资料库</span><button className="icon-button" aria-label="刷新工作区" onClick={() => void perform('refresh', async () => { await Promise.all([loadJobs(), refreshMetadata(), refreshFavCount()]); if (selectedId) await loadDetail(selectedId) })}><Icon name="source" size={17} /></button></div></header>
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
              <div className="detail-heading"><div className="detail-kicker"><span>{sourceNames[job.source] || job.source}</span><External url={job.url}>查看原始岗位</External></div><h2>{job.title}</h2><div className="detail-company">{job.company}<span>·</span><Icon name="pin" size={14} />{job.location || '地点待确认'}</div><div className="detail-heading-bottom"><strong>{job.salary || '薪资未披露'}</strong>{isStale(job.updated_at, 30) && <span className="stale-hint" title="这份档案已超过 30 天未更新，薪资与职责可能已变化；可在收藏夹「反查」重读源站，或按原条件重新采集。">数据较旧 · 建议反查</span>}<Button className="button secondary small" disabled={Boolean(busy)} title={job.starred ? '取消收藏；收藏不会因重新采集而丢失' : '收藏岗位，可在收藏夹反查与横评'} onClick={() => job && void toggleStar(job)}><Icon name="star" size={15} />{job.starred ? '已收藏' : '收藏'}</Button><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => openBatchEdit([job])}>编辑档案</Button><Button className="button secondary small" busy={busy === 'analyze' || Boolean(analyzeRun)} title={analyzeDisabledReason(job)} disabled={Boolean(busy) || needsFullJd(job) || Boolean(analyzeRun)} onClick={() => void analyze()}><Icon name="check" size={16} />评估适配</Button><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => { setWorkshopPrefill({ jobId: job.id, company: job.company, jobTitle: job.title }); setView('workshop') }}><Icon name="wand" size={16} />优化简历</Button></div></div>
              <ManualJobDescription key={job.id} job={job} onSaved={async saved => { if (activeJobId.current === saved.id) { await loadDetail(saved.id); setAnalysis(null) }; await loadJobs() }} /><div className="jd-state"><div><strong>{job.description_status === 'complete' ? '完整JD已保存' : needsFullJd(job) ? '仅列表摘要 · 待补全' : '岗位材料已保存'}</strong><p>{job.detail_collected_at ? `正文读取于 ${date(job.detail_collected_at)}` : needsFullJd(job) ? ((job.source === 'boss' || job.source === 'zhaopin') ? '需要完整职责与任职要求，才能评估是否适合投递' : '该平台列表未给出完整职责，可在岗位档案补充后再评估') : '请核对岗位材料完整性及原始出处'}</p></div>{(job.source === 'boss' || job.source === 'zhaopin') && <Button className="button secondary small" busy={busy === 'job-detail'} disabled={Boolean(busy)} onClick={() => void perform('job-detail', async () => { const id = job.id, generation = jobGeneration.current; const result = await post<{ task: Task; job: Job }>(`/jobs/${encodeURIComponent(id)}/detail`); if (taskSucceeded(result.task)) toast(result.task.message); else failedTaskToast(result.task, '完整职责读取'); await Promise.all([generation === jobGeneration.current ? loadDetail(id) : Promise.resolve(), loadJobs(), refreshMetadata()]); if (generation === jobGeneration.current) setAnalysis(null) })}>{job.description_status === 'complete' ? '更新完整职责' : '读取完整职责'}</Button>}</div><div className="detail-tabs" role="tablist" aria-label="岗位档案内容" onKeyDown={navigateTabs}>{(['overview', 'analysis', 'evidence'] as const).map(item => <button key={item} id={`job-${item}-tab`} role="tab" aria-controls="job-detail-panel" tabIndex={tab === item ? 0 : -1} aria-selected={tab === item} className={tab === item ? 'active' : ''} onClick={() => setTab(item)}>{item === 'overview' ? '岗位与公司' : item === 'analysis' ? '适配分析' : '证据资料'}{item === 'evidence' && <span>{evidenceTotal}</span>}</button>)}</div>
              <div id="job-detail-panel" className="detail-content" role="tabpanel" aria-labelledby={`job-${tab}-tab`}>
                {tab === 'overview' && <><div className="section-label"><span className="label-line" />岗位描述</div><div className={`description-text${descExpanded ? '' : ' collapsed'}`}>{plain(job.description) || '尚无岗位描述，请补充原始岗位资料。'}</div>{job.description.length > 600 && <button className="text-button" onClick={() => setDescExpanded(value => !value)}>{descExpanded ? '收起描述' : '展开全文'}</button>}<div className="company-section"><div className="section-heading"><div><span className="section-label">公司情报</span><h3>了解 {job.company}</h3></div><span className="purpose-heading-actions"><Button className="button secondary small" title="围绕这家公司已保存的全部岗位与资料发起对话（在公司资料库的「公司对话」页打开）" onClick={() => openCompanyChat(job.company)}>公司对话<Icon name="chat" size={14} /></Button><Button className="button secondary small" busy={busy === 'company'} disabled={Boolean(busy)} onClick={() => void collectCompany()}>一键完备<Icon name="arrow" size={14} /></Button></span></div><p className="muted body-small">一键串完单岗完整参考路径：多平台检索岗位与公告 → 快查公开页 → 补读完整职责 → 采集已启用信号源 → 抽取材料数字 → AI 行业判断 → 重算报告。官方页面和付费 API 可在下方「按来源补数据」中选择；遇登录/验证会停下来等你处理。</p>{detail.company_evidence?.length ? <>{detail.company_evidence.length > 4 && <div className="company-pager"><button className="text-button" disabled={companyPage === 0} onClick={() => setCompanyPage(page => page - 1)}>上一页</button><span className="tiny-label">{companyPage + 1} / {Math.ceil(detail.company_evidence.length / 4)} 页 · 共 {detail.company_evidence.length} 条</span><button className="text-button" disabled={companyPage >= Math.ceil(detail.company_evidence.length / 4) - 1} onClick={() => setCompanyPage(page => page + 1)}>下一页</button></div>}<div className="company-evidence-list">{detail.company_evidence.slice(companyPage * 4, companyPage * 4 + 4).map(e => <EvidenceCard key={e.id} evidence={e} />)}</div></> : <div className="inline-empty">尚未收集到该公司的资料。你可以多平台检索公开岗位与公告，或补充已有材料。</div>}<button className="text-button" onClick={() => openModal('evidence')}><Icon name="plus" size={14} />补充公司资料</button><CompanyReference key={job.id} onSignalBusy={value => setBusy(value ? 'reliability' : '')} onSignalCollected={(report, history) => { if (job.id !== activeJobId.current) return; setReliability(report); setReliabilityHistory(history); setReliabilityAction(null); void refreshMetadata() }} onConfigureSources={() => { setView('sources'); setSourcesTab('catalog') }} onAgentCollect={openGlobalAgent} report={reliability} history={reliabilityHistory} busy={Boolean(busy)} loading={reliabilityLoading} action={reliabilityAction} company={job.company} onOpenCompare={() => setView('compare')} onCompute={() => void recomputeReference()} onCoverage={() => void runReferenceAction('coverage')} onExtract={() => void runReferenceAction('extract')} onOpenEvidence={openReferenceEvidence} onOpenEvidenceModal={() => openModal('evidence')} /></div></>}
                {tab === 'analysis' && jobVersions && jobVersions.length > 0 && <div className="eval-resume-bar"><span className="analysis-history-label">评估简历</span><select value={pinnedVersion?.id ?? ''} aria-label="选择评估用简历" onChange={event => void switchEvalResume(event.target.value)}>
                  <option value="">我的简历（全局）</option>
                  {jobVersions.map(version => <option key={version.id} value={version.id}>存档《{version.title}》</option>)}
                </select><span className="tiny-label">评估适配自动使用所选简历；工坊针对该岗位的新产出会自动接管</span></div>}
                {tab === 'analysis' && (!analysis ? (analyzeRun ? <AnalyzeProgress run={analyzeRun} onCancel={() => void cancelAnalyze()} /> : <Empty title={busy === 'analyze' ? '正在对应岗位要求…' : '让经历与要求逐项对照'} icon="resume">{busy === 'analyze' ? '分析完成后，将展示匹配依据、未知项与面试核实问题。' : <>{resumeSaved ? '点击“评估适配”，查看有依据的匹配与缺口。' : <>先在<button className="inline-button" onClick={() => setView('resume')}>我的简历</button>保存个人经历，再生成适配分析。</>}分数仅供辅助比较，不代表录用概率。</>}</Empty>) : <>
                  {analysisHistory && analysisHistory.length > 1 && <div className="analysis-history"><span className="analysis-history-label">历史评估</span><div className="analysis-history-list">{analysisHistory.map(item => <button key={item.id} className={item.id === activeReportId ? 'active' : ''} title={item.method === 'rule_assisted' ? '规则分析（未绑定分析模型时自动回退）' : '模型辅助语义分析'} onClick={() => void loadHistoryReport(item.id)}>{date(item.created_at)}{item.score != null ? ` · ${Math.round(item.score)} 分` : ''}{item.method === 'rule_assisted' ? ' · 规则' : ''}</button>)}</div></div>}
                  <AnalysisView analysis={analysis} onStatusChange={(quote, status) => void changeRequirementStatus(quote, status)} />
                </>)}
                {tab === 'evidence' && <><div className="section-heading"><div><h3>证据与出处</h3></div><button className="text-button" onClick={() => openModal('evidence')}><Icon name="plus" size={14} />补充资料</button></div>{evidenceTotal ? <JobEvidence key={job.id} jobId={job.id} focus={evidenceFocus} /> : <Empty title="尚无证据资料">采集岗位或补充公司材料后，原文与来源会保存在这里。</Empty>}</>}
              </div>
            </>}
          </section>
          {job && <section className="chat-section job-chat"><button className="button secondary small job-return" onClick={returnToJob}>返回岗位</button><ChatWorkspace key={job.id} scope={{ kind: 'job', jobId: job.id, company: job.company, title: job.title }} modelConfigured={modelConfigured} onConfigureModel={configureModel} onNavigateConversation={navigateConversation} onOpened={chatOpened} onManageExtensions={() => openSettings('extensions')} onOpenKnowledge={() => setView('knowledge')} /></section>}
        </div>}
      </>}

      {view === 'resume' && <><section className="page-intro"><div><h1>我的简历</h1><p>你的简历是适配分析的依据，保存在当前本地工作区。</p></div>{resumeRevision != null && <span className="revision-label">版本 {resumeRevision}</span>}</section><div className="resume-layout"><form className="paper-panel resume-editor" onSubmit={saveResume}><div className="section-heading"><h2>简历与事实档案</h2><label className="format-select"><span className="sr-only">简历格式</span><select value={resumeFormat} onChange={event => { setResumeFormat(event.target.value); setResumeSaved(false) }}><option value="text">简历纯文本 / Markdown</option><option value="facts_yaml">ResumeLoom facts.yaml</option></select></label></div><ResumeImport disabled={Boolean(busy) || loading} hasText={Boolean(resumeText.trim())} onApply={(text, format) => { setResumeText(text); setResumeFormat(format); setResumeSaved(false); toast('已提取并填入编辑区，请核对内容后保存。') }} /><label htmlFor="resume-text" className="field-label">简历正文 · 可直接编辑或粘贴</label><textarea className="resume-textarea" id="resume-text" value={resumeText} onChange={event => { setResumeText(event.target.value); setResumeSaved(false) }} required maxLength={100000} spellCheck={false} placeholder={resumeFormat === 'facts_yaml' ? 'schema_version: 1\nfacts:\n  ...' : '填写技能、工作经历、项目经验，以及地点和薪资等求职偏好。\n\n建议保留具体职责、项目背景和经过确认的成果。'} /><div className="editor-footer"><span className={resumeSaved ? '' : 'editor-unsaved'}>{resumeText.length.toLocaleString()} 字符 · {resumeSaved ? '已保存' : '未保存的修改'}</span><Button className="button primary" busy={busy === 'resume'} disabled={Boolean(busy)} type="submit">保存到本地<Icon name="check" size={17} /></Button></div></form><aside className="paper-panel resume-guide"><span className="section-label">整理建议</span><h2>整理可核对的经历</h2><div className="guide-item"><b>01</b><div><h3>保留具体经历</h3><p>技能、职责和项目事实，比笼统的自我评价更有帮助。</p></div></div><div className="guide-item"><b>02</b><div><h3>未知就留作未知</h3><p>未提到的技能不能自动认定为不会，也不能当作已经掌握。</p></div></div><div className="guide-item"><b>03</b><div><h3>复用 ResumeLoom</h3><p>可导入 PDF、Word .docx 和文本文件，也可使用 facts.yaml。扫描件需先 OCR；旧版 .doc 请另存为 .docx。</p></div></div><div className="privacy-note"><Icon name="book" size={18} /><p>文件在本机提取文字，确认保存后仅保留文本。启用外部模型分析时，相关资料可能发送至你配置的模型服务。</p></div></aside></div></>}

      {view === 'profile' && <ProfileView />}
      {view === 'workshop' && <ResumeWorkshop prefill={workshopPrefill} onPrefillConsumed={() => setWorkshopPrefill(null)} onOpenResume={() => setView('resume')} />}

      {view === 'sources' && <SourcesView active={view === 'sources'} tab={sourcesTab} onTabChange={setSourcesTab}
        sources={sources} loading={loading} jobsTotal={jobsTotal} browser={browser} onBrowserStatus={setBrowser}
        tasks={tasks} tasksInfo={tasksInfo} loadTasks={loadTasks} busy={busy} perform={perform}
        refreshMetadata={refreshMetadata} onJobsChanged={loadJobs} onOpenJobs={() => { setView('jobs'); setPaneFocus('info') }}
        query={query} onQueryChange={setQuery} location={location} onLocationChange={setLocation}
        browserSource={browserSource} onBrowserSourceChange={setBrowserSource}
        browserLimit={browserLimit} onBrowserLimitChange={setBrowserLimit}
        cityPinyin={cityPinyin} locations={locations} sourceNames={sourceNames} />}

      {view === 'logs' && <LogView />}

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
    <Tutorial open={tutorialOpen} onClose={() => setTutorialOpen(false)} />
  </div>
}

function AnalyzeProgress({ run, onCancel }: { run: AnalyzeRunState; onCancel: () => void }) {
  const [elapsed, setElapsed] = useState(0)
  useEffect(() => {
    const timer = setInterval(() => setElapsed(Math.round((Date.now() - run.startedAt) / 1000)), 1000)
    return () => clearInterval(timer)
  }, [run.startedAt])
  const matchDetail = run.match.total != null ? `已完成 ${run.match.done}/${run.match.total} 组`
    : run.match.done > 0 ? `已完成 ${run.match.done} 组` : ''
  const stages: { status: AnalyzeStageStatus; label: string; detail: string }[] = [
    { status: run.extract.status, label: '拆解岗位要求', detail: run.extract.total != null ? `${run.extract.total} 条` : '' },
    { status: run.match.status, label: '匹配简历事实', detail: matchDetail },
    { status: run.score.status, label: '校验计分', detail: '' },
  ]
  const minutes = Math.floor(elapsed / 60)
  return <section className="paper-panel analyze-progress">
    <div className="section-heading"><div><h2>正在评估适配<span className="analyze-elapsed">{minutes > 0 ? `${minutes} 分 ${elapsed % 60} 秒` : `${elapsed} 秒`}</span></h2><p className="body-small muted">拆解 → 分组匹配 → 校验计分，已完成的条目实时出现在下方；取消将在当前小组完成后生效。</p></div><button className="button secondary small" onClick={onCancel}>取消分析</button></div>
    <ul className="analyze-stages">{stages.map(stage => <li key={stage.label} className={stage.status}>
      <span className="stage-marker">{stage.status === 'done' ? '✓' : stage.status === 'running' ? <span className="spinner" /> : '○'}</span>
      {stage.label}{stage.detail && <span className="stage-detail">{stage.detail}</span>}
    </li>)}</ul>
    {run.items.length > 0 ? <div className="requirements-table-wrap"><table className="requirements-table"><thead><tr><th scope="col">岗位要求</th><th scope="col">判断</th></tr></thead><tbody>{run.items.map((item, index) => <tr key={index}>
      <td><strong>{item.requirement}</strong>{item.hard && <span className="hard-label">硬性要求</span>}<span className="requirement-dimension">{dimensionNames[item.dimension] || item.dimension}</span></td>
      <td><span className={`requirement-status ${item.status}`}><i />{requirementNames[item.status] || item.status}</span></td>
    </tr>)}</tbody></table></div> : <p className="muted body-small">等待第一批映射结果…</p>}
  </section>
}
const ANALYSIS_STATUS_ORDER: Record<string, number> = { unmet: 0, partial: 1, unknown: 2, met: 3 }

function AnalysisView({ analysis, onStatusChange }: { analysis: Analysis; onStatusChange: (quote: string, status: string) => void }) {
  const coverage = Math.max(0, Math.min(100, analysis.coverage * 100))
  const judged = analysis.requirements.filter(item => item.status !== 'unknown').length
  const score = analysis.score
  // 表格工具栏：纯客户端筛选排序，原始顺序恒为基线，切换筛选不打乱报告本身。
  const [statusFilter, setStatusFilter] = useState('')
  const [dimensionFilter, setDimensionFilter] = useState('')
  const [hardOnly, setHardOnly] = useState(false)
  const [sortMode, setSortMode] = useState<'default' | 'status' | 'dimension'>('default')
  const editable = analysis.method === 'model_assisted'
  // 报告拆成子页签：单页不再被右侧栏拉长，条目少时也不留大片空白。
  const [analysisTab, setAnalysisTab] = useState<'mapping' | 'hard' | 'basis' | 'interview' | 'facts'>('mapping')
  const subTabs: { id: typeof analysisTab; label: string; count?: number }[] = [
    { id: 'mapping', label: '要求映射', count: analysis.requirements.length },
    ...(analysis.hard_conditions?.length ? [{ id: 'hard' as const, label: '硬性条件', count: analysis.hard_conditions.length }] : []),
    { id: 'basis', label: '依据与缺口' },
    { id: 'interview', label: '面试核实', count: analysis.questions.length },
    { id: 'facts', label: '薪酬与公司' },
  ]
  const rows = analysis.requirements
    .map((item, index) => ({ item, index }))
    .filter(({ item }) => (!statusFilter || item.status === statusFilter) && (!dimensionFilter || item.dimension === dimensionFilter) && (!hardOnly || item.hard))
    .sort((a, b) => sortMode === 'status' ? (ANALYSIS_STATUS_ORDER[a.item.status] ?? 9) - (ANALYSIS_STATUS_ORDER[b.item.status] ?? 9)
      : sortMode === 'dimension' ? (dimensionNames[a.item.dimension] || a.item.dimension).localeCompare(dimensionNames[b.item.dimension] || b.item.dimension)
      : a.index - b.index)
  return <>
    <div className="analysis-topline">
      <div className="analysis-score"><strong>{score == null ? '—' : Math.round(score)}</strong><span>{score == null ? '暂不生成总分' : '/ 100 综合适配参考'}</span></div>
      <div className="analysis-coverage"><span>{editable ? '语义证据覆盖' : '词项核实覆盖'} {Math.round(coverage)}%</span><span>{judged} / {analysis.requirements.length} 项有对应依据</span><progress className="coverage-progress" value={coverage} max="100" aria-label="要求证据覆盖率" /></div>
      <div className="analysis-tags"><span>{editable ? '模型辅助分析' : '规则辅助 · 词项核实'}</span><span>简历：{analysis.resume_label || '我的简历'}</span></div>
    </div>
    <p className="analysis-disclaimer">未知项可在「判断」列手动改判（按你的确认重算并跨重评保留）；硬性条件也可在「个人资料库 → 求职偏好」提前申报，评估时自动判上；分数仅供辅助比较，不代表录用概率</p>
    {analysis.limitations && <details className="analysis-limitations"><summary>分析范围与限制</summary><p>{readable(analysis.limitations)}</p></details>}
    <div className="analysis-subtabs" role="tablist" aria-label="适配报告分区" onKeyDown={navigateTabs}>
      {subTabs.map(item => <button key={item.id} id={`analysis-${item.id}-tab`} role="tab" aria-controls="analysis-subtab-panel" aria-selected={analysisTab === item.id} tabIndex={analysisTab === item.id ? 0 : -1} className={analysisTab === item.id ? 'active' : ''} onClick={() => setAnalysisTab(item.id)}>{item.label}{item.count != null && <span>{item.count}</span>}</button>)}
    </div>
    <div id="analysis-subtab-panel" className="analysis-subtab-panel" role="tabpanel" aria-labelledby={`analysis-${analysisTab}-tab`}>
      {analysisTab === 'mapping' && <>
        <div className="analysis-toolbar">
          <select value={statusFilter} aria-label="按状态筛选" onChange={event => setStatusFilter(event.target.value)}>
            <option value="">全部状态</option>
            {(['met', 'partial', 'unmet', 'unknown'] as const).map(status => <option key={status} value={status}>{requirementNames[status]}</option>)}
          </select>
          <select value={dimensionFilter} aria-label="按维度筛选" onChange={event => setDimensionFilter(event.target.value)}>
            <option value="">全部维度</option>
            {['skill', 'experience', 'industry', 'preference'].map(dimension => <option key={dimension} value={dimension}>{dimensionNames[dimension] || dimension}</option>)}
          </select>
          <label className="agent-checkbox" title="只看标注为硬性要求（学历/年限/地点等门槛）的条目">
            <input type="checkbox" checked={hardOnly} onChange={event => setHardOnly(event.target.checked)} />仅看硬性
          </label>
          <select value={sortMode} aria-label="排序方式" onChange={event => setSortMode(event.target.value as typeof sortMode)}>
            <option value="default">按原文顺序</option>
            <option value="status">按判断（问题在前）</option>
            <option value="dimension">按维度</option>
          </select>
          <span className="tiny-label">{rows.length} / {analysis.requirements.length} 条</span>
        </div>
        {analysis.requirements.length ? (rows.length ? <div className="requirements-table-wrap"><table className="requirements-table"><thead><tr><th scope="col">岗位要求</th><th scope="col">简历依据</th><th scope="col">判断</th></tr></thead><tbody>{rows.map(({ item }) => <tr key={item.jd_quote || item.requirement}>
      <td><strong>{item.requirement}</strong>{item.hard && <span className="hard-label">硬性要求</span>}<span className="requirement-dimension">{dimensionNames[item.dimension] || item.dimension}</span>{item.jd_quote && <details className="jd-source"><summary>JD 原文</summary><blockquote>{item.jd_quote}</blockquote></details>}</td>
      <td><p>{item.rationale || (Array.isArray(item.evidence) && item.evidence.length > 0 ? '简历材料中找到对应词项，实际熟练程度待核实' : '尚未提供对应事实，待核实')}</p>{Array.isArray(item.evidence) && item.evidence.length > 0 && <span className="fact-reference">简历事实 {item.evidence.join('、')}</span>}</td>
      <td>{editable && item.jd_quote
        ? <span className="status-cell"><select className="status-select" value={item.status} aria-label="手动改判状态" onChange={event => onStatusChange(item.jd_quote!, event.target.value)}>
            <option value="met">满足</option><option value="partial">部分满足</option><option value="unmet">未满足</option><option value="unknown">待确认</option>
          </select>{item.overridden && <span className="overridden-badge" title="按你的手动确认计分，重新评估时自动沿用">已确认</span>}</span>
        : <span className={`requirement-status ${item.status}`}><i />{requirementNames[item.status] || item.status}</span>}</td>
    </tr>)}</tbody></table></div> : <div className="inline-empty">当前筛选下没有条目</div>) : <div className="inline-empty">尚未提取到可分析的要求</div>}
      </>}
      {analysisTab === 'hard' && Boolean(analysis.hard_conditions?.length) && <div className="hard-conditions in-subtab"><h4>硬性条件 · 单独核实</h4>{analysis.hard_conditions?.map((item, index) => <HardCondition key={index} value={item} />)}</div>}
      {analysisTab === 'basis' && <div className="analysis-columns"><AnalysisList title="已有依据" items={analysis.strengths} /><AnalysisList title="缺口与未知" items={analysis.gaps} /></div>}
      {analysisTab === 'interview' && <AnalysisList title="面试时建议核实" items={analysis.questions} />}
      {analysisTab === 'facts' && <div className="analysis-facts-grid"><SalaryView value={analysis.salary} /><div className="analysis-note company-summary"><h4>公司证据</h4><p>{readable(analysis.company_summary)}</p><span className="source-annotation">出处见「证据资料」 · {analysis.evidence_ids.length} 条关联证据</span></div></div>}
    </div>
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
