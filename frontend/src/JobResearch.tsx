import { useCallback, useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { api, post, plain, date, isStale, relativeDays, computeReliability, collectCompanySignals, extractCompanySignals, getReliability, getReliabilityHistory, patchJobs, startCompanyResearch, startAnalyzeRun, cancelAnalyzeRun, openAnalyzeStream, listJobReports, getJobReport, updateAnalysisStatus, listResumeVersions, pinResumeVersion, getAnalyzeRun } from './api'
import type { Analysis, AnalyzeRunStart, Evidence, Job, PageInfo, ReliabilityHistory, ReliabilityReport, ReportSummary, ResumeVersionSummary, Source, SourceHealth, Task, ConversationNavigation } from './api'
import { toast, dismissToast, failedTaskToast, taskSucceeded } from './toast'
import { Pagination, emptyPage } from './Pagination'
import { EvidenceCard } from './EvidenceCard'
import { CityInput } from './CityInput'
import type { SavedLocation } from './CityInput'
import { Button, Empty, External } from './ui'
import { BossImport } from './BossImport'
import { CompanyReference } from './CompanyReference'
import { ManualJobDescription } from './ManualJobDescription'
import { JobCard } from './JobCard'
import { ChatWorkspace } from './chat/ChatWorkspace'
import { PipelineFollowEditor } from './PipelineFollowEditor'
import { PIPELINE_NAMES, PIPELINE_ORDER, isDueOverdue } from './FavoritesView'
import { AnalysisView, AnalyzeProgress } from './AnalysisView'
import type { AnalyzeRunState } from './AnalysisView'
import { navigateTabs } from './tabNavigation'
import { Icon } from './Icon'
import type { WorkshopPrefill } from './ResumeWorkshop'
import type { SettingsTab } from './navigation'
import type { SourcesTab } from './SourcesView'

// 岗位研究工作区（2026-11-19 自 App.tsx 拆出）：采集检索、档案列表、岗位详情（公司情报 /
// 适配分析 / 证据资料）、岗位对话与全部岗位类弹窗。常驻挂载（hidden 切换），切换视图不丢
// 筛选与选中状态；跨视图联动经三个信号进来：openRequest（收藏/今日概览点名岗位或列表）、
// favoritesTick（收藏变动后按需重刷）、refreshTick（顶栏刷新）。
export type JobOpenRequest = { kind: 'job'; id: string; token: number } | { kind: 'list'; token: number }

function jobQuery(filters: { q: string; location: string; source: string; salaryMin: string; salaryMax: string; starred?: boolean }) {
  const params = new URLSearchParams({ q: filters.q, location: filters.location, source: filters.source })
  if (filters.salaryMin) params.set('salary_min', String(Math.round(Number(filters.salaryMin) * 1000)))
  if (filters.salaryMax) params.set('salary_max', String(Math.round(Number(filters.salaryMax) * 1000)))
  if (filters.starred) params.set('starred', 'true')
  return params
}
const transportNames: Record<string, string> = { http_api: '接口', browser_page: '需浏览器', company_web: '网页快查', manual: '手动' }
function needsFullJd(job: Job) {
  return job.description_status === 'listing' || (['boss', 'zhaopin'].includes(job.source) && job.description_status !== 'complete')
}

export function JobResearch({
  active, loading, sources, locations, cityPinyin, modelConfigured, resumeSaved, sourceNames, sourceHealth,
  query, onQueryChange, location, onLocationChange, source, onSourceChange,
  refreshMetadata, refreshFavorites, onTotalChange,
  onOpenSettings, onConfigureModel, onOpenSources, onOpenCompanyChat, onOpenGlobalAgent, onOpenCompare, onOpenKnowledge, onOpenResume,
  onNavigateConversation, onConversationOpened, onWorkshopPrefill, onOpenInterview,
  openRequest, onOpenHandled, favoritesTick, refreshTick,
}: {
  active: boolean
  loading: boolean
  sources: Source[]
  locations: SavedLocation[]
  cityPinyin: Record<string, string[]>
  modelConfigured: boolean
  resumeSaved: boolean
  sourceNames: Record<string, string>
  // 来源健康度（2026-11-19）：App 在 refreshMetadata 里一并拉取；检索区按所选来源显示上次采集结果。
  sourceHealth: SourceHealth[]
  // 采集表单草稿与「数据与采集」页共享同一份（App 持有，两处受控）。
  query: string; onQueryChange: (value: string) => void
  location: string; onLocationChange: (value: string) => void
  source: string; onSourceChange: (value: string) => void
  refreshMetadata: () => Promise<void>
  refreshFavorites: () => Promise<void>
  onTotalChange: (total: number) => void
  onOpenSettings: (tab: SettingsTab) => void
  onConfigureModel: () => void
  onOpenSources: (tab: SourcesTab) => void
  onOpenCompanyChat: (company: string) => void
  onOpenGlobalAgent: (prompt: string) => void
  onOpenCompare: () => void
  onOpenKnowledge: () => void
  onOpenResume: () => void
  onOpenInterview: (jobId: string) => void
  onNavigateConversation: (request: ConversationNavigation) => void
  onConversationOpened: (requestId: string) => void
  onWorkshopPrefill: (prefill: WorkshopPrefill) => void
  openRequest: JobOpenRequest | null
  onOpenHandled: () => void
  favoritesTick: number
  refreshTick: number
}) {
  const [jobs, setJobs] = useState<Job[]>([])
  const [jobsInfo, setJobsInfo] = useState<PageInfo>(emptyPage)
  const [jobsTotal, setJobsTotal] = useState(0)
  // 首次岗位列表未回前保持骨架屏：App 的全局 loading 只覆盖元数据，列表另有自己的就绪位。
  const [jobsLoading, setJobsLoading] = useState(true)
  const booting = loading || jobsLoading
  const jobFilters = useRef({ q: '', location: '', source: '', salaryMin: '', salaryMax: '', starred: false })
  const jobsRequest = useRef(0)
  const [collectLimit, setCollectLimit] = useState(20)
  const [busy, setBusy] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [archiveDraft, setArchiveDraft] = useState({ q: '', source: '', location: '', salaryMin: '', salaryMax: '', starred: false })
  const [batchEdit, setBatchEdit] = useState<{ ids: string[] } | null>(null)
  const [tagsChanged, setTagsChanged] = useState(false)
  const [batchDraft, setBatchDraft] = useState({ location: '', salary: '', tags: '' })
  const [confirmDelete, setConfirmDelete] = useState<{ ids: string[]; label: string } | null>(null)
  // 跟进编辑弹窗目标（2026-11-18）：岗位详情的「跟进」按钮打开。
  const [followJob, setFollowJob] = useState<Job | null>(null)
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
    if (token === jobsRequest.current) { setJobs(data.jobs); setJobsInfo(data); const active = filters; if (!active.q && !active.location && !active.source && !active.salaryMin && !active.salaryMax && !active.starred) { setJobsTotal(data.total); onTotalChange(data.total) } }
    return data.jobs
  }, [onTotalChange])
  useEffect(() => { void loadJobs().finally(() => setJobsLoading(false)) }, [loadJobs])

  // ── 跨视图信号 ──
  const firstFavoritesTick = useRef(true)
  useEffect(() => {
    if (firstFavoritesTick.current) { firstFavoritesTick.current = false; return }
    // 收藏变动会同时影响档案页的「只看收藏」过滤结果，按需重刷。
    if (jobFilters.current.starred) void perform('filter', async () => { await loadJobs(undefined, 1) })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [favoritesTick])
  const firstRefreshTick = useRef(true)
  useEffect(() => {
    if (firstRefreshTick.current) { firstRefreshTick.current = false; return }
    void perform('refresh', async () => {
      await Promise.all([loadJobs(), refreshMetadata(), refreshFavorites()])
      if (selectedId) await loadDetail(selectedId)
    })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refreshTick])
  useEffect(() => {
    if (!openRequest) return
    if (openRequest.kind === 'job') selectJob(openRequest.id)
    else setPaneFocus('info')
    onOpenHandled()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openRequest])
  // 刷新回到岗位研究时接续上次选中的岗位：先验证还在库里（被删则清掉记忆），再走 selectJob 全量复位。
  useEffect(() => {
    if (!active) return
    let last: string | null = null
    try { last = sessionStorage.getItem('jobscope-last-job') } catch { /* 存储不可用忽略 */ }
    if (!last) return
    void api<Job>(`/jobs/${encodeURIComponent(last)}`)
      .then(() => selectJob(last as string))
      .catch(() => { try { sessionStorage.removeItem('jobscope-last-job') } catch { /* 同上 */ } })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
      else if (pinnedVersion) { await pinResumeVersion(pinnedVersion.id, false); toast('已解绑，该岗位评估适配将回退「我的简历」。') }
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
    try { sessionStorage.setItem('jobscope-last-job', id) } catch { /* 存储不可用时仅本次会话生效 */ }
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
          'error', false, { label: '去浏览器采集', onClick: () => onOpenSources('collect') }, 20000)
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
        const status: 'pending' | 'running' | 'done' = data.status === 'done' ? 'done' : 'running'
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
        await loadJobs(); selectJob(result.job.id); toast('岗位已导入，来源标记为用户材料。')
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
  const jobSources = sources.filter(item => item.kind === 'jobs')
  const hasArchiveFilters = Object.values(jobFilters.current).some(Boolean)
  const emptyLibrary = !booting && jobsTotal === 0 && !hasArchiveFilters && !job
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
      void refreshFavorites()
      toast(starred ? '已收藏，可在收藏夹反查与横评' : '已取消收藏')
    })
  }

  // 机会管线（岗位详情快捷操作，2026-11-18）：与收藏夹同一套后端语义——设状态自动收藏，
  // 清状态连带清跟进信息；本地同步刷新列表与详情，避免再拉一次接口。
  async function setPipelineStatus(item: Job, value: 'interested' | 'applied' | 'interviewing' | 'offer' | 'archived' | '') {
    await perform('pipeline', async () => {
      await patchJobs([item.id], { pipeline_status: value })
      const local = value
        ? { pipeline_status: value as Job['pipeline_status'], pipeline_status_changed_at: new Date().toISOString() }
        : { pipeline_status: undefined, pipeline_status_changed_at: undefined, pipeline_note: undefined, pipeline_due: undefined }
      setJobs(list => list.map(entry => entry.id === item.id ? { ...entry, ...local } : entry))
      setDetail(current => current?.job.id === item.id ? { ...current, job: { ...current.job, ...local } } : current)
      void refreshFavorites()
      toast(value ? `已标记为「${PIPELINE_NAMES[value]}」` : '已移出管线')
    })
  }

  function applyFollowPatch(item: Job, patch: { pipeline_note?: string; pipeline_due?: string }) {
    setJobs(list => list.map(entry => entry.id === item.id ? { ...entry, ...patch } : entry))
    setDetail(current => current?.job.id === item.id ? { ...current, job: { ...current.job, ...patch } } : current)
  }

  // 报告缺口 → 工坊闭环（P1，2026-11-19）：未满足/部分满足的要求与缺口清单组装成优化重点
  // 说明，走既有 prefill 链路（自动选中岗位 + 预填说明），优化产出存档后可回评对比。
  function optimizeFromGaps(report: Analysis) {
    if (!job) return
    const problemRequirements = report.requirements
      .filter(item => item.status === 'unmet' || item.status === 'partial')
      .map(item => String(item.requirement || '').trim()).filter(Boolean)
    const gaps = (report.gaps || []).map(item => plain(item)).filter(Boolean)
    const lines: string[] = []
    if (problemRequirements.length) lines.push(`优先补强以下未满足/部分满足的岗位要求：\n- ${problemRequirements.join('\n- ')}`)
    if (gaps.length) lines.push(`评估指出的缺口：\n- ${gaps.join('\n- ')}`)
    // instructions 后端上限 2000 字符，超长截断而不是报错打回。
    onWorkshopPrefill({ jobId: job.id, company: job.company, jobTitle: job.title, instructions: lines.join('\n\n').slice(0, 2000) })
  }
  async function setStarredSelected(starred: boolean) {
    await perform('star-batch', async () => {
      const result = await patchJobs([...selectedIds], { starred })
      toast(`${starred ? '已收藏' : '已取消收藏'} ${result.updated} 份岗位`)
      setSelectedIds(new Set())
      await loadJobs(undefined, jobsInfo.page)
      if (selectedId) await loadDetail(selectedId)
      void refreshFavorites()
    })
  }
  function openJobChat() { jobScroll.current = { window: window.scrollY, detail: document.querySelector('.detail-panel')?.scrollTop || 0, list: document.querySelector('.job-list-panel')?.scrollTop || 0 }; setPaneFocus('chat') }
  function returnToJob() { setPaneFocus('info'); requestAnimationFrame(() => { window.scrollTo({ top: jobScroll.current.window, behavior: 'instant' }); const detailPane = document.querySelector('.detail-panel'); const listPane = document.querySelector('.job-list-panel'); if (detailPane) detailPane.scrollTop = jobScroll.current.detail; if (listPane) listPane.scrollTop = jobScroll.current.list }) }

  return <>
    <div className="jobs-view" hidden={!active}>
      <section className="page-intro compact"><h1 className="sr-only">岗位研究</h1></section>
      {paneFocus !== 'chat' && <div className="job-entry-actions"><button className="button primary" onClick={() => openModal('boss-import')} title="捕获项目浏览器里看到的 Boss 页面、粘贴分享链接，批量导入岗位">从 Boss 导入</button><button className="button secondary" onClick={() => openModal('job')}>粘贴已有岗位 JD</button><button className="button secondary" onClick={() => setDiscoveryOpen(!discoveryOpen)} aria-expanded={discoveryOpen}>查找外部岗位</button></div>}
      <section className="search-section" aria-labelledby="collect-heading" hidden={!discoveryOpen || paneFocus === 'chat'}>
        <div className="search-section-heading"><h2 id="collect-heading"><Icon name="globe" size={16} />发现新岗位</h2><button className="text-button" onClick={() => onOpenSources('collect')}>管理数据源<Icon name="arrow" size={13} /></button></div>
        <form className="search-form" onSubmit={collect}>
          <label className="collect-field collect-keyword"><span>岗位关键词</span><span className="collect-control"><Icon name="search" size={17} /><input value={query} onChange={event => onQueryChange(event.target.value)} placeholder="岗位、技能或公司" required maxLength={200} /></span></label>
          <div className="collect-field collect-location"><label htmlFor="collect-location">工作地点</label><div className="collect-control"><Icon name="pin" size={17} /><CityInput id="collect-location" cities={selectedSource?.cities || []} hot={selectedSource?.hot || []} pinyin={cityPinyin} locations={locations} value={location} onChange={onLocationChange} placeholder="不限地点" maxLength={100} /></div></div>
          <label className="collect-field collect-source"><span>岗位数据源</span><select value={source} onChange={event => onSourceChange(event.target.value)}>{jobSources.map(item => <option key={item.id} value={item.id}>{item.name} · {transportNames[item.transport_mode] || item.transport_mode}</option>)}</select></label>
          <label className="collect-field collect-count"><span>采集数量</span><span className="collect-control"><input type="number" min={1} max={1000} value={collectLimit} onChange={event => setCollectLimit(Math.max(1, Math.min(1000, Number(event.target.value) || 20)))} inputMode="numeric" aria-label="采集数量" /><span>条</span></span></label>
          <Button className="button primary" type="submit" busy={busy === 'collect'} disabled={Boolean(busy)}>检索岗位<Icon name="arrow" size={17} /></Button>
        </form>
        {/* 需要登录的平台在提交前就亮明前置条件（独立元素，避免被矮屏下 footnote 的折叠规则藏掉）。 */}
        {selectedSource?.login === 'required' && <p className="collect-login-note" role="note">「{selectedSource.name}」需要登录后才能返回岗位：请先到「数据与采集 → 浏览器采集」打开专用浏览器并登录，再回到这里用浏览器入口采集；未登录直接检索只会得到登录或验证提示。</p>}
        <div className="search-footnote"><span><span className="small-dot" />{selectedSource?.description || '标注“需浏览器”的来源，需要先在项目 Chrome 中登录。'}</span></div>
        {/* 采集前健康预检（2026-11-19）：上次采集的真实结果在提交前就亮出来——被登录墙挡过、
            连续失败时先去处理，而不是点了才知道；只陈述事实与补法，不给诊断结论。 */}
        {(() => {
          const health = sourceHealth.find(item => item.source === source)
          if (!health || !health.last_run_at) return null
          const warn = health.last_status !== 'completed'
          const statusText = { completed: '成功', needs_login: '被登录墙挡下', needs_verification: '需要人工验证', failed: '失败', backoff: '退避等待中', running: '进行中', pending: '等待中' }[health.last_status] || health.last_status
          return <p className={`source-health-line${warn ? ' warn' : ''}`} role="note" title={health.last_message || undefined}>
            上次采集 {statusText} · {relativeDays(health.last_run_at, date(health.last_run_at))}{health.failed ? ` · 近期失败 ${health.failed} 次` : ''}{warn ? '。建议先处理再采集：' : ''}
            {warn && <button type="button" className="text-button" onClick={() => onOpenSources('health')}>看来源健康<Icon name="arrow" size={12} /></button>}
          </p>
        })()}
      </section>

      {job && paneFocus !== 'chat' && <div className="job-reading-actions"><span>{job.title}</span><button className="button primary small" onClick={openJobChat}>问这份岗位</button></div>}
      {!emptyLibrary && <div className={`research-layout focus-${paneFocus}${job ? '' : ' no-chat'}`}>
        <section className="job-list-panel" aria-labelledby="archive-heading"><div className="panel-title"><h2 id="archive-heading">岗位档案 <span>{jobsInfo.total.toLocaleString()}</span></h2>{hasArchiveFilters && <button className="text-button archive-reset" disabled={Boolean(busy)} onClick={clearArchiveFilters}>清除筛选</button>}</div>
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
              <div className="filter-actions"><button type="button" className="text-button" disabled={Boolean(busy)} onClick={clearArchiveFilters}>清除</button><Button className="button secondary small" type="submit" busy={busy === 'filter'} disabled={Boolean(busy)}>应用条件</Button></div>
            </details>
          </form>
          {jobs.length > 0 && <div className="job-select-bar"><label className="agent-checkbox"><input type="checkbox" checked={allPageSelected} onChange={togglePageSelection} />本页全选</label><button className="text-button" disabled={Boolean(busy)} onClick={() => void selectAllFiltered()}>选择全部 {jobsInfo.total} 条</button>{selectedIds.size > 0 && <div className="selection-actions"><span>已选 {selectedIds.size} 项</span><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => void setStarredSelected(true)}>收藏</Button><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => void setStarredSelected(false)}>取消收藏</Button><Button className="button secondary small" disabled={Boolean(busy)} onClick={openBatchEditForSelection}>编辑</Button><Button className="button secondary small danger-text" disabled={Boolean(busy)} onClick={() => askDelete([...selectedIds], `已选的 ${selectedIds.size} 份岗位档案`)}>删除</Button><button className="text-button" onClick={() => setSelectedIds(new Set())}>取消</button></div>}</div>}
          {booting ? <div className="loading-state"><span className="spinner" />正在连接资料库…</div> : jobs.length === 0 ? <Empty title={hasArchiveFilters ? '没有符合条件的岗位' : '从一个机会开始'} icon="search">{hasArchiveFilters ? <>试试其他关键词，或<button className="inline-button" disabled={Boolean(busy)} onClick={clearArchiveFilters}>清除筛选</button>查看全部档案。</> : '检索真实岗位，或导入你正在考虑的职位。采集后的岗位会保存在这里。'}</Empty> : <div className={`job-list${busy === 'filter' || busy === 'page-jobs' ? ' refreshing' : ''}`}>{jobs.map(item => <JobCard key={item.id} item={item} selected={selectedId === item.id} checked={selectedIds.has(item.id)} onToggleSelection={toggleJobSelection} onOpen={selectJob} onToggleStar={item => void toggleStar(item)} />)}</div>}
          <Pagination info={jobsInfo} label="岗位档案" busy={Boolean(busy)} onPage={page => void perform('page-jobs', async () => { await loadJobs(undefined, page) })} />
        </section>

        <section className="detail-panel" aria-label="岗位详情" tabIndex={-1}>
          {selectedId && <button className="mobile-back text-button" onClick={() => { document.querySelector('.job-list-panel')?.scrollIntoView({ block: 'start' }); document.querySelector<HTMLElement>('.job-card-body[aria-pressed="true"]')?.focus({ preventScroll: true }) }}><Icon name="arrow" size={16} />返回岗位列表</button>}
          {detailLoading ? <div className="loading-state"><span className="spinner" />正在读取岗位档案…</div> : !job ? <div className="welcome-panel"><h2>先选一份岗位，再逐项核对</h2><p>选择左侧档案，查看要求、公司资料与简历依据。</p></div> : <>
            <div className="detail-heading"><div className="detail-kicker"><span>{sourceNames[job.source] || job.source}</span><External url={job.url}>查看原始岗位</External></div><h2>{job.title}</h2><div className="detail-company">{job.company}<span>·</span><Icon name="pin" size={14} />{job.location || '地点待确认'}</div><div className="detail-heading-bottom"><strong>{job.salary || '薪资未披露'}</strong>{isStale(job.updated_at, 30) && <span className="stale-hint" title="这份档案已超过 30 天未更新，薪资与职责可能已变化；可在收藏夹「反查」重读源站，或按原条件重新采集。">数据较旧 · 建议反查</span>}{job.pipeline_due && <span className={`follow-due${isDueOverdue(job.pipeline_due) ? ' overdue' : ''}`} title="跟进截止日；到期待办出现在「今日概览」">截止 {job.pipeline_due}{isDueOverdue(job.pipeline_due) ? '（逾期）' : ''}</span>}<Button className="button secondary small" disabled={Boolean(busy)} title={job.starred ? '取消收藏；收藏不会因重新采集而丢失' : '收藏岗位，可在收藏夹反查与横评'} onClick={() => job && void toggleStar(job)}><Icon name="star" size={15} />{job.starred ? '已收藏' : '收藏'}</Button><select className="pipeline-select detail-pipeline-select" value={job.pipeline_status || ''} disabled={Boolean(busy)} aria-label="机会管线状态" title="机会管线状态：标记投递进展；给未收藏的岗位设状态会自动收藏" onChange={event => void setPipelineStatus(job, event.target.value as 'interested' | 'applied' | 'interviewing' | 'offer' | 'archived' | '')}><option value="">未跟踪</option>{PIPELINE_ORDER.map(status => <option key={status} value={status}>{PIPELINE_NAMES[status]}</option>)}</select><Button className="button secondary small" disabled={Boolean(busy)} title="设置下一步行动与截止日；到期的会出现在「今日概览」待办" onClick={() => setFollowJob(job)}>跟进{job.pipeline_due ? `（${job.pipeline_due.slice(5).replace('-', '/')}）` : ''}</Button><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => openBatchEdit([job])}>编辑档案</Button><Button className="button secondary small" busy={busy === 'analyze' || Boolean(analyzeRun)} title={analyzeDisabledReason(job)} disabled={Boolean(busy) || needsFullJd(job) || Boolean(analyzeRun)} onClick={() => void analyze()}><Icon name="check" size={16} />评估适配</Button><Button className="button secondary small" disabled={Boolean(busy)} onClick={() => onWorkshopPrefill({ jobId: job.id, company: job.company, jobTitle: job.title })}><Icon name="wand" size={16} />优化简历</Button><Button className="button secondary small" title="用 AI 面试官按这个岗位模拟一轮面试；评估缺口会变成追问重点" onClick={() => onOpenInterview(job.id)}><Icon name="mic" size={16} />模拟面试</Button></div></div>
            <ManualJobDescription key={job.id} job={job} onSaved={async saved => { if (activeJobId.current === saved.id) { await loadDetail(saved.id); setAnalysis(null) }; await loadJobs() }} /><div className="jd-state"><div><strong>{job.description_status === 'complete' ? '完整JD已保存' : needsFullJd(job) ? '仅列表摘要 · 待补全' : '岗位材料已保存'}</strong><p>{job.detail_collected_at ? `正文读取于 ${date(job.detail_collected_at)}` : needsFullJd(job) ? ((job.source === 'boss' || job.source === 'zhaopin') ? '需要完整职责与任职要求，才能评估是否适合投递' : '该平台列表未给出完整职责，可在岗位档案补充后再评估') : '请核对岗位材料完整性及原始出处'}</p></div>{(job.source === 'boss' || job.source === 'zhaopin') && <Button className="button secondary small" busy={busy === 'job-detail'} disabled={Boolean(busy)} onClick={() => void perform('job-detail', async () => { const id = job.id, generation = jobGeneration.current; const result = await post<{ task: Task; job: Job }>(`/jobs/${encodeURIComponent(id)}/detail`); if (taskSucceeded(result.task)) toast(result.task.message); else failedTaskToast(result.task, '完整职责读取'); await Promise.all([generation === jobGeneration.current ? loadDetail(id) : Promise.resolve(), loadJobs(), refreshMetadata()]); if (generation === jobGeneration.current) setAnalysis(null) })}>{job.description_status === 'complete' ? '更新完整职责' : '读取完整职责'}</Button>}</div><div className="detail-tabs" role="tablist" aria-label="岗位档案内容" onKeyDown={navigateTabs}>{(['overview', 'analysis', 'evidence'] as const).map(item => <button key={item} id={`job-${item}-tab`} role="tab" aria-controls="job-detail-panel" tabIndex={tab === item ? 0 : -1} aria-selected={tab === item} className={tab === item ? 'active' : ''} onClick={() => setTab(item)}>{item === 'overview' ? '岗位与公司' : item === 'analysis' ? '适配分析' : '证据资料'}{item === 'evidence' && <span>{evidenceTotal}</span>}</button>)}</div>
            <div id="job-detail-panel" className="detail-content" role="tabpanel" aria-labelledby={`job-${tab}-tab`}>
              {tab === 'overview' && <><div className={`description-text${descExpanded ? '' : ' collapsed'}`}>{plain(job.description) || '尚无岗位描述，请补充原始岗位资料。'}</div>{job.description.length > 600 && <button className="text-button" onClick={() => setDescExpanded(value => !value)}>{descExpanded ? '收起描述' : '展开全文'}</button>}<div className="company-section"><div className="section-heading"><div><h3>了解 {job.company}</h3></div><span className="purpose-heading-actions"><Button className="button secondary small" title="围绕这家公司已保存的全部岗位与资料发起对话（在公司资料库的「公司对话」页打开）" onClick={() => onOpenCompanyChat(job.company)}>公司对话<Icon name="chat" size={14} /></Button><Button className="button secondary small" title="一键串完单岗完整参考路径：多平台检索岗位与公告 → 快查公开页 → 补读完整职责 → 采集已启用信号源 → 抽取材料数字 → AI 行业判断 → 重算报告；官方页面和付费 API 可在下方「按来源补数据」中选择，遇登录/验证会停下来等你处理" busy={busy === 'company'} disabled={Boolean(busy)} onClick={() => void collectCompany()}>一键完备<Icon name="arrow" size={14} /></Button></span></div>{detail.company_evidence?.length ? <>{detail.company_evidence.length > 4 && <div className="company-pager"><button className="text-button" disabled={companyPage === 0} onClick={() => setCompanyPage(page => page - 1)}>上一页</button><span className="tiny-label">{companyPage + 1} / {Math.ceil(detail.company_evidence.length / 4)} 页 · 共 {detail.company_evidence.length} 条</span><button className="text-button" disabled={companyPage >= Math.ceil(detail.company_evidence.length / 4) - 1} onClick={() => setCompanyPage(page => page + 1)}>下一页</button></div>}<div className="company-evidence-list">{detail.company_evidence.slice(companyPage * 4, companyPage * 4 + 4).map(e => <EvidenceCard key={e.id} evidence={e} />)}</div></> : <div className="inline-empty">尚未收集到该公司的资料。你可以多平台检索公开岗位与公告，或补充已有材料。</div>}<button className="text-button" onClick={() => openModal('evidence')}><Icon name="plus" size={14} />补充公司资料</button><CompanyReference key={job.id} onSignalBusy={value => setBusy(value ? 'reliability' : '')} onSignalCollected={(report, history) => { if (job.id !== activeJobId.current) return; setReliability(report); setReliabilityHistory(history); setReliabilityAction(null); void refreshMetadata() }} onConfigureSources={() => onOpenSources('catalog')} onAgentCollect={onOpenGlobalAgent} report={reliability} history={reliabilityHistory} busy={Boolean(busy)} loading={reliabilityLoading} action={reliabilityAction} company={job.company} onOpenCompare={onOpenCompare} onCompute={() => void recomputeReference()} onCoverage={() => void runReferenceAction('coverage')} onExtract={() => void runReferenceAction('extract')} onOpenEvidence={openReferenceEvidence} onOpenEvidenceModal={() => openModal('evidence')} /></div></>}
              {tab === 'analysis' && jobVersions && jobVersions.length > 0 && <div className="eval-resume-bar"><span className="analysis-history-label">评估简历</span><select value={pinnedVersion?.id ?? ''} aria-label="选择评估用简历" onChange={event => void switchEvalResume(event.target.value)}>
                <option value="">我的简历（全局）</option>
                {jobVersions.map(version => <option key={version.id} value={version.id}>存档《{version.title}》</option>)}
              </select><span className="tiny-label">评估适配自动使用所选简历；工坊针对该岗位的新产出会自动接管</span></div>}
              {tab === 'analysis' && (!analysis ? (analyzeRun ? <AnalyzeProgress run={analyzeRun} onCancel={() => void cancelAnalyze()} /> : <Empty title={busy === 'analyze' ? '正在对应岗位要求…' : '让经历与要求逐项对照'} icon="resume">{busy === 'analyze' ? '分析完成后，将展示匹配依据、未知项与面试核实问题。' : <>{resumeSaved ? '点击“评估适配”，查看有依据的匹配与缺口。' : <>先在<button className="inline-button" onClick={onOpenResume}>我的简历</button>保存个人经历，再生成适配分析。</>}分数仅供辅助比较，不代表录用概率。</>}</Empty>) : <>
                {analysisHistory && analysisHistory.length > 1 && <div className="analysis-history"><span className="analysis-history-label">历史评估</span><div className="analysis-history-list">{analysisHistory.map(item => <button key={item.id} className={item.id === activeReportId ? 'active' : ''} title={item.method === 'rule_assisted' ? '规则分析（未绑定分析模型时自动回退）' : '模型辅助语义分析'} onClick={() => void loadHistoryReport(item.id)}>{date(item.created_at)}{item.score != null ? ` · ${Math.round(item.score)} 分` : ''}{item.method === 'rule_assisted' ? ' · 规则' : ''}</button>)}</div></div>}
                <AnalysisView analysis={analysis} onStatusChange={(quote, status) => void changeRequirementStatus(quote, status)} onOptimizeGaps={() => optimizeFromGaps(analysis)} onInterviewGaps={() => job && onOpenInterview(job.id)} />
              </>)}
              {tab === 'evidence' && <><div className="section-heading"><div><h3>证据与出处</h3></div><button className="text-button" onClick={() => openModal('evidence')}><Icon name="plus" size={14} />补充资料</button></div>{evidenceTotal ? <JobEvidence key={job.id} jobId={job.id} focus={evidenceFocus} /> : <Empty title="尚无证据资料">采集岗位或补充公司材料后，原文与来源会保存在这里。</Empty>}</>}
            </div>
          </>}
        </section>
        {job && <section className="chat-section job-chat"><button className="button secondary small job-return" onClick={returnToJob}>返回岗位</button><ChatWorkspace key={job.id} scope={{ kind: 'job', jobId: job.id, company: job.company, title: job.title }} modelConfigured={modelConfigured} onConfigureModel={onConfigureModel} onNavigateConversation={onNavigateConversation} onOpened={onConversationOpened} onManageExtensions={() => onOpenSettings('extensions')} onOpenKnowledge={onOpenKnowledge} /></section>}
      </div>}
    </div>
    {modal && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) setModal(null) }}><section className="modal paper-panel" role="dialog" aria-modal="true" aria-labelledby="modal-title" onKeyDown={event => {
        if (event.key === 'Escape' && !busy) { event.stopPropagation(); setModal(null) }
        if (event.key === 'Tab') {
          const focusable = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), a[href]'))
          const first = focusable[0], last = focusable[focusable.length - 1]
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
        }
      }}><div className="section-heading"><div><h2 id="modal-title">{modal === 'job' ? '导入岗位' : modal === 'evidence' ? '补充公司资料' : modal === 'boss-import' ? '从 Boss 导入' : modal === 'batch-edit' ? '批量编辑档案' : '确认删除岗位'}</h2></div><button className="icon-button" aria-label="关闭弹窗" onClick={() => setModal(null)} disabled={Boolean(busy)}><Icon name="close" /></button></div><p className="muted body-small">{modal === 'job' ? '粘贴原始岗位信息，来源将标记为“用户导入”。' : modal === 'evidence' ? '保留原文和来源，帮助后续核实。请勿填写 Cookie 或访问令牌。' : modal === 'boss-import' ? '你在项目浏览器里看到的 Boss 页面就是导入源：捕获当前页、粘贴分享链接，重复岗位自动归并。' : modal === 'batch-edit' ? `将应用到已选的 ${batchEdit?.ids.length ?? 0} 份档案；留空的字段保持不变，薪资按原文覆盖（如 15–25K·13薪）。` : `将删除${confirmDelete?.label || '所选岗位'}，连同其岗位证据、适配报告与岗位对话，此操作不可恢复。`}</p>{(modal === 'job' || modal === 'evidence') && <><form onSubmit={importRecord}><label className="field-label" htmlFor="import-title">{modal === 'job' ? '岗位名称' : '资料标题'} <span>*</span></label><input id="import-title" name="title" required maxLength={300} autoFocus /><label className="field-label" htmlFor="import-company">公司名称 <span>*</span></label><input id="import-company" name="company" required maxLength={300} defaultValue={modal === 'evidence' ? job?.company : ''} />{modal === 'job' ? <div className="form-two"><div><label className="field-label" htmlFor="import-location">工作地点</label><input id="import-location" name="location" maxLength={200} /></div><div><label className="field-label" htmlFor="import-salary">原始薪资描述</label><input id="import-salary" name="salary" maxLength={300} placeholder="保留原文，如 20–25K·13薪" /></div></div> : <><label className="field-label" htmlFor="import-kind">资料类型</label><select id="import-kind" name="kind" defaultValue="user_material"><option value="user_material">用户提供材料</option><option value="employee_statement">员工自述</option><option value="company_statement">企业自述</option><option value="disclosure">正式披露</option><option value="media_report">媒体报道</option></select></>}<label className="field-label" htmlFor="import-url">原始链接（可选）</label><input id="import-url" name="url" type="url" maxLength={2000} placeholder="https://" /><label className="field-label" htmlFor="import-body">{modal === 'job' ? '岗位描述' : '资料原文'} <span>*</span></label><textarea id="import-body" name={modal === 'job' ? 'description' : 'text'} required rows={7} maxLength={100000} /><div className="modal-actions"><button type="button" className="button secondary" onClick={() => setModal(null)} disabled={Boolean(busy)}>取消</button><Button className="button primary" type="submit" busy={busy === 'import'} disabled={Boolean(busy)}>保存资料<Icon name="check" size={16} /></Button></div></form></>}
              {modal === 'boss-import' && <BossImport onClose={() => setModal(null)} onImported={() => { void loadJobs(); void refreshFavorites(); void refreshMetadata() }} />}
              {modal === 'batch-edit' && batchEdit && <form onSubmit={submitBatchEdit}><label className="field-label" htmlFor="batch-location">工作地点</label><input id="batch-location" value={batchDraft.location} onChange={event => setBatchDraft({ ...batchDraft, location: event.target.value })} maxLength={300} autoFocus /><label className="field-label" htmlFor="batch-salary">薪资描述</label><input id="batch-salary" value={batchDraft.salary} onChange={event => setBatchDraft({ ...batchDraft, salary: event.target.value })} maxLength={300} placeholder="15–25K·13薪" /><label className="field-label" htmlFor="batch-tags">个人标签（逗号分隔，可清空；平台标签保留）</label><input id="batch-tags" value={batchDraft.tags} onChange={event => { setTagsChanged(true); setBatchDraft({ ...batchDraft, tags: event.target.value }) }} maxLength={500} placeholder="已核实, 优先投递" /><div className="modal-actions"><button type="button" className="button secondary" onClick={() => setModal(null)} disabled={Boolean(busy)}>取消</button><Button className="button primary" type="submit" busy={busy === 'batch-edit'} disabled={Boolean(busy)}>保存修改<Icon name="check" size={16} /></Button></div></form>}
              {modal === 'confirm-delete' && confirmDelete && <div className="modal-actions"><button type="button" className="button secondary" onClick={() => setModal(null)} disabled={Boolean(busy)}>取消</button><Button className="button danger" busy={busy === 'batch-delete'} disabled={Boolean(busy)} onClick={() => void perform('batch-delete', submitBatchDelete)}>确认删除</Button></div>}
            </section></div>}
    {followJob && <PipelineFollowEditor job={followJob} onClose={() => setFollowJob(null)} onSaved={patch => applyFollowPatch(followJob, patch)} />}
  </>
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
