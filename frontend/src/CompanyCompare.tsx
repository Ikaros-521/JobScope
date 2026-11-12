import { useEffect, useMemo, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { computeReliability, getCompareCompanies, getCompanyLayers, getReliability } from './api'
import type { CompareCompany, CompareLayer, CompareLayers, ReliabilityReport } from './api'
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
type LayerRow = CompareLayers & { company: string }

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
function layerKeyOf(layer: CompareLayer) { return `${layer.role}|${layer.experience}|${layer.education}` }
function layerLabel(layer: CompareLayer) { return `${layer.role} · ${layer.experience} · ${layer.education}` }

export function CompanyCompare() {
  const [query, setQuery] = useState('')
  const [suggestions, setSuggestions] = useState<CompareCompany[]>([])
  const [searching, setSearching] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const [highlight, setHighlight] = useState(0)
  const searchBox = useRef<HTMLDivElement>(null)
  const [selected, setSelected] = useState<Row[]>([])
  const [reports, setReports] = useState<Record<string, ReliabilityReport | null>>({})
  const [layers, setLayers] = useState<Record<string, LayerRow>>({})
  const [layerKey, setLayerKey] = useState('')
  const [computing, setComputing] = useState(false)
  const [progress, setProgress] = useState('')

  useEffect(() => {
    let active = true
    setSearching(true)
    const timer = window.setTimeout(() => {
      getCompareCompanies(query.trim())
        .then(data => { if (active) { setSuggestions(data.companies); setSearching(false) } })
        .catch(() => { if (active) { setSuggestions([]); setSearching(false) } })
    }, 250)
    return () => { active = false; clearTimeout(timer) }
  }, [query])

  // 点到检索框外部就收起浮层；mousedown 阶段判断，点击结果项时不会先被 blur 吃掉。
  useEffect(() => {
    const onDocMouseDown = (event: MouseEvent) => {
      if (searchBox.current && !searchBox.current.contains(event.target as Node)) setSearchOpen(false)
    }
    document.addEventListener('mousedown', onDocMouseDown)
    return () => document.removeEventListener('mousedown', onDocMouseDown)
  }, [])
  useEffect(() => { setHighlight(0) }, [suggestions])

  // 岗位分层概要随所选公司变化重取：薪资对比按 职类×经验×学历 分层，不依赖参考报告是否生成。
  useEffect(() => {
    if (!selected.length) { setLayers({}); return }
    let active = true
    const timer = window.setTimeout(() => {
      getCompanyLayers(selected.map(item => item.company))
        .then(data => { if (active) setLayers(Object.fromEntries(data.companies.map(row => [row.company, row]))) })
        .catch(() => { if (active) setLayers({}) })
    }, 300)
    return () => { active = false; clearTimeout(timer) }
  }, [selected])

  const rows = useMemo(() => selected.map(item => ({ ...item, report: reports[item.company] })), [selected, reports])

  // 加入对比的唯一入口：下拉点选或回车选高亮项。刻意不做「按原文加入」——
  // 横评的数据面是已保存岗位与报告，未登记的公司加了也是空行；等检索请求返回再选。
  function pick(name: string) {
    const trimmed = name.trim()
    if (!trimmed) return
    if (selected.some(item => item.company === trimmed)) { toast('这家公司已在对比列表里。'); return }
    if (selected.length >= MAX_COMPANIES) { toast(`一次最多横评 ${MAX_COMPANIES} 家，先移除几家再加入。`); return }
    setSelected(prev => [...prev, { company: trimmed }])
    setQuery(''); setSearchOpen(false)
    getReliability(trimmed)
      .then(({ report }) => setReports(prev => ({ ...prev, [trimmed]: report })))
      .catch(err => toast((err as Error).message, 'error'))
  }
  function submitSearch(event: FormEvent) {
    event.preventDefault()
    if (suggestions[highlight]) pick(suggestions[highlight].company)
  }
  function onSearchKeyDown(event: React.KeyboardEvent) {
    if (event.key === 'ArrowDown' && suggestions.length) { event.preventDefault(); setHighlight(index => (index + 1) % suggestions.length) }
    else if (event.key === 'ArrowUp' && suggestions.length) { event.preventDefault(); setHighlight(index => (index - 1 + suggestions.length) % suggestions.length) }
    else if (event.key === 'Enter') {
      // 显式处理回车：不依赖浏览器对表单的默认提交行为（合成事件/某些输入法下不会触发）。
      event.preventDefault()
      if (suggestions[highlight]) pick(suggestions[highlight].company)
    }
    else if (event.key === 'Escape') setSearchOpen(false)
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

  // ── 图表 option 全部由 rows/layers 推导；没有可用数据时面板退成一句提示，不画空坐标系。 ──
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

  // 岗位构成：公司级薪资中位数不可比的根源在职类构成不同——先把构成摆出来。
  const mixOption = useMemo<ChartOption | null>(() => {
    const entries = selected.map(item => ({ company: item.company, data: layers[item.company] }))
    if (!entries.some(entry => (entry.data?.total_jobs ?? 0) > 0)) return null
    const roles: string[] = []
    for (const entry of entries) for (const item of entry.data?.mix ?? []) if (!roles.includes(item.role)) roles.push(item.role)
    return {
      color: PALETTE,
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: { top: 0, textStyle: { fontSize: 11 }, type: 'scroll' },
      grid: { left: 8, right: 16, top: 30, bottom: 8, containLabel: true },
      xAxis: { type: 'category', data: entries.map(entry => shortName(entry.company)),
        axisLabel: { fontSize: 11, interval: 0, rotate: entries.length > 4 ? 15 : 0 } },
      yAxis: { type: 'value', name: '已存岗位', nameTextStyle: { fontSize: 11 }, axisLabel: { fontSize: 11 } },
      series: roles.map(role => ({
        name: role, type: 'bar', stack: 'jobs', barWidth: 26,
        data: entries.map(entry => entry.data?.mix.find(item => item.role === role)?.jobs ?? 0),
      })),
    }
  }, [selected, layers])

  // 同层薪资：选定 职类×经验×学历，比各公司该层报价中位数与同城同层对照；口径与报告同源同闸门。
  const layerOptions = useMemo(() => {
    const merged: Record<string, { key: string; label: string; total: number }> = {}
    for (const row of Object.values(layers)) {
      for (const item of row.layers) {
        const key = layerKeyOf(item)
        const entry = merged[key] ?? (merged[key] = { key, label: layerLabel(item), total: 0 })
        entry.total += item.company_jobs
      }
    }
    return Object.values(merged).sort((a, b) => b.total - a.total)
  }, [layers])
  const activeLayer = layerOptions.find(item => item.key === layerKey) ?? layerOptions[0]
  const layerPayRows = useMemo(() => {
    if (!activeLayer) return []
    return selected.map(item => {
      const row = layers[item.company]
      return {
        company: item.company, city: row?.city ?? '',
        layer: row?.layers.find(item => layerKeyOf(item) === activeLayer.key),
      }
    })
  }, [selected, layers, activeLayer])
  const layerPayOption = useMemo<ChartOption | null>(() => {
    const passing = layerPayRows.filter(row => row.layer?.gate === 'pass' && row.layer?.company_median)
    if (!passing.length) return null
    return {
      tooltip: {
        trigger: 'axis', axisPointer: { type: 'shadow' },
        formatter: (params: unknown) => {
          const list = Array.isArray(params) ? params as { dataIndex: number }[] : [params as { dataIndex: number }]
          const index = list[0]?.dataIndex ?? 0
          const row = passing[index]
          if (!row?.layer) return ''
          const d = row.layer
          return [
            `<b>${row.company}</b>（${row.city}）`,
            `本公司该层 ${fmt(d.company_median!)} 元（${d.company_jobs} 条）`,
            `同城同层 ${fmt(d.city_median!)} 元（${d.city_jobs} 条）`,
            `比值 ${d.ratio}`,
            '报价对比 ≠ 实际到手薪资',
          ].join('<br/>')
        },
      },
      legend: { top: 0, textStyle: { fontSize: 11 } },
      grid: { left: 8, right: 16, top: 30, bottom: 8, containLabel: true },
      xAxis: { type: 'category', data: passing.map(row => shortName(row.company)),
        axisLabel: { fontSize: 11, interval: 0, rotate: passing.length > 3 ? 18 : 0 } },
      yAxis: { type: 'value', name: '元/月', nameTextStyle: { fontSize: 11 }, axisLabel: { fontSize: 11 } },
      series: [
        { name: '本公司该层中位数', type: 'bar', barWidth: 16, data: passing.map(row => row.layer!.company_median!), itemStyle: { color: '#2f6f6f' } },
        { name: '同城同层中位数', type: 'bar', barWidth: 16, data: passing.map(row => row.layer!.city_median!), itemStyle: { color: '#c9b98a' } },
      ],
    }
  }, [layerPayRows])
  const layerGapNote = useMemo(() => {
    const parts = layerPayRows.filter(row => row.layer?.gate !== 'pass').map(row => {
      if (!row.layer) return `${row.company}（无此层岗位）`
      if (row.layer.gate === 'few_company') return `${row.company}（本公司 ${row.layer.company_jobs} 条，需 ≥3 条）`
      return `${row.company}（同城同层 ${row.layer.city_jobs} 条，需 ≥10 条）`
    })
    return parts.join(' · ')
  }, [layerPayRows])

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
    <section className="page-intro"><div><h1>数据分析 · 公司横评</h1><p>把几家公司放在一起看：六维参考、岗位构成、同层薪资与描述具体度。薪资对比按职类·经验·学历分层，全部来自本地已保存材料。</p></div><span className="revision-label">本地计算 · 不联网</span></section>

    <section className="paper-panel compare-controls">
      <div className="section-heading">
        <div><h2>选择对比公司</h2><p className="body-small muted">从已登记的公司里选 2-{MAX_COMPANIES} 家；分数由各家的已保存材料算出，维度覆盖与样本量可能不同。</p></div>
        <button type="button" className="button primary" disabled={!selected.length || computing} onClick={() => void recomputeAll()}>
          {computing ? <span className="spinner" /> : null}{computing && progress ? progress : '重新计算所选'}
        </button>
      </div>
      <form className="compare-add-row" onSubmit={submitSearch}>
        <div className="compare-search" ref={searchBox}>
          <input value={query} onChange={event => { setQuery(event.target.value); setSearchOpen(true) }}
                 onFocus={() => setSearchOpen(true)} onKeyDown={onSearchKeyDown}
                 placeholder="检索公司名，点选结果加入对比" maxLength={300} aria-label="检索公司名称"
                 aria-autocomplete="list" role="combobox" aria-expanded={searchOpen} />
          {searchOpen && suggestions.length > 0
            ? <div className="compare-search-panel" role="listbox">
                {suggestions.map((item, index) => {
                  const chosen = selected.some(row => row.company === item.company)
                  return <button type="button" role="option" aria-selected={index === highlight} key={item.company}
                                  className={`compare-search-item${index === highlight ? ' active' : ''}`}
                                  disabled={chosen}
                                  onMouseEnter={() => setHighlight(index)}
                                  onClick={() => pick(item.company)}>
                    <span className="compare-search-name">{item.company}</span>
                    <span className="compare-search-meta">
                      {item.jobs_count > 0 ? <>{item.jobs_count} 条岗位</> : <span className="muted">无岗位</span>}
                      {item.score != null
                        ? <span className="compare-search-score"> · 参考分 {num(item.score)} · 覆盖 {Math.round((item.confidence ?? 0) * 100)}%</span>
                        : <span className="muted"> · 未出分</span>}
                      {chosen ? <span className="compare-search-chosen">已加入</span> : null}
                    </span>
                  </button>
                })}
              </div>
            : null}
          {searchOpen && searching && query.trim() && suggestions.length === 0
            ? <div className="compare-search-panel compare-search-none">搜索中…</div>
            : null}
          {searchOpen && !searching && query.trim() && suggestions.length === 0
            ? <div className="compare-search-panel compare-search-none">没有匹配「{query.trim()}」的已登记公司。先在岗位研究里采集该公司的岗位，或等资料库登记后再来。</div>
            : null}
        </div>
        <button className="button secondary" type="submit" disabled={!suggestions[highlight]}>加入所选</button>
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
          <p className="body-small muted">在上方输入公司名加入对比（最多 {MAX_COMPANIES} 家）。候选来自公司资料库登记过的主体；从没算过参考分的公司会先显示「还没有参考报告」，加入后一键重算即可。薪资对比默认按岗位分层，公司级中位数只作构成参考。</p>
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
            <div className="compare-chart-title"><h3>岗位构成</h3><span className="tiny-label">职类分布不同，公司级薪资中位数不可直接比</span></div>
            {mixOption ? <EChart className="compare-chart" option={mixOption} /> : <p className="compare-chart-empty">所选公司还没有已保存岗位。</p>}
          </section>
          <section className="paper-panel compare-chart-panel">
            <div className="compare-chart-title">
              <h3>同层薪资对比</h3>
              {layerOptions.length
                ? <select className="compare-layer-select" value={activeLayer?.key ?? ''} aria-label="选择对比层级"
                          onChange={event => setLayerKey(event.target.value)}>
                    {layerOptions.map(item => <option key={item.key} value={item.key}>{item.label}（合计 {item.total} 条）</option>)}
                  </select>
                : null}
            </div>
            <span className="tiny-label">同职类·同经验·同学历的报价中位数；各公司只与各自城市的同层对照</span>
            {layerPayOption ? <EChart className="compare-chart" option={layerPayOption} /> : <p className="compare-chart-empty">{layerOptions.length ? '所选层级没有公司过闸（本公司 ≥3 条、同城同层 ≥10 条）。' : '所选公司还分不出可比层（岗位缺职类或经验/学历标注）。'}</p>}
            {layerGapNote ? <p className="tiny-label compare-layer-note">未入图：{layerGapNote}</p> : null}
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
                <th>岗位样本</th><th>完整JD</th><th>薪资比值（报告口径）</th><th>计算于</th>
              </tr></thead>
              <tbody>{rows.map(row => {
                const report = row.report
                const pay = findingData(report, 'pay_vs_city_median')
                const coverage = findingData(report, 'salary_quote_coverage')
                const jd = findingData(report, 'jd_specificity')
                const layersRow = layers[row.company]
                return <tr key={row.company}>
                  <td className="compare-table-company">{row.company}</td>
                  <td>{report == null ? (report === undefined ? '读取中' : '暂不出分')
                    : report.score == null ? <span className="muted">暂不出分</span>
                      : <b style={{ color: tone(report.score) }}>{num(report.score)}</b>}</td>
                  <td>{report ? `${Math.round(report.confidence * 100)}%` : '—'}</td>
                  {DIMENSIONS.map(([key]) => <td key={key}>{dimScore(report, key) == null ? <span className="muted">—</span> : num(dimScore(report, key) as number)}</td>)}
                  <td>{layersRow ? `${layersRow.parseable}/${layersRow.total_jobs}` : (coverage ? `${coverage.known ?? 0}/${coverage.total ?? 0}` : '—')}</td>
                  <td>{jd ? (jd.jobs ?? 0) : '—'}</td>
                  <td>{pay && (pay.ratio ?? 0) > 0 ? `比值 ${pay.ratio}` : '—'}</td>
                  <td>{report ? date(report.computed_at) : '未生成'}</td>
                </tr>})}</tbody>
            </table>
          </div>
        </section>

        <section className="paper-panel compare-disclaimer">
          <p className="tiny-label">对比图只呈现各公司本地已保存材料算出的主观参考值：维度覆盖低、样本量小的公司分数波动大（悬停图表可见样本数），图中 0 值维度表示「暂无依据」而不是负面信号。薪资对比按职类·经验·学历分层——不同职类构成的公司之间，公司级中位数不可直接比。这是阅读辅助，不是公司排名，也不构成对任何公司的评价或结论。</p>
        </section>
      </>}
  </div>
}
