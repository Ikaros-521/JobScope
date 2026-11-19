import { useEffect, useState } from 'react'
import { readable } from './api'
import type { Analysis } from './api'
import { navigateTabs } from './tabNavigation'
import { Icon } from './Icon'

// 适配报告组件簇（2026-11-19 自 App.tsx 拆出）：报告本体、流式进度面板与各子视图。
// 数据与动作全部经 props 传入——这里只负责呈现与纯客户端的筛选排序。

const dimensionNames: Record<string, string> = { skill: '技能', skills: '技能', experience: '经历与职责', domain: '行业与业务', industry: '行业与业务', preference: '求职偏好', preferences: '求职偏好' }
const requirementNames: Record<string, string> = { met: '满足', matched: '满足', satisfied: '满足', partial: '部分满足', not_met: '未满足', unmet: '未满足', missing: '未满足', unknown: '待确认', confirmed: '满足' }
const ANALYSIS_STATUS_ORDER: Record<string, number> = { unmet: 0, partial: 1, unknown: 2, met: 3 }

// 评估适配流式运行的前端状态：阶段进展 + 已到达的映射条目（done 帧整体替换为正式报告）。
export type AnalyzeStageStatus = 'pending' | 'running' | 'done'
export type AnalyzeRunState = {
  id: string; startedAt: number
  extract: { status: AnalyzeStageStatus; total: number | null }
  match: { status: AnalyzeStageStatus; done: number; total: number | null }
  score: { status: AnalyzeStageStatus }
  items: Analysis['requirements']
}

export function AnalyzeProgress({ run, onCancel }: { run: AnalyzeRunState; onCancel: () => void }) {
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

export function AnalysisView({ analysis, onStatusChange, onOptimizeGaps }: { analysis: Analysis; onStatusChange: (quote: string, status: string) => void; onOptimizeGaps?: () => void }) {
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
      {analysisTab === 'basis' && <>
        <div className="analysis-columns"><AnalysisList title="已有依据" items={analysis.strengths} /><AnalysisList title="缺口与未知" items={analysis.gaps} /></div>
        {/* 报告→工坊闭环（P1）：读出缺口不止步于展示，带着未满足项直接进优化流程。 */}
        {(analysis.gaps?.length || analysis.requirements.some(item => item.status === 'unmet' || item.status === 'partial')) && onOptimizeGaps && (
          <div className="analysis-gap-actions">
            <button type="button" className="button secondary small" onClick={onOptimizeGaps}><Icon name="wand" size={14} />带着缺口去工坊优化</button>
            <span className="tiny-label">未满足 / 部分满足的要求与缺口清单，会作为优化重点说明带入工坊</span>
          </div>)}
      </>}
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
