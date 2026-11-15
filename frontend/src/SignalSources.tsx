import { useCallback, useEffect, useRef, useState } from 'react'
import { collectCompanySignals, computeReliability, getReliabilityHistory, listSignalSources } from './api'
import type { ReliabilityHistory, ReliabilityReport, SignalSource, Task } from './api'
import { failedTaskToast, toast } from './toast'

export const signalTaskNames: Record<string, string> = {
  signal_jobscope_jobs: '本地岗位与证据', signal_gsxt: '公示系统（待验证）',
  signal_credit_china: '信用中国信号', signal_court_records: '裁判文书信号',
  signal_qcc: '企查查付费 API', signal_tyc: '天眼查付费 API',
  signal_qcc_web: '企查查公开页', signal_tyc_web: '天眼查公开页', signal_aiqicha_web: '爱企查公开页',
  signal_llm_industry: 'AI 行业判断',
  reliability_compute: '重算求职参考', qcc: '企查查公开网页', tyc: '天眼查公开网页',
}
export const signalStatusNames: Record<string, string> = {
  completed: '已完成', success: '已完成', partial: '部分完成', failed: '失败',
  running: '执行中', pending: '等待中', skipped: '已跳过', unsupported: '未接入',
  needs_verification: '需要验证', needs_login: '需要登录', backoff: '浏览器正忙', degraded: '已降级',
}
// 需要用户动手（登录/人工验证）或本轮干脆没采成的状态：当场红色长提示，不能被收尾的
// 「已重算」话术盖过去。partial 拿到了部分数据，留在逐来源列表里说明即可。
const ATTENTION_STATUSES = new Set(['needs_login', 'needs_verification', 'failed', 'backoff',
  'unsupported', 'skipped', 'degraded'])

// 勾选记忆：换岗位 / 切页面后不丢勾选（岗位详情的参考组件按 job.id 重挂载，每次重置会逼用户重勾）。
// 只记非付费来源；付费 API 保持「每次手动勾选、采完自动取消」的防误计费约定，不进记忆。
const SELECTION_STORAGE_KEY = 'jobscope.signal-selection-v1'

function readSavedSelection(): string[] | null {
  try {
    const raw = localStorage.getItem(SELECTION_STORAGE_KEY)
    if (!raw) return null
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed) || parsed.some(item => typeof item !== 'string')) return null
    return parsed
  } catch { return null }
}

export function useSignalSourceSelection() {
  const [sources, setSources] = useState<Record<string, SignalSource> | null>(null)
  const [selected, setSelectedState] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const request = useRef(0)
  // 付费与 manual_only（AI 行业判断，消耗模型额度）同待遇：不入勾选记忆、采完自动取消、默认不勾。
  const manualIds = useRef<Set<string>>(new Set())
  const load = useCallback(async () => {
    const token = ++request.current
    setLoading(true); setError('')
    try {
      const data = await listSignalSources()
      if (token !== request.current) return
      setSources(data.sources)
      manualIds.current = new Set(Object.keys(data.sources)
        .filter(id => data.sources[id].tier === 'paid' || data.sources[id].manual_only))
      // 有记忆就沿用上次勾选（只剔除已下线/不可用与付费/AI 项兜底）；第一次使用才默认全选免费可用来源。
      const saved = readSavedSelection()
      if (saved) setSelectedState(saved.filter(id => data.sources[id].ready && !manualIds.current.has(id)))
      else setSelectedState(Object.keys(data.sources).filter(id => data.sources[id].ready && data.sources[id].tier === 'free' && data.sources[id].status === 'available' && !data.sources[id].manual_only))
    } catch (err) { if (token === request.current) setError((err as Error).message) }
    finally { if (token === request.current) setLoading(false) }
  }, [])
  // 用户每次改动都写入记忆（付费与 AI 判断不入记忆）；存储不可用时静默退化为不记忆，不影响本次操作。
  const setSelected = useCallback((ids: string[]) => {
    setSelectedState(ids)
    try { localStorage.setItem(SELECTION_STORAGE_KEY, JSON.stringify(ids.filter(id => !manualIds.current.has(id)))) } catch { /* 忽略 */ }
  }, [])
  useEffect(() => { void load(); return () => { request.current++ } }, [load])
  return { sources, selected, setSelected, loading, error, load }
}

export function SignalSourceChoices({ sources, selected, onChange, disabled }: {
  sources: Record<string, SignalSource>; selected: string[]; onChange: (ids: string[]) => void; disabled?: boolean
}) {
  return <div className="signal-choices">
    {Object.entries(sources).map(([id, item]) => <label key={id} className={`signal-choice${!item.ready ? ' unavailable' : ''}`}>
      <input type="checkbox" checked={selected.includes(id)} disabled={disabled || !item.ready}
        onChange={event => onChange(event.target.checked ? [...selected, id] : selected.filter(value => value !== id))} />
      <span><strong>{item.name}</strong><small>{!item.implemented ? '尚未接入' : !item.ready ? item.unavailable_reason : item.manual_only ? 'AI 判断 · 手动选择，消耗模型额度' : item.tier === 'paid' ? '付费 API · 本次手动选择，可能计费' : item.status === 'needs_verification' ? '待实测 · 手动尝试，可能需浏览器验证' : '本地统计 · 不联网'}</small></span>
    </label>)}
  </div>
}

export function SignalTaskResults({ tasks }: { tasks: Task[] }) {
  if (!tasks.length) return null
  return <ul className="signal-results" aria-label="逐来源执行结果">
    {tasks.map(task => <li key={task.id}>
      <div><strong>{signalTaskNames[task.source] || task.source}</strong><span>{signalStatusNames[task.status] || task.status} · {task.count ?? 0} 条</span></div>
      <details><summary>{task.message ? task.message.length > 100 ? `${task.message.slice(0, 100)}…（展开说明）` : task.message : '等待执行'}</summary><p>{task.message || '等待执行'}</p></details>
    </li>)}
  </ul>
}

export function SignalCollector({ company, disabled, onBusyChange, onCollected, onConfigure }: {
  company: string; disabled: boolean; onBusyChange: (busy: boolean) => void;
  onCollected: (report: ReliabilityReport, history: ReliabilityHistory | null) => void; onConfigure: () => void
}) {
  const { sources, selected, setSelected, loading, error, load } = useSignalSourceSelection()
  const [working, setWorking] = useState(false)
  const [results, setResults] = useState<Task[]>([])
  const [progress, setProgress] = useState('')
  const generation = useRef(0)
  useEffect(() => { generation.current++; setResults([]); setProgress(''); return () => { generation.current++ } }, [company])
  async function collect() {
    if (working || disabled || !sources || !selected.length) return
    const token = generation.current
    const current = () => generation.current === token
    const queue = [...selected]
    // 采完取消付费与 AI 判断勾选（防下次误计费/误耗额度）；其余勾选保留并随 setSelected 写入记忆。
    setSelected(queue.filter(id => sources[id].tier !== 'paid' && !sources[id].manual_only))
    setWorking(true); onBusyChange(true); setResults([])
    const finished: Task[] = []
    try {
      for (const [index, id] of queue.entries()) {
        if (current()) setProgress(`采集中 ${index + 1}/${queue.length} · ${sources[id].name}`)
        let task: Task
        try {
          const result = await collectCompanySignals(company, id)
          task = result.task || { id: id, source: `signal_${id}`, query: company, status: result.status, message: result.message, count: result.signals.length, created_at: new Date().toISOString() }
        } catch (err) {
          task = { id, source: `signal_${id}`, query: company, status: 'failed', message: (err as Error).message, count: 0, created_at: new Date().toISOString() }
        }
        finished.push(task)
        if (ATTENTION_STATUSES.has(task.status)) failedTaskToast(task, sources[id].name)
        if (current()) setResults(previous => [...previous, task])
      }
      // 切页只停止界面更新；已经明确选定的队列与重算仍然完成，记录保存在后端。
      if (current()) setProgress('采集已结束，正在重算已有材料…')
      const report = await computeReliability(company)
      if (current()) onCollected(report, null)
      let historyMessage = ''
      try {
        const history = await getReliabilityHistory(company)
        if (current()) onCollected(report, history.history)
      } catch { historyMessage = '历史记录暂时读取失败，当前报告已更新。' }
      const stuck = finished.filter(task => ATTENTION_STATUSES.has(task.status)).length
      if (stuck) {
        const tail = `${stuck} 个来源未成功（需登录、人工验证或失败），处理方式见各来源结果。${historyMessage}`
        if (current()) setProgress(`所选来源已执行：${tail}求职参考已按已有材料重算。`)
        toast(`「${company}」${tail}`, 'error')
      } else {
        if (current()) setProgress(`所选来源已执行，求职参考已重算；各来源的结果见下方。${historyMessage}`)
        toast(`「${company}」信号采集已结束，求职参考已重算。${historyMessage || '可查看逐来源结果或采集记录。'}`)
      }
    } catch (err) {
      if (current()) setProgress(`采集后重算未完成：${(err as Error).message}`)
      toast(`「${company}」重算未完成：${(err as Error).message}`, 'error')
    } finally {
      if (current()) setWorking(false)
      onBusyChange(false)
    }
  }
  return <details className="signal-collection">
    <summary>按来源补数据</summary>
    <p className="tiny-label">选中的来源会实际采集并保存可溯源信号，完成后重算。勾选会跨岗位记住，换岗位无需重勾；付费 API 与 AI 行业判断为防误耗额度不记忆，仍每次手动勾选。企查查 / 天眼查付费 API 与多平台检索的公开网页独立。</p>
    {loading ? <p role="status" className="tiny-label">正在读取来源设置…</p> : error ? <p className="tiny-label" role="status">读取失败：{error}</p> : sources ? <SignalSourceChoices sources={sources} selected={selected} onChange={setSelected} disabled={working || disabled} /> : null}
    <div className="signal-collection-actions">
      <button className="button secondary small" type="button" disabled={working || disabled || loading || Boolean(error) || !selected.length} onClick={() => void collect()}>{working ? <span className="spinner" /> : null}{working ? '采集与重算中…' : '采集所选并重算'}</button>
      <button className="text-button" type="button" disabled={working || disabled || loading} onClick={() => void load()}>刷新来源</button>
      <button className="text-button" type="button" disabled={working || disabled} onClick={onConfigure}>配置来源与接口</button>
    </div>
    {progress ? <p className="tiny-label" role="status">{progress}</p> : null}
    <SignalTaskResults tasks={results} />
  </details>
}
