import { useCallback, useEffect, useState } from 'react'
import { getDuplicateScan, markDuplicates, releaseDuplicates, date, relativeDays } from './api'
import type { DedupCandidate, DedupGroup, DedupScan } from './api'
import { sourceNames } from './JobCard'
import { toast } from './toast'
import { Button } from './ui'
import { Icon } from './Icon'
import './jobOps.css'

// 岗位去重面板（2026-11-19）：同一岗位多平台/多次采集各存一条时，标记主次而不是删除。
// 重复项默认不再出现在列表与统计（薪资中位数、管线回顾都不再双算），可随时拆开还原；
// 收藏与管线这类用户意图在标记时上提给主记录，不会静默消失。扫描是纯规则（同公司 + 标题
// 归一相等或互相包含），只出候选，标记永远由人工确认。

const MATCH_NAMES: Record<string, string> = { exact: '同名', contains: '包含' }
const sourceName = (id: string) => sourceNames[id] || id

function JobMini({ item, keep }: { item: DedupCandidate['a']; keep?: boolean }) {
  return <div className={`dedup-job${keep ? ' keep' : ''}`}>
    <strong>{item.title}</strong>
    <div className="dedup-job-meta"><span>{item.company}</span>{item.location && <span>{item.location}</span>}{item.salary && <span>{item.salary}</span>}</div>
    <div className="dedup-job-meta"><span>{sourceName(item.source)}</span><span>{relativeDays(item.updated_at, date(item.updated_at))}</span>{item.pipeline_status && <span>管线：{item.pipeline_status}</span>}</div>
  </div>
}

export function JobDedup({ onJobsChanged }: { onJobsChanged: () => void }) {
  const [scan, setScan] = useState<DedupScan | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try { setScan(await getDuplicateScan()) } catch (err) { toast((err as Error).message, 'error') }
    finally { setLoading(false) }
  }, [])
  useEffect(() => { void load() }, [load])

  async function mark(candidate: DedupCandidate, keepId: string) {
    const other = keepId === candidate.a.id ? candidate.b : candidate.a
    setBusy(`mark-${keepId}`)
    try {
      const result = await markDuplicates(keepId, [other.id])
      toast(`已合并：保留「${result.primary.title}」；「${other.title}」不再出现在列表与统计，可随时拆开。`, 'notice', false, { label: '查看岗位研究', onClick: onJobsChanged })
      await load()
      onJobsChanged()
    } catch (err) { toast((err as Error).message, 'error') }
    finally { setBusy('') }
  }

  async function release(id: string, title: string) {
    setBusy(`release-${id}`)
    try {
      const result = await releaseDuplicates([id])
      if (result.released) toast(`「${title}」已拆开，恢复出现在列表与统计。`)
      else toast('这份岗位已不在重复组里。')
      await load()
      onJobsChanged()
    } catch (err) { toast((err as Error).message, 'error') }
    finally { setBusy('') }
  }

  return <section className="dedup-panel" aria-label="岗位去重">
    <p className="dedup-hint">
      同一个岗位常被多个平台或多次采集各存一条，薪资统计会被重复样本带偏。这里按 <b>同公司 + 职位名归一后相等或互相包含</b> 扫出候选，
      你确认哪份保留：标记只是<b>主次</b>，不删除任何档案——被标记的份默认不再出现在列表与统计，收藏/管线会先上提给保留的那份，可随时拆开。
    </p>
    <div className="dedup-actions">
      <Button className="button secondary small" busy={loading} disabled={Boolean(busy)} onClick={() => { setLoading(true); void load() }}><Icon name="search" size={14} />重新扫描</Button>
      <span className="tiny-label">{scan ? `候选 ${scan.candidates.length} 组${scan.truncated ? '（已截断，处理后再扫）' : ''} · 现有重复组 ${scan.groups.length} 组` : '正在扫描…'}</span>
    </div>

    {scan && scan.groups.length > 0 && <>
      <div className="section-heading"><div><h3>已标记的重复组</h3></div></div>
      {scan.groups.map(group => <GroupCard key={group.primary.id} group={group} busy={busy} onRelease={release} />)}
    </>}

    {scan && scan.candidates.length > 0 && <>
      <div className="section-heading"><div><h3>候选（点「保留此份」确认合并）</h3></div></div>
      {scan.candidates.map(candidate => <div key={`${candidate.a.id}-${candidate.b.id}`} className="dedup-candidate">
        <div className="dedup-candidate-head"><span className="dedup-match-tag">{MATCH_NAMES[candidate.match] || candidate.match}</span><span>{candidate.company}</span></div>
        <div className="dedup-pair">
          <JobMini item={candidate.a} keep={false} />
          <JobMini item={candidate.b} keep={false} />
        </div>
        <div className="dedup-actions">
          <button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={() => void mark(candidate, candidate.a.id)}>保留上份</button>
          <button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={() => void mark(candidate, candidate.b.id)}>保留下份</button>
        </div>
      </div>)}
    </>}

    {scan && !scan.candidates.length && !scan.groups.length && <div className="inline-empty">没扫出同公司且职位名匹配的候选对。不同平台的职位名写法差异大时，规则扫不出来是正常的——它们本来也可能是不同岗位。</div>}
  </section>
}

function GroupCard({ group, busy, onRelease }: { group: DedupGroup; busy: string; onRelease: (id: string, title: string) => Promise<void> }) {
  return <div className="dedup-group">
    <div className="dedup-group-title"><strong>{group.primary.title}</strong><span>保留中 · {sourceName(group.primary.source)} · {relativeDays(group.primary.updated_at, date(group.primary.updated_at))}</span></div>
    {group.duplicates.map(dup => <div key={dup.id} className="dedup-dup-row">
      <strong>{dup.title}</strong>
      <span>{sourceName(dup.source)} · {relativeDays(dup.updated_at, date(dup.updated_at))}</span>
      <button type="button" className="text-button" disabled={Boolean(busy)} title="取消重复标记，恢复出现在列表与统计" onClick={() => void onRelease(dup.id, dup.title)}>拆开</button>
    </div>)}
  </div>
}
