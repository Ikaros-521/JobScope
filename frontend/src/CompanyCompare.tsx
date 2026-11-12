import { useEffect, useMemo, useState } from 'react'
import type { FormEvent } from 'react'
import { computeReliability, getCompareCompanies, getReliability } from './api'
import type { CompareCompany, ReliabilityReport } from './api'
import { toast } from './toast'
import { EChart } from './EChart'
import type { ChartOption } from './EChart'

// 六维的固定顺序与中文标签，与后端 rubric.DIMENSIONS 对齐；雷达图与明细表共用。
const DIMENSIONS: [key: string, label: string][] = [
  ['stability', '稳定性'], ['pay', '薪资待遇'], ['operations', '实际经营'],
  ['people', '人员情况'], ['professional', '专业性'], ['industry', '行业前景'],
]
// 最多 8 家：雷达图再多家就糊成一团，条形图也放不下。
const MAX_COMPANIES = 8
const PALETTE = ['#3b82f6', '#10b981', '#f59e0b', '#8b5cf6', '#ef4444', '#06b6d4', '#ec4899', '#84cc16']

type Row = { company: string; report?: ReliabilityReport | null }

function tone(score: number) {
  const clamped = Math.max(0, Math.min(100, score))
  return `hsl(168 32% ${Math.round(64 - (clamped / 100) * 38)}%)`
}
function shortName(name: string) { return name.length > 10 ? `${name.slice(0, 9)}…` : name }
function num(value: number) { return Math.round(value * 10) / 10 }
function fmt(value: number) { return Math.round(value).toLocaleString('zh-CN') }
function date(value: string) { return new Date(value).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }) }
function findingData(report: ReliabilityReport | null | undefined, rule: string): Record<string, number> | undefined {
  for (const dim of report?.dimensions ?? []) {
    const found = dim.findings.find(item => item.rule === rule)
    if (found?.data) return found.data
  }
  return undefined
}
function dimScore(report: ReliabilityReport | null | undefined, key: string) {
  return report?.dimensions.find(item => item.dimension === key)?.score ?? null
}

export function CompanyCompare() {
  const [query, setQuery] = useState('')
  const [suggestions, setSuggestions] = useState<CompareCompany[]>([])
  const [selected, setSelected] = useState<Row[]>([])
  const [reports, setReports] = useState<Record<string, ReliabilityReport | null>>({})
  const [computing, setComputing] = useState(false)
  const [progress, setProgress] = useState('')

  useEffect(() => {
    let active = true
    const timer = window.setTimeout(() => {
      getCompareCompanies(query.trim()).then(data => { if (active) setSuggestions(data.companies) }).catch(() => { if (active) setSuggestions([]) })
    }, 250)
    return () => { active = false; clearTimeout(timer) }
  }, [query])

  const rows = useMemo(() => selected.map(item => ({ ...item, report: reports[item.company] })), [selected, reports])

  async function addCompany(event: FormEvent) {
    event.preventDefault()
    const name = query.trim()
    if (!name) return
    if (selected.some(item => item.company === name)) { toast('这家公司已在对比列表里。'); return }
    if (selected.length >= MAX_COMPANIES) { toast(`一次最多横评 ${MAX_COMPANIES} 家，先移除几家再加入。`); return }
    setSelected(prev => [...prev, { company: name }])
    setQuery('')
    try {
      const { report } = await getReliability(name)
      setReports(prev => ({ ...prev, [name]: report }))
    } catch (err) { toast((err as Error).message, 'error') }
  }
  function removeCompany(name: string) {
    setSelected(prev => prev.filter(item => item.company !== name))
  }
  async function recomputeAll() {
    if (!selected.length || computing) return
    setComputing(true)
    const done: string[] = []
    const failed: string[] = []
    for (const [index, item] of selected.entries()) {
      setProgress(`重算中 ${index + 1}/${selected.length}`)
      try {
        const report = await computeReliability(item.company)
        setReports(prev => ({ ...prev, [item.company]: report }))
        done.push(item.company)
      } catch (err) { failed.push(`${item.company}：${(err as Error).message}`) }
    }
    setComputing(false); setProgress('')
    toast(failed.length ? `重算完成 ${done.length} 家，失败 ${failed.length} 家：${failed.join('；')}` : `已重新计算 ${done.length} 家，图表已更新。`)
    getCompareCompanies(query.trim()).then(data => setSuggestions(data.companies)).catch(() => {})
  }

  // 四张图的 option 全部由 rows 推导；没有可用数据时面板退成一句提示，不画空坐标系。
  const radarOption = useMemo<ChartOption | null>(() => {
    const usable = rows.filter(row => row.report?.dimensions.some(item => item.score != null))
    if (!usable.length) return null
    return {
      color: PALETTE,
      tooltip: {},
      legend: { bottom: 0, type: 'scroll', textStyle: { fontSize: 11 } },
      radar: {
        indicator: DIMENSIONS.map(([, label]) => ({ name: label, max: 100 })),
        radius: '58%', center: ['50%', '44%'], splitNumber: 4,
        axisName: { color: '#6b7280', fontSize: 11 },
      },
      series: [{
        type: 'radar', symbolSize: 3,
        data: usable.map(row => ({
          name: shortName(row.company),
          value: DIMENSIONS.map(([key]) => dimScore(row.report, key) ?? 0),
          lineStyle: { width: 2 }, areaStyle: { opacity: 0.07 },
        })),
      }],
    }
  }, [rows])

  const overallOption = useMemo<ChartOption | null>(() => {
    const scored = rows.filter(row => row.report?.score != null)
      .sort((a, b) => (b.report?.score ?? 0) - (a.report?.score ?? 0))
    if (!scored.length) return null
    return {
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: { top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 8, right: 40, top: 30, bottom: 8, containLabel: true },
      xAxis: { type: 'value', max: 100, axisLabel: { fontSize: 11 } },
      yAxis: { type: 'category', inverse: true, data: scored.map(row => shortName(row.company)), axisLabel: { fontSize: 11 } },
      series: [
        { name: '综合参考分', type: 'bar', barWidth: 14, data: scored.map(row => row.report!.score as number),
          itemStyle: { color: (param: { value: number }) => tone(param.value) },
          label: { show: true, position: 'right', fontSize: 11 } },
        { name: '维度覆盖 %（按权重）', type: 'bar', barWidth: 14,
          data: scored.map(row => Math.round((row.report?.confidence ?? 0) * 100)),
          itemStyle: { color: '#c7d2d8' }, label: { show: true, position: 'right', fontSize: 11, color: '#64748b' } },
      ],
    }
  }, [rows])

  const payRows = useMemo(() => rows
    .map(row => ({ company: row.company, data: findingData(row.report, 'pay_vs_city_median') }))
    .filter(row => row.data && (row.data.company_median ?? 0) > 0), [rows])
  const payOption = useMemo<ChartOption | null>(() => {
    if (!payRows.length) return null
    return {
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: (params: unknown) => {
          const list = Array.isArray(params) ? params as { dataIndex: number }[] : [params as { dataIndex: number }]
          const index = list[0]?.dataIndex ?? 0
          const row = payRows[index]
          if (!row?.data) return ''
          const d = row.data
          return [
            `<b>${row.company}</b>`,
            `公司报价中位数 ${fmt(d.company_median)} 元（${d.company_jobs} 条）`,
            `同城 ${fmt(d.city_median)} 元（${d.city_jobs} 条）`,
            `比值 ${d.ratio}`,
            '报价对比 ≠ 实际到手薪资',
          ].join('<br/>')
        },
      },
      legend: { top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 8, right: 16, top: 30, bottom: 8, containLabel: true },
      xAxis: { type: 'category', data: payRows.map(row => shortName(row.company)),
        axisLabel: { fontSize: 11, interval: 0, rotate: payRows.length > 3 ? 18 : 0 } },
      yAxis: { type: 'value', name: '元/月', nameTextStyle: { fontSize: 11 }, axisLabel: { fontSize: 11 } },
      series: [
        { name: '公司报价中位数', type: 'bar', barWidth: 16, data: payRows.map(row => row.data!.company_median), itemStyle: { color: '#2f6f6f' } },
        { name: '同城报价中位数', type: 'bar', barWidth: 16, data: payRows.map(row => row.data!.city_median), itemStyle: { color: '#c9b98a' } },
      ],
    }
  }, [payRows])

  const jdRows = useMemo(() => rows
    .map(row => ({ company: row.company, data: findingData(row.report, 'jd_specificity') }))
    .filter(row => row.data && (row.data.jobs ?? 0) > 0), [rows])
  const jdOption = useMemo<ChartOption | null>(() => {
    if (!jdRows.length) return null
    return {
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: (params: unknown) => {
          const list = Array.isArray(params) ? params as { dataIndex: number }[] : [params as { dataIndex: number }]
          const index = list[0]?.dataIndex ?? 0
          const row = jdRows[index]
          if (!row?.data) return ''
          const d = row.data
          return [
            `<b>${row.company}</b>`,
            `${d.jobs} 条完整岗位描述`,
            `平均 ${fmt(d.chars_mean)} 字 · 职责条目 ${num(d.items_mean)} 条`,
            `可识别技术栈 ${num(d.skills_mean)} 项 · 量化结果条目占比 ${Math.round((d.quantified_share ?? 0) * 100)}%`,
            '衡量描述具体度，不代表技术实力',
          ].join('<br/>')
        },
      },
      grid: { left: 8, right: 30, top: 24, bottom: 8, containLabel: true },
      xAxis: { type: 'category', data: jdRows.map(row => shortName(row.company)),
        axisLabel: { fontSize: 11, interval: 0, rotate: jdRows.length > 3 ? 18 : 0 } },
      yAxis: { type: 'value', name: '平均字数', nameTextStyle: { fontSize: 11 }, axisLabel: { fontSize: 11 } },
      series: [{ type: 'bar', barWidth: 20, data: jdRows.map(row => row.data!.chars_mean), itemStyle: { color: '#4a7ba6' },
        label: { show: true, position: 'top', fontSize: 11 } }],
    }
  }, [jdRows])

  return <div className="compare-page">
    <section className="page-intro"><div><h1>数据分析 · 公司横评</h1><p>把几家公司放在一起看：六维参考、薪资报价、岗位描述具体度，全部来自本地已保存材料。</p></div><span className="revision-label">本地计算 · 不联网</span></section>

    <section className="paper-panel compare-controls">
      <div className="section-heading">
        <div><h2>选择对比公司</h2><p className="body-small muted">从已登记的公司里选 2-{MAX_COMPANIES} 家；分数由各家的已保存材料算出，维度覆盖与样本量可能不同。</p></div>
        <button type="button" className="button primary" disabled={!selected.length || computing} onClick={() => void recomputeAll()}>
          {computing ? <span className="spinner" /> : null}{computing && progress ? progress : '重新计算所选'}
        </button>
      </div>
      <form className="compare-add-row" onSubmit={addCompany}>
        <input list="compare-companies" value={query} onChange={event => setQuery(event.target.value)}
               placeholder="输入公司名，从候选里选择" maxLength={300} aria-label="公司名称" />
        <datalist id="compare-companies">{suggestions.map(item => (
          <option key={item.company} value={item.company}>
            {item.score != null ? `参考分 ${num(item.score)} · 覆盖 ${Math.round((item.confidence ?? 0) * 100)}%` : '还没有参考报告'}
          </option>))}</datalist>
        <button className="button secondary" type="submit" disabled={!query.trim()}>加入对比</button>
      </form>
      {selected.length
        ? <div className="compare-chips">{selected.map(item => (
          <span key={item.company} className="compare-chip">
            <span className="compare-chip-status" data-state={reports[item.company] === undefined ? 'loading' : reports[item.company] ? 'ready' : 'missing'} />
            {item.company}
            <button type="button" aria-label={`移除 ${item.company}`} onClick={() => removeCompany(item.company)}>×</button>
          </span>))}</div>
        : <p className="tiny-label">还没有选择公司。加入后点「重新计算所选」会先自动刷新各家的本地统计——存过岗位就能出分。</p>}
    </section>

    {selected.length === 0
      ? <section className="paper-panel compare-empty">
          <h3>把几家公司放在一起看</h3>
          <p className="body-small muted">在上方输入公司名加入对比（最多 {MAX_COMPANIES} 家）。候选来自公司资料库登记过的主体；从没算过参考分的公司会先显示「还没有参考报告」，加入后一键重算即可。</p>
        </section>
      : <>
        <div className="compare-grid">
          <section className="paper-panel compare-chart-panel">
            <div className="compare-chart-title"><h3>六维参考对比</h3><span className="tiny-label">雷达 · 0 值表示该维度暂无依据，不代表负面信号</span></div>
            {radarOption ? <EChart className="compare-chart" option={radarOption} /> : <p className="compare-chart-empty">所选公司都还没有可绘制的维度分，先重算。</p>}
          </section>
          <section className="paper-panel compare-chart-panel">
            <div className="compare-chart-title"><h3>综合参考分与维度覆盖</h3><span className="tiny-label">排列只是阅读顺序；覆盖低的公司分数波动大</span></div>
            {overallOption ? <EChart className="compare-chart" option={overallOption} /> : <p className="compare-chart-empty">还没有公司出综合分，先重算。</p>}
          </section>
          <section className="paper-panel compare-chart-panel">
            <div className="compare-chart-title"><h3>薪资报价对比</h3><span className="tiny-label">招聘平台报价中位数，不是实际到手薪资</span></div>
            {payOption ? <EChart className="compare-chart" option={payOption} /> : <p className="compare-chart-empty">还没有公司过薪资样本闸门（公司 ≥3 条、同城 ≥20 条可解析报价）。</p>}
          </section>
          <section className="paper-panel compare-chart-panel">
            <div className="compare-chart-title"><h3>岗位描述具体度</h3><span className="tiny-label">只统计完整 JD；衡量讲得多具体，不是技术实力</span></div>
            {jdOption ? <EChart className="compare-chart" option={jdOption} /> : <p className="compare-chart-empty">还没有公司有完整岗位描述（列表摘要不参与评估）。</p>}
          </section>
        </div>

        <section className="paper-panel compare-table-panel">
          <div className="section-heading"><div><h2>明细对照</h2><p className="body-small muted">每格数字都可回溯：在岗位档案的求职参考里展开对应维度查看依据与出处。</p></div></div>
          <div className="compare-table-wrap">
            <table className="compare-table">
              <thead><tr>
                <th>公司</th><th>综合分</th><th>覆盖</th>
                {DIMENSIONS.map(([, label]) => <th key={label}>{label}</th>)}
                <th>岗位样本</th><th>完整JD</th><th>报价中位数（公司/同城）</th><th>计算于</th>
              </tr></thead>
              <tbody>{rows.map(row => {
                const report = row.report
                const pay = findingData(report, 'pay_vs_city_median')
                const coverage = findingData(report, 'salary_quote_coverage')
                const jd = findingData(report, 'jd_specificity')
                return <tr key={row.company}>
                  <td className="compare-table-company">{row.company}</td>
                  <td>{report == null ? (report === undefined ? '读取中' : '暂不出分')
                    : report.score == null ? <span className="muted">暂不出分</span>
                      : <b style={{ color: tone(report.score) }}>{num(report.score)}</b>}</td>
                  <td>{report ? `${Math.round(report.confidence * 100)}%` : '—'}</td>
                  {DIMENSIONS.map(([key]) => <td key={key}>{dimScore(report, key) == null ? <span className="muted">—</span> : num(dimScore(report, key) as number)}</td>)}
                  <td>{coverage ? `${coverage.known ?? 0}/${coverage.total ?? 0}` : '—'}</td>
                  <td>{jd ? (jd.jobs ?? 0) : '—'}</td>
                  <td>{pay && (pay.company_median ?? 0) > 0 ? `${fmt(pay.company_median)} / ${fmt(pay.city_median)}（${pay.ratio}）` : '—'}</td>
                  <td>{report ? date(report.computed_at) : '未生成'}</td>
                </tr>})}</tbody>
            </table>
          </div>
        </section>

        <section className="paper-panel compare-disclaimer">
          <p className="tiny-label">对比图只呈现各公司本地已保存材料算出的主观参考值：维度覆盖低、样本量小的公司分数波动大（悬停图表可见样本数），图中 0 值维度表示「暂无依据」而不是负面信号。这是阅读辅助，不是公司排名，也不构成对任何公司的评价或结论。</p>
        </section>
      </>}
  </div>
}
