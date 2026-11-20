import { useCallback, useEffect, useRef, useState } from 'react'
import { Markdown } from './chat/Markdown'
import { Icon } from './Icon'
import {
  api, cancelInterviewRun, createInterviewSession, deleteInterviewSession, finishInterview, getInterviewMessages,
  getInterviewSession, listInterviewPersonas, listInterviewSessions, openInterviewStream, sendInterviewTurn,
} from './api'
import type { InterviewMessage, InterviewPersonaCard, InterviewSession, InterviewSessionSummary, InterviewStyle } from './api'

// 模拟面试工作区（2026-11-19）：左侧场次列表 + 右侧「面试间 | 复盘报告」。
// 面试官是纯拟真的无工具 agent：对话中零提示零点评，评价全部在复盘页签。
// 流式渲染只复用 Markdown，不复用 ChatWorkspace（模型下拉/工具选项不适用面试场景）。

export type InterviewDeepLink = { jobId: string; token: number } | null

type StreamState = { runId: string; answer: string } | null

const STATUS_NAMES: Record<string, string> = { planning: '出题中', ready: '待开始', live: '面试中', reporting: '复盘生成中', done: '已复盘', failed: '出题失败' }
const INTENSITY_NAMES: Record<string, string> = { gentle: '温和', standard: '标准', stress: '压力面' }
const SOURCE_NAMES: Record<string, string> = { jd: 'JD', gap: '评估缺口', resume: '简历', general: '通用' }

export default function InterviewView({ deepLink, modelConfigured, onConfigureModel }: {
  deepLink: InterviewDeepLink; modelConfigured: boolean; onConfigureModel: () => void
}) {
  const [personas, setPersonas] = useState<InterviewPersonaCard[]>([])
  const [sessions, setSessions] = useState<InterviewSessionSummary[]>([])
  const [selected, setSelected] = useState<string>('')
  const [session, setSession] = useState<InterviewSession | null>(null)
  const [messages, setMessages] = useState<InterviewMessage[]>([])
  const [stream, setStream] = useState<StreamState>(null)
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
  // starting：点「开始出题」到场次建好之间的一次性闸门，防连点重场，也让按钮立刻进加载态。
  const [starting, setStarting] = useState(false)
  const streamRef = useRef<EventSource | null>(null)
  const selectedRef = useRef('')
  selectedRef.current = selected
  const threadRef = useRef<HTMLDivElement>(null)

  const refreshSessions = useCallback(async () => {
    try { setSessions((await listInterviewSessions()).sessions) } catch { /* 列表失败不阻塞面试间 */ }
  }, [])

  const closeStream = useCallback(() => { streamRef.current?.close(); streamRef.current = null; setStream(null) }, [])

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

  // 岗位详情/评估缺口深链：打开新建面板、切到岗位库模式并预选岗位（#/interview?job=<id> 刷新同路径）。
  useEffect(() => {
    if (!deepLink) return
    setCreating(true); setCard(''); setMode('job'); setJobId(deepLink.jobId); setJobLabel('')
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
    setStarting(true)
    try {
      const started = await createInterviewSession({
        persona_id: card, company_style: style, round, intensity, question_count: questionCount,
        ...(targetJobId ? { job_id: targetJobId } : {}),
        ...(mode === 'jd' ? { jd_text: jdText.trim(), company: companyName.trim(), ...(jobTitle.trim() ? { job_title: jobTitle.trim() } : {}) } : {}),
        ...(mode === 'free' ? { company: companyName.trim(), ...(card === 'custom' && jobTitle.trim() ? { job_title: jobTitle.trim() } : {}) } : {}),
      })
      setCreating(false); setJdText(''); setJobTitle(''); setCompanyName('')
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
      attach(started.run_id)
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

  const plan = session?.question_plan
  const progress = session?.progress
  const askedCount = progress ? Math.min(progress.current_index + 1, plan?.questions.length ?? 0) : 0
  const busy = streamRef.current !== null
  const report = session?.report
  return <div className="interview-page">
    <aside className="interview-side">
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
    </aside>
    <div className="interview-main">
      {error && <div className="iv-error" role="alert"><span>{error}</span><button className="iv-error-close" onClick={() => setError('')} aria-label="关闭提示"><Icon name="close" size={14} /></button></div>}
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
        busy={busy || starting} onStart={() => void startSession()} starting={starting} />}
      {session && !creating && <>
        <div className="interview-subtabs" role="tablist" aria-label="面试分区" style={{ marginTop: 8 }}>
          <button role="tab" aria-selected={tab === 'room'} className={tab === 'room' ? 'active' : ''} onClick={() => setTab('room')}>面试间</button>
          <button role="tab" aria-selected={tab === 'report'} className={tab === 'report' ? 'active' : ''} onClick={() => setTab('report')} disabled={!report}>复盘报告{report ? '' : '（结束后生成）'}</button>
        </div>
        {tab === 'room' ? <RoomPanel
          session={session} plan={plan ?? null} progress={progress ?? { phase: 'opening', current_index: 0, rounds: 0, segments: [] }} askedCount={askedCount} messages={messages}
          streamAnswer={stream?.answer ?? ''} busy={busy} answer={answer} onAnswer={setAnswer}
          onSend={() => void send()} onFinish={() => void finish()} onStop={() => void stop()} threadRef={threadRef} />
        : <ReportPanel report={report ?? null} persona={session.persona} />}
      </>}
    </div>
  </div>
}

function CreatePanel({ cards, styles, card, onCard, style, onStyle, round, onRound, intensity, onIntensity, questionCount, onQuestionCount, mode, onMode, jobId, jobLabel, onPickJob, jobTitle, onJobTitle, companyName, onCompanyName, jdText, onJdText, modelConfigured, onConfigureModel, busy, starting, onStart }: {
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
  busy: boolean; starting: boolean; onStart: () => void
}) {
  const active = cards.find(item => item.id === card)
  const customNeedsTitle = card === 'custom' && mode === 'free' && !jobTitle.trim()
  const startDisabled = !card || busy || !modelConfigured || customNeedsTitle || (mode === 'job' && !jobId)
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
      <label>题量<select value={questionCount} onChange={event => onQuestionCount(Number(event.target.value))} aria-label="题量">
        {[6, 8, 10].map(value => <option key={value} value={value}>{value} 题</option>)}
      </select></label>
    </div>}
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
        : <span className="tiny-label">出题约需 1-3 分钟（岗位 JD + 评估缺口 + 简历 + 公司资料 四源合一），完成后进入面试间</span>}
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
    material.jd_summary ? '岗位 JD' : '',
    material.gaps?.length ? '评估缺口' : '',
    material.resume_digest ? (material.resume_label ? `简历存档《${material.resume_label}》` : '简历') : '',
    material.company_brief ? '公司资料' : '',
  ].filter(Boolean)
  return <div className="iv-waiting">
    <div className="iv-waiting-head"><span className="spinner" />正在出题…已等待 {elapsedLabel}</div>
    {sources.length > 0 && <div className="iv-waiting-sources">出题材料已就位：{sources.map(item => <em key={item}>{item}</em>)}</div>}
    <p className="iv-waiting-note">岗位专家正按材料定制 {session.config.question_count} 道题，约 1-3 分钟。留在本页出完自动进面试间；切去别的页面后，从左侧场次列表点开本场继续。</p>
  </div>
}

function RoomPanel({ session, plan, progress, askedCount, messages, streamAnswer, busy, answer, onAnswer, onSend, onFinish, onStop, threadRef }: {
  session: InterviewSession
  plan: InterviewSession['question_plan']
  progress: InterviewSession['progress']
  askedCount: number
  messages: InterviewMessage[]
  streamAnswer: string; busy: boolean
  answer: string; onAnswer: (value: string) => void
  onSend: () => void; onFinish: () => void; onStop: () => void
  threadRef: React.RefObject<HTMLDivElement | null>
}) {
  const total = plan?.questions.length ?? 0
  const ready = session.status === 'ready'
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
    {session.status === 'reporting' && <div className="iv-state">复盘生成中：逐题点评（含好差答案对比）→ 汇总维度与高危清单…<span className="spinner" /></div>}
    <div className="interview-thread" ref={threadRef}>
      {ready && plan && <div className="iv-plan">
        <p className="iv-plan-opening">{plan.opening}</p>
        <ol>{plan.questions.map((item, index) => <li key={index}>
          <span className="iv-topic">{item.topic}</span><span className="iv-source">{SOURCE_NAMES[item.source] || '通用'}</span>
          <p>{item.question}</p>
        </li>)}</ol>
        <div className="iv-plan-note">面试官会按这份题单逐题追问（每题最多 3 层）；回答越真实，复盘越有价值。</div>
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

function ReportPanel({ report, persona }: { report: InterviewSession['report']; persona: InterviewSession['persona'] }) {
  if (!report) return <div className="iv-state">复盘还没有生成。回面试间点「结束并生成复盘」。</div>
  return <div className="interview-report">
    <section className="paper-panel">
      <div className="section-heading"><h2>本场总评</h2><span className="tiny-label">{persona.name} · {persona.round_label} · 复盘了 {report.reviewed ?? report.questions.length} 题</span></div>
      <p className="iv-summary">{report.summary}</p>
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
          <p className="iv-critique"><Markdown content={item.critique} /></p>
          {item.missing.length > 0 && <p className="iv-missing">缺失要素：{item.missing.join('、')}</p>}
          {item.model_answer && <div className="iv-model-answer"><strong>高分示范</strong><Markdown content={item.model_answer} /></div>}
          {item.bad_answer_note && <p className="iv-bad-answer"><strong>踩坑提醒</strong>{item.bad_answer_note}</p>}
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
