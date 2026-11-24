import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { date, getWorkStyleTrials, submitWorkStyle } from './api'
import type {
  JdAssessmentCrossNote, JdAssessmentCrossState, StyleAnswer, StyleAssessmentItem, StyleItem,
  WorkStyleAxisReport, WorkStyleBand, WorkStylePole,
} from './api'
import { toast } from './toast'
import { Button } from './ui'
import { ConfirmDialog } from './ConfirmDialog'

// 工作风格卷（2026-11-24，第二种考卷）：49 句陈述按五级同意度作答（MBTI 式自陈题）→ 四轴剖面 + 逐轴三档 +
// 原型速记 + 与「JD 取舍卷」互证。计分在服务端确定性完成（无 LLM）。
// 报告的主结论是**逐轴分档**，不是类型名：连续特质切出来的「原型」重测只有约两成一致（后端 work_style.py
// 模块说明有仿真数字），而单个轴的方向是稳定的。所以原型名只作速记，版面以四轴剖面和逐轴解读为主，
// 重测时并排显示上次结论——把不稳定性摆在明面上，而不是让它看起来像「测得不准」。
// 交互：点选项后停 150ms 让人看见自己的选择再自动翻页，翻页后锁 320ms 输入（连点不会误选下一句同一位置
// 的选项）；可返回修改，回到已答的句子上重选即覆盖，反应时从这次重看起算。

const ADVANCE_DELAY = 150
const LOCK_MS = 320
const AXIS_COLORS: Record<string, string> = { focus: '#3aa8a0', thinking: '#5a8fd6', deciding: '#8b6fc0', rhythm: '#e8a33d' }
const BAND_TEXT: Record<WorkStyleBand, string> = { clear: '明显', slight: '略偏', even: '不明显' }

type Phase = 'test' | 'submitting'    // 其余时候：没有报告显示引导页，有报告显示报告

type Props = {
  ready: boolean                         // 历史已加载（此前不渲染，免得先闪引导页再跳报告）
  latest: StyleAssessmentItem | null
  previous: StyleAssessmentItem | null   // 上一次的报告，重测对比用
  cross: JdAssessmentCrossState | null   // null = 后端没有互证接口
  onSubmitted: (item: StyleAssessmentItem) => void
  onBusy: (busy: boolean) => void
  onSwitchPaper: () => void
}

function axisState(axis: WorkStyleAxisReport) {
  return axis.band === 'even' || !axis.pole_name ? '不明显' : `${axis.pole_name}（${axis.band_label}）`
}

// 重测对比：逐轴比上一次的结论（两极相同、或两次都不明显算一致），挂在类型卡的「上次」一行上。
function compareWithPrevious(previous: StyleAssessmentItem | null, current: StyleAssessmentItem | null) {
  if (!previous || !current) return null
  const before = previous.payload.report, now = current.payload.report
  const changes = now.axes.map(axis => {
    const old = before.axes.find(item => item.key === axis.key)
    const same = Boolean(old) && (old!.band === 'even') === (axis.band === 'even') && old!.pole === axis.pole
    return { label: axis.label, same, text: `${axis.label}${same ? '一致' : '变化'}：${old ? axisState(old) : '—'} → ${axisState(axis)}` }
  })
  return {
    before: before.type, same: changes.filter(change => change.same).length, total: changes.length,
    detail: changes.map(change => change.text).join('\n'),
  }
}

export function WorkStylePaper({ ready, latest, previous, cross, onSubmitted, onBusy, onSwitchPaper }: Props) {
  const [phase, setPhase] = useState<Phase | null>(null)
  const [items, setItems] = useState<StyleItem[]>([])
  const [scale, setScale] = useState<{ value: number; label: string }[]>([])
  const [index, setIndex] = useState(0)
  // 按题序存全量作答；返回修改只覆盖对应槽位，反应时按最终选择的重看时间重计。
  const [answers, setAnswers] = useState<(StyleAnswer | null)[]>([])
  const [confirmRetake, setConfirmRetake] = useState(false)
  const shownAt = useRef(0)
  const lockUntil = useRef(0)
  const timer = useRef<number | undefined>(undefined)
  // 会话种子：开卷时随机生成，交卷回传同值；重测换种子 = 换一张卷（题序不同，题目相同）。
  const seedRef = useRef(-1)

  // 两卷都常驻挂载，所以只在历史就绪后取一次卷。
  useEffect(() => {
    if (!ready) return
    seedRef.current = Math.floor(Math.random() * 2147483647)
    getWorkStyleTrials(seedRef.current)
      .then(data => { setItems(Array.isArray(data.items) ? data.items : []); setScale(data.scale || []) })
      .catch(() => setItems([]))                 // 前端已更新而后端没重启：没有这个接口
  }, [ready])

  const testing = phase !== null
  useEffect(() => { onBusy(testing) }, [testing, onBusy])
  useEffect(() => () => window.clearTimeout(timer.current), [])

  const answered = answers.filter(Boolean) as StyleAnswer[]
  const allDone = items.length > 0 && answered.length === items.length
  const current = answers[index] || null

  function show(position: number) {
    window.clearTimeout(timer.current)
    lockUntil.current = performance.now() + LOCK_MS
    shownAt.current = performance.now()
    setIndex(position)
  }

  function pick(value: number) {
    const item = items[index]
    if (phase !== 'test' || !item) return
    const now = performance.now()
    if (now < lockUntil.current) return
    setAnswers(prev => {
      const copy = [...prev]
      copy[index] = { item_id: item.id, value, rt_ms: Math.max(0, Math.round(now - shownAt.current)) }
      return copy
    })
    if (index + 1 < items.length) {              // 最后一句不翻页，停在那里等交卷
      lockUntil.current = now + ADVANCE_DELAY + LOCK_MS
      window.clearTimeout(timer.current)
      timer.current = window.setTimeout(() => show(index + 1), ADVANCE_DELAY)
    }
  }

  // 键盘作答（1–5）：按住不放的重复触发不认，否则一句答完会顺势答掉下一句。
  useEffect(() => {
    if (phase !== 'test') return
    const onKey = (event: KeyboardEvent) => {
      if (event.repeat || event.ctrlKey || event.metaKey || event.altKey) return
      if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return
      if (['1', '2', '3', '4', '5'].includes(event.key)) { event.preventDefault(); pick(Number(event.key) - 1) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  function startTest() {
    setAnswers([]); setPhase('test'); show(0)
  }

  function retake() {
    setConfirmRetake(false)
    seedRef.current = Math.floor(Math.random() * 2147483647)
    getWorkStyleTrials(seedRef.current)
      .then(data => {
        if (!data.items?.length) { toast('后端还在运行旧版测评接口，请重启 JobScope 后端后刷新页面', 'error'); return }
        setItems(data.items); setScale(data.scale || scale); startTest()
      })
      .catch((err: Error) => toast(err.message, 'error'))
  }

  function submit() {
    if (phase !== 'test' || !allDone) return
    setPhase('submitting')
    submitWorkStyle(answered, seedRef.current)
      .then(item => { onSubmitted(item); setPhase(null); toast('测评完成，报告已保存在本机', 'notice') })
      .catch((err: Error) => { toast(err.message, 'error'); setPhase('test') })
  }

  if (!ready) return null
  const report = latest?.payload.report
  const item = items[index]
  // 作答中不显示「上次测评」，免得误会成当前结果
  const heading = report && !testing
    ? `上次测评 ${date(latest!.created_at)} · ${report.quality.grade === 'ok' ? '认真度检查通过' : '认真度检查有警示'}`
    : '约 4 分钟 · 结果只存本机'

  return <>
    <section className="page-intro compact"><h1 className="sr-only">工作风格测评</h1>
      <span className="revision-label">{heading}</span></section>

    {testing && item && <section className="paper-panel jd-panel">
      <div className="jd-progress-row"><span className="jd-count">{answered.length} / {items.length}</span>
        <div className="jd-progress" role="progressbar" aria-valuenow={answered.length} aria-valuemin={0} aria-valuemax={items.length}><i style={{ width: `${(answered.length / items.length) * 100}%` }} /></div>
        <span className="jd-hint">数字键 1–5 可作答 · 可返回修改</span></div>
      <p className="ws-statement">{item.text}</p>
      <div className="ws-scale" aria-label="同意程度">
        {scale.map(step => <button key={step.value} type="button" aria-pressed={current?.value === step.value}
          className={`ws-scale-step${current?.value === step.value ? ' is-on' : ''}`}
          disabled={phase === 'submitting'} onClick={() => pick(step.value)}>
          <i className="ws-scale-key">{step.value + 1}</i><span>{step.label}</span>
        </button>)}
      </div>
      <div className="jd-nav-row">
        <button className="button secondary" disabled={index === 0 || phase === 'submitting'} onClick={() => show(index - 1)}>← 上一句</button>
        {index + 1 < items.length && <button className="button secondary" disabled={!current || phase === 'submitting'} onClick={() => show(index + 1)}>下一句 →</button>}
        {index + 1 === items.length && <>
          <span className="jd-nav-note">{allDone ? '全部答完，可返回检查' : current ? '本句已答，还有未答题' : '答完本句后可交卷'}</span>
          <button className="button secondary" disabled={phase === 'submitting'} onClick={() => show(0)}>从头检查</button>
          <Button className="button primary" busy={phase === 'submitting'} disabled={!allDone || phase === 'submitting'} onClick={submit}>交卷</Button>
        </>}
      </div>
    </section>}

    {!testing && (report
      ? <StyleReport item={latest!} previous={previous} cross={cross} onRetake={() => setConfirmRetake(true)} onSwitchPaper={onSwitchPaper} />
      : <section className="paper-panel jd-panel">
        <div className="section-heading"><div><h2>用 49 句陈述，画出你的工作风格剖面</h2>
          <p className="body-small muted">每句是一句对工作方式的描述，按「非常不同意 — 非常同意」选一个。没有对错，按你真实的样子选，不用想成理想中的自己。做完得到一份工作风格报告：四根轴上你偏向哪一极、每极的优势和要留意的地方、找工作时该找和该慎的信号，并和「JD 取舍卷」的结论互证。</p></div></div>
        <ul className="jd-intro-list">
          <li><b>{items.length || 49} 句</b>，约 4 分钟，凭第一反应选。</li>
          <li>点选项后自动跳下一句；随时「← 上一句」改，答完可从头检查再交卷，数字键 1–5 也可以作答。中途离开即作废。</li>
          <li>报告以<b>逐轴结论</b>为准：「原型名」只是最强的两个倾向的速记，重测时可能换；每个轴的方向是稳定的。</li>
        </ul>
        {!items.length
          ? <p className="jd-empty-line">题目没取到：后端可能还在运行旧版接口，请重启 JobScope 后端后刷新页面。</p>
          : <div className="jd-intro-actions">
            <Button className="button primary" onClick={startTest}>开始测评</Button>
          </div>}
      </section>)}
    {confirmRetake && <ConfirmDialog title="重新测评" message="新报告完成后会取代当前展示，历史仍保留在数据库。"
      confirmLabel="开始重测" onConfirm={retake} onCancel={() => setConfirmRetake(false)} />}
  </>
}

function StyleReport({ item, previous, cross, onRetake, onSwitchPaper }: {
  item: StyleAssessmentItem; previous: StyleAssessmentItem | null; cross: JdAssessmentCrossState | null
  onRetake: () => void; onSwitchPaper: () => void
}) {
  const report = item.payload.report
  const quality = report.quality
  const type = report.type
  const comparison = compareWithPrevious(previous, item)
  const typedKeys = new Set(report.poles.map(pole => pole.key))
  const evenAxes = report.axes.filter(axis => !axis.pole || !typedKeys.has(axis.pole))
  const bandOf = (band: WorkStyleBand, name?: string | null) => band === 'even' || !name
    ? BAND_TEXT.even : `${BAND_TEXT[band]} ${name}`
  const shorthand = type.name !== type.headline && type.poles.length > 0

  return <section className="paper-panel jd-panel">
    <div className="section-heading"><div><h2>你的工作风格报告</h2>
      <p className="body-small muted">四根轴各自分档：「明显」重测很稳定，「略偏」方向大概率正确但重测可能归为不明显。原型名只是最强的两个倾向的速记。</p></div>
      <Button className="button secondary" onClick={onRetake}>重新测评</Button></div>

    <div className={`jd-quality${quality.grade === 'weak' ? ' jd-quality-warn' : ''}`}
      title={quality.grade === 'ok' ? '注意力校验与作答节奏两项检查通过' : '存在未通过的检查项，结论可信度打折'}>
      <b>{quality.grade === 'ok' ? '认真度检查通过' : '认真度检查有警示'}</b>
      <span>注意力{quality.attention_pass ? '通过' : '未通过'} · 过快 {quality.fast_items} 句 · 共 {quality.items} 句</span>
      {quality.flags.length > 0 && <em>{quality.flags.join('；')}</em>}
    </div>

    <div className="ws-type-card">
      <span className="ws-kicker">你的工作风格剖面</span>
      <h3 className="ws-headline">{type.headline}</h3>
      {shorthand && <p className="ws-shorthand">速记：最接近「<b>{type.name}</b>」</p>}
      {type.rank.length > 0 && <p className="ws-rank">
        {type.rank.map(rank => <span key={rank.pole} className={`ws-rank-chip ws-band-${rank.band}`}>{rank.name}<em>{BAND_TEXT[rank.band]}</em></span>)}
      </p>}
      <p className="ws-essence">{type.essence}</p>
      <dl className="ws-fit">
        <div><dt>常见对口的工作形态</dt><dd>{type.fits}</dd></div>
        <div><dt>容易内耗的地方</dt><dd>{type.drains}</dd></div>
      </dl>
      {comparison && <p className="ws-retake" title={comparison.detail}>
        上次（{date(previous!.created_at)}）：最接近「{comparison.before.name}」 · {comparison.total} 轴中 {comparison.same} 轴结论一致
      </p>}
    </div>

    <h3 className="jd-block-title"><i>1</i>四轴剖面</h3>
    <div className="ws-axes">
      {report.axes.map(axis => {
        const pos = Math.round(Math.min(96, Math.max(4, 50 + axis.d * 23)))
        const color = AXIS_COLORS[axis.key] || '#5a8fd6'
        return <div key={axis.key} className="ws-axis"
          title={`${axis.question}　轴分 ${axis.d.toFixed(2)}：平均比中线向${axis.d >= 0 ? '右' : '左'}偏离 ${Math.abs(axis.d).toFixed(2)} 格（标准误 ${axis.se.toFixed(2)}）`}>
          <div className="ws-axis-head"><span className="ws-axis-label">{axis.label}</span>
            <span className={`ws-band ws-band-${axis.band}`}>{bandOf(axis.band, axis.pole_name)}</span></div>
          <div className="ws-slider"><i className="ws-slider-mid" />
            {axis.d !== 0 && <i className="ws-slider-fill" style={axis.d > 0 ? { left: '50%', width: `${pos - 50}%`, background: color } : { left: `${pos}%`, width: `${50 - pos}%`, background: color }} />}
            <i className="ws-slider-thumb" style={{ left: `${pos}%`, borderColor: color }} />
          </div>
          <div className="ws-axis-poles">
            <span className={axis.pole === axis.pole_a ? 'is-on' : ''}>{axis.pole_a_name}</span>
            <span className={axis.pole === axis.pole_b ? 'is-on' : ''}>{axis.pole_b_name}</span>
          </div>
        </div>
      })}
    </div>

    <h3 className="jd-block-title"><i>2</i>逐轴解读</h3>
    <div className="ws-pole-grid">
      {report.poles.map(pole => <PoleCard key={pole.key} pole={pole} axis={report.axes.find(axis => axis.pole === pole.key)} />)}
      {evenAxes.map(axis => <div key={axis.key} className="ws-axis-even"><b>{axis.label}</b>：没有明显偏向，两种风格你都会用，看情境切换。</div>)}
    </div>

    <AssessmentCrossSection cross={cross} side="style" step={3} onGo={onSwitchPaper} />

    <p className="jd-note">{report.note}</p>
  </section>
}

function PoleCard({ pole, axis }: { pole: WorkStylePole; axis?: WorkStyleAxisReport }) {
  return <article className="ws-pole-card">
    <header className="ws-pole-head"><b>{pole.name}</b>
      <span className={`ws-band ws-band-${pole.band}`}>{BAND_TEXT[pole.band]}</span>
      {axis && <span className="ws-axis-tag">{axis.label}</span>}
    </header>
    <p className="ws-tagline">{pole.tagline}</p>
    <div className="ws-pole-cols">
      <div><h4>你的优势</h4><ul>{pole.strengths.map(text => <li key={text}>{text}</li>)}</ul></div>
      <div><h4>留意</h4><ul>{pole.watch_outs.map(text => <li key={text}>{text}</li>)}</ul></div>
    </div>
    <div className="ws-cues">
      <span className="ws-cue-label is-seek" title="JD 里出现这些说法，通常和这一极合得来">找</span>
      {pole.seek.map(text => <span key={text} className="ws-cue">{text}</span>)}
      <span className="ws-cue-label is-caution" title="JD 里出现这些说法，要想想会不会和这一极冲突">慎</span>
      {pole.caution.map(text => <span key={text} className="ws-cue is-caution">{text}</span>)}
    </div>
  </article>
}

// 两卷互证区块，两份报告共用：风格卷是第 3 块，取舍卷是第 4 块。后端没有互证接口（旧后端）时整块不渲染。
export function AssessmentCrossSection({ cross, side, step, onGo }: {
  cross: JdAssessmentCrossState | null; side: 'bws' | 'style'; step: number; onGo: () => void
}) {
  if (!cross) return null
  const otherName = side === 'bws' ? '工作风格卷' : 'JD 取舍卷'
  let body: ReactNode
  if (cross.cross) {
    body = cross.cross.notes.length === 0
      ? <p className="jd-empty-line">{cross.cross.summary}</p>
      : <>
        <p className="jd-empty-line">{cross.cross.summary}</p>
        <ul className="ws-cross-list">
          {cross.cross.notes.map((note, i) => <CrossNote key={`${note.style_pole}-${note.bws_key}-${i}`} note={note} />)}
        </ul>
      </>
  } else if (cross.legacy.includes(side === 'bws' ? 'style' : 'bws')) {
    body = <p className="jd-empty-line">「{otherName}」那份还是旧版题型的报告，重新测评后才能互证。
      <Button className="button secondary small" onClick={onGo}>去{otherName}</Button></p>
  } else {
    body = <div className="ws-cross-cta">
      <p>做完「{otherName}」后，这里会给出两卷结论的互证：哪些互相印证、哪些需要权衡。</p>
      <Button className="button secondary small" onClick={onGo}>去做{otherName}</Button>
    </div>
  }
  return <>
    <h3 className="jd-block-title"><i>{step}</i>与「{otherName}」互证</h3>
    {body}
  </>
}

function CrossNote({ note }: { note: JdAssessmentCrossNote }) {
  const match = note.kind === 'match'
  return <li className={`ws-cross-note${match ? ' is-match' : ' is-tradeoff'}`}>
    <span className={`jd-ico ${match ? 'jd-ico-good' : 'jd-ico-warn'}`}>{match ? '✓' : '↔'}</span>
    <div>
      <p><b>{note.kind_label}</b>　{note.text}</p>
      <span className="ws-cross-evidence">风格卷：{note.style_name}（{BAND_TEXT[note.style_band]}） · 取舍卷：「{note.bws_phrase}」出现 {note.bws_n} 次，{note.bws_worst} 次选最不想投、{note.bws_best} 次选最想投</span>
    </div>
  </li>
}
