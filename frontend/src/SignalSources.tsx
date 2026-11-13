import { useCallback, useEffect, useRef, useState } from 'react'
import { collectCompanySignals, computeReliability, getReliabilityHistory, listSignalSources } from './api'
import type { ReliabilityHistory, ReliabilityReport, SignalSource, Task } from './api'
import { toast } from './toast'

export const signalTaskNames: Record<string, string> = {
  signal_jobscope_jobs: '本地岗位与证据', signal_gsxt: '公示系统（待验证）',
  signal_credit_china: '信用中国信号', signal_court_records: '裁判文书信号',
  signal_qcc: '企查查付费 API', signal_tyc: '天眼查付费 API',
  reliability_compute: '重算求职参考', qcc: '企查查公开网页', tyc: '天眼查公开网页',
}
export const signalStatusNames: Record<string, string> = {
  completed: '已完成', success: '已完成', partial: '部分完成', failed: '失败',
  running: '执行中', pending: '等待中', skipped: '已跳过', unsupported: '未接入',
  needs_verification: '需要验证', needs_login: '需要登录', backoff: '浏览器正忙', degraded: '已降级',
}

export function useSignalSourceSelection() {
  const [sources, setSources] = useState<Record<string, SignalSource> | null>(null)
  const [selected, setSelected] = useState<string[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const request = useRef(0)
  const load = useCallback(async () => {
    const token = ++request.current
    setLoading(true); setError('')
    try {
      const data = await listSignalSources()
      if (token !== request.current) return
      setSources(data.sources)
      // 每次刷新重新选免费已验证来源；付费 API 和待实测页面都由用户在本次操作中勾选。
      setSelected(Object.keys(data.sources).filter(id => data.sources[id].ready && data.sources[id].tier === 'free' && data.sources[id].status === 'available'))
    } catch (err) { if (token === request.current) setError((err as Error).message) }
    finally { if (token === request.current) setLoading(false) }
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
      <span><strong>{item.name}</strong><small>{!item.implemented ? '尚未接入' : !item.ready ? item.unavailable_reason : item.tier === 'paid' ? '付费 API · 本次手动选择，可能计费' : item.status === 'needs_verification' ? '待实测 · 手动尝试，可能需浏览器验证' : '本地统计 · 不联网'}</small></span>
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
    setSelected(ids => ids.filter(id => sources[id].tier !== 'paid'))
    setWorking(true); onBusyChange(true); setResults([])
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
      if (current()) setProgress(`所选来源已执行，求职参考已重算；各来源的结果见下方。${historyMessage}`)
      toast(`「${company}」信号采集已结束，求职参考已重算。${historyMessage || '可查看逐来源结果或采集记录。'}`)
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
    <p className="tiny-label">选中的来源会实际采集并保存可溯源信号，完成后重算。企查查 / 天眼查付费 API 与多平台检索的公开网页独立；每次采集需手动选择付费来源。</p>
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
