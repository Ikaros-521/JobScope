import { useCallback, useEffect, useRef, useState } from 'react'
import { date, getJdAssessments, getJdAssessmentTrials, submitJdAssessment } from './api'
import type { JdAssessmentItem, JdTrial } from './api'
import { toast } from './toast'
import { Button } from './ui'

// 反向JD测评（2026-11-22 首版，2026-11-23 扩池）：40 道 JD 片段二选一（36 组配对 + 质检题），
// 反推「你的隐性筛选条件」。报告只输出可操作的结论（含某片段的选中率差异 + 优先/回避
// 筛选关键词），不输出人格标签——避开 MBTI 式巴纳姆效应。计分在服务端确定性完成（无 LLM）。
// 交互约定（借 jsPsych 规范）：强制二选一、不可回退改答案、反应时 <800ms 判无效、
// 内嵌 1 道注意力校验题与 3 道前后重复锚点题，质检不过的报告会明确标注。
// 报告版面（2026-11-22 用户反馈重做）：借鉴测评报告三栏式——左卡六维滑杆
// （彩色轨道+游标+两极标注），中列回避清单（! 图标）、右卡优先清单（✓ 图标），
// 底部 18 条片段两列明细。
// 表层多样性（2026-11-23）：每条特征 3 变体轮换 + 填充 16 条 + 会话种子随机卷面，
// 消除「整卷都是同样 12 句话」的既视感；计分按键池化，统计底座不变。
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
type JdAnswer = { trial_id: string; choice: 'a' | 'b'; rt_ms: number }

function percent(value: number) { return `${Math.round(value * 100)}%` }

// 权重（约 [-3, 3]）映射到滑杆位置：中性=50%，越偏好越靠右。
function sliderPos(weight: number) { return Math.round(Math.min(97, Math.max(3, 50 + weight * 17))) }

export function JdAssessmentView() {
  const [phase, setPhase] = useState<Phase>('loading')
  const [trials, setTrials] = useState<JdTrial[]>([])
  const [latest, setLatest] = useState<JdAssessmentItem | null>(null)
  const [index, setIndex] = useState(0)
  // 按题序存全量作答（初始顺序作答逐步填满）；返回修改只改对应槽位，反应时按最终选择的重看时间重计，
  // 误触后回改不会被 800ms 质检闸门误杀。
  const [answers, setAnswers] = useState<(JdAnswer | null)[]>([])
  const trialShownAt = useRef(0)
  // 会话种子：开卷时随机生成，交卷回传同值（服务端靠它复现锚点配对）；重测换种子 = 换一张卷。
  const seedRef = useRef(-1)

  const load = useCallback(async () => {
    try {
      if (seedRef.current < 0) seedRef.current = Math.floor(Math.random() * 2147483647)
      const [trialData, history] = await Promise.all([getJdAssessmentTrials(seedRef.current), getJdAssessments()])
      setTrials(trialData.trials)
      setLatest(history.assessments[0] || null)
      setPhase(history.assessments[0] ? 'report' : 'intro')
    } catch (err) { toast((err as Error).message, 'error'); setPhase('intro') }
  }, [])
  useEffect(() => { void load() }, [load])

  const answeredCount = answers.filter(Boolean).length
  const allDone = trials.length > 0 && answeredCount === trials.length
  const currentAnswer = answers[index] || null

  // 作答/修改：记录反应时后写入当前题并前进一格；最后一题答完停留，由底部「交卷」收口。
  function choose(choice: 'a' | 'b') {
    if (phase !== 'test') return
    const rt = Math.max(0, Math.round(performance.now() - trialShownAt.current))
    setAnswers(prev => {
      const next = [...prev]
      next[index] = { trial_id: trials[index].id, choice, rt_ms: rt }
      return next
    })
    if (index + 1 < trials.length) {
      setIndex(index + 1)
      trialShownAt.current = performance.now()
    }
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

  // 键盘作答（←/→ 或 A/B）：按 jsPsych 惯例给键盘通路，但以点击为主通路。
  useEffect(() => {
    if (phase !== 'test') return
    const onKey = (event: KeyboardEvent) => {
      if (['ArrowLeft', 'a', 'A'].includes(event.key)) choose('a')
      else if (['ArrowRight', 'b', 'B'].includes(event.key)) choose('b')
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  function startTest() {
    setAnswers([]); setIndex(0); trialShownAt.current = performance.now(); setPhase('test')
  }
  function retake() {
    if (!window.confirm('重新测一次？新报告完成后会取代当前展示（历史仍保留在数据库）。')) return
    // 换种子换卷：新题序 + 新变体 + 新填充，重测不是重做同一张卷。
    seedRef.current = Math.floor(Math.random() * 2147483647)
    setPhase('loading')
    getJdAssessmentTrials(seedRef.current)
      .then(data => { setTrials(data.trials); startTest() })
      .catch((err: Error) => { toast(err.message, 'error'); setPhase('report') })
  }

  if (phase === 'loading') return null
  const report = latest?.payload.report
  const trial = trials[index]

  return <section className="jd-wrap" aria-label="反向JD测评">
    <section className="page-intro compact"><h1 className="sr-only">反向JD测评</h1>
      <span className="revision-label">{report ? `上次测评 ${date(latest!.created_at)} · ${report.quality.grade === 'ok' ? '质检通过' : '质检有警示'}` : '约 5 分钟 · 结果只存本机'}</span></section>

    {phase === 'intro' && <section className="paper-panel jd-panel">
      <div className="section-heading"><div><h2>用你的选择，反推你的筛选条件</h2>
        <p className="body-small muted">每题给你两张由真实 JD 片段拼成的岗位卡，选一张更愿意投的。做完立刻得到一份「隐性筛选条件报告」：不是人格标签，而是每一类片段的选中率差异，和下一轮投递直接能用的筛选/回避关键词。</p></div></div>
      <ul className="jd-intro-list">
        <li><b>{trials.length} 题</b>，约 5 分钟，凭直觉选，不用想太久——直觉才是要测的东西。</li>
        <li>没有对错；点错随时「← 上一题」改，全部答完还能从头检查一遍再交卷。中途离开即作废。</li>
        <li>卷内含注意力校验与重复校验题：质检不过的报告会如实标注可信度。</li>
      </ul>
      <div className="jd-intro-actions">
        <Button className="button primary" onClick={startTest} disabled={!trials.length}>开始测评</Button>
        {report && <Button className="button secondary" onClick={() => setPhase('report')}>查看上次报告</Button>}
      </div>
    </section>}

    {(phase === 'test' || phase === 'submitting') && trial && <section className="paper-panel jd-panel">
      <div className="jd-progress-row"><span className="jd-count">{answeredCount} / {trials.length}</span>
        <div className="jd-progress" role="progressbar" aria-valuenow={answeredCount} aria-valuemin={0} aria-valuemax={trials.length}><i style={{ width: `${trials.length ? (answeredCount / trials.length) * 100 : 0}%` }} /></div>
        <span className="jd-hint">← / → 选卡 · 可返回修改</span></div>
      <div className="jd-stage">
        {(['a', 'b'] as const).map(side => <button key={side} type="button"
          className={`jd-card${currentAnswer?.choice === side ? ' jd-card-picked' : ''}`} disabled={phase === 'submitting'}
          onClick={() => choose(side)}>
          <span className={`jd-card-side jd-side-${side}`}>{side === 'a' ? '岗位 A' : '岗位 B'}{currentAnswer?.choice === side && <i className="jd-picked-tag">已选</i>}</span>
          <ul>{trial[side].map((phrase, i) => <li key={i}>{phrase}</li>)}</ul>
        </button>)}
      </div>
      <div className="jd-nav-row">
        <button className="button secondary" disabled={index === 0 || phase === 'submitting'} onClick={goPrev}>← 上一题</button>
        {index + 1 < trials.length && <button className="button secondary" disabled={!currentAnswer || phase === 'submitting'} onClick={goNext}>下一题 →</button>}
        {index + 1 === trials.length && currentAnswer && <>
          <span className="jd-nav-note">{allDone ? '全部答完，可返回检查' : '本题已答，还有未答题'}</span>
          <button className="button secondary" disabled={phase === 'submitting'} onClick={() => { setIndex(0); trialShownAt.current = performance.now() }}>从头检查</button>
          <Button className="button primary" busy={phase === 'submitting'} disabled={!allDone || phase === 'submitting'} onClick={submit}>交卷</Button>
        </>}
      </div>
      <p className="jd-hint jd-hint-center">{currentAnswer
        ? `当前已选岗位 ${currentAnswer.choice.toUpperCase()}，点卡片即修改`
        : '哪张更愿意投？凭直觉，点它。'}</p>
    </section>}

    {phase === 'report' && report && <section className="paper-panel jd-panel">
      <div className="section-heading"><div><h2>你的隐性筛选条件报告</h2>
        <p className="body-small muted">结论按共现校正后的净效应排序；选中率是原始统计，供交叉核对。</p></div>
        <Button className="button secondary" onClick={retake}>重新测评</Button></div>

      <div className={`jd-quality${report.quality.grade === 'weak' ? ' jd-quality-warn' : ''}`}
        title={report.quality.grade === 'ok' ? '注意力、重复一致性与作答速度三项质检全部通过' : '存在质检验证未通过的项，结论可信度打折'}>
        <b>{report.quality.grade === 'ok' ? '质检通过' : '质检有警示'}</b>
        <span>重复一致 {report.quality.anchor_agreement}/{report.quality.anchors} · 注意力{report.quality.attention_pass ? '通过' : '未通过'} · 有效 {report.quality.valid_trials}/{report.quality.rt_excluded + report.quality.valid_trials} 题</span>
        {report.quality.flags.length > 0 && <em>{report.quality.flags.join('；')}</em>}
      </div>

      <div className="jd-report-grid">
        <div className="jd-dims-card">
          <h3 className="jd-block-title"><i>1</i>维度倾向</h3>
          {report.dimensions.map(d => {
            const pos = sliderPos(d.weight)
            const color = DIM_COLORS[d.key] || '#5a8fd6'
            return <div key={d.key} className="jd-dim-row"
              title={`维度净效应 ${d.weight.toFixed(2)}（两条片段均值）：越靠右越常被你选中，越靠左越常被你跳过`}>
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
            return <div className="jd-pref-item" key={key} title={`含该片段的卡被选中 ${percent(f.rate_present)}，不含时 ${percent(f.rate_absent)}`}>
              <span className="jd-ico jd-ico-warn">!</span>
              <div><b>{f.screen}</b><span>含「{f.phrase}」的岗位，你选中率仅 {percent(f.rate_present)}（基准 {percent(f.rate_absent)}）。</span></div>
            </div>
          }) : <p className="jd-empty-line">{report.actions.avoid_line}</p>}
        </div>

        <div className="jd-pref-col jd-pref-card">
          <h3 className="jd-block-title"><i>3</i>下一轮优先看</h3>
          {report.actions.prefer.length > 0 ? report.actions.prefer.map(key => {
            const f = report.features.find(item => item.key === key)!
            return <div className="jd-pref-item" key={key} title={`含该片段的卡被选中 ${percent(f.rate_present)}，不含时 ${percent(f.rate_absent)}`}>
              <span className="jd-ico jd-ico-good">✓</span>
              <div><b>{f.screen}</b><span>含「{f.phrase}」的岗位，你选中率 {percent(f.rate_present)}（基准 {percent(f.rate_absent)}）。</span></div>
            </div>
          }) : <p className="jd-empty-line">{report.actions.prefer_line}</p>}
        </div>
      </div>

      <h3 className="jd-block-title jd-detail-title">全部片段一览</h3>
      <div className="jd-detail-grid">
        {report.features.map(f => {
          const tone = f.verdict === 'strong_like' ? 'jd-ico-strong' : f.verdict === 'like' ? 'jd-ico-good' : f.verdict === 'avoid' || f.verdict === 'strong_avoid' ? 'jd-ico-warn' : 'jd-ico-dot'
          const glyph = f.verdict === 'strong_like' || f.verdict === 'like' ? '✓' : f.verdict === 'avoid' || f.verdict === 'strong_avoid' ? '!' : f.verdict === 'uncertain' ? '?' : '·'
          return <div className="jd-detail-row" key={f.key}
            title={`含该片段的卡被选中 ${percent(f.rate_present)}，不含时 ${percent(f.rate_absent)}；净效应 ${f.weight.toFixed(2)}${f.low_confidence ? '；样本不足' : ''}`}>
            <span className={`jd-ico ${tone}`}>{glyph}</span>
            <div className="jd-detail-main"><b>{f.screen}</b><span>{f.phrase}</span></div>
            <span className="jd-rates">{percent(f.rate_present)} <i>vs</i> {percent(f.rate_absent)}</span>
          </div>
        })}
      </div>

      <p className="jd-note">{report.note}</p>
    </section>}
  </section>
}
