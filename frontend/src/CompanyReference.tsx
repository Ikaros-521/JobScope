import { date } from './api'
import type { ReliabilityDiff, ReliabilityFinding, ReliabilityHistory, ReliabilityReport } from './api'

// 措辞只能来自后端 wording.py 的模板：这个组件只排版，不替公司写任何结论。
// 唯一允许自己写的文字是标签、按钮名和「这一步在做什么」的说明。

// 来源分类只说明「这条依据属于哪一类材料」，不指向任何具体个人。
// employee / media 的「未见独立核实」是后端加在正文里的，这里原样保留，不改写、不加强度。
const sourceNames: Record<string, string> = {
  official: '官方公开材料', paid_api: '付费接口材料', platform: '平台统计',
  user_material: '你提供的材料', employee: '个人自述', media: '媒体报道',
}

// 缺口的行动入口按规则归属：本地规则点「本地重算」，抽取规则点「从材料抽取」，
// 其余（官方登记类）只能靠你自己补材料。措辞与归属都是本项目自己的说明，不是对公司的结论。
const LOCAL_RULES = ['salary_quote_coverage', 'disclosure_available', 'pay_vs_city_median', 'jd_specificity', 'industry_media_count']
const EXTRACT_RULES = ['team_size_statement', 'disclosure_revenue_trend', 'disclosure_headcount_trend', 'employee_review_sample']

function gapAction(line: string): 'local' | 'extract' | 'material' {
  if (LOCAL_RULES.some(rule => line.includes(rule))) return 'local'
  if (EXTRACT_RULES.some(rule => line.includes(rule))) return 'extract'
  return 'material'
}

const gapActionNames: Record<'local' | 'extract' | 'material', string> = {
  local: '本地重算', extract: '从材料抽取', material: '补资料',
}

// 单一强调色的连续色阶：只有同一色相的深浅，分数低不等于变红，也不出现警示标签。
function tone(score: number) {
  const clamped = Math.max(0, Math.min(100, score))
  return `hsl(168 32% ${Math.round(64 - (clamped / 100) * 38)}%)`
}

const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 1 })

function Finding({ finding, onOpenEvidence }: { finding: ReliabilityFinding; onOpenEvidence: (evidenceId: string) => void }) {
  return <li>
    <p>{finding.text}</p>
    <span className="reliability-finding-meta">
      {/* material 行只画文字与出处：score 在后端已是 null，那个数字量的是本项目存了几条材料，不是对公司的评价。 */}
      {finding.kind === 'assessment' && finding.score !== null
        ? <span className="reliability-finding-score" style={{ color: tone(finding.score) }}>依据分 {number(finding.score)}</span>
        : null}
      {finding.kind === 'assessment' ? <span className="tiny-label">{sourceNames[finding.source_class] || finding.source_class}</span> : null}
      {finding.collected_at ? <span className="tiny-label">采集于 {date(finding.collected_at)}</span> : null}
      <button type="button" className="reliability-evidence-link" onClick={() => onOpenEvidence(finding.evidence_id)}>出处 {finding.evidence_id}</button>
    </span>
  </li>
}

function Gap({ line, busy, onAction }: { line: string; busy: boolean; onAction: (action: 'local' | 'extract' | 'material') => void }) {
  const action = gapAction(line)
  return <li className="reliability-gap">
    <span>{line}</span>
    <button type="button" className="text-button" disabled={Boolean(busy)} onClick={() => onAction(action)}>
      {gapActionNames[action]}
    </button>
  </li>
}

function Trend({ history }: { history: ReliabilityHistory | null }) {
  if (!history || history.items.length === 0) return null
  return <details className="reliability-trend">
    <summary>这家公司的历史趋势（{history.items.length} 次计算）</summary>
    <p className="tiny-label">趋势只对比这家公司自己的历史快照；没有跨公司对比、排名或榜单。</p>
    <ol className="reliability-trend-items">
      {history.items.map(item => <li key={item.input_hash}>
        <span>{date(item.computed_at)}</span>
        <b>{item.score === null ? '未出分' : number(item.score)}</b>
        <span className="tiny-label">维度覆盖 {Math.round(item.confidence * 100)}%（按权重）</span>
      </li>)}
    </ol>
    {history.diffs.map(diff => <TrendDiff key={diff.after} diff={diff} />)}
  </details>
}

function TrendDiff({ diff }: { diff: ReliabilityDiff }) {
  const changed = diff.rules.length + diff.materials.length
  if (!changed) return null
  return <div className="reliability-trend-diff">
    <p className="tiny-label">{date(diff.before)} → {date(diff.after)}：{diff.score_before === null ? '未出分' : number(diff.score_before)} → {diff.score === null ? '未出分' : number(diff.score)}</p>
    <ul>
      {diff.rules.map(item => <li key={item.rule}>
        {item.rule}：{item.before === null ? '无依据' : number(item.before)} → {item.after === null ? '无依据' : number(item.after)}
      </li>)}
      {diff.materials.map(item => <li key={item.rule}>
        {item.rule}：{item.appeared ? '新增材料登记' : '登记移除'}
      </li>)}
    </ul>
  </div>
}

export function CompanyReference({ report, history, busy, action, onCompute, onCoverage, onExtract, onOpenEvidence, onOpenEvidenceModal }: {
  report: ReliabilityReport | null
  history: ReliabilityHistory | null
  busy: boolean
  action: { label: string; status: string; message: string } | null
  onCompute: () => void
  onCoverage: () => void
  onExtract: () => void
  onOpenEvidence: (evidenceId: string) => void
  onOpenEvidenceModal: () => void
}) {
  const score = report?.score ?? null
  const gapActionHandler = (kind: 'local' | 'extract' | 'material') => {
    if (kind === 'local') onCoverage()
    else if (kind === 'extract') onExtract()
    else onOpenEvidenceModal()
  }
  return <section className="reliability-panel" aria-label="求职参考">
    <div className="section-heading">
      <div><span className="section-label">求职参考</span><h3>{report ? `${report.company} · 主观参考` : '主观参考'}</h3></div>
      <button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={onCompute}>{busy ? '整理中…' : report ? '重新计算' : '生成参考'}</button>
    </div>

    {/* 没有综合分时不画空环、不摆占位大数字，直接说明依据不足并交给下面的缺口清单。 */}
    {score === null
      ? <p className="reliability-empty"><strong>依据不足，暂不出分</strong><span className="muted body-small">{report ? report.summary : '这家公司的参考还没有生成过。生成只使用本项目已保存的信号，不会联网。'}</span></p>
      : <div className="reliability-score">
        <strong style={{ color: tone(score) }}>{number(score)}</strong>
        <span>综合参考分 · 维度覆盖 {Math.round((report?.confidence ?? 0) * 100)}%（按权重）</span>
        <p className="body-small">{report?.summary}</p>
      </div>}

    {action ? <p className="reliability-action" role="status">
      <span className="reliability-action-status">{action.status}</span>
      <span>{action.label}：{action.message}</span>
    </p> : null}

    <div className="reliability-grid">{(report?.dimensions ?? []).map(item => (
      <details key={item.dimension} className="reliability-dimension">
        <summary>
          <span>{item.label}</span>
          <b style={item.score === null ? undefined : { color: tone(item.score) }}>{item.score === null ? '暂不出分' : number(item.score)}</b>
        </summary>
        {item.score !== null ? <div className="reliability-meter" aria-hidden="true"><i style={{ width: `${Math.max(2, Math.min(100, item.score))}%`, background: tone(item.score) }} /></div> : null}
        {item.score !== null ? <p className="tiny-label">本维度依据覆盖 {Math.round(item.confidence * 100)}%</p> : null}
        {item.findings.length
          ? <ul>{item.findings.map(finding => <Finding key={`${finding.rule}-${finding.evidence_id}`} finding={finding} onOpenEvidence={onOpenEvidence} />)}</ul>
          : <p className="tiny-label">该维度暂无可展示的依据。</p>}
        {item.missing.length ? <ul className="reliability-gaps">
          <li className="reliability-gap-title"><span className="tiny-label">缺口（每条都可以直接行动）</span></li>
          {item.missing.map(line => <Gap key={line} line={line} busy={Boolean(busy)} onAction={gapActionHandler} />)}
        </ul> : null}
      </details>
    ))}</div>

    <div className="reliability-actions">
      <button type="button" className="text-button" disabled={Boolean(busy)} onClick={onCoverage}>用已有材料算覆盖度</button>
      <button type="button" className="text-button" disabled={Boolean(busy)} onClick={onExtract}>从材料里抽取信号</button>
    </div>

    {/* 免责与边界是常驻区块，不放进 hover 或折叠里：读到分数的人要同时读到限制。 */}
    {report ? <div className="reliability-legal">
      <p className="tiny-label">{report.disclaimer}</p>
      <ul>{report.limitations.map(line => <li key={line}>{line}</li>)}</ul>
      <p className="tiny-label">规则 {report.rubric_version} · 措辞 {report.wording_version} · 声明 {report.disclaimer_version} · 计算于 {date(report.computed_at)} · 输入 {report.input_hash.slice(0, 12)}</p>
      <Trend history={history} />
    </div> : <p className="reliability-legal tiny-label">生成后会在这里列出免责声明、限制条件与措辞版本；未生成的内容不会出现在页面上。</p>}
  </section>
}
