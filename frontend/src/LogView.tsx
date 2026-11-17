import { useCallback, useEffect, useRef, useState } from 'react'
import type { ChangeEvent, FormEvent, UIEvent } from 'react'
import { getLogs } from './api'
import type { LogEntry } from './api'
import { Icon } from './Icon'

// 运行日志页：轮询后端进程内的环形缓冲，与控制台同源的记录就地可查，不必切回后端窗口。
// 缓冲只保留最近 2000 条且服务重启即清空——这是「看现场」工具，不是持久化归档；
// DEBUG 明细要以后端 JOBSCOPE_LOG_LEVEL=DEBUG 启动才会产生，页面只负责展示。
const LEVEL_ORDER: Record<string, number> = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 }
const MIN_LEVELS: { value: string; label: string }[] = [
  { value: '', label: '全部等级' },
  { value: 'INFO', label: 'INFO 及以上' },
  { value: 'WARNING', label: 'WARNING 及以上' },
  { value: 'ERROR', label: '仅错误与严重' },
]
const MAX_LOADED = 3000 // 前端最多保留的条数；超出从最早丢弃（后端缓冲本身也只有 2000 条）。
const POLL_MS = 2000
const FOLLOW_GAP = 48 // 距底部多少像素内视为「贴着底部」，继续自动跟随新日志。

export function LogView() {
  const [entries, setEntries] = useState<LogEntry[]>([])
  const [minLevel, setMinLevel] = useState('')
  const [query, setQuery] = useState('')
  const [paused, setPaused] = useState(false)
  const [following, setFollowing] = useState(true)
  const [pendingNew, setPendingNew] = useState(0)
  const [error, setError] = useState('')
  const afterRef = useRef(0)
  const stickBottom = useRef(true)
  const streamRef = useRef<HTMLDivElement>(null)

  // 收到一批增量：seq 回卷（服务重启）就清空本地全量重拉；否则追加并裁掉最老的。
  const apply = useCallback((data: { entries: LogEntry[]; latest: number }) => {
    if (data.latest < afterRef.current) {
      afterRef.current = 0
      setEntries([])
      return
    }
    afterRef.current = data.latest
    if (data.entries.length) {
      setEntries(current => {
        const next = [...current, ...data.entries]
        return next.length > MAX_LOADED ? next.slice(next.length - MAX_LOADED) : next
      })
      if (!stickBottom.current) setPendingNew(count => count + data.entries.length)
    }
  }, [])

  // 首次进入：全量拉最近一段，之后由轮询接手。
  useEffect(() => {
    let active = true
    getLogs(0, 1000)
      .then(data => { if (active) { afterRef.current = data.latest; setEntries(data.entries); setError('') } })
      .catch(err => { if (active) setError((err as Error).message) })
    return () => { active = false }
  }, [])

  // 轮询：页面隐藏或用户暂停时跳过；失败静默，下一轮继续。
  useEffect(() => {
    const timer = window.setInterval(() => {
      if (paused || document.hidden) return
      getLogs(afterRef.current, 1000).then(data => { apply(data); setError('') }).catch(() => { /* 轮询失败不打断页面 */ })
    }, POLL_MS)
    return () => window.clearInterval(timer)
  }, [paused, apply])

  // 跟随模式：贴底时每批新日志都滚到底；用户上滚就停跟随并累计「未读」数。
  useEffect(() => {
    if (!following) return
    const el = streamRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [entries, following])

  function onScroll(event: UIEvent<HTMLDivElement>) {
    const el = event.currentTarget
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < FOLLOW_GAP
    stickBottom.current = nearBottom
    if (nearBottom && !following) { setFollowing(true); setPendingNew(0) }
    else if (!nearBottom && following) setFollowing(false)
  }

  function jumpToBottom() {
    stickBottom.current = true
    setFollowing(true); setPendingNew(0)
    const el = streamRef.current
    if (el) el.scrollTop = el.scrollHeight
  }

  function clearView() {
    // 只清本地显示；afterRef 保持在最新 seq，清掉的旧日志不会随下一轮轮询回来。
    setEntries([]); setPendingNew(0); setFollowing(true); stickBottom.current = true
  }

  function search(event: FormEvent) {
    event.preventDefault()
    setQuery(text => text.trim())
  }

  function pickLevel(event: ChangeEvent<HTMLSelectElement>) { setMinLevel(event.target.value) }

  function downloadLog() {
    const text = visible.map(entry => `${entry.ts} ${entry.level.padEnd(8)} ${entry.logger}: ${entry.message}`).join('\n')
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = `jobscope-logs-${new Date().toISOString().slice(0, 19).replace(/[T:]/g, '-')}.log`
    link.click()
    URL.revokeObjectURL(url)
  }

  const needle = query.trim().toLowerCase()
  const visible = entries.filter(entry =>
    (!minLevel || (LEVEL_ORDER[entry.level] ?? 99) >= (LEVEL_ORDER[minLevel] ?? 0))
    && (!needle || entry.message.toLowerCase().includes(needle) || entry.logger.toLowerCase().includes(needle)))
  const errorCount = visible.filter(entry => (LEVEL_ORDER[entry.level] ?? 0) >= LEVEL_ORDER.ERROR).length

  return <>
    <section className="page-intro"><div><h1>运行日志</h1>
      <p>后端进程的实时日志（保留最近 2000 条，重启清空）。采集、对话与后台任务的起止和报错都会出现在这里；DEBUG 明细需以 <code>JOBSCOPE_LOG_LEVEL=DEBUG</code> 启动后端。</p>
    </div><span className="revision-label">{following ? '实时跟随' : '已上滚查看历史'}</span></section>
    <div className="log-view">
      <form className="log-toolbar" onSubmit={search}>
        <label className="search-input"><Icon name="search" size={16} /><span className="sr-only">日志关键词</span>
          <input value={query} onChange={event => setQuery(event.target.value)} placeholder="即时过滤消息与模块名，如 collect、信号、失败" maxLength={200} /></label>
        <label className="log-level-field"><span className="sr-only">最低等级</span>
          <select value={minLevel} onChange={pickLevel}>{MIN_LEVELS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label>
        <span className="log-count">{visible.length.toLocaleString()} 条{errorCount > 0 && <em className="log-error-count">{errorCount} 条错误</em>}</span>
        <span className="log-toolbar-actions">
          <button type="button" className="text-button" onClick={() => setPaused(value => !value)}>{paused ? '继续刷新' : '暂停刷新'}</button>
          <button type="button" className="text-button" onClick={clearView}>清空显示</button>
          <button type="button" className="text-button" onClick={downloadLog} disabled={!visible.length}><Icon name="external" size={13} />下载当前</button>
        </span>
      </form>
      {error && <div className="log-error-banner">日志读取失败：{error}</div>}
      <div className="log-stream" ref={streamRef} onScroll={onScroll} role="log" aria-label="运行日志流">
        {visible.length === 0
          ? <div className="log-empty">{entries.length ? '当前筛选没有命中任何日志。' : '暂无日志。发起一次采集、对话或参考重算后再来看。'}</div>
          : visible.map(entry => <div key={entry.seq} className={`log-row level-${entry.level.toLowerCase()}`}>
            <span className="log-time">{entry.ts.slice(11)}</span>
            <span className="log-level">{entry.level}</span>
            <span className="log-logger" title={entry.logger}>{entry.logger}</span>
            <span className="log-message">{entry.message}</span>
          </div>)}
      </div>
      {!following && pendingNew > 0 && <button type="button" className="log-follow-chip" onClick={jumpToBottom}>↓ {pendingNew} 条新日志，回到底部</button>}
    </div>
  </>
}
