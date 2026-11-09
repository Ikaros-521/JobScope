import { date } from './api'
import type { ReliabilityFinding, ReliabilityReport } from './api'

// 措辞只能来自后端 wording.py 的模板：这个组件只排版，不替公司写任何结论。
// 唯一允许自己写的文字是标签、按钮名和「这一步在做什么」的说明。

// 来源分类只说明「这条依据属于哪一类材料」，不指向任何具体个人。
// employee / media 的「未见独立核实」是后端加在正文里的，这里原样保留，不改写、不加强度。
const sourceNames: Record<string, string> = {
  official: '官方公开材料', paid_api: '付费接口材料', platform: '平台统计',
  user_material: '你提供的材料', employee: '个人自述', media: '媒体报道',
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
      <button type="button" className="reliability-evidence-link" onClick={() => onOpenEvidence(finding.evidence_id)}>出处 {finding.evidence_id}</button>
    </span>
  </li>
}

export function CompanyReference({ report, busy, action, onCompute, onCoverage, onExtract, onOpenEvidence }: {
  report: ReliabilityReport | null
  busy: boolean
  action: { label: string; status: string; message: string } | null
  onCompute: () => void
  onCoverage: () => void
  onExtract: () => void
  onOpenEvidence: (evidenceId: string) => void
}) {
  const score = report?.score ?? null
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

    <div className="reliability-grid">{(report?.dimensions ?? []).map(item => <article key={item.dimension}>
      <header><span>{item.label}</span><b style={item.score === null ? undefined : { color: tone(item.score) }}>{item.score === null ? '暂不出分' : number(item.score)}</b></header>
      {item.score !== null ? <div className="reliability-meter" aria-hidden="true"><i style={{ width: `${Math.max(2, Math.min(100, item.score))}%`, background: tone(item.score) }} /></div> : null}
      {item.score !== null ? <p className="tiny-label">本维度依据覆盖 {Math.round(item.confidence * 100)}%</p> : null}
      {item.findings.length ? <ul>{item.findings.map(finding => <Finding key={`${finding.rule}-${finding.evidence_id}`} finding={finding} onOpenEvidence={onOpenEvidence} />)}</ul>
        : <p className="tiny-label">该维度暂无可展示的依据。</p>}
      {item.missing.length ? <p className="reliability-gaps"><span className="tiny-label">缺口</span>{item.missing.map(line => <span key={line}>{line}</span>)}</p> : null}
    </article>)}</div>

    <div className="reliability-actions">
      <button type="button" className="text-button" disabled={Boolean(busy)} onClick={onCoverage}>用已有材料算覆盖度</button>
      <button type="button" className="text-button" disabled={Boolean(busy)} onClick={onExtract}>从材料里抽取信号</button>
    </div>

    {/* 免责与边界是常驻区块，不放进 hover 或折叠里：读到分数的人要同时读到限制。 */}
    {report ? <div className="reliability-legal">
      <p className="tiny-label">{report.disclaimer}</p>
      <ul>{report.limitations.map(line => <li key={line}>{line}</li>)}</ul>
      <p className="tiny-label">规则 {report.rubric_version} · 措辞 {report.wording_version} · 声明 {report.disclaimer_version} · 计算于 {date(report.computed_at)} · 输入 {report.input_hash.slice(0, 12)}</p>
    </div> : <p className="reliability-legal tiny-label">生成后会在这里列出免责声明、限制条件与措辞版本；未生成的内容不会出现在页面上。</p>}
  </section>
}
