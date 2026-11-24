import { useCallback, useEffect, useRef, useState } from 'react'
import { date, getJdAssessments, getJdAssessmentTrials, submitJdAssessment } from './api'
import type { JdAnswer, JdAssessmentItem, JdAssessmentReport, JdTrial } from './api'
import { toast } from './toast'
import { Button } from './ui'

// 反向JD测评（2026-11-22 首版，2026-11-24 题型改 best-worst 选题）：每题 4 段真实 JD 片段，点出
// 「最想投」「最不想投」各一条，反推「你的隐性筛选条件」。报告只输出可操作的结论（每段片段被选为
// 最想/最不想的比例 + 优先/回避筛选关键词），不输出人格标签——避开 MBTI 式巴纳姆效应。
// 计分在服务端确定性完成（无 LLM）。
// 交互约定（借 jsPsych 规范）：每题强制点两下（先最想投、再最不想投），点完自动前进；可返回修改，
// 回到已答的题后点任意一条即重新选择；反应时 = 本题这次停留到点完第二下，<800ms 判无效，误触后
// 回改不会被质检闸门误杀；卷内含 1 道注意力校验题与 3 道重复校验题，质检不过的报告会明确标注。
// 报告版面（2026-11-22 用户反馈重做）：借鉴测评报告三栏式——左卡六维滑杆（彩色轨道+游标+两极标注），
// 中列回避清单（! 图标）、右卡优先清单（✓ 图标），底部 18 条片段两列明细。
// 旧版（两张岗位卡二选一）的已存报告没有 score/best/worst 字段，只给「重新测评」入口，不硬渲染。
const VERDICT_LABELS: Record<string, string> = {
  strong_like: '明显偏好', like: '偏好', neutral: '中性',
  avoid: '回避', strong_avoid: '明显回避', uncertain: '样本不足',
}

// 六维滑杆各自一个色相（借测评报告多色滑杆的辨识度），与全局绿色主色不冲突。
const DIM_COLORS: Record<string, string> = {
  achievement: '#3aa8a0', independence: '#e8a33d', recognition: '#4caf7d',
  relationships: '#8b6fc0', support: '#5a8fd6', conditions: '#e06a6a',
}

type Phase = 'loading' | 'intro' | 'test' | 'submitting' | 'report'

function percent(value: number) { return `${Math.round(value * 100)}%` }

// 倾向分（[-1, 1]，最想投率 − 最不想投率）映射到滑杆位置：中性=50%，越偏好越靠右。
function sliderPos(score: number) { return Math.round(Math.min(97, Math.max(3, 50 + score * 45))) }

function isLegacyReport(report: JdAssessmentReport) {
  return report.features.some(f => typeof f.score !== 'number' || typeof f.best !== 'number')
}

const hasItems = (trials: JdTrial[]) => trials.length > 0 && trials.every(t => Array.isArray(t.items) && t.items.length === 4)

export function JdAssessmentView() {
  const [phase, setPhase] = useState<Phase>('loading')
  const [trials, setTrials] = useState<JdTrial[]>([])
  const [latest, setLatest] = useState<JdAssessmentItem | null>(null)
  const [index, setIndex] = useState(0)
  // 按题序存全量作答（初始顺序作答逐步填满）；返回修改只改对应槽位，反应时按最终选择的重看时间重计，
  // 误触后回改不会被 800ms 质检闸门误杀。
  const [answers, setAnswers] = useState<(JdAnswer | null)[]>([])
  // 当前题已点出的「最想投」（还没点「最不想投」，所以尚未写入 answers）。
  const [pending, setPending] = useState<number | null>(null)
  const trialShownAt = useRef(0)
  // 会话种子：开卷时随机生成，交卷回传同值（服务端靠它复现题序与锚点配对）；重测换种子 = 换一张卷。
  const seedRef = useRef(-1)

  const load = useCallback(async () => {
    try {
      if (seedRef.current < 0) seedRef.current = Math.floor(Math.random() * 2147483647)
      const [trialData, history] = await Promise.all([getJdAssessmentTrials(seedRef.current), getJdAssessments()])
      // 前端已更新而后端没重启时，旧接口还返回「岗位卡二选一」的卷面：明确提示，别渲染成空白。
      setTrials(hasItems(trialData.trials) ? trialData.trials : [])
      setLatest(history.assessments[0] || null)
      setPhase(history.assessments[0] ? 'report' : 'intro')
    } catch (err) { toast((err as Error).message, 'error'); setPhase('intro') }
  }, [])
  useEffect(() => { void load() }, [load])
  useEffect(() => { setPending(null) }, [index, phase])

  const answeredCount = answers.filter(Boolean).length
  const allDone = trials.length > 0 && answeredCount === trials.length
  const currentAnswer = answers[index] || null

  function store(next: JdAnswer | null) {
    setAnswers(prev => { const copy = [...prev]; copy[index] = next; return copy })
  }

  // 点一下：未答的题上第一下 = 最想投、第二下 = 最不想投（写答案并前进）；再点最想投那条 = 取消。
  // 已答的题上点任意一条 = 清掉旧答案、以这条重新开始（反应时从这次重看起算）。
  function pick(i: number) {
    if (phase !== 'test' || !trials[index]) return
    if (currentAnswer) { store(null); setPending(i); trialShownAt.current = performance.now(); return }
    if (pending === null) { setPending(i); return }
    if (pending === i) { setPending(null); return }
    store({ trial_id: trials[index].id, best: pending, worst: i,
            rt_ms: Math.max(0, Math.round(performance.now() - trialShownAt.current)) })
    setPending(null)                                    // 最后一题不换页，靠这里清掉半选状态
    if (index + 1 < trials.length) { setIndex(index + 1); trialShownAt.current = performance.now() }
  }
  function goPrev() {
    if (index > 0 && phase === 'test') { setIndex(index - 1); trialShownAt.current = performance.now() }
  }
  function goNext() {
    if (index + 1 < trials.length && phase === 'test' && answers[index]) { setIndex(index + 1); trialShownAt.current = performance.now() }
  }
  function submit() {
    if (phase !== 'test' || !allDone) return
    setPhase('submitting')
    submitJdAssessment(answers.filter(Boolean) as JdAnswer[], seedRef.current)
      .then(item => { setLatest(item); setPhase('report'); toast('测评完成，报告已保存在本机', 'notice') })
      .catch((err: Error) => { toast(err.message, 'error'); setPhase('test') })
  }

  // 键盘作答（1–4 = 点对应那条，Backspace = 取消刚点的最想投）：给键盘通路，但以点击为主通路。
  useEffect(() => {
    if (phase !== 'test') return
    const onKey = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey) return
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return
      if (['1', '2', '3', '4'].includes(event.key)) pick(Number(event.key) - 1)
      else if (event.key === 'Backspace' && !answers[index] && pending !== null) setPending(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  function startTest() {
    setAnswers([]); setIndex(0); setPending(null); trialShownAt.current = performance.now(); setPhase('test')
  }
  function retake() {
    if (!window.confirm('重新测一次？新报告完成后会取代当前展示（历史仍保留在数据库）。')) return
    // 换种子换卷：新题序 + 新变体 + 新块内顺序，重测不是重做同一张卷。
    seedRef.current = Math.floor(Math.random() * 2147483647)
    setPhase('loading')
    getJdAssessmentTrials(seedRef.current)
      .then(data => {
        if (!hasItems(data.trials)) { setTrials([]); toast('后端还在运行旧版测评接口，请重启 JobScope 后端后刷新页面', 'error'); setPhase('report'); return }
        setTrials(data.trials); startTest()
      })
      .catch((err: Error) => { toast(err.message, 'error'); setPhase('report') })
  }

  if (phase === 'loading') return null
  const report = latest?.payload.report
  const legacy = report ? isLegacyReport(report) : false
  const trial = trials[index]
  const scored = report ? report.quality.total_trials - report.quality.anchors - 1 : 0
  const testing = phase === 'test' || phase === 'submitting'      // 作答中不显示「上次测评」，免得误会成当前结果
  const heading = report && !legacy && !testing
    ? `上次测评 ${date(latest!.created_at)} · ${report.quality.grade === 'ok' ? '质检通过' : '质检有警示'}`
    : '约 4 分钟 · 结果只存本机'

  return <section className="jd-wrap" aria-label="反向JD测评">
    <section className="page-intro compact"><h1 className="sr-only">反向JD测评</h1>
      <span className="revision-label">{heading}</span></section>

    {phase === 'intro' && <section className="paper-panel jd-panel">
      <div className="section-heading"><div><h2>用你的选择，反推你的筛选条件</h2>
        <p className="body-small muted">每题给你 4 段真实 JD 里的片段，点出最想投的一条和最不想投的一条。做完立刻得到一份「隐性筛选条件报告」：不是人格标签，而是每段片段被你选为最想投 / 最不想投的比例，和下一轮投递直接能用的筛选 / 回避关键词。</p></div></div>
      <ul className="jd-intro-list">
        <li><b>{trials.length} 题</b>，约 4 分钟，凭直觉选，不用想太久——直觉才是要测的东西。</li>
        <li>每题点两下：先点最想投的，再点最不想投的。没有对错；点错随时「← 上一题」改，全部答完还能从头检查一遍再交卷。中途离开即作废。</li>
        <li>卷内含注意力校验与重复校验题：质检不过的报告会如实标注可信度。</li>
      </ul>
      {!trials.length
        ? <p className="jd-empty-line">题目没取到：后端可能还在运行旧版接口，请重启 JobScope 后端后刷新页面。</p>
        : <div className="jd-intro-actions">
          <Button className="button primary" onClick={startTest}>开始测评</Button>
          {report && <Button className="button secondary" onClick={() => setPhase('report')}>查看上次报告</Button>}
        </div>}
    </section>}

    {(phase === 'test' || phase === 'submitting') && trial && <section className="paper-panel jd-panel">
      <div className="jd-progress-row"><span className="jd-count">{answeredCount} / {trials.length}</span>
        <div className="jd-progress" role="progressbar" aria-valuenow={answeredCount} aria-valuemin={0} aria-valuemax={trials.length}><i style={{ width: `${(answeredCount / trials.length) * 100}%` }} /></div>
        <span className="jd-hint">数字键 1–4 可作答 · 可返回修改</span></div>
      <p className="jd-ask">哪条最让你想投？哪条最让你不想投？</p>
      <div className="jd-items">
        {trial.items.map((phrase, i) => {
          const isBest = currentAnswer ? currentAnswer.best === i : pending === i
          const isWorst = currentAnswer?.worst === i
          return <button key={i} type="button" aria-pressed={isBest || isWorst}
            className={`jd-item${isBest ? ' jd-item-best' : ''}${isWorst ? ' jd-item-worst' : ''}`}
            disabled={phase === 'submitting'} onClick={() => pick(i)}>
            <i className="jd-item-key">{i + 1}</i><span>{phrase}</span>
            {isBest && <em className="jd-item-tag">最想投</em>}{isWorst && <em className="jd-item-tag">最不想投</em>}
          </button>
        })}
      </div>
      <div className="jd-nav-row">
        <button className="button secondary" disabled={index === 0 || phase === 'submitting'} onClick={goPrev}>← 上一题</button>
        {index + 1 < trials.length && <button className="button secondary" disabled={!currentAnswer || phase === 'submitting'} onClick={goNext}>下一题 →</button>}
        {index + 1 === trials.length && <>
          <span className="jd-nav-note">{allDone ? '全部答完，可返回检查' : currentAnswer ? '本题已答，还有未答题' : '答完本题后可交卷'}</span>
          <button className="button secondary" disabled={phase === 'submitting'} onClick={() => { setIndex(0); trialShownAt.current = performance.now() }}>从头检查</button>
          <Button className="button primary" busy={phase === 'submitting'} disabled={!allDone || phase === 'submitting'} onClick={submit}>交卷</Button>
        </>}
      </div>
      <p className="jd-hint jd-hint-center" aria-live="polite">{currentAnswer
        ? `已选：最想投第 ${currentAnswer.best + 1} 条、最不想投第 ${currentAnswer.worst + 1} 条；点任意一条可重新选择`
        : pending === null ? '先点一条你最想投的' : `已把第 ${pending + 1} 条选为最想投，再点一条最不想投的`}</p>
    </section>}

    {phase === 'report' && report && (legacy
      ? <section className="paper-panel jd-panel">
        <div className="section-heading"><div><h2>你的隐性筛选条件报告</h2>
          <p className="body-small muted">这份报告来自旧版题型（两张岗位卡二选一），新版报告按「最想投 / 最不想投」计分，重新测评后即可查看。</p></div>
          <Button className="button secondary" onClick={retake}>重新测评</Button></div>
      </section>
      : <section className="paper-panel jd-panel">
      <div className="section-heading"><div><h2>你的隐性筛选条件报告</h2>
        <p className="body-small muted">按「最想投率 − 最不想投率」排序：越靠右越常被你选为最想投，越靠左越常被你选为最不想投。</p></div>
        <Button className="button secondary" onClick={retake}>重新测评</Button></div>

      <div className={`jd-quality${report.quality.grade === 'weak' ? ' jd-quality-warn' : ''}`}
        title={report.quality.grade === 'ok' ? '注意力、重复一致性与作答速度三项质检全部通过' : '存在质检验证未通过的项，结论可信度打折'}>
        <b>{report.quality.grade === 'ok' ? '质检通过' : '质检有警示'}</b>
        <span>重复一致 {report.quality.anchor_agreement}/{report.quality.anchors} · 注意力{report.quality.attention_pass ? '通过' : '未通过'} · 有效 {report.quality.valid_trials}/{scored} 题</span>
        {report.quality.flags.length > 0 && <em>{report.quality.flags.join('；')}</em>}
      </div>

      <div className="jd-report-grid">
        <div className="jd-dims-card">
          <h3 className="jd-block-title"><i>1</i>维度倾向</h3>
          {report.dimensions.map(d => {
            const pos = sliderPos(d.score)
            const color = DIM_COLORS[d.key] || '#5a8fd6'
            return <div key={d.key} className="jd-dim-row"
              title={`维度倾向分 ${d.score.toFixed(2)}（三条片段的均值）：越靠右越常被你选为最想投，越靠左越常被你选为最不想投`}>
              <div className="jd-dim-head">
                <span className="jd-dim-name">{d.label}</span>
                <span className="jd-dim-pct" style={{ color }}>{pos}%<em>{VERDICT_LABELS[d.verdict] || d.verdict}</em></span>
              </div>
              <div className="jd-slider">
                <i className="jd-slider-fill" style={{ width: `${pos}%`, background: color }} />
                <i className="jd-slider-thumb" style={{ left: `${pos}%`, borderColor: color }} />
              </div>
              <div className="jd-dim-poles"><span>回避</span><span>偏好</span></div>
            </div>
          })}
        </div>

        <div className="jd-pref-col">
          <h3 className="jd-block-title"><i>2</i>先降级或跳过</h3>
          {report.actions.avoid.length > 0 ? report.actions.avoid.map(key => {
            const f = report.features.find(item => item.key === key)!
            return <div className="jd-pref-item" key={key} title={rateTitle(f)}>
              <span className="jd-ico jd-ico-warn">!</span>
              <div><b>{f.screen}</b><span>「{f.phrase}」出现 {f.n_present} 次，你 {f.worst} 次选为最不想投、{f.best} 次选为最想投。</span></div>
            </div>
          }) : <p className="jd-empty-line">{report.actions.avoid_line}</p>}
        </div>

        <div className="jd-pref-col jd-pref-card">
          <h3 className="jd-block-title"><i>3</i>下一轮优先看</h3>
          {report.actions.prefer.length > 0 ? report.actions.prefer.map(key => {
            const f = report.features.find(item => item.key === key)!
            return <div className="jd-pref-item" key={key} title={rateTitle(f)}>
              <span className="jd-ico jd-ico-good">✓</span>
              <div><b>{f.screen}</b><span>「{f.phrase}」出现 {f.n_present} 次，你 {f.best} 次选为最想投、{f.worst} 次选为最不想投。</span></div>
            </div>
          }) : <p className="jd-empty-line">{report.actions.prefer_line}</p>}
        </div>
      </div>

      <h3 className="jd-block-title jd-detail-title">全部片段一览</h3>
      <div className="jd-detail-grid">
        {report.features.map(f => {
          const tone = f.verdict === 'strong_like' ? 'jd-ico-strong' : f.verdict === 'like' ? 'jd-ico-good' : f.verdict === 'avoid' || f.verdict === 'strong_avoid' ? 'jd-ico-warn' : 'jd-ico-dot'
          const glyph = f.verdict === 'strong_like' || f.verdict === 'like' ? '✓' : f.verdict === 'avoid' || f.verdict === 'strong_avoid' ? '!' : f.verdict === 'uncertain' ? '?' : '·'
          return <div className="jd-detail-row" key={f.key} title={rateTitle(f)}>
            <span className={`jd-ico ${tone}`}>{glyph}</span>
            <div className="jd-detail-main"><b>{f.screen}</b><span>{f.phrase}</span></div>
            <span className="jd-rates">最想 {percent(f.best_rate)} <i>/</i> 最不想 {percent(f.worst_rate)}</span>
          </div>
        })}
      </div>

      <p className="jd-note">{report.note}</p>
    </section>)}
  </section>
}

function rateTitle(f: JdAssessmentReport['features'][number]) {
  return `出现 ${f.n_present} 次：${f.best} 次选为最想投、${f.worst} 次选为最不想投；倾向分 ${f.score.toFixed(2)}` +
    (f.low_confidence ? '；样本不足' : '')
}
