import { useEffect, useState } from 'react'
import { toast } from './toast'
import { date, getAttribution, getFailureMuseum, saveAttribution } from './api'
import type { AttributionTagDef } from './api'

// 失败博物馆共用标注组件（2026-11-23）：挂在模拟面试复盘报告与真实复盘报告底部。
// 结构化归因（CBT 思维记录范式）：点选 + 一句话备注，不做评判只做归因分流——
// 可控项变成下一场的准备清单，不可控项只做定位，阻止「我不行」式的全盘内化。
// 分类学与已有标注都来自 /api/interview/failure-museum（常量进后端代码，前端不自造）。

// 面试官暗线弱点信号（ADVANCE_TOOL.weakness 枚举）→ 可控标签：与后端
// failure_museum.WEAKNESS_TO_TAG 同源，改枚举时两边同步（后端测试有覆盖闸）。
const WEAKNESS_SIGNAL_TAGS: Record<string, string> = {
  无量化数据: 'qa_missing', 无个人动作: 'qa_missing', 无结果: 'qa_missing',
  STAR缺失: 'structure', 含糊其辞: 'vague', 知识硬伤: 'prep_knowledge',
}

const OUTCOME_OPTIONS: Record<string, { value: string; label: string }[]> = {
  mock: [
    { value: 'self_positive', label: '自评：整体满意' },
    { value: 'self_neutral', label: '自评：一般' },
    { value: 'self_negative', label: '自评：不满意' },
  ],
  real: [
    { value: 'pass', label: '结果：通过' },
    { value: 'fail', label: '结果：未通过' },
    { value: 'unknown', label: '结果：未知' },
  ],
}

export default function AttributionPanel({ source, sessionId, signals, onSaved }: {
  source: 'mock' | 'real'; sessionId: string; signals?: string[]; onSaved?: () => void
}) {
  const [defs, setDefs] = useState<{ controlled: AttributionTagDef[]; uncontrolled: AttributionTagDef[] } | null>(null)
  const [selected, setSelected] = useState<Record<string, string>>({})  // tagId → 一句话备注
  const [outcome, setOutcome] = useState('')
  const [note, setNote] = useState('')
  const [savedAt, setSavedAt] = useState('')
  const [saving, setSaving] = useState(false)
  const [loadError, setLoadError] = useState('')

  useEffect(() => {
    let alive = true
    // 面试官暗线信号 → 建议标签（带出现次数）；只有还没归档过时才自动预选。
    const suggested = new Map<string, number>()
    for (const signal of signals ?? []) {
      const tag = WEAKNESS_SIGNAL_TAGS[signal]
      if (tag) suggested.set(tag, (suggested.get(tag) ?? 0) + 1)
    }
    void (async () => {
      try {
        const [museum, existing] = await Promise.all([
          getFailureMuseum(),
          getAttribution(sessionId).catch(() => null),
        ])
        if (!alive) return
        setDefs({ controlled: museum.controlled, uncontrolled: museum.uncontrolled })
        if (existing) {
          const map: Record<string, string> = {}
          for (const item of existing.tags) map[item.tag] = item.note
          setSelected(map)
          setOutcome(existing.outcome)
          setNote(existing.note)
          setSavedAt(existing.updated_at)
        } else if (suggested.size) {
          setSelected(Object.fromEntries([...suggested.keys()].map(tag => [tag, ''])))
        }
      } catch (err) { if (alive) setLoadError((err as Error).message) }
    })()
    return () => { alive = false }
    // sessionId 变化（切换场次）时整体重载。
  }, [source, sessionId])

  function toggle(tagId: string) {
    setSelected(current => {
      const next = { ...current }
      if (tagId in next) delete next[tagId]
      else next[tagId] = ''
      return next
    })
  }

  async function save() {
    if (!Object.keys(selected).length) { toast('至少勾选一个归因标签', 'error'); return }
    setSaving(true)
    try {
      const saved = await saveAttribution({
        source, session_id: sessionId, outcome,
        tags: Object.entries(selected).map(([tag, tagNote]) => ({
          scope: defs?.controlled.some(item => item.id === tag) ? 'controlled' : 'uncontrolled',
          tag, note: tagNote,
        })),
        note,
      })
      setSavedAt(saved.updated_at)
      toast('归因已入馆：重复失分点会在「失败博物馆」自动点名', 'notice')
      onSaved?.()
    } catch (err) { toast((err as Error).message, 'error') }
    finally { setSaving(false) }
  }

  const groups: { key: 'controlled' | 'uncontrolled'; title: string; caption: string; defs: AttributionTagDef[] }[] = defs
    ? [
        { key: 'controlled', title: '可控因素', caption: '能改变的——重复出现会进下一场准备清单', defs: defs.controlled },
        { key: 'uncontrolled', title: '不可控因素', caption: '环境因素——只记录定位，不内化不评判', defs: defs.uncontrolled },
      ]
    : []

  return <section className="paper-panel attr-panel" aria-label="失败博物馆归因标注">
    <div className="section-heading">
      <h3>归因标注</h3>
      <span className="tiny-label">这场里发生了什么：勾选 + 可选一句话备注{savedAt ? ` · 上次归档 ${date(savedAt)}` : ''}</span>
      <button className="button primary small" disabled={saving || !defs} onClick={() => void save()}>
        {saving ? <span className="spinner" /> : '保存归因'}
      </button>
    </div>
    {loadError && <p className="iv-missing">标注加载失败：{loadError}</p>}
    {!defs && !loadError && <p className="muted body-small">分类加载中…<span className="spinner" /></p>}
    {groups.map(group => <div key={group.key} className="attr-group">
      <div className="attr-group-head"><strong>{group.title}</strong><span className="tiny-label">{group.caption}</span></div>
      <div className="attr-chips">
        {group.defs.map(item => <button key={item.id}
          className={`attr-chip${item.id in selected ? ' active' : ''}${item.id in selected && signals?.length ? ' suggested' : ''}`}
          title={item.hint + (item.advice ? `\n建议：${item.advice}` : '')}
          aria-pressed={item.id in selected}
          onClick={() => toggle(item.id)}>{item.label}</button>)}
      </div>
      {group.defs.filter(item => item.id in selected).map(item => <label key={item.id} className="attr-note">
        <span className="sr-only">{item.label}备注</span>
        <input value={selected[item.id]} maxLength={200} placeholder={`${item.label}：一句话备注（可选，跨场报告会用上）`}
          onChange={event => setSelected(current => ({ ...current, [item.id]: event.target.value }))} />
      </label>)}
    </div>)}
    {signals?.length ? <p className="tiny-label attr-signals">系统观察信号（面试官暗线记录）：{[...new Set(signals)].join('、')} —— 已按此预选，可增删</p> : null}
    <div className="attr-footer">
      <label>结果（选填，用于结果对比）<select value={outcome} onChange={event => setOutcome(event.target.value)} aria-label="本场结果">
        <option value="">不填</option>
        {(OUTCOME_OPTIONS[source] ?? []).map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
      </select></label>
      <label className="attr-overall">整场备注<input value={note} maxLength={500} placeholder="一句话整体记录（可选）"
        onChange={event => setNote(event.target.value)} aria-label="整场备注" /></label>
    </div>
  </section>
}
