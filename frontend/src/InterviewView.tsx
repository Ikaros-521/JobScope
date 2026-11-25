import { useCallback, useEffect, useRef, useState } from 'react'
import { Markdown } from './chat/Markdown'
import { Icon } from './Icon'
import { ConfirmDialog } from './ConfirmDialog'
import { toast } from './toast'
import AttributionPanel from './AttributionPanel'
import {
  api, cancelInterviewRun, cancelTranscriptRun, createInterviewSession, date, deleteInterviewSession,
  deleteInterviewTranscript, finishInterview, generateTranscriptReport, getAttribution, getFailureMuseum, getInterviewMessages, getInterviewSession,
  getInterviewTranscript, importInterviewTranscript, listInterviewPersonas, listInterviewSessions,
  listInterviewTranscripts, listInterviewWeakPoints, openInterviewStream, openTranscriptRunStream, relabelInterviewTranscript,
  saveTranscriptSegments, sendInterviewTurn,
} from './api'
import type { AttributionRecord, InterviewDrillRef, InterviewMessage, InterviewPersonaCard, InterviewReport, InterviewReview, InterviewSession, InterviewSessionSummary, InterviewStyle, InterviewWeakPoint, InterviewWeakPoints, Transcript, TranscriptReport, TranscriptSegment, TranscriptSummary } from './api'

// 模拟面试工作区（2026-11-19）：左侧场次列表 + 右侧「面试间 | 复盘报告」。
// 面试官是纯拟真的无工具 agent：对话中零提示零点评，评价全部在复盘页签。
// 流式渲染只复用 Markdown，不复用 ChatWorkspace（模型下拉/工具选项不适用面试场景）。
// 2026-11-21 增加并列的第二条链路「真实复盘」：外部 ASR 逐字稿导入 → 模型初标说话人
// → 人工核对修正 → 结合 JD 的复盘报告（侧栏顶部分段切换，两套工作区互不干扰）。

export type InterviewDeepLink = { jobId: string; focus: string; drill?: InterviewDrillRef[]; token: number } | null

type StreamState = { runId: string; answer: string } | null
// 复盘等后台 run 的实时进度（SSE progress 事件）：stage=review 逐题点评 done/total，summary=汇总中。
type RunProgress = { stage: 'review' | 'summary'; done: number; total: number; topic?: string } | null
// 渐进式复盘 feed：start 事件铺全部题卡骨架，review 事件按题号原位填充内容，done 换终版报告。
type ReportFeedItem = { index: number; topic: string; question: string; previous_problems?: string[]; review?: { critique: string; missing: string[]; model_answer: string; bad_answer_note: string } }
type ReportFeed = { items: ReportFeedItem[]; done: number; total: number; stage: 'review' | 'summary' } | null

const STATUS_NAMES: Record<string, string> = { planning: '出题中', ready: '待开始', live: '面试中', reporting: '复盘生成中', done: '已复盘', failed: '出题失败' }
const INTENSITY_NAMES: Record<string, string> = { gentle: '温和', standard: '标准', stress: '压力面' }
const SOURCE_NAMES: Record<string, string> = { jd: 'JD', gap: '评估缺口', resume: '简历', general: '通用', weakness: '专项强化' }
// 一场最多重练的错题数（与后端 InterviewCreateInput.drill 上限一致）。
const MAX_DRILLS = 12
const TRANSCRIPT_STATUS_NAMES: Record<string, string> = { imported: '待标注', labeling: '标注中', labeled: '待核对', reporting: '报告生成中', done: '已复盘', failed: '标注失败' }
// 说话人固定选项（与后端归一化取值一致）：me=候选人，interviewer_N=第 N 位面试官。
const SPEAKER_OPTIONS = ['me', 'interviewer_1', 'interviewer_2', 'interviewer_3', 'interviewer_4', 'interviewer_5']
function speakerLabel(speaker: string) { return speaker === 'me' ? '候选人' : `面试官${speaker.replace('interviewer_', '') || '?'}` }

export default function InterviewView({ deepLink, modelConfigured, onConfigureModel }: {
  deepLink: InterviewDeepLink; modelConfigured: boolean; onConfigureModel: () => void
}) {
  const [personas, setPersonas] = useState<InterviewPersonaCard[]>([])
  const [sessions, setSessions] = useState<InterviewSessionSummary[]>([])
  const [selected, setSelected] = useState<string>('')
  const [session, setSession] = useState<InterviewSession | null>(null)
  const [messages, setMessages] = useState<InterviewMessage[]>([])
  const [stream, setStream] = useState<StreamState>(null)
  const [runProgress, setRunProgress] = useState<RunProgress>(null)
  const [reportFeed, setReportFeed] = useState<ReportFeed>(null)
  const [error, setError] = useState('')
  const [tab, setTab] = useState<'room' | 'report'>('room')
  // 新建面板：卡片=岗位专家（细分到岗位类型），公司风格是独立开关；目标岗位三模式：
  // 岗位库选择（自动带 JD/评估缺口/简历存档/公司资料）｜粘贴 JD｜自由练（自定义卡必须填岗位名）。
  const [creating, setCreating] = useState(false)
  const [card, setCard] = useState<string>('')
  const [styles, setStyles] = useState<InterviewStyle[]>([])
  const [style, setStyle] = useState('generic')
  const [round, setRound] = useState('')
  const [intensity, setIntensity] = useState<'gentle' | 'standard' | 'stress'>('standard')
  const [questionCount, setQuestionCount] = useState(8)
  const [mode, setMode] = useState<'job' | 'jd' | 'free'>('free')
  const [jobId, setJobId] = useState('')
  const [jobLabel, setJobLabel] = useState('')
  const [jobTitle, setJobTitle] = useState('')
  const [companyName, setCompanyName] = useState('')
  const [jdText, setJdText] = useState('')
  const [answer, setAnswer] = useState('')
  // 备战计划带来的当日专项：出题时作为 weak_focus 定向覆盖，并在新建面板顶部明示。
  const [focusHint, setFocusHint] = useState('')
  // 错题强化：选中的错题引用（来源记录 + report.questions 下标）。由「错题强化官」卡片的错题本面板维护；
  // 复盘页的「重练此题 / 强化重练本场错题」也带着引用打开新建面板（真实复盘页入口会切回模拟侧）。
  const [drillSelection, setDrillSelection] = useState<InterviewDrillRef[]>([])
  // starting：点「开始出题」到场次建好之间的一次性闸门，防连点重场，也让按钮立刻进加载态。
  const [starting, setStarting] = useState(false)
  // 真实复盘：侧栏顶部分段切换 模拟/真实；逐字稿列表与选中 id 挂在本层，
  // 导入面板/标注核对/复盘报告的工作态由 RealWorkspace 自管（自带流与编辑副本）。
  const [side, setSide] = useState<'mock' | 'real'>('mock')
  const [transcripts, setTranscripts] = useState<TranscriptSummary[]>([])
  const [selectedTranscript, setSelectedTranscript] = useState('')
  const streamRef = useRef<EventSource | null>(null)
  const selectedRef = useRef('')
  selectedRef.current = selected
  const threadRef = useRef<HTMLDivElement>(null)

  const refreshSessions = useCallback(async () => {
    try { setSessions((await listInterviewSessions()).sessions) } catch { /* 列表失败不阻塞面试间 */ }
  }, [])

  const refreshTranscripts = useCallback(async () => {
    try { setTranscripts((await listInterviewTranscripts()).transcripts) } catch { /* 列表失败不阻塞工作区 */ }
  }, [])

  useEffect(() => { if (side === 'real') void refreshTranscripts() }, [side, refreshTranscripts])

  async function removeTranscript(id: string) {
    try {
      await deleteInterviewTranscript(id)
      if (selectedTranscript === id) setSelectedTranscript('')
      void refreshTranscripts()
    } catch (err) { setError((err as Error).message) }
  }

  const closeStream = useCallback(() => { streamRef.current?.close(); streamRef.current = null; setStream(null); setRunProgress(null); setReportFeed(null) }, [])

  // 待首装场次的 id：新建后先拉 planning 态场次当等待区；若 SSE done 抢先换装了题单，
  // 迟到的 GET 结果不得拿旧 planning 态把就绪场覆盖回去（否则等待区永远卡住）。
  const planningRef = useRef('')

  const loadSession = useCallback(async (id: string) => {
    try {
      const detail = await getInterviewSession(id)
      planningRef.current = ''
      setSession(detail.session)
      if (detail.session.conversation_id) {
        const data = await getInterviewMessages(id)
        if (selectedRef.current === id) setMessages(data.messages)
      } else if (selectedRef.current === id) {
        setMessages([])
      }
      // 后台仍在跑（切走再回来）：重连事件流续播，无需用户操作。
      if (detail.active_run && !detail.active_run.done && streamRef.current === null) {
        attach(detail.active_run.run_id)
      }
      return detail.session
    } catch (err) { setError((err as Error).message); return null }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const attach = useCallback((runId: string) => {
    closeStream()
    setStream({ runId, answer: '' })
    const source = openInterviewStream(runId, (event, data) => {
      if (event === 'delta') {
        setStream(current => current && current.runId === runId ? { ...current, answer: current.answer + String(data.text ?? '') } : current)
        return
      }
      if (event === 'progress') {
        // 复盘渐进事件三段：start 铺骨架 → review 原位填充 → summary 汇总中。
        // 事件流重连会从 0 重放，按题号折叠天然幂等。
        if (data.stage === 'start' && Array.isArray(data.items)) {
          setReportFeed({ items: (data.items as Record<string, unknown>[]).map(item => ({
            index: Number(item.index ?? 0), topic: String(item.topic ?? ''), question: String(item.question ?? ''),
            // 错题强化场次：骨架卡先带上次失分点，点评填入后即可对照（重放幂等：整表重建）。
            previous_problems: Array.isArray(item.previous_problems) ? (item.previous_problems as string[]) : undefined,
          })), done: 0, total: Number(data.total ?? (data.items as unknown[]).length), stage: 'review' })
        } else if (data.stage === 'review' && data.index != null) {
          const index = Number(data.index)
          setReportFeed(current => current ? {
            ...current, done: Number(data.done ?? current.done), stage: 'review',
            items: current.items.map(item => item.index === index
              ? { ...item, review: data.review as ReportFeedItem['review'] } : item),
          } : current)
        } else if (data.stage === 'summary') {
          setReportFeed(current => current ? { ...current, stage: 'summary' } : current)
        }
        setRunProgress({ stage: data.stage === 'summary' ? 'summary' : 'review',
                         done: Number(data.done ?? 0), total: Number(data.total ?? 0), topic: data.topic ? String(data.topic) : undefined })
        return
      }
      if (event === 'done') {
        closeStream()
        planningRef.current = ''
        const next = data.session as InterviewSession | undefined
        if (selectedRef.current && next && next.id === selectedRef.current) {
          setSession(next)
          void getInterviewMessages(next.id).then(item => { if (selectedRef.current === next.id) setMessages(item.messages) }).catch(() => {})
        } else if (next) {
          setSelected(next.id); selectedRef.current = next.id; setSession(next)
          void getInterviewMessages(next.id).then(item => { if (selectedRef.current === next.id) setMessages(item.messages) }).catch(() => {})
        }
        if (next?.status === 'done') setTab('report')
        void refreshSessions()
        return
      }
      if (event === 'error') {
        closeStream(); setError(String(data.message ?? '生成失败'))
        if (selectedRef.current) void loadSession(selectedRef.current)
        void refreshSessions()
        return
      }
      if (event === 'cancelled') {
        closeStream(); setError('已停止本轮生成')
        if (selectedRef.current) void loadSession(selectedRef.current)
        void refreshSessions()
        return
      }
      if (event === 'end') {
        // done 未达而流终止（服务重启等）：以服务端状态为准解锁。
        closeStream()
        if (selectedRef.current) void loadSession(selectedRef.current)
      }
    })
    source.onerror = () => { /* 短暂网络故障由 EventSource 自动重连续播事件日志 */ }
    streamRef.current = source
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [closeStream, refreshSessions])

  useEffect(() => {
    void listInterviewPersonas().then(data => { setPersonas(data.cards); setStyles(data.styles) }).catch(err => setError((err as Error).message))
    void refreshSessions()
    return closeStream
  }, [refreshSessions, closeStream])

  // 岗位详情/评估缺口/备战计划深链：打开新建面板并预选岗位；focus 是备战专项，注入出题 weak_focus。
  // drill（失败博物馆/备战计划任务卡的「错题强化」直达）进「错题强化官」并预选错题——数组字段
  // 存在即算深链，空数组=这场没有可预选的错题，让用户在错题本里自选。
  // 三者全空的深链不算深链（普通刷新后可能收到），忽略之，避免误开新建面板挡住面试间。
  useEffect(() => {
    if (!deepLink) return
    const drills = deepLink.drill
    if (!deepLink.jobId && !deepLink.focus && !drills) return
    setCreating(true); setMode(deepLink.jobId ? 'job' : 'free')
    setJobId(deepLink.jobId); setJobLabel('')
    if (drills) {
      setFocusHint(''); setSide('mock'); setCard('reinforce'); setRound('business'); setError('')
      setDrillSelection(drills.slice(0, MAX_DRILLS))
    } else {
      setFocusHint(deepLink.focus || ''); setCard('')
    }
    if (!deepLink.jobId) return
    void api<{ job: { title: string; company: string } }>(`/jobs/${encodeURIComponent(deepLink.jobId)}`)
      .then(data => setJobLabel(`${data.job.company} · ${data.job.title}`))
      .catch(() => setJobLabel(''))
  }, [deepLink])

  // 流式期间贴底：新 delta 与新消息都把面试间滚到最下。
  useEffect(() => { threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight }) }, [messages, stream?.answer])

  const selectSession = useCallback((id: string) => {
    closeStream(); setError(''); setAnswer(''); setTab('room')
    setSelected(id); selectedRef.current = id; setSession(null); setMessages([])
    void loadSession(id)
  }, [closeStream, loadSession])

  async function startSession() {
    if (starting || busy) return
    setError('')
    if (!card) { setError('请先选择面试官岗位'); return }
    const targetJobId = mode === 'job' ? jobId : ''
    if (mode === 'job' && !targetJobId) { setError('请从岗位库选择一个岗位'); return }
    if (card === 'custom' && mode === 'free' && !jobTitle.trim()) { setError('自定义岗位需要先填写目标岗位名称'); return }
    if (card === 'reinforce' && !drillSelection.length) { setError('请先选择要重练的错题'); return }
    setStarting(true)
    try {
      const started = await createInterviewSession({
        persona_id: card, company_style: style, round, intensity, question_count: questionCount,
        ...(targetJobId ? { job_id: targetJobId } : {}),
        ...(mode === 'jd' ? { jd_text: jdText.trim(), company: companyName.trim(), ...(jobTitle.trim() ? { job_title: jobTitle.trim() } : {}) } : {}),
        ...(mode === 'free' ? { company: companyName.trim(), ...(card === 'custom' && jobTitle.trim() ? { job_title: jobTitle.trim() } : {}) } : {}),
        ...(focusHint ? { focus: focusHint } : {}),
        // 错题强化：每道错题出一道变式题，题量随错题数（question_count 由服务端覆盖）。
        ...(card === 'reinforce' ? { drill: drillSelection } : {}),
      })
      setCreating(false); setJdText(''); setJobTitle(''); setCompanyName(''); setFocusHint(''); setDrillSelection([])
      setTab('room')
      setSelected(started.session_id); selectedRef.current = started.session_id
      setSession(null); setMessages([])
      attach(started.run_id)
      // 立刻把 planning 态场次拉进面试间当等待区（出题在后台跑）：不再让表单原地干等 1-3 分钟。
      planningRef.current = started.session_id
      try {
        const detail = await getInterviewSession(started.session_id)
        if (selectedRef.current === started.session_id && planningRef.current === started.session_id) setSession(detail.session)
      } catch { /* 拉取失败不打断：SSE done 事件兜底换装 */ }
      void refreshSessions()
    } catch (err) { setError((err as Error).message) }
    finally { setStarting(false) }
  }

  async function send() {
    if (!session || streamRef.current) return
    const content = answer.trim()
    if (session.status === 'ready' ? false : !content) return
    const optimistic = content
    setAnswer('')
    if (optimistic) setMessages(current => [...current, { id: `tmp-${Date.now()}`, ts: '', role: 'user', content: optimistic }])
    try {
      const started = await sendInterviewTurn(session.id, optimistic)
      attach(started.run_id)
    } catch (err) {
      setError((err as Error).message)
      if (optimistic) setMessages(current => current.filter(item => !item.id.startsWith('tmp-')))
    }
  }

  async function finish() {
    if (!session || streamRef.current) return
    try {
      const started = await finishInterview(session.id)
      // 乐观置 reporting：finish 只返回 run_id，不置的话客户端 session 停在 ready/done，
      // 渐进复盘视图永远不亮（done 事件会用服务端权威状态覆盖）。
      setSession(current => current ? { ...current, status: 'reporting' } : current)
      attach(started.run_id)
      // 点结束即进复盘页签看渐进生成（骨架→逐题填充→汇总→终版），不在面试间干等进度条。
      setTab('report')
    } catch (err) { setError((err as Error).message) }
  }

  async function stop() {
    if (!stream) return
    try { await cancelInterviewRun(stream.runId) } catch { /* 等服务端 cancelled 事件兜底 */ }
  }

  async function removeSession(id: string) {
    try {
      await deleteInterviewSession(id)
      if (selectedRef.current === id) { closeStream(); setSelected(''); selectedRef.current = ''; setSession(null); setMessages([]) }
      void refreshSessions()
    } catch (err) { setError((err as Error).message) }
  }

  // ---- 错题强化：从复盘页带着错题打开「错题强化官」的新建面板（真实复盘页入口会切回模拟侧） ----
  function openDrill(refs: InterviewDrillRef[], job?: { id: string; label: string }) {
    if (!refs.length) return
    setError(''); setFocusHint('')
    setSide('mock'); setCreating(true); setCard('reinforce'); setRound('business')
    setDrillSelection(refs.slice(0, MAX_DRILLS))
    // 来源岗位直接带上：同一岗位的 JD 再问一遍，变式题才贴岗；没有岗位锚点则留自由练。
    if (job?.id) { setMode('job'); setJobId(job.id); setJobLabel(job.label) }
    else { setMode('free'); setJobId(''); setJobLabel('') }
  }

  function defaultDrillSelection(book: InterviewWeakPoints) {
    // 直接点卡片进来（没带预设）：默认勾最近一场复盘的错题，优先还没补齐的——读完复盘顺手就能练。
    const first = book.items[0]
    if (!first) return []
    const group = book.items.filter(item => item.source === first.source && item.session_id === first.session_id)
    const pending = group.filter(item => item.practice?.after !== 0)
    return (pending.length ? pending : group).flatMap(item => item.refs.slice(0, 1)).slice(0, MAX_DRILLS)
  }

  // 场次复盘里一道题的错题引用。错题强化场次的题带 drill_ref（它重练的原错题）：再练一轮练的是
  // 原题——强化场自己的变式题不进错题本，指过来会被服务端判为失效引用。
  function drillTargetOf(session: InterviewSession, item: InterviewReview | undefined, index: number): InterviewDrillRef {
    return item?.drill_ref ?? { source: 'mock', session_id: session.id, index }
  }

  const plan = session?.question_plan
  const progress = session?.progress
  const askedCount = progress ? Math.min(progress.current_index + 1, plan?.questions.length ?? 0) : 0
  const busy = streamRef.current !== null
  const report = session?.report
  // 错题强化入口只在后端支持时出现：dist 重建后前端先于后端生效（8765 后端要重启才换代码），
  // 旧后端没有「错题强化官」，露出按钮只会点了报错。
  const drillReady = personas.some(item => item.id === 'reinforce')
  // 场次记录导出（题单+对话实录+复盘+归因标注）：组装 Markdown，不经过后端。
  const exportable = Boolean(session && (session.question_plan || messages.length))

  async function copyRecord() {
    if (!session) return
    const bundle = await loadAttributionBundle(session.id)
    const ok = await copyText(buildInterviewMarkdown(session, messages, bundle))
    toast(ok ? '面试记录已复制到剪贴板' : '复制失败，可改用「导出MD」', ok ? 'notice' : 'error')
  }

  async function downloadRecord() {
    if (!session) return
    const bundle = await loadAttributionBundle(session.id)
    const url = URL.createObjectURL(new Blob([buildInterviewMarkdown(session, messages, bundle)], { type: 'text/markdown;charset=utf-8' }))
    const link = document.createElement('a')
    link.href = url; link.download = interviewFileName(session); link.click()
    URL.revokeObjectURL(url)
  }

  return <div className="interview-page">
    <aside className="interview-side">
      <div className="iv-side-modes" role="tablist" aria-label="面试模式">
        <button role="tab" aria-selected={side === 'mock'} className={side === 'mock' ? 'active' : ''} onClick={() => setSide('mock')}>模拟面试</button>
        <button role="tab" aria-selected={side === 'real'} className={side === 'real' ? 'active' : ''} onClick={() => setSide('real')}>真实复盘</button>
      </div>
      {side === 'mock' && <>
        <div className="interview-side-head">
          <span>面试场次</span>
          <button className="button primary small" onClick={() => { setCreating(value => !value); setCard(''); setError('') }}><Icon name="plus" size={14} />新建</button>
        </div>
        <div className="interview-session-list">
          {sessions.length === 0 && <p className="muted body-small">还没有场次。选一位面试官开一场。</p>}
          {sessions.map(item => <div key={item.id} className={`interview-session-item${item.id === selected ? ' active' : ''}`} onClick={() => selectSession(item.id)}>
            <strong>{item.title}</strong>
            <span className="tiny-label">{item.persona_name || '自定义'} · {item.round_label}</span>
            <span className={`iv-status ${item.status}`}>{STATUS_NAMES[item.status] || item.status}{item.has_report ? ' · 有复盘' : ''}</span>
            <button className="iv-session-delete" aria-label="删除场次" title="删除场次" onClick={event => { event.stopPropagation(); void removeSession(item.id) }}><Icon name="close" size={13} /></button>
          </div>)}
        </div>
      </>}
      {side === 'real' && <>
        <div className="interview-side-head">
          <span>逐字稿</span>
          <button className="button primary small" onClick={() => { setSelectedTranscript(''); setError('') }}><Icon name="plus" size={14} />导入逐字稿</button>
        </div>
        <div className="interview-session-list">
          {transcripts.length === 0 && <p className="muted body-small">还没有逐字稿。把外部 ASR 转写的面试文本粘进来开始复盘。</p>}
          {transcripts.map(item => <div key={item.id} className={`interview-session-item${item.id === selectedTranscript ? ' active' : ''}`} onClick={() => { setSelectedTranscript(item.id); setError('') }}>
            <strong>{item.job_title || '未命名岗位'}</strong>
            <span className="tiny-label">{[item.company, date(item.created_at)].filter(Boolean).join(' · ')}</span>
            <span className={`iv-status ${item.status}`}>{TRANSCRIPT_STATUS_NAMES[item.status] || item.status}{item.has_report ? ' · 有报告' : ''}</span>
            <button className="iv-session-delete" aria-label="删除逐字稿" title="删除逐字稿" onClick={event => { event.stopPropagation(); void removeTranscript(item.id) }}><Icon name="close" size={13} /></button>
          </div>)}
        </div>
      </>}
    </aside>
    <div className="interview-main">
      {error && <div className="iv-error" role="alert"><span>{error}</span><button className="iv-error-close" onClick={() => setError('')} aria-label="关闭提示"><Icon name="close" size={14} /></button></div>}
      {side === 'real' ? <RealWorkspace transcriptId={selectedTranscript} modelConfigured={modelConfigured} onConfigureModel={onConfigureModel}
          onCreated={setSelectedTranscript} onChanged={refreshTranscripts} onDrill={drillReady ? openDrill : undefined} />
      : <>
      {focusHint && <div className="iv-focus-banner" role="status">
        <Icon name="target" size={16} />
        <span>备战计划专项：<b>{focusHint}</b>——本场出题会定向覆盖这个主题。</span>
        <button className="text-button" onClick={() => setFocusHint('')}>移除</button>
      </div>}
      {(!session || creating) && <CreatePanel
        cards={personas} styles={styles} card={card}
        onCard={value => { setCard(value); const preset = personas.find(item => item.id === value); setRound(preset?.default_round ?? '') }}
        style={style} onStyle={setStyle}
        round={round} onRound={setRound} intensity={intensity} onIntensity={setIntensity}
        questionCount={questionCount} onQuestionCount={setQuestionCount}
        mode={mode} onMode={setMode} jobId={jobId} jobLabel={jobLabel}
        onPickJob={(id, label) => { setJobId(id); setJobLabel(label) }}
        jobTitle={jobTitle} onJobTitle={setJobTitle} companyName={companyName} onCompanyName={setCompanyName}
        jdText={jdText} onJdText={setJdText}
        modelConfigured={modelConfigured} onConfigureModel={onConfigureModel}
        drillSelection={drillSelection}
        onDrillSelection={setDrillSelection}
        onDrillLoaded={book => { if (!drillSelection.length) setDrillSelection(defaultDrillSelection(book)) }}
        onUseJob={(id, label) => { setMode('job'); setJobId(id); setJobLabel(label) }}
        busy={busy || starting} onStart={() => void startSession()} starting={starting} />}
      {session && !creating && <>
        <div className="iv-subtab-row">
          <div className="interview-subtabs" role="tablist" aria-label="面试分区">
            <button role="tab" aria-selected={tab === 'room'} className={tab === 'room' ? 'active' : ''} onClick={() => setTab('room')}>面试间</button>
            <button role="tab" aria-selected={tab === 'report'} className={tab === 'report' ? 'active' : ''} onClick={() => setTab('report')} disabled={!report}>复盘报告{report ? '' : '（结束后生成）'}</button>
          </div>
          <div className="iv-subtabs-actions">
            <button className="button secondary small" disabled={!exportable} onClick={() => void copyRecord()}
              title="复制完整面试记录（题单+对话实录+复盘）为 Markdown，可直接粘贴给其他 AI 分析">复制记录</button>
            <button className="button secondary small" disabled={!exportable} onClick={downloadRecord}
              title="下载完整面试记录的 .md 文件">导出MD</button>
          </div>
        </div>
        {tab === 'room' ? <RoomPanel
          session={session} plan={plan ?? null} progress={progress ?? { phase: 'opening', current_index: 0, rounds: 0, segments: [] }} askedCount={askedCount} messages={messages}
          streamAnswer={stream?.answer ?? ''} busy={busy} answer={answer} onAnswer={setAnswer}
          onSend={() => void send()} onFinish={() => void finish()} onStop={() => void stop()} threadRef={threadRef}
          reportProgress={runProgress} />
        : session.status === 'reporting'
          ? <ProgressiveReport feed={reportFeed} />
          : <><ReportPanel report={report ?? null} persona={session.persona}
                onRegenerate={() => void finish()} busy={busy}
                onDrillQuestion={drillReady ? index => openDrill([drillTargetOf(session, report?.questions[index], index)],
                  { id: session.job_id, label: [session.company, session.job_title].filter(Boolean).join(' · ') }) : undefined}
                onDrillWeak={drillReady ? () => openDrill(
                  (session.report?.questions ?? []).map((item, index) => (item.missing.length ? drillTargetOf(session, item, index) : null))
                    .filter((ref): ref is InterviewDrillRef => ref !== null),
                  { id: session.job_id, label: [session.company, session.job_title].filter(Boolean).join(' · ') }) : undefined} />
            {/* 失败博物馆归因标注：复盘报告的自然延伸（可控/不可控分流，跨场报告在这里取数） */}
            <AttributionPanel source="mock" sessionId={session.id}
              signals={(progress?.segments ?? []).flatMap(item => item.signals ?? [])} /></>}
      </>}
      </>}
    </div>
  </div>
}

function CreatePanel({ cards, styles, card, onCard, style, onStyle, round, onRound, intensity, onIntensity, questionCount, onQuestionCount, mode, onMode, jobId, jobLabel, onPickJob, jobTitle, onJobTitle, companyName, onCompanyName, jdText, onJdText, modelConfigured, onConfigureModel, drillSelection, onDrillSelection, onDrillLoaded, onUseJob, busy, starting, onStart }: {
  cards: InterviewPersonaCard[]; styles: InterviewStyle[]; card: string; onCard: (value: string) => void
  style: string; onStyle: (value: string) => void
  round: string; onRound: (value: string) => void
  intensity: 'gentle' | 'standard' | 'stress'; onIntensity: (value: 'gentle' | 'standard' | 'stress') => void
  questionCount: number; onQuestionCount: (value: number) => void
  mode: 'job' | 'jd' | 'free'; onMode: (value: 'job' | 'jd' | 'free') => void
  jobId: string; jobLabel: string; onPickJob: (id: string, label: string) => void
  jobTitle: string; onJobTitle: (value: string) => void
  companyName: string; onCompanyName: (value: string) => void
  jdText: string; onJdText: (value: string) => void
  modelConfigured: boolean; onConfigureModel: () => void
  // 错题强化（错题强化官卡片）：选中的错题引用、选题面板加载完的回调、一键带上错题来源岗位。
  drillSelection: InterviewDrillRef[]; onDrillSelection: (next: InterviewDrillRef[]) => void
  onDrillLoaded: (book: InterviewWeakPoints) => void; onUseJob: (id: string, label: string) => void
  busy: boolean; starting: boolean; onStart: () => void
}) {
  const active = cards.find(item => item.id === card)
  const customNeedsTitle = card === 'custom' && mode === 'free' && !jobTitle.trim()
  // 错题强化：题量随选中的错题数（每道一道变式题），至少选一道才能开面。
  const drilling = card === 'reinforce'
  const startDisabled = !card || busy || !modelConfigured || customNeedsTitle || (mode === 'job' && !jobId) || (drilling && !drillSelection.length)
  return <section className="interview-create">
    <h2 className="sr-only">新建模拟面试</h2>
    {!modelConfigured && <div className="iv-model-hint">模拟面试需要先绑定分析模型。<button className="inline-button" onClick={onConfigureModel}>去设置中心</button></div>}
    <div className="iv-cards">
      {cards.map(item => <button key={item.id} className={`iv-card${item.id === card ? ' active' : ''}`} onClick={() => onCard(item.id)}>
        <strong>{item.name}</strong>
        <p>{item.blurb}</p>
        <span className="iv-card-tags">{item.tags.map(tag => <em key={tag}>{tag}</em>)}</span>
      </button>)}
    </div>
    {active && <div className="iv-config">
      <label>公司风格<select value={style} onChange={event => onStyle(event.target.value)} aria-label="公司面试风格">
        {styles.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select></label>
      <label>轮次<select value={round} onChange={event => onRound(event.target.value)} aria-label="面试轮次">
        {active.rounds.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select></label>
      <label>强度<select value={intensity} onChange={event => onIntensity(event.target.value as 'gentle' | 'standard' | 'stress')} aria-label="面试强度">
        {(['gentle', 'standard', 'stress'] as const).map(value => <option key={value} value={value}>{INTENSITY_NAMES[value]}</option>)}
      </select></label>
      {drilling
        ? <span className="tiny-label iv-drill-count" role="status">题量 = 已选 <b>{drillSelection.length}</b> 道错题，每道出一道变式题</span>
        : <label>题量<select value={questionCount} onChange={event => onQuestionCount(Number(event.target.value))} aria-label="题量">
          {[6, 8, 10].map(value => <option key={value} value={value}>{value} 题</option>)}
        </select></label>}
    </div>}
    {drilling && <DrillPicker selection={drillSelection} onSelection={onDrillSelection} onLoaded={onDrillLoaded}
      jobId={jobId} onUseJob={onUseJob} />}
    <div className="iv-modes" role="tablist" aria-label="目标岗位来源">
      {([['job', '从岗位库选'], ['jd', '粘贴 JD'], ['free', '自由练']] as const).map(([value, label]) =>
        <button key={value} className={mode === value ? 'active' : ''} onClick={() => onMode(value)}>{label}</button>)}
    </div>
    <div className="iv-target">
      {mode === 'job' && <>
        <JobPicker value={jobId} label={jobLabel} onSelect={onPickJob} />
        {jobId && <span className="tiny-label">将自动带上该岗位的完整 JD、评估缺口、简历存档与公司资料库情报</span>}
      </>}
      {mode === 'jd' && <>
        <textarea className="iv-jd" value={jdText} onChange={event => onJdText(event.target.value)} rows={4} maxLength={20000}
          placeholder="粘贴完整的职位描述（职责 + 任职要求），出题与追问都以它为准" />
        <div className="iv-target-row">
          <input className="iv-mini-input" value={jobTitle} onChange={event => onJobTitle(event.target.value)} maxLength={120} placeholder="岗位名称（可选，如：数据分析师）" aria-label="岗位名称" />
          <input className="iv-mini-input" value={companyName} onChange={event => onCompanyName(event.target.value)} maxLength={120} placeholder="公司名称（可选，用于带上公司资料）" aria-label="公司名称" />
        </div>
      </>}
      {mode === 'free' && <div className="iv-target-row">
        {card === 'custom' && <input className="iv-mini-input" value={jobTitle} onChange={event => onJobTitle(event.target.value)} maxLength={120}
          placeholder="目标岗位名称（必填，如：数据分析师）" aria-label="目标岗位名称" />}
        <input className="iv-mini-input" value={companyName} onChange={event => onCompanyName(event.target.value)} maxLength={120} placeholder="公司名称（可选，用于带上公司资料）" aria-label="公司名称" />
      </div>}
    </div>
    <div className="iv-create-actions">
      <button className="button primary" disabled={startDisabled} onClick={onStart}>{starting ? <span className="spinner" /> : <Icon name="mic" size={16} />}开始出题</button>
      {customNeedsTitle ? <span className="tiny-label iv-warn">先填目标岗位名称，面试官才知道面什么</span>
        : drilling ? <span className="tiny-label">{drillSelection.length
          ? '出题约需 1-2 分钟：每道错题换问法出一道变式题，面试中不提示，复盘时逐题对照上次'
          : '先从下面的错题本勾选要重练的题'}</span>
        : <span className="tiny-label">出题约需 1-3 分钟（岗位 JD + 评估缺口 + 简历 + 公司资料 四源合一），完成后进入面试间</span>}
    </div>
  </section>
}

// ---- 错题本选题面板（错题强化官专用，2026-11-24）----
// 既往复盘（模拟 + 真实）里被点了失分点的题，按来源场次分组。列表由后端规则统计秒出；
// 勾选状态在外层（建场入参就是这串引用），这里只负责展示与切换。
const DRILL_SOURCE_NAMES: Record<string, string> = { mock: '模拟面试', real: '真实复盘' }
const refKey = (ref: InterviewDrillRef) => `${ref.source}|${ref.session_id}|${ref.index}`

type DrillGroup = { key: string; source: string; label: string; at: string; items: InterviewWeakPoint[] }

function groupWeakPoints(items: InterviewWeakPoint[]): DrillGroup[] {
  // items 已按最近出现排好序：首次出现的顺序 = 最近场次在前
  const groups = new Map<string, DrillGroup>()
  for (const item of items) {
    const key = `${item.source}|${item.session_id}`
    if (!groups.has(key)) groups.set(key, {
      key, source: item.source, at: item.last_at, items: [],
      label: item.label || `${DRILL_SOURCE_NAMES[item.source]}（未挂岗位）`,
    })
    groups.get(key)!.items.push(item)
  }
  return [...groups.values()]
}

function DrillPicker({ selection, onSelection, onLoaded, jobId, onUseJob }: {
  selection: InterviewDrillRef[]; onSelection: (next: InterviewDrillRef[]) => void
  onLoaded: (book: InterviewWeakPoints) => void
  jobId: string; onUseJob: (id: string, label: string) => void
}) {
  const [book, setBook] = useState<InterviewWeakPoints | null>(null)
  const [error, setError] = useState('')
  const [filter, setFilter] = useState('')

  useEffect(() => {
    let alive = true
    listInterviewWeakPoints().then(data => { if (alive) { setBook(data); onLoaded(data) } })
      .catch(err => { if (alive) setError((err as Error).message) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const items = book?.items ?? []
  const selected = new Set(selection.map(refKey))
  const isSelected = (item: InterviewWeakPoint) => item.refs.some(ref => selected.has(refKey(ref)))
  const visible = filter ? items.filter(item => item.tags.includes(filter)) : items
  const groups = groupWeakPoints(visible)
  const listed = new Set(items.flatMap(item => item.refs.map(refKey)))
  // 带进来的引用指向没被点过失分点的题（「重练此题」）：不在错题本里，单独计数说明。
  const extra = selection.filter(ref => !listed.has(refKey(ref))).length
  const full = selection.length >= MAX_DRILLS
  const tagLabels = Object.fromEntries((book?.tags ?? []).map(tag => [tag.id, tag.label]))

  const toggleItem = (item: InterviewWeakPoint) => {
    const keys = item.refs.map(refKey)
    if (isSelected(item)) onSelection(selection.filter(ref => !keys.includes(refKey(ref))))
    else if (!full) onSelection([...selection, item.refs[0]])
  }
  const toggleGroup = (group: DrillGroup) => {
    const keys = group.items.flatMap(item => item.refs.map(refKey))
    if (group.items.every(isSelected)) onSelection(selection.filter(ref => !keys.includes(refKey(ref))))
    else onSelection([...selection, ...group.items.filter(item => !isSelected(item)).map(item => item.refs[0])].slice(0, MAX_DRILLS))
  }
  // 全选/清空只作用于当前筛出来的题：被筛掉的分组里已勾的保留。
  const visibleKeys = new Set(visible.flatMap(item => item.refs.map(refKey)))
  const selectVisible = () => onSelection([...selection, ...visible.filter(item => !isSelected(item)).map(item => item.refs[0])].slice(0, MAX_DRILLS))
  const clearVisible = () => onSelection(selection.filter(ref => !visibleKeys.has(refKey(ref))))

  // 选中的错题都来自同一个岗位而目标还没选：一键带上来源岗位，变式题才贴着原 JD。
  const chosen = items.filter(isSelected)
  const commonJob = chosen.length && chosen.every(item => item.job_id === chosen[0].job_id) ? chosen[0] : null
  const sessionCount = new Set(items.map(item => `${item.source}|${item.session_id}`)).size

  return <section className="iv-drill" aria-label="错题本">
    <div className="iv-drill-head">
      <strong>错题本</strong>
      {book && <span className="tiny-label">共 {book.total} 题 · 来自 {sessionCount} 份复盘{book.limitations.length ? `（${book.limitations.join('；')}）` : ''}</span>}
      <span className="iv-progress iv-drill-selected" role="status">已选 {selection.length}/{MAX_DRILLS}</span>
      {items.length > 0 && <span className="iv-drill-bulk">
        <button className="text-button" onClick={selectVisible}>全选</button>
        <button className="text-button" onClick={clearVisible} disabled={!visible.some(isSelected)}>清空</button>
      </span>}
    </div>
    {error && <div className="iv-error" role="alert"><span>{error}</span><button className="iv-error-close" onClick={() => setError('')} aria-label="关闭提示"><Icon name="close" size={14} /></button></div>}
    {book && book.patterns.length > 0 && <div className="iv-drill-filters" role="group" aria-label="按失分要素筛选">
      <span className="tiny-label">失分要素</span>
      <button className={!filter ? 'active' : ''} onClick={() => setFilter('')}>全部</button>
      {book.patterns.map(item => <button key={item.tag} className={filter === item.tag ? 'active' : ''}
        title={`在 ${item.count} 道错题里被点到的失分要素`} onClick={() => setFilter(filter === item.tag ? '' : item.tag)}>{item.label} {item.count}</button>)}
    </div>}
    {!jobId && commonJob?.job_id && <button className="iv-drill-job" onClick={() => onUseJob(commonJob.job_id, [commonJob.company, commonJob.job_title].filter(Boolean).join(' · '))}
      title="带上来源岗位的 JD、评估缺口与简历存档出变式题"><Icon name="target" size={14} />选中的错题都来自「{[commonJob.company, commonJob.job_title].filter(Boolean).join(' · ')}」，带上它的 JD 重练</button>}
    <div className="iv-drill-list">
      {!book && !error && <div className="iv-state">正在整理错题本…<span className="spinner" /></div>}
      {book && items.length === 0 && <div className="iv-drill-empty">还没有可强化的错题。完成一场模拟面试，或在「真实复盘」导入逐字稿并生成报告后，答得不好的题会自动收进这里。</div>}
      {book && items.length > 0 && groups.length === 0 && <div className="iv-drill-empty">没有命中这项失分要素的错题。</div>}
      {groups.map(group => <div key={group.key} className="iv-drill-group">
        <label className="iv-drill-group-head">
          <input type="checkbox" checked={group.items.every(isSelected)} onChange={() => toggleGroup(group)} aria-label={`选择「${group.label}」的全部错题`} />
          <strong>{group.label}</strong>
          <span className="tiny-label">{DRILL_SOURCE_NAMES[group.source]} · {date(group.at)} · {group.items.length} 题</span>
        </label>
        {group.items.map(item => <label key={item.id} className={`iv-drill-item${isSelected(item) ? ' selected' : ''}`}>
          <input type="checkbox" checked={isSelected(item)} disabled={!isSelected(item) && full} onChange={() => toggleItem(item)}
            aria-label={`重练：${item.question}`} />
          <span className="iv-drill-body">
            <span className="iv-drill-q">
              <span className="iv-drill-text">{item.question}</span>
              {item.occurrences > 1 && <em className="iv-drill-badge repeat" title="同一道题在多份复盘里都被点了失分点">{item.occurrences} 份复盘都出现</em>}
              {item.practice && <em className={`iv-drill-badge ${item.practice.after === 0 ? 'cleared' : 'again'}`}
                title={`最近一次重练（${date(item.practice.at)}）：缺失要素 上次 ${item.practice.before} 条 → 本次 ${item.practice.after} 条${item.practice.count > 1 ? `，共重练 ${item.practice.count} 次` : ''}`}>
                {item.practice.after === 0 ? `重练过 · 本次没再被点出缺失` : `重练过 · 缺失要素 ${item.practice.before}→${item.practice.after}`}</em>}
            </span>
            <span className="iv-drill-problems" title={item.problems.join('；')}>{item.problems.length
              ? <>上次失分：{item.problems.slice(0, 2).join('；')}{item.problems.length > 2 ? ` 等 ${item.problems.length} 条` : ''}</>
              : '这道题没有被点出失分点，按原题考察点出变式题'}</span>
            {item.tags.length > 0 && <span className="iv-drill-tags">{item.tags.slice(0, 3).map(tagId =>
              <i key={tagId}>{tagLabels[tagId] ?? tagId}</i>)}</span>}
          </span>
        </label>)}
      </div>)}
      {extra > 0 && <p className="tiny-label iv-drill-extra">另有 {extra} 道题不在错题本里（没被点过失分点），已按「重练此题」带入。</p>}
    </div>
  </section>
}

// 岗位检索下拉（combobox）：输入走服务端 /jobs?q= 实时检索（title/company/description 子串匹配），
// 空查询给最近更新的 20 条；岗位多时靠关键词收窄，不靠翻页。
function JobPicker({ value, label, onSelect }: { value: string; label: string; onSelect: (id: string, label: string) => void }) {
  const [query, setQuery] = useState('')
  const [focused, setFocused] = useState(false)
  const [results, setResults] = useState<{ id: string; label: string; meta: string }[]>([])
  const [loading, setLoading] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const tokenRef = useRef(0)

  useEffect(() => {
    const timer = setTimeout(() => {
      const ticket = ++tokenRef.current
      const params = new URLSearchParams({ page: '1', page_size: '20' })
      if (query.trim()) params.set('q', query.trim())
      setLoading(true)
      api<{ jobs: { id: string; title: string; company: string; salary?: string; location?: string }[] }>(`/jobs?${params.toString()}`)
        .then(data => {
          if (tokenRef.current !== ticket) return
          setResults(data.jobs.map(item => ({
            id: item.id, label: `${item.company} · ${item.title}`,
            meta: [item.salary, item.location].filter(Boolean).join(' ｜ '),
          })))
        })
        .catch(() => { if (tokenRef.current === ticket) setResults([]) })
        .finally(() => { if (tokenRef.current === ticket) setLoading(false) })
    }, 250)
    return () => clearTimeout(timer)
  }, [query])

  // 点外面才收下拉：列表项用 onMouseDown preventDefault，避免先失焦把选中吞掉。
  useEffect(() => {
    function onDocMouseDown(event: MouseEvent) {
      if (boxRef.current && !boxRef.current.contains(event.target as Node)) setFocused(false)
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [])

  return <div className="iv-picker" ref={boxRef}>
    <input className="iv-picker-input" value={focused ? query : label}
      placeholder="输入公司或岗位关键词检索（如：阿里 数据），空输入给最近岗位"
      onFocus={() => { setFocused(true); setQuery('') }}
      onKeyDown={event => { if (event.key === 'Escape') setFocused(false) }}
      onChange={event => setQuery(event.target.value)}
      aria-label="检索目标岗位" />
    {focused && <div className="iv-picker-list" role="listbox" aria-label="岗位检索结果">
      {loading && results.length === 0 && <div className="iv-picker-empty">检索中…</div>}
      {!loading && results.length === 0 && <div className="iv-picker-empty">没有匹配的岗位——可切「粘贴 JD」或「自由练」</div>}
      {results.map(item => <button key={item.id} className={`iv-picker-item${item.id === value ? ' active' : ''}`} title={item.label}
        onMouseDown={event => event.preventDefault()}
        onClick={() => { onSelect(item.id, item.label); setQuery(''); setFocused(false) }}>
        <span>{item.label}</span>{item.meta && <em>{item.meta}</em>}
      </button>)}
    </div>}
  </div>
}

// 复盘进度行：首次生成与「重新生成」共用（SSE progress 实时刷新）。
function ReportStatusLine({ progress }: { progress: RunProgress }) {
  return <div className="iv-state">
    {progress?.stage === 'summary' ? <>逐题点评完成，正在汇总维度与高危清单…</>
      : progress ? <>逐题点评 {progress.done}/{progress.total}{progress.topic ? ` · ${progress.topic}` : ''}…</>
      : <>复盘生成中：逐题点评（含好差答案对比）→ 汇总维度与高危清单…</>}
    <span className="spinner" />
  </div>
}

// 出题等待区（2026-11-20）：点「开始出题」立刻跳进来接管主区——材料清单 + 真实计时，
// 表单不再原地干等。计时锚在场次 created_at 上，切场次再回来不归零。
function PlanningWait({ session }: { session: InterviewSession }) {
  const createdAt = new Date(session.created_at).getTime()
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const elapsed = Number.isFinite(createdAt) ? Math.max(0, Math.round((now - createdAt) / 1000)) : 0
  const elapsedLabel = elapsed >= 60 ? `${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒` : `${elapsed} 秒`
  const material = session.material ?? {}
  const sources = [
    material.drills?.length ? `错题 ${material.drills.length} 道` : '',
    material.jd_summary ? '岗位 JD' : '',
    material.gaps?.length ? '评估缺口' : '',
    material.resume_digest ? (material.resume_label ? `简历存档《${material.resume_label}》` : '简历') : '',
    material.company_brief ? '公司资料' : '',
  ].filter(Boolean)
  return <div className="iv-waiting">
    <div className="iv-waiting-head"><span className="spinner" />正在出题…已等待 {elapsedLabel}</div>
    {sources.length > 0 && <div className="iv-waiting-sources">出题材料已就位：{sources.map(item => <em key={item}>{item}</em>)}</div>}
    <p className="iv-waiting-note">{material.drills?.length
      ? `面试官正为 ${material.drills.length} 道错题各出一道变式题（换问法，防背答案），约 1-2 分钟。`
      : `岗位专家正按材料定制 ${session.config.question_count} 道题，约 1-3 分钟。`}留在本页出完自动进面试间；切去别的页面后，从左侧场次列表点开本场继续。</p>
  </div>
}

function RoomPanel({ session, plan, progress, askedCount, messages, streamAnswer, busy, answer, onAnswer, onSend, onFinish, onStop, threadRef, reportProgress }: {
  session: InterviewSession
  plan: InterviewSession['question_plan']
  progress: InterviewSession['progress']
  askedCount: number
  messages: InterviewMessage[]
  streamAnswer: string; busy: boolean
  answer: string; onAnswer: (value: string) => void
  onSend: () => void; onFinish: () => void; onStop: () => void
  threadRef: React.RefObject<HTMLDivElement | null>
  reportProgress: RunProgress
}) {
  const total = plan?.questions.length ?? 0
  const ready = session.status === 'ready'
  // 错题强化场次：题单里的 weakness 题标「错题重练」；失分点不进题单预览（面试中零提示，对照留给复盘）。
  const drilling = Boolean(session.material?.drills?.length)
  const sourceName = (item: { source: string }) => drilling && item.source === 'weakness' ? '错题重练' : SOURCE_NAMES[item.source] || '通用'
  return <div className="interview-room">
    <div className="interview-room-head">
      <div className="iv-room-meta">
        <strong>{session.persona.name}</strong>
        <span>{session.persona.round_label} · {session.persona.intensity_label}</span>
        {session.status === 'live' && total > 0 && <span className="iv-progress">Q{Math.min(askedCount, total)}/{total}</span>}
        <span className={`iv-status ${session.status}`}>{STATUS_NAMES[session.status] || session.status}</span>
      </div>
      <div className="iv-room-actions">
        {busy && <button className="button secondary small" onClick={onStop}>停止</button>}
        {(session.status === 'ready' || session.status === 'live') && <button className="button secondary small" disabled={busy} onClick={onFinish}><Icon name="check" size={14} />结束并生成复盘</button>}
      </div>
    </div>
    {session.status === 'planning' && <PlanningWait session={session} />}
    {session.status === 'failed' && <div className="iv-state">出题失败，删除本场后重新开一场即可。</div>}
    {session.status === 'reporting' && <ReportStatusLine progress={reportProgress} />}
    <div className="interview-thread" ref={threadRef}>
      {ready && plan && <div className="iv-plan">
        <p className="iv-plan-opening">{plan.opening}</p>
        <ol>{plan.questions.map((item, index) => <li key={index}>
          <span className="iv-topic">{item.topic}</span><span className="iv-source">{sourceName(item)}</span>
          <p>{item.question}</p>
        </li>)}</ol>
        <div className="iv-plan-note">{drilling
          ? '错题强化：每道题是一道错题的变式题，面试官会盯住你上次没答到位的地方追问（面试中不提示）；复盘时逐题对照上次。'
          : '面试官会按这份题单逐题追问（每题最多 3 层）；回答越真实，复盘越有价值。'}</div>
        {plan.fallback && <p className="tiny-label iv-warn">出题模型暂时不可用，本场按原题重问——追问仍会对准上次失分点。</p>}
        <button className="button primary" onClick={onSend}><Icon name="mic" size={16} />开始面试</button>
      </div>}
      {messages.map(item => <div key={item.id} className={`iv-msg ${item.role}`}>
        <span className="iv-who">{item.role === 'assistant' ? session.persona.name : '你'}</span>
        {item.role === 'assistant' ? <div className="iv-content"><Markdown content={item.content} /></div> : <div className="iv-content">{item.content}</div>}
      </div>)}
      {streamAnswer && <div className="iv-msg assistant streaming">
        <span className="iv-who">{session.persona.name}</span>
        <div className="iv-content"><Markdown content={streamAnswer} /></div>
      </div>}
    </div>
    {progress?.phase === 'wrapup' && session.status === 'live' && <div className="iv-wrapup-bar">
      <span>面试官已进入反问收尾，随时可结束本场</span>
      <button className="button primary small" disabled={busy} onClick={onFinish}><Icon name="check" size={14} />结束并生成复盘</button>
    </div>}
    {!ready && session.status === 'live' && <div className="interview-composer">
      <textarea
        value={answer} onChange={event => onAnswer(event.target.value)}
        placeholder="像真面试一样回答（讲具体项目、给数据）；Ctrl+Enter 提交" rows={3} maxLength={8000}
        onKeyDown={event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); onSend() } }}
      />
      <div className="iv-composer-actions">
        <span className="tiny-label">面试中不剧透不点评 · 评价看复盘</span>
        <button className="button primary" disabled={busy || !answer.trim()} onClick={onSend}>提交回答</button>
      </div>
    </div>}
  </div>
}

// 渐进式复盘：start 事件铺全部题卡（shimmer 骨架），review 事件按题号原位填充真内容，
// summary 阶段顶部提示汇总中；done 事件无缝替换为终版报告。等待变成提前阅读。
function ProgressiveReport({ feed }: { feed: ReportFeed }) {
  return <div className="interview-report iv-report-live">
    <div className="iv-live-head">
      <span className="spinner" />
      <strong>复盘生成中</strong>
      {feed && <span className="iv-progress">{feed.done}/{feed.total}</span>}
      <span className="tiny-label">{feed?.stage === 'summary' ? '汇总总评与高危清单…' : '逐题点评完成即展示，可先读起来'}</span>
    </div>
    {feed?.items.map(item => item.review
      ? <details key={item.index} open className="iv-review filled">
          <summary><span className="iv-topic">{item.topic}</span>{item.question}</summary>
          {item.previous_problems?.length ? <p className="iv-previous">上次失分：{item.previous_problems.join('、')}</p> : null}
          <p className="iv-critique"><Markdown content={item.review.critique} /></p>
          {item.review.missing?.length > 0 && <p className="iv-missing">缺失要素：{item.review.missing.join('、')}</p>}
          {item.review.model_answer && <div className="iv-model-answer"><strong>高分示范</strong><Markdown content={item.review.model_answer} /></div>}
          {item.review.bad_answer_note && <p className="iv-bad-answer"><strong>踩坑提醒</strong>{item.review.bad_answer_note}</p>}
        </details>
      : <div key={item.index} className="iv-review pending">
          <summary><span className="iv-topic">{item.topic}</span>{item.question}</summary>
          {item.previous_problems?.length ? <p className="iv-previous">上次失分：{item.previous_problems.join('、')}</p> : null}
          <p className="iv-pending-note">点评生成中…</p>
          <span className="iv-skeleton"><i /><i /><i /></span>
        </div>)}
    {!feed && <ReportStatusLine progress={null} />}
  </div>
}

// 错题强化的纯计数对照条：只摆数字不下结论——点评模型几乎每题都会点出可改进处，「补齐」不等于「过关」。
function DrillStatsLine({ stats }: { stats: NonNullable<InterviewReport['drill_stats']> }) {
  return <p className="iv-drill-stats" role="status">
    <Icon name="target" size={14} />
    <span>错题强化对照：重练 {stats.total} 道错题，<b>{stats.cleared}</b> 道本次没再被点出缺失要素；缺失要素合计 上次 {stats.before} 条 → 本次 {stats.after} 条。</span>
  </p>
}

// 逐题复盘底部的「重练此题」：把这题带去「错题强化官」（没被点失分点的题也能练）。
function ReviewDrillAction({ onClick }: { onClick: () => void }) {
  return <div className="iv-review-foot">
    <button className="text-button" onClick={onClick}
      title="把这题带去「错题强化官」：换问法重考一遍，面试官盯住上次的失分点追问，复盘对照这次的表现"><Icon name="mic" size={13} />重练此题</button>
  </div>
}

// 终版复盘报告。错题强化场次多两样：总评下的计数对照条、每题的「上次失分」行；
// 「重练此题 / 强化重练本场错题」把答得不好的题带去再开一场强化面。
function ReportPanel({ report, persona, onRegenerate, busy, onDrillQuestion, onDrillWeak }: {
  report: InterviewSession['report']; persona: InterviewSession['persona']
  onRegenerate: () => void; busy: boolean
  onDrillQuestion?: (index: number) => void; onDrillWeak?: () => void
}) {
  if (!report) return <div className="iv-state">复盘还没有生成。回面试间点「结束并生成复盘」。</div>
  const weakCount = report.questions.filter(item => item.missing?.length).length
  return <div className="interview-report">
    <section className="paper-panel">
      <div className="section-heading"><h2>本场总评</h2><span className="tiny-label">{persona.name} · {persona.round_label} · 复盘了 {report.reviewed ?? report.questions.length} 题</span>
        <div className="iv-report-actions">
          {onDrillWeak && weakCount > 0 && <button className="button primary small" onClick={onDrillWeak}
            title="把本场被点了缺失要素的题带去「错题强化官」：换问法重考一遍，复盘逐题对照这次"><Icon name="mic" size={14} />强化重练本场错题（{weakCount}）</button>}
          <button className="button secondary small" disabled={busy} onClick={onRegenerate} title="对同一批问答重新跑逐题点评与汇总，覆盖当前报告"><Icon name="source" size={14} />重新生成</button>
        </div>
      </div>
      <p className="iv-summary">{report.summary}</p>
      {report.drill_stats && <DrillStatsLine stats={report.drill_stats} />}
      <div className="iv-dimensions">
        {report.dimensions.map(item => <div key={item.name} className="iv-dimension">
          <strong>{item.name}</strong>
          <span className="iv-score" data-score={item.score}>{'★'.repeat(item.score)}{'☆'.repeat(5 - item.score)}</span>
          <p>{item.comment}</p>
        </div>)}
      </div>
    </section>
    <section className="paper-panel">
      <h3>逐题复盘（好答案 vs 踩坑）</h3>
      <div className="iv-reviews">
        {report.questions.map((item, index) => <details key={index} open={index === 0} className="iv-review">
          <summary><span className="iv-topic">{item.topic}</span>{item.question}</summary>
          {item.previous_problems?.length ? <p className="iv-previous">上次失分：{item.previous_problems.join('、')}</p> : null}
          <p className="iv-critique"><Markdown content={item.critique} /></p>
          {item.missing.length > 0 && <p className="iv-missing">缺失要素：{item.missing.join('、')}</p>}
          {item.model_answer && <div className="iv-model-answer"><strong>高分示范</strong><Markdown content={item.model_answer} /></div>}
          {item.bad_answer_note && <p className="iv-bad-answer"><strong>踩坑提醒</strong>{item.bad_answer_note}</p>}
          {onDrillQuestion && <ReviewDrillAction onClick={() => onDrillQuestion(index)} />}
        </details>)}
      </div>
    </section>
    <div className="analysis-columns">
      <AnalysisQuickList title="下次真面试前必须补（高危清单）" items={report.risk_list} />
      <AnalysisQuickList title="行动建议" items={report.actions} />
    </div>
  </div>
}

function AnalysisQuickList({ title, items }: { title: string; items: string[] }) {
  return <section className="analysis-list paper-panel"><h4>{title}</h4>
    {items?.length ? <ul>{items.map((item, index) => <li key={index}>{item}</li>)}</ul> : <p className="muted">本轮没有特别突出的项。</p>}
  </section>
}

// ---- 真实复盘工作区：导入面板（未选中时）｜逐字稿工作台「说话人核对 | 复盘报告」 ----
// 与模拟面试并列的第二条链路（2026-11-21）：外部 ASR 文本进来 → 模型初标说话人
// → 用户逐条核对修正（PATCH 全量按序替换）→ 确认后生成结合 JD 的复盘报告。
// 流式与编辑态全部自管，切换选中逐字稿即整体重置。

// 标准格式示例：逐字稿自带「说话人：原话」前缀时导入端直读切分、跳过模型标注（降本增效）。
// 「说话人1/2」类无语义前缀认不出谁是候选人，仍走模型标注——示例刻意用候选人/面试官称呼。
const FORMAT_EXAMPLE = `面试官：你好，请先简单介绍一下自己。
候选人：好的，我做后端开发五年，主要技术栈是 Python 和 Go。
面试官1：讲讲你最有代表性的项目，你负责哪部分？
候选人：会员增长项目里我负责链路设计，把下单转化率提升了 12%。
面试官1：这个 12% 的口径是怎么统计的？
候选人：A/B 实验，实验组和对照组各跑 30 天得出的。`

type TranscriptFeedItem = { index: number; question: string; review?: { critique: string; good: string[]; bad: string[]; suggestion: string } }
// 渐进式复盘 feed：start 铺题卡骨架，review 原位填充，done 换终版报告（同模拟面试复盘 feed）。
type TranscriptReportFeed = { items: TranscriptFeedItem[]; done: number; total: number; stage: 'review' | 'summary' } | null

function RealWorkspace({ transcriptId, modelConfigured, onConfigureModel, onCreated, onChanged, onDrill }: {
  transcriptId: string; modelConfigured: boolean; onConfigureModel: () => void
  onCreated: (id: string) => void; onChanged: () => void
  // 错题强化入口：带着真实复盘里的错题引用切到模拟侧开强化场（由外层 InterviewView 落地）；后端不支持时为 undefined。
  onDrill?: (refs: InterviewDrillRef[], job?: { id: string; label: string }) => void
}) {
  const [transcript, setTranscript] = useState<Transcript | null>(null)
  const [error, setError] = useState('')
  const [run, setRun] = useState('')  // 挂接中的逐字稿 run id（'' = 无流）
  const [feed, setFeed] = useState<TranscriptReportFeed>(null)
  const [labelSegs, setLabelSegs] = useState<TranscriptSegment[]>([])  // 标注流式实况：段闭合即点亮
  const [tab, setTab] = useState<'labels' | 'report'>('labels')
  const [segments, setSegments] = useState<TranscriptSegment[]>([])
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [importing, setImporting] = useState(false)
  const [confirmRelabel, setConfirmRelabel] = useState(false)
  const [jobId, setJobId] = useState('')
  const [jobLabel, setJobLabel] = useState('')
  const [text, setText] = useState('')
  const streamRef = useRef<EventSource | null>(null)
  const idRef = useRef('')
  idRef.current = transcriptId
  // 待首装逐字稿：导入/重标后先拉进行中状态当等待区；SSE done 抢先换装后，
  // 迟到的 GET 结果不得拿旧状态把新段落覆盖回去（同模拟面试 planningRef 手法）。
  const pendingRef = useRef('')

  const closeStream = useCallback(() => { streamRef.current?.close(); streamRef.current = null; setRun(''); setFeed(null) }, [])

  const loadTranscript = useCallback(async (id: string) => {
    try {
      const detail = await getInterviewTranscript(id)
      pendingRef.current = ''
      setTranscript(detail.transcript)
      setSegments(detail.transcript.segments)
      setLabelSegs([])
      setDirty(false)
      // 后台仍在跑（切走再回来）：重连事件流续播，无需用户操作。
      if (detail.active_run && !detail.active_run.done && streamRef.current === null) attach(detail.active_run.run_id)
    } catch (err) { setError((err as Error).message) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const attach = useCallback((runId: string) => {
    closeStream()
    setRun(runId)
    const source = openTranscriptRunStream(runId, (event, data) => {
      if (event === 'start') {
        setLabelSegs([])  // 事件重放从 0 开始：先清流式实况保证重放幂等
        return
      }
      if (event === 'progress') {
        // 标注流式事件：reset 清空（模型重试前），segment 逐段点亮（重放按 start/reset 清零后追加幂等）。
        if (data.stage === 'label_reset') { setLabelSegs([]); return }
        if (data.stage === 'label_segment' && data.segment) {
          const seg = data.segment as { speaker: string; text: string }
          setLabelSegs(current => [...current, { index: current.length, speaker: String(seg.speaker ?? ''), text: String(seg.text ?? '') }])
          return
        }
        // 复盘渐进事件三段：start 铺骨架 → review 原位填充 → summary 汇总中；重连重放按题号折叠幂等。
        if (data.stage === 'start' && Array.isArray(data.items)) {
          setFeed({ items: (data.items as Record<string, unknown>[]).map(item => ({
            index: Number(item.index ?? 0), question: String(item.question ?? ''),
          })), done: 0, total: Number(data.total ?? (data.items as unknown[]).length), stage: 'review' })
        } else if (data.stage === 'review' && data.index != null) {
          const index = Number(data.index)
          setFeed(current => current ? {
            ...current, done: Number(data.done ?? current.done), stage: 'review',
            items: current.items.map(item => item.index === index
              ? { ...item, review: data.review as TranscriptFeedItem['review'] } : item),
          } : current)
        } else if (data.stage === 'summary') {
          setFeed(current => current ? { ...current, stage: 'summary' } : current)
        }
        return
      }
      if (event === 'done') {
        closeStream()
        pendingRef.current = ''
        setLabelSegs([])
        const next = data.transcript as Transcript | undefined
        if (next && next.id === idRef.current) {
          setTranscript(next)
          setSegments(next.segments)
          setDirty(false)
          if (data.kind === 'report') setTab('report')
        }
        onChanged()
        return
      }
      if (event === 'error') {
        closeStream(); setError(String(data.message ?? '生成失败'))
        if (idRef.current) void loadTranscript(idRef.current)
        onChanged()
        return
      }
      if (event === 'cancelled') {
        closeStream(); setError('已停止本次生成')
        if (idRef.current) void loadTranscript(idRef.current)
        onChanged()
        return
      }
      if (event === 'end') {
        // done 未达而流终止（服务重启等）：以服务端状态为准解锁。
        closeStream()
        if (idRef.current) void loadTranscript(idRef.current)
      }
    })
    source.onerror = () => { /* 短暂网络故障由 EventSource 自动重连续播事件日志 */ }
    streamRef.current = source
  }, [closeStream, onChanged, loadTranscript])

  // 切换选中逐字稿：清工作态并拉详情；未选中（导入面板）时清空一切。卸载收流。
  useEffect(() => {
    setError(''); setTab('labels'); setFeed(null); setLabelSegs([]); setText(''); setJobId(''); setJobLabel('')
    if (!transcriptId) { closeStream(); setTranscript(null); setSegments([]); setDirty(false); return }
    void loadTranscript(transcriptId)
    return closeStream
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [transcriptId])

  async function doImport() {
    if (importing || run) return
    const payload = text.trim()
    if (!jobId) { setError('请先选择本次面试对应的岗位'); return }
    if (payload.length < 50) { setError('逐字稿太短（至少 50 字），请确认粘贴的是面试转写文本'); return }
    setError('')
    setImporting(true)
    try {
      const started = await importInterviewTranscript(jobId, payload)
      setText(''); setJobId(''); setJobLabel('')
      onCreated(started.transcript_id)
      onChanged()
      if (started.run_id) {
        attach(started.run_id)
        // 立刻把标注中状态拉进来当等待区（done 事件兜底换装）。
        pendingRef.current = started.transcript_id
        try {
          const detail = await getInterviewTranscript(started.transcript_id)
          if (pendingRef.current === started.transcript_id) { setTranscript(detail.transcript); setSegments(detail.transcript.segments); setDirty(false) }
        } catch { /* 拉取失败不打断：SSE done 兜底 */ }
      } else {
        // 标准格式直读：无标注 run，切选中后 useEffect 拉详情直接进核对页签。
        toast('已按标准格式直接导入（未调用模型），请核对说话人', 'notice')
      }
    } catch (err) { setError((err as Error).message) }
    finally { setImporting(false) }
  }

  async function saveSegments() {
    if (!transcript || saving || run) return
    setSaving(true)
    try {
      const saved = await saveTranscriptSegments(transcript.id, segments.map(item => ({ speaker: item.speaker, text: item.text })))
      setTranscript(saved.transcript)
      setSegments(saved.transcript.segments)
      setDirty(false)
      toast('标注已保存', 'notice')
      onChanged()
    } catch (err) { setError((err as Error).message) }
    finally { setSaving(false) }
  }

  async function startReport() {
    if (!transcript || run) return
    if (dirty) { setError('标注有未保存的修改，请先保存再生成报告'); return }
    try {
      const started = await generateTranscriptReport(transcript.id)
      // 乐观置 reporting：渐进视图立即可见（done 事件用服务端权威状态覆盖）。
      setTranscript(current => current ? { ...current, status: 'reporting' } : current)
      setTab('report')
      attach(started.run_id)
    } catch (err) { setError((err as Error).message) }
  }

  async function relabel() {
    if (!transcript || run) return
    setConfirmRelabel(false)
    try {
      const started = await relabelInterviewTranscript(transcript.id)
      pendingRef.current = transcript.id
      attach(started.run_id)
      try {
        const detail = await getInterviewTranscript(transcript.id)
        if (pendingRef.current === transcript.id) { setTranscript(detail.transcript); setSegments(detail.transcript.segments); setDirty(false) }
      } catch { /* 拉取失败不打断：SSE done 兜底 */ }
    } catch (err) { setError((err as Error).message) }
  }

  async function stopRun() {
    if (!run) return
    try { await cancelTranscriptRun(run) } catch { /* 服务端 cancelled 事件兜底 */ }
  }

  const busy = run !== ''
  if (!transcriptId) {
    return <section className="interview-create iv-import">
      <h2 className="sr-only">导入真实面试逐字稿</h2>
      {!modelConfigured && <div className="iv-model-hint">未绑定分析模型：标准格式逐字稿（带候选人/面试官前缀）可直接导入，生成复盘报告前需绑定。<button className="inline-button" onClick={onConfigureModel}>去设置中心</button></div>}
      <p className="iv-import-intro">把外部 ASR 工具（通义听悟、飞书妙记、讯飞等）转写的面试逐字稿粘进来：自带「候选人 / 面试官」前缀的标准格式直接切分入库（免模型标注），其余格式由模型按段标注说话人，逐条核对修正后生成结合岗位 JD 的复盘报告。</p>
      <JobPicker value={jobId} label={jobLabel} onSelect={(id, label) => { setJobId(id); setJobLabel(label) }} />
      {!jobId && <span className="tiny-label">必选：先选这次面试对应的岗位，复盘报告会结合 JD 评估回答匹配度</span>}
      <details className="iv-format-example">
        <summary>标准格式示例（按此格式粘贴可跳过模型标注，直接导入）</summary>
        <pre>{FORMAT_EXAMPLE}</pre>
        <p className="tiny-label">一行一句：「我/候选人/求职者：」是你，「面试官/考官/面试官N：」是面试官，允许时间戳前缀。非匹配行并入上一段；「说话人1/2」这类认不出谁是候选人的格式仍走模型标注。</p>
        <button className="inline-button" onClick={() => setText(FORMAT_EXAMPLE)}>插入示例文本试一试</button>
      </details>
      <textarea className="iv-jd iv-transcript-text" value={text} rows={10} maxLength={40000}
        placeholder="粘贴面试逐字稿。每行带「候选人：/面试官：」前缀（见格式示例）可直接导入；无前缀或「说话人N」格式由模型标注，原文不改写不删字。"
        onChange={event => setText(event.target.value)} aria-label="逐字稿文本" />
      <div className="iv-create-actions">
        <button className="button primary" disabled={!jobId || text.trim().length < 50 || importing} onClick={() => void doImport()}>
          {importing ? <span className="spinner" /> : <Icon name="resume" size={16} />}导入并标注说话人
        </button>
        <span className="tiny-label">{text.length}/40000 · {modelConfigured ? '标准格式直接导入，其余由模型标注（约 1-2 分钟）' : '标准格式直接导入无需模型'}</span>
      </div>
    </section>
  }
  if (!transcript) return <div className="iv-state">逐字稿加载中…<span className="spinner" /></div>
  const hasReport = Boolean(transcript.report)
  const reportStale = hasReport && transcript.status !== 'done'  // done 上改过标注 → 报告过期待重生成
  const jobLabelFull = [transcript.company, transcript.job_title].filter(Boolean).join(' · ')
  return <div className="iv-real">
    <div className="iv-subtab-row">
      <div className="interview-subtabs" role="tablist" aria-label="逐字稿分区">
        <button role="tab" aria-selected={tab === 'labels'} className={tab === 'labels' ? 'active' : ''} onClick={() => setTab('labels')}>说话人核对</button>
        <button role="tab" aria-selected={tab === 'report'} className={tab === 'report' ? 'active' : ''} onClick={() => setTab('report')} disabled={!hasReport}>复盘报告{hasReport ? '' : '（核对后生成）'}</button>
      </div>
      <div className="iv-subtabs-actions">
        {busy && <button className="button secondary small" onClick={() => void stopRun()}>停止</button>}
      </div>
    </div>
    {error && <div className="iv-error" role="alert"><span>{error}</span><button className="iv-error-close" onClick={() => setError('')} aria-label="关闭提示"><Icon name="close" size={14} /></button></div>}
    {tab === 'labels' ? <LabelReviewPanel
        transcript={transcript} segments={segments} live={labelSegs} dirty={dirty} saving={saving} busy={busy}
        onSegment={(index, patch) => { setSegments(current => current.map(item => item.index === index ? { ...item, ...patch } : item)); setDirty(true) }}
        onSave={() => void saveSegments()} onReport={() => void startReport()} onRelabel={() => setConfirmRelabel(true)} />
      : transcript.status === 'reporting'
        ? <TranscriptProgressiveReport feed={feed} />
        : <><TranscriptReportPanel report={transcript.report} jobLabel={jobLabelFull} stale={reportStale}
              onRegenerate={() => void startReport()} busy={busy}
              onDrillQuestion={onDrill ? index => onDrill([{ source: 'real', session_id: transcript.id, index }],
                { id: transcript.job_id, label: jobLabelFull }) : undefined}
              onDrillWeak={onDrill ? () => onDrill(
                (transcript.report?.questions ?? []).map((item, index) => (item.bad.length ? index : -1)).filter(index => index >= 0)
                  .map(index => ({ source: 'real' as const, session_id: transcript.id, index })),
                { id: transcript.job_id, label: jobLabelFull }) : undefined} />
          {/* 失败博物馆归因标注：真实面试失败是主要馆藏（有真实结果可归因） */}
          <AttributionPanel source="real" sessionId={transcript.id} onSaved={onChanged} /></>}
    {confirmRelabel && <ConfirmDialog danger title="重新标注" message="重新标注会覆盖当前段落，含已做的人工修正。"
      confirmLabel="重新标注" onConfirm={() => void relabel()} onCancel={() => setConfirmRelabel(false)} />}
  </div>
}

// 标注核对页签：流式实况（标注中，段落逐条点亮）/ 失败重试 / 段落编辑器（角色下拉 + 文本框）。
function LabelReviewPanel({ transcript, segments, live, dirty, saving, busy, onSegment, onSave, onReport, onRelabel }: {
  transcript: Transcript; segments: TranscriptSegment[]; live: TranscriptSegment[]; dirty: boolean; saving: boolean; busy: boolean
  onSegment: (index: number, patch: { speaker?: string; text?: string }) => void
  onSave: () => void; onReport: () => void; onRelabel: () => void
}) {
  const status = transcript.status
  const canReport = status === 'labeled' || status === 'done'
  return <div className="iv-label-panel">
    {status === 'labeling' && <div className="iv-waiting iv-labeling-live">
      <div className="iv-waiting-head"><span className="spinner" />正在标注说话人…<ElapsedSince createdAt={transcript.created_at} />
        {live.length > 0 && <span className="iv-progress">已标 {live.length} 段</span>}</div>
      <LabelLiveList segments={live} />
      <p className="iv-waiting-note">模型边输出边标注，段落逐条点亮（候选人 / 多位面试官，原文逐字保留）。中途关闭或刷新页面都不丢：后台继续标注，回来从左侧列表点开接着看。完成后逐条核对。</p>
    </div>}
    {status === 'failed' && <div className="iv-state">标注失败：{transcript.error || '模型返回无效'}
      <button className="button secondary small" disabled={busy} onClick={onRelabel}>重新标注</button></div>}
    {status === 'imported' && <div className="iv-state">尚未标注。<button className="button secondary small" disabled={busy} onClick={onRelabel}>开始标注</button></div>}
    {status !== 'labeling' && status !== 'imported' && status !== 'failed' && <>
      <div className="iv-label-toolbar">
        <span className="tiny-label">{status === 'reporting' ? '报告生成中，仍可修正标注（保存后需重新生成报告）'
          : dirty ? '有未保存的修改'
          : status === 'done' ? '报告已生成；改完标注记得重新生成'
          : '逐条核对说话人与文本，改完保存'}</span>
        <div className="iv-label-actions">
          <button className="button secondary small" disabled={busy || saving || !dirty} onClick={onSave}>{saving ? <span className="spinner" /> : null}保存修正</button>
          <button className="button secondary small" disabled={busy} onClick={onRelabel} title="重新用模型标注，会覆盖人工修正">重新标注</button>
          <button className="button primary small" disabled={busy || !canReport} onClick={onReport}><Icon name="chart" size={14} />生成复盘报告</button>
        </div>
      </div>
      {transcript.labeling_meta?.method === 'format' && <p className="tiny-label iv-label-note">本稿按标准格式直接导入（未调用模型），说话人取自原文前缀，核对无误即可生成报告。</p>}
      <div className="iv-segments">
        {segments.map(item => <div key={item.index} className={`iv-segment ${item.speaker === 'me' ? 'me' : 'other'}`}>
          <select value={item.speaker} aria-label={`第 ${item.index + 1} 段说话人`} disabled={busy}
            onChange={event => onSegment(item.index, { speaker: event.target.value })}>
            {SPEAKER_OPTIONS.map(option => <option key={option} value={option}>{speakerLabel(option)}</option>)}
          </select>
          <textarea value={item.text} rows={Math.min(6, Math.max(2, Math.ceil(item.text.length / 60)))} disabled={busy}
            onChange={event => onSegment(item.index, { text: event.target.value })} aria-label={`第 ${item.index + 1} 段内容`} />
        </div>)}
        {segments.length === 0 && <p className="muted">还没有标注段落。</p>}
      </div>
      <p className="tiny-label iv-label-note">报告以这里确认过的逐字稿为准：候选人 =「候选人」，面试官按编号区分；同一人连续的话已合并成段。</p>
    </>}
  </div>
}

// 标注流式实况列表：段落逐条 fade-in 点亮，贴底滚动，尾部 shimmer 表示仍在生成。
// 刷新/断线由 SSE 重放重建（start/reset 清零后逐段追加，天然幂等）。
function LabelLiveList({ segments }: { segments: TranscriptSegment[] }) {
  const boxRef = useRef<HTMLDivElement>(null)
  useEffect(() => { boxRef.current?.scrollTo({ top: boxRef.current.scrollHeight }) }, [segments.length])
  if (!segments.length) return <div className="iv-live-labels iv-live-empty"><span className="iv-skeleton"><i /><i /><i /></span></div>
  return <div className="iv-live-labels" ref={boxRef}>
    {segments.map(item => <div key={item.index} className={`iv-live-seg ${item.speaker === 'me' ? 'me' : 'other'}`}>
      <em>{speakerLabel(item.speaker)}</em><p>{item.text}</p>
    </div>)}
    <div className="iv-live-tail"><span className="iv-skeleton"><i /></span></div>
  </div>
}

// 计时（锚在 created_at 上）：标注等待区与出题等待区同款，切走再回来不归零。
function ElapsedSince({ createdAt }: { createdAt: string }) {
  const created = new Date(createdAt).getTime()
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const elapsed = Number.isFinite(created) ? Math.max(0, Math.round((now - created) / 1000)) : 0
  return <span>已等待 {elapsed >= 60 ? `${Math.floor(elapsed / 60)} 分 ${elapsed % 60} 秒` : `${elapsed} 秒`}</span>
}

// 渐进式复盘报告：start 铺题卡骨架 → review 原位填充 → 终版无缝替换（同模拟面试复盘）。
function TranscriptProgressiveReport({ feed }: { feed: TranscriptReportFeed }) {
  return <div className="interview-report iv-report-live">
    <div className="iv-live-head">
      <span className="spinner" />
      <strong>复盘报告生成中</strong>
      {feed && <span className="iv-progress">{feed.done}/{feed.total}</span>}
      <span className="tiny-label">{feed?.stage === 'summary' ? '汇总评分与岗位匹配…' : '逐题点评完成即展示，可先读起来'}</span>
    </div>
    {feed?.items.map(item => item.review
      ? <details key={item.index} open className="iv-review filled">
          <summary>{item.question}</summary>
          <p className="iv-critique"><Markdown content={item.review.critique} /></p>
          {item.review.good.length > 0 && <p className="iv-good">亮点：{item.review.good.join('；')}</p>}
          {item.review.bad.length > 0 && <p className="iv-missing">问题：{item.review.bad.join('；')}</p>}
          {item.review.suggestion && <p className="iv-bad-answer"><strong>下次这样答</strong>{item.review.suggestion}</p>}
        </details>
      : <div key={item.index} className="iv-review pending">
          <summary>{item.question}</summary>
          <p className="iv-pending-note">点评生成中…</p>
          <span className="iv-skeleton"><i /><i /><i /></span>
        </div>)}
    {!feed && <div className="iv-state">复盘生成中：问答拆解 → 逐题点评 → 汇总评分与岗位匹配…<span className="spinner" /></div>}
  </div>
}

// 终版复盘报告：总评分/一句话总评 → 岗位匹配度 → 优点/问题两栏 → 逐题问答拆解 → 行动清单。
// 「重练此题 / 强化重练本场错题」把真实面试里答得不好的题带去模拟侧开错题强化场。
function TranscriptReportPanel({ report, jobLabel, stale, onRegenerate, busy, onDrillQuestion, onDrillWeak }: {
  report: TranscriptReport | null; jobLabel: string; stale: boolean; onRegenerate: () => void; busy: boolean
  onDrillQuestion?: (index: number) => void; onDrillWeak?: () => void
}) {
  if (!report) return <div className="iv-state">复盘报告还没有生成。回「说话人核对」页签点「生成复盘报告」。</div>
  const match = report.jd_match ?? { fit_score: 0, evidence: [], gaps: [] }
  const weakCount = report.questions.filter(item => item.bad?.length).length
  return <div className="interview-report">
    <section className="paper-panel">
      <div className="section-heading"><h2>本场总评</h2>
        <span className="tiny-label">{jobLabel}{report.reviewed ? ` · 拆解了 ${report.reviewed} 题问答` : ''}</span>
        <div className="iv-report-actions">
          {onDrillWeak && weakCount > 0 && <button className="button primary small" onClick={onDrillWeak}
            title="把本场被点了问题的题带去「错题强化官」：换问法重考一遍，复盘逐题对照这次"><Icon name="mic" size={14} />强化重练本场错题（{weakCount}）</button>}
          <button className="button secondary small" disabled={busy} onClick={onRegenerate} title="基于当前标注重新生成复盘报告"><Icon name="source" size={14} />重新生成</button>
        </div>
      </div>
      <p className="iv-summary"><span className="iv-score" data-score={report.overall_score}>{'★'.repeat(Math.max(report.overall_score, 0))}{'☆'.repeat(5 - Math.max(report.overall_score, 0))}</span>{report.summary}</p>
      {report.limitations?.length ? <p className="iv-missing">{report.limitations.join('；')}</p> : null}
    </section>
    <section className="paper-panel">
      <div className="section-heading"><h3>岗位匹配度</h3>
        <span className="iv-score" data-score={match.fit_score}>{'★'.repeat(Math.max(match.fit_score, 0))}{'☆'.repeat(5 - Math.max(match.fit_score, 0))}</span>
      </div>
      {(match.evidence.length || match.gaps.length) ? <div className="analysis-columns">
        <AnalysisQuickList title="回答中的匹配证据" items={match.evidence} />
        <AnalysisQuickList title="暴露的差距" items={match.gaps} />
      </div> : <p className="muted">没有可展示的匹配证据与差距（可能未结合 JD）。</p>}
    </section>
    <div className="analysis-columns">
      <AnalysisQuickList title="整体亮点" items={report.strengths} />
      <AnalysisQuickList title="整体问题" items={report.problems} />
    </div>
    <section className="paper-panel">
      <h3>逐题问答拆解</h3>
      <div className="iv-reviews">
        {report.questions.map((item, index) => <details key={index} open={index === 0} className="iv-review">
          <summary>{item.question}</summary>
          <p className="iv-critique"><Markdown content={item.critique} /></p>
          {item.good.length > 0 && <p className="iv-good">亮点：{item.good.join('；')}</p>}
          {item.bad.length > 0 && <p className="iv-missing">问题：{item.bad.join('；')}</p>}
          {item.suggestion && <p className="iv-bad-answer"><strong>下次这样答</strong>{item.suggestion}</p>}
          {onDrillQuestion && <ReviewDrillAction onClick={() => onDrillQuestion(index)} />}
        </details>)}
      </div>
    </section>
    <AnalysisQuickList title="下次同类面试行动清单" items={report.actions} />
    {stale && <p className="tiny-label iv-warn">报告生成后标注有过修改：以上报告基于旧标注，重新生成以更新。</p>}
  </div>
}

// ---- 记录导出：把一场面试拼成自足的 Markdown（题单 → 对话实录 → 复盘），复制或落盘给其他 AI 复用 ----

async function copyText(text: string): Promise<boolean> {
  try { await navigator.clipboard.writeText(text); return true } catch { return false }
}

function interviewFileName(session: InterviewSession): string {
  const base = [session.company, session.job_title].filter(Boolean).join('-') || session.title
  const d = new Date(session.created_at)
  const stamp = Number.isNaN(d.getTime()) ? session.id
    : `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}-${String(d.getHours()).padStart(2, '0')}${String(d.getMinutes()).padStart(2, '0')}`
  return `面试记录-${base}-${stamp}.md`.replace(/[\\/:*?"<>|]/g, '_')
}

// 归因标注导出的取数：标签显示名从分类学解析（失败退回 tag id），未标注过静默缺省。
type AttributionBundle = { attribution: AttributionRecord | null; labels: Map<string, string> }
const ATTRIBUTION_OUTCOME_NAMES: Record<string, string> = {
  self_positive: '自评满意', self_neutral: '自评一般', self_negative: '自评不满意',
  pass: '通过', fail: '未通过', unknown: '结果未知',
}

async function loadAttributionBundle(sessionId: string): Promise<AttributionBundle> {
  const labels = new Map<string, string>()
  try {
    const museum = await getFailureMuseum()
    for (const def of [...museum.controlled, ...museum.uncontrolled]) labels.set(def.id, def.label)
  } catch { /* 报告照常导出，标签退回 id */ }
  try { return { attribution: await getAttribution(sessionId), labels } } catch { return { attribution: null, labels } }
}

function buildInterviewMarkdown(session: InterviewSession, messages: InterviewMessage[], attributionBundle: AttributionBundle): string {
  const persona = session.persona
  const plan = session.question_plan
  const report = session.report
  const lines: string[] = []
  const target = [session.company, session.job_title].filter(Boolean).join(' · ') || session.title
  lines.push(`# 模拟面试记录：${target}`, '')
  const meta = [
    `面试官：${persona.name}（${persona.round_label} · ${persona.intensity_label}${persona.company ? ` · ${persona.company}` : ''}）`,
    `开场时间：${date(session.created_at)}`,
    plan ? `题单 ${plan.questions.length} 题` : '',
  ].filter(Boolean)
  lines.push(meta.join(' ｜ '), '')
  if (plan) {
    lines.push('## 题单', '')
    plan.questions.forEach((item, index) => {
      lines.push(`${index + 1}. 【${item.topic}】${item.question}${item.focus ? `（考察：${item.focus}）` : ''}`)
    })
    lines.push('')
  }
  if (messages.length) {
    lines.push('## 对话实录', '')
    for (const item of messages) {
      lines.push(`**${item.role === 'assistant' ? '面试官' : '候选人'}：** ${item.content}`, '')
    }
  }
  if (report) {
    lines.push('## 复盘报告', '', '### 总评', '', report.summary, '')
    if (report.drill_stats) {
      lines.push(`错题强化对照：重练 ${report.drill_stats.total} 道错题，${report.drill_stats.cleared} 道本次没再被点出缺失要素；缺失要素合计 上次 ${report.drill_stats.before} 条 → 本次 ${report.drill_stats.after} 条。`, '')
    }
    if (report.dimensions.length) {
      lines.push('### 维度评分', '')
      for (const item of report.dimensions) {
        lines.push(`- ${item.name}：${'★'.repeat(item.score)}${'☆'.repeat(5 - item.score)} —— ${item.comment}`)
      }
      lines.push('')
    }
    if (report.questions.length) {
      lines.push('### 逐题点评', '')
      report.questions.forEach((item, index) => {
        lines.push(`#### ${index + 1}. 【${item.topic}】${item.question}`, '',
          `**点评：** ${item.critique}`)
        if (item.previous_problems?.length) lines.push(`**上次失分要素：** ${item.previous_problems.join('、')}`)
        if (item.missing.length) lines.push(`**缺失要素：** ${item.missing.join('、')}`)
        if (item.model_answer) lines.push(`**高分示范：** ${item.model_answer}`)
        if (item.bad_answer_note) lines.push(`**踩坑提醒：** ${item.bad_answer_note}`)
        lines.push('')
      })
    }
    if (report.risk_list?.length) lines.push('### 高危清单', '', ...report.risk_list.map(item => `- ${item}`), '')
    if (report.actions?.length) lines.push('### 行动建议', '', ...report.actions.map(item => `- ${item}`), '')
  }
  if (attributionBundle.attribution) {
    const { attribution, labels } = attributionBundle
    lines.push('## 归因标注（失败博物馆）', '')
    if (attribution.outcome) lines.push(`- 结果：${ATTRIBUTION_OUTCOME_NAMES[attribution.outcome] ?? attribution.outcome}`)
    for (const [title, scope] of [['可控因素', 'controlled'], ['不可控因素', 'uncontrolled']] as const) {
      const group = attribution.tags.filter(item => item.scope === scope)
      if (!group.length) continue
      lines.push(`### ${title}`, '')
      for (const item of group) lines.push(`- ${labels.get(item.tag) ?? item.tag}${item.note ? `：${item.note}` : ''}`)
      lines.push('')
    }
    if (attribution.note) lines.push(`**整场备注：** ${attribution.note}`, '')
  }
  return lines.join('\n')
}
