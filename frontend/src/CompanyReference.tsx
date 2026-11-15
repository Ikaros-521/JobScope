import { date } from './api'
import type { ReliabilityDiff, ReliabilityFinding, ReliabilityHistory, ReliabilityReport } from './api'
import { toast } from './toast'
import { addCompareCompany } from './compareSelectionStore'
import { SignalCollector, signalStatusNames } from './SignalSources'

// 措辞只能来自后端 wording.py 的模板：这个组件只排版，不替公司写任何结论。
// 唯一允许自己写的文字是标签、按钮名和「这一步在做什么」的说明。

// 来源分类只说明「这条依据属于哪一类材料」，不指向任何具体个人。
// employee / media 的「未见独立核实」是后端加在正文里的，这里原样保留，不改写、不加强度。
const sourceNames: Record<string, string> = {
  official: '官方公开材料', paid_api: '付费接口材料', platform: '平台统计',
  user_material: '你提供的材料', employee: '个人自述', media: '媒体报道', llm: '模型判断',
}

// 缺口的行动入口按规则归属：本地规则点「盘点已有资料」，抽取规则点「提取资料数字」，
// 其余（官方登记类）只能靠你自己补材料。措辞与归属都是本项目自己的说明，不是对公司的结论。
const LOCAL_RULES = ['salary_quote_coverage', 'disclosure_available', 'pay_vs_city_median', 'jd_specificity', 'industry_media_count']
const EXTRACT_RULES = ['team_size_statement', 'disclosure_revenue_trend', 'disclosure_headcount_trend', 'employee_review_sample']

// 每个维度「怎么才能出分」的一句话通路说明：维度卡收起时就显示，别让「暂不出分」变成哑谜。
// 口径与后端 rubric 的闸门一致（limits.py：公司侧 1 条、同层基线 5 条），只讲「差什么、去哪补」。
const DIMENSION_CHANNELS: Record<string, string> = {
  stability: '补法：企查查/天眼查/爱企查公开页采司龄·参保·企业类型',
  pay: '补法：该公司 1 条报价即可比，需同城同层对照 ≥5 条（库里其他公司岗位自动充当，一次多平台检索就够）',
  operations: '补法：采集「爱企查公开页」自动读最近两期年报的人数/营收同比；或手动导入年报材料跑「提取资料数字」',
  people: '补法：公司页人员规模 + JD 团队自述凑 2 条（采集公开页 + 补全 JD）后跑「提取资料数字」',
  professional: '补法：补全该岗位完整描述（有 1 条评 1 条，样本量随句子展示）',
  industry: '补法：在「按来源补数据」手动勾选「AI 行业判断」',
}

// 按钮名只讲功能不讲实现；tip 是悬浮时「这一步在做什么」的完整说明，缺口行与底部按钮共用同一份。
const ACTION_INFO: Record<'local' | 'extract' | 'material', { name: string; tip: string }> = {
  local: {
    name: '盘点已有资料',
    tip: '用本项目已保存的岗位和材料在本地算一遍（不联网）：得出薪资统计、岗位描述分析等依据，并告诉你哪些维度材料够、哪些还缺。完成后自动重算参考分。',
  },
  extract: {
    name: '提取资料数字',
    tip: '从你补充的公告、公司资料里找出明确写着的数字（如团队规模、营收变化），补进打分依据。只认原文数字，不作猜测；完成后自动重算参考分。',
  },
  material: {
    name: '补资料',
    tip: '打开补充资料窗口，手动贴入公告、报告等材料；也可在「按来源补数据」中尝试官方页面或已配置的付费 API。',
  },
}

function gapAction(line: string): 'local' | 'extract' | 'material' {
  if (LOCAL_RULES.some(rule => line.includes(rule))) return 'local'
  if (EXTRACT_RULES.some(rule => line.includes(rule))) return 'extract'
  return 'material'
}

const gapActionNames: Record<'local' | 'extract' | 'material', string> = {
  local: ACTION_INFO.local.name, extract: ACTION_INFO.extract.name, material: ACTION_INFO.material.name,
}

// 单一强调色的连续色阶：只有同一色相的深浅，分数低不等于变红，也不出现警示标签。
function tone(score: number) {
  const clamped = Math.max(0, Math.min(100, score))
  return `hsl(168 32% ${Math.round(64 - (clamped / 100) * 38)}%)`
}

const number = (value: number) => value.toLocaleString('zh-CN', { maximumFractionDigits: 1 })

// 事实小图的取数声明：只画 data 里声明过的数字，不从句子反解析；material 与未声明的规则不出图。
// 条形只陈述事实（数字 + 口径 + 样本数），条长不做任何好坏暗示；没有数据就不画，不留空坐标轴。
const FACT_BARS: Record<string, { key: string; label: string; cap?: number }[]> = {
  pay_vs_city_median: [
    { key: 'company_median', label: '本公司同层中位数' },
    { key: 'city_median', label: '同城同层中位数' },
  ],
  jd_specificity: [
    { key: 'chars_mean', label: '描述均长（字）', cap: 600 },
    { key: 'items_mean', label: '职责条目均数', cap: 8 },
    { key: 'skills_mean', label: '技术栈命中均数', cap: 8 },
    { key: 'quantified_share', label: '量化条目占比', cap: 1 },
  ],
  disclosure_revenue_trend: [
    { key: 'previous', label: '上期营收' },
    { key: 'current', label: '本期营收' },
  ],
  disclosure_headcount_trend: [
    { key: 'previous', label: '上期在职人数' },
    { key: 'current', label: '本期在职人数' },
  ],
}

const FACT_CAPTIONS: Record<string, string> = {
  pay_vs_city_median: '单位：元/月；条长按两边较大值对齐。样本口径见上方句子。',
  jd_specificity: '满条口径：600 字 / 8 条职责 / 8 项技术栈 / 100% 量化。',
  disclosure_revenue_trend: '数字自带公告口径（万元、亿元等），本工具不换算；条长按两期较大值对齐。',
  disclosure_headcount_trend: '单位：人；条长按两期较大值对齐。人数变动有多种成因。',
}

function FactBars({ finding }: { finding: ReliabilityFinding }) {
  const spec = FACT_BARS[finding.rule]
  const data = finding.data
  if (finding.kind !== 'assessment' || !spec || !data) return null
  const rows = spec.map(item => ({ ...item, value: data[item.key] }))
  if (!rows.length || rows.some(row => typeof row.value !== 'number')) return null
  // 带 cap 的行按固定口径归一；没有 cap 的行（薪资、同比两端）按这组数字的较大值对齐。
  const scaled = spec.some(row => row.cap)
  const scale = Math.max(1e-9, ...rows.map(row => Math.abs(row.value as number)))
  return <div className="reliability-bars">
    {rows.map(row => {
      const value = row.value as number
      const fill = Math.max(0, Math.min(100, (scaled ? value / (row.cap as number) : Math.abs(value) / scale) * 100))
      return <div key={row.key} className="reliability-bar-row">
        <span className="reliability-bar-label">{row.label}</span>
        <span className="reliability-bar-track"><i style={{ width: `${fill}%`, background: tone(finding.score as number) }} /></span>
        <span className="reliability-bar-value">{number(value)}</span>
      </div>
    })}
    <span className="tiny-label">{FACT_CAPTIONS[finding.rule]}</span>
  </div>
}

// 历史趋势折线：综合分实线、维度覆盖虚线，都只来自这家公司自己的快照。
// 某次快照没出分就断线，不硬连两个不连续的点；只有一次计算时不画线。
function TrendChart({ history }: { history: ReliabilityHistory }) {
  const points = history.items.map(item => ({ score: item.score, coverage: Math.round(item.confidence * 100) }))
  if (points.length < 2) return null
  const width = 300
  const height = 96
  const padX = 10
  const padY = 10
  const x = (index: number) => padX + (index * (width - 2 * padX)) / (points.length - 1)
  const y = (value: number) => height - padY - (Math.max(0, Math.min(100, value)) / 100) * (height - 2 * padY)
  const segments: string[] = []
  let current: string[] = []
  points.forEach((point, index) => {
    if (point.score === null) {
      if (current.length > 1) segments.push(current.join(' '))
      current = []
      return
    }
    current.push(`${x(index)},${y(point.score)}`)
  })
  if (current.length > 1) segments.push(current.join(' '))
  const coveragePath = points.map((point, index) => `${x(index)},${y(point.coverage)}`).join(' ')
  const dots = points.map((point, index) => point.score === null ? null : { cx: x(index), cy: y(point.score) }).filter(Boolean) as { cx: number; cy: number }[]
  return <div className="reliability-trend-chart">
    <svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="综合参考分与维度覆盖的历史走势">
      <line x1={padX} y1={y(50)} x2={width - padX} y2={y(50)} className="reliability-trend-grid" />
      {segments.map((path, index) => <polyline key={index} points={path} fill="none" className="reliability-trend-score" />)}
      <polyline points={coveragePath} fill="none" className="reliability-trend-coverage" />
      {dots.map((dot, index) => <circle key={index} cx={dot.cx} cy={dot.cy} r={2.5} className="reliability-trend-dot" />)}
    </svg>
    <p className="tiny-label"><span className="reliability-legend-score">—</span> 综合参考分 · <span className="reliability-legend-coverage">‥</span> 维度覆盖（按权重）· 纵轴 0–100</p>
  </div>
}

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
    <FactBars finding={finding} />
  </li>
}

function Gap({ line, busy, onAction }: { line: string; busy: boolean; onAction: (action: 'local' | 'extract' | 'material') => void }) {
  const action = gapAction(line)
  return <li className="reliability-gap">
    <span>{line}</span>
    <button type="button" className="text-button" disabled={Boolean(busy)} title={ACTION_INFO[action].tip} onClick={() => onAction(action)}>
      {gapActionNames[action]}
    </button>
  </li>
}

function Trend({ history }: { history: ReliabilityHistory | null }) {
  if (!history || history.items.length === 0) return null
  return <details className="reliability-trend">
    <summary>这家公司的历史趋势（{history.items.length} 次计算）</summary>
    <p className="tiny-label">趋势只对比这家公司自己的历史快照；没有跨公司对比、排名或榜单。</p>
    <TrendChart history={history} />
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

export function CompanyReference({ report, history, busy, loading, action, company, onCompute, onCoverage, onExtract, onOpenEvidence, onOpenEvidenceModal, onOpenCompare, onSignalBusy, onSignalCollected, onConfigureSources, onAgentCollect }: {
  report: ReliabilityReport | null
  history: ReliabilityHistory | null
  busy: boolean
  loading?: boolean
  action: { label: string; status: string; message: string } | null
  company: string
  onCompute: () => void
  onCoverage: () => void
  onExtract: () => void
  onOpenEvidence: (evidenceId: string) => void
  onOpenEvidenceModal: () => void
  onOpenCompare?: () => void
  onSignalBusy: (busy: boolean) => void
  onSignalCollected: (report: ReliabilityReport, history: ReliabilityHistory | null) => void
  onConfigureSources: () => void
  // 把「按缺口补齐」的指令交给全局对话的 Agent：跳转 + 预填由外层（App）完成。
  onAgentCollect?: (prompt: string) => void
}) {
  const score = report?.score ?? null
  // 暂不出分的维度清单 + 给 Agent 的预填指令：缺口文案取各维度第一条 missing（去掉规则名前缀）。
  const unscored = (report?.dimensions ?? []).filter(item => item.score === null)
  const agentPrompt = unscored.length ? [
    `请帮我补齐「${company}」的求职参考数据。当前 ${unscored.length} 个维度暂不出分，缺口如下：`,
    ...unscored.map(item => {
      const reason = (item.missing[0] || '尚无来源').replace(/^[a-z_]+（/, '').replace(/）$/, '')
      const channel = (DIMENSION_CHANNELS[item.dimension] || '').replace('补法：', '')
      return `- ${item.label}：${reason}${channel ? '；' + channel : ''}`
    }),
    '请先用 company_profile 看缺口，再按需依次执行：collect_jobs 采集该公司岗位 → job_deep_read 补完整 JD → '
      + 'signal_collect 逐免费来源（jobscope_jobs/aiqicha_web/qcc_web/tyc_web）→ signal_extract 抽取 → reliability_refresh 重算。',
    '每步如实汇报：遇到 needs_login/needs_verification 停下来告诉我怎么处理（不重试不绕过）；'
      + '结束后说明哪些维度仍缺、还缺什么口径。分数保持「主观参考」口径。',
  ].join('\n') : ''
  const gapActionHandler = (kind: 'local' | 'extract' | 'material') => {
    if (kind === 'local') onCoverage()
    else if (kind === 'extract') onExtract()
    else onOpenEvidenceModal()
  }
  function joinCompare() {
    const result = addCompareCompany(company)
    if (result === 'exists') { toast('这家公司已在横评列表里。'); return }
    if (result === 'full') { toast('横评最多 8 家，先在数据分析页移除几家再加入。'); return }
    toast('已加入横评，可继续评估其他公司', 'notice', false, onOpenCompare ? { label: '去横评', onClick: onOpenCompare } : undefined)
  }
  return <section className="reliability-panel" aria-label="求职参考">
    <div className="section-heading">
      <div><span className="section-label">求职参考</span><h3>{report ? `${report.company} · 主观参考` : '主观参考'}</h3></div>
      <div className="purpose-heading-actions">
        <button type="button" className="button secondary small" disabled={!company.trim()} onClick={joinCompare}>加入横评</button>
        <button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={onCompute}>{busy ? '整理中…' : report ? '重新计算' : '生成参考'}</button>
      </div>
    </div>
    {/* 评估对象是公司主体，不是岗位/个人——不写明这一点，「稳定性」会被读成工作稳不稳定。 */}
    <p className="tiny-label">评估对象是「这家公司」本身：公司稳定性看成立年限、参保与涉诉，薪资待遇看其公开报价水平；都不是这份工作或你个人的稳定性。</p>

    {/* 没有综合分时不画空环、不摆占位大数字；读取中先说明数据在路上，别让慢请求被误读成「没生成过」。 */}
    {score === null
      ? <p className="reliability-empty"><strong>{loading ? '正在读取这家公司的参考…' : '依据不足，暂不出分'}</strong><span className="muted body-small">{loading ? '参考数据来自本地资料库，读取完成后会在这里展示。' : report ? report.summary : '这家公司的参考还没有生成过。生成只使用本项目已保存的信号，不会联网。'}</span></p>
      : <div className="reliability-score">
        <strong style={{ color: tone(score) }}>{number(score)}</strong>
        <span>综合参考分 · 维度覆盖 {Math.round((report?.confidence ?? 0) * 100)}%（按权重）</span>
        <p className="body-small">{report?.summary}</p>
      </div>}

    {action ? <p className="reliability-action" role="status">
      <span className="reliability-action-status">{signalStatusNames[action.status] || action.status}</span>
      <span>{action.label}：{action.message}</span>
    </p> : null}

    {/* 暂不出分必须自带原因与补法（用户反馈：只写「暂不出分」像哑谜）；Agent 入口把整份缺口交给全局对话协同补齐。 */}
    {unscored.length > 0 && report ? <div className="reliability-coach">
      <div>
        <strong>{unscored.length} 个维度暂不出分</strong>
        <ul>{unscored.map(item => <li key={item.dimension}><b>{item.label}</b>：{DIMENSION_CHANNELS[item.dimension] || '缺口与口径见卡片内说明'}</li>)}</ul>
      </div>
      {onAgentCollect ? <button type="button" className="button secondary small" disabled={Boolean(busy)}
        onClick={() => onAgentCollect(agentPrompt)}>让 Agent 协同补齐</button> : null}
    </div> : null}

    <div className="reliability-grid">{(report?.dimensions ?? []).map(item => (
      <details key={item.dimension} className="reliability-dimension">
        <summary>
          <span>{item.label}{item.score === null && DIMENSION_CHANNELS[item.dimension]
            ? <small className="reliability-gap-hint">{DIMENSION_CHANNELS[item.dimension]}</small> : null}</span>
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
      <button type="button" className="text-button" disabled={Boolean(busy)} title={ACTION_INFO.local.tip} onClick={onCoverage}>{ACTION_INFO.local.name}</button>
      <button type="button" className="text-button" disabled={Boolean(busy)} title={ACTION_INFO.extract.tip} onClick={onExtract}>{ACTION_INFO.extract.name}</button>
    </div>

    <SignalCollector company={company} disabled={busy} onBusyChange={onSignalBusy} onCollected={onSignalCollected} onConfigure={onConfigureSources} />

    {/* 免责与边界是常驻区块，不放进 hover 或折叠里：读到分数的人要同时读到限制。 */}
    {report ? <div className="reliability-legal">
      <p className="tiny-label">{report.disclaimer}</p>
      <ul>{report.limitations.map(line => <li key={line}>{line}</li>)}</ul>
      <p className="tiny-label">规则 {report.rubric_version} · 措辞 {report.wording_version} · 声明 {report.disclaimer_version} · 计算于 {date(report.computed_at)} · 输入 {report.input_hash.slice(0, 12)}</p>
      <Trend history={history} />
    </div> : <p className="reliability-legal tiny-label">生成后会在这里列出免责声明、限制条件与措辞版本；未生成的内容不会出现在页面上。</p>}
  </section>
}
