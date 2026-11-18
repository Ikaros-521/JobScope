import { useMemo } from 'react'

// 简历改动对比看板：以优化落库时的基础简历快照（resume_versions.base_text）为基准，
// 做行级对比 + 变更行内的字级高亮 + 相同内容折叠，帮助用户核对 AI 到底改了什么。
// 正文体量小（几千字符量级），自实现 LCS 即可，不引第三方 diff 库。

type DiffSeg = { text: string; changed: boolean }
type DiffRow =
  | { kind: 'same' | 'add' | 'del'; text: string; segs: DiffSeg[] | null }
  | { kind: 'fold'; text: string; segs: null }

const CONTEXT_LINES = 3 // 与 git 一致：变更块上下各保留 3 行相同内容
const MAX_LCS_CELLS = 1_000_000 // 行级 LCS 规模上限，超限退化为整块替换
const MAX_PAIR_CHARS = 400 // 行内字级对比的中间段上限，超长整行标色

function splitLines(text: string): string[] {
  return text.replace(/\r\n?/g, '\n').split('\n')
}

/** 序列 LCS：返回两侧每个元素是否属于公共子序列。超过 maxCells 返回 null（调用方退化处理）。 */
function lcsFlags(a: string[], b: string[], maxCells: number): { aKept: boolean[]; bKept: boolean[] } | null {
  const n = a.length, m = b.length
  if (n === 0 || m === 0) return { aKept: a.map(() => false), bKept: b.map(() => false) }
  if (n * m > maxCells) return null
  const width = m + 1
  const dp = new Uint32Array((n + 1) * width)
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i * width + j] = a[i] === b[j]
        ? dp[(i + 1) * width + j + 1] + 1
        : Math.max(dp[(i + 1) * width + j], dp[i * width + j + 1])
    }
  }
  const aKept = new Array<boolean>(n).fill(false)
  const bKept = new Array<boolean>(m).fill(false)
  let i = 0, j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) { aKept[i] = bKept[j] = true; i++; j++ }
    else if (dp[(i + 1) * width + j] >= dp[i * width + j + 1]) i++
    else j++
  }
  return { aKept, bKept }
}

/** 把布尔保留标记折叠成连续片段。 */
function toSegs(items: string[], kept: boolean[]): DiffSeg[] {
  const segs: DiffSeg[] = []
  for (let k = 0; k < items.length; k++) {
    const changed = !kept[k]
    const last = segs[segs.length - 1]
    if (last && last.changed === changed) last.text += items[k]
    else segs.push({ text: items[k], changed })
  }
  return segs
}

/** 变更行内的字级切分：掐掉公共前后缀，中间段做小规模 LCS，输出两侧带高亮标记的片段。 */
function charSegs(oldText: string, newText: string): { oldSegs: DiffSeg[]; newSegs: DiffSeg[] } {
  let start = 0
  const minLen = Math.min(oldText.length, newText.length)
  while (start < minLen && oldText[start] === newText[start]) start++
  let endOld = oldText.length, endNew = newText.length
  while (endOld > start && endNew > start && oldText[endOld - 1] === newText[endNew - 1]) { endOld--; endNew-- }
  const midOld = oldText.slice(start, endOld)
  const midNew = newText.slice(start, endNew)
  let oldMid: DiffSeg[], newMid: DiffSeg[]
  if (midOld && midNew && midOld.length <= MAX_PAIR_CHARS && midNew.length <= MAX_PAIR_CHARS) {
    // Array.from 按码点切，emoji 等代理对不会被拆开
    const flags = lcsFlags(Array.from(midOld), Array.from(midNew), MAX_PAIR_CHARS * MAX_PAIR_CHARS)
    if (flags) {
      oldMid = toSegs(Array.from(midOld), flags.aKept)
      newMid = toSegs(Array.from(midNew), flags.bKept)
    } else {
      oldMid = [{ text: midOld, changed: true }]
      newMid = [{ text: midNew, changed: true }]
    }
  } else {
    oldMid = midOld ? [{ text: midOld, changed: true }] : []
    newMid = midNew ? [{ text: midNew, changed: true }] : []
  }
  const wrap = (mid: DiffSeg[], text: string, s: number, e: number): DiffSeg[] => [
    ...(s ? [{ text: text.slice(0, s), changed: false as const }] : []),
    ...mid,
    ...(e < text.length ? [{ text: text.slice(e), changed: false as const }] : []),
  ]
  return { oldSegs: wrap(oldMid, oldText, start, endOld), newSegs: wrap(newMid, newText, start, endNew) }
}

function computeDiff(base: string, current: string): { rows: DiffRow[]; added: number; removed: number; identical: boolean } {
  const a = splitLines(base)
  const b = splitLines(current)
  const flags = lcsFlags(a, b, MAX_LCS_CELLS)
  const raw: DiffRow[] = []
  if (!flags) {
    for (const text of a) raw.push({ kind: 'del', text, segs: null })
    for (const text of b) raw.push({ kind: 'add', text, segs: null })
  } else {
    let i = 0, j = 0
    while (i < a.length || j < b.length) {
      if (i < a.length && j < b.length && flags.aKept[i] && flags.bKept[j]) {
        raw.push({ kind: 'same', text: a[i], segs: null }); i++; j++
        continue
      }
      // 一个替换块：连续删除 + 连续新增；行数对齐的成对做字级高亮，多出的整行标色
      const dels: string[] = [], adds: string[] = []
      while (i < a.length && !flags.aKept[i]) dels.push(a[i++])
      while (j < b.length && !flags.bKept[j]) adds.push(b[j++])
      const pairs = Math.min(dels.length, adds.length)
      for (let k = 0; k < pairs; k++) {
        const segs = charSegs(dels[k], adds[k])
        raw.push({ kind: 'del', text: dels[k], segs: segs.oldSegs })
        raw.push({ kind: 'add', text: adds[k], segs: segs.newSegs })
      }
      for (let k = pairs; k < dels.length; k++) raw.push({ kind: 'del', text: dels[k], segs: null })
      for (let k = pairs; k < adds.length; k++) raw.push({ kind: 'add', text: adds[k], segs: null })
    }
  }
  // 相同内容折叠：只保留变更块上下 CONTEXT_LINES 行，git 风格
  const changeIdx = new Set<number>()
  raw.forEach((row, idx) => { if (row.kind !== 'same') changeIdx.add(idx) })
  const kept = raw.map((row, idx) => {
    if (row.kind !== 'same') return true
    for (let d = -CONTEXT_LINES; d <= CONTEXT_LINES; d++) if (changeIdx.has(idx + d)) return true
    return false
  })
  const rows: DiffRow[] = []
  let pendingSame = 0
  raw.forEach((row, idx) => {
    if (kept[idx]) {
      if (pendingSame) rows.push({ kind: 'fold', text: `已折叠 ${pendingSame} 行相同内容`, segs: null })
      pendingSame = 0
      rows.push(row)
    } else pendingSame++
  })
  if (pendingSame) rows.push({ kind: 'fold', text: `已折叠 ${pendingSame} 行相同内容`, segs: null })
  const added = raw.filter(row => row.kind === 'add').length
  const removed = raw.filter(row => row.kind === 'del').length
  return { rows, added, removed, identical: added === 0 && removed === 0 }
}

function SegText({ row }: { row: Extract<DiffRow, { kind: 'add' | 'del' | 'same' }> }) {
  if (row.text === '') return <span>{'\u00A0'}</span>
  if (!row.segs) return <span>{row.text}</span>
  return <span>{row.segs.map((seg, idx) => seg.changed
    ? <mark key={idx}>{seg.text}</mark>
    : <span key={idx}>{seg.text}</span>)}</span>
}

/** 改动对比看板：base 为优化时的基础简历快照，current 为当前（可再编辑）正文。 */
export function ResumeDiff({ base, current }: { base: string; current: string }) {
  const { rows, added, removed, identical } = useMemo(() => computeDiff(base, current), [base, current])
  if (identical) {
    return <div className="resume-diff-empty">当前正文与基础简历快照一致，没有改动。</div>
  }
  return (
    <div className="resume-diff" role="figure" aria-label="改动对比看板">
      <div className="resume-diff-legend">
        <span className="diff-stat-add">+{added} 行</span>
        <span className="diff-stat-del">−{removed} 行</span>
        <span className="tiny-label">基准：开始优化时的基础简历快照；高亮处为行内具体改动</span>
      </div>
      <div className="resume-diff-body">
        {rows.map((row, idx) => row.kind === 'fold'
          ? <div key={idx} className="diff-row diff-fold">⋯ {row.text}</div>
          : <div key={idx} className={`diff-row diff-${row.kind}`}>
            <span className="diff-gutter" aria-hidden="true">{row.kind === 'add' ? '+' : row.kind === 'del' ? '−' : ''}</span>
            <span className="diff-text"><SegText row={row} /></span>
          </div>)}
      </div>
    </div>
  )
}
