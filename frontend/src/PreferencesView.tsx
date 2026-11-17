import { useCallback, useEffect, useState } from 'react'
import { createJobPreference, deleteJobPreference, listJobPreferences, updateJobPreference } from './api'
import type { JobPreference } from './api'
import { toast } from './toast'
import { Icon } from './Icon'

// 求职偏好（2026-11-17）：硬性条件的提前申报——到岗方式、排班作息、薪资期望、学历门槛等。
// 评估适配时启用中的条目作为「已确认事实」注入匹配管线，模型据此判「满足/不满足」，
// 不再把这些硬性要求留成待确认。与「个人资料库」分工：那边存经历材料，这边存立场答案。
const DIMENSIONS = ['preference', 'skill', 'experience', 'industry'] as const
const DIMENSION_NAMES: Record<string, string> = { preference: '求职偏好', skill: '技能', experience: '经历与职责', industry: '行业与业务' }
const EMPTY_DRAFT = { dimension: 'preference', statement: '', enabled: true }

export function PreferencesView() {
  const [entries, setEntries] = useState<JobPreference[] | null>(null)
  const [filter, setFilter] = useState<string>('')
  const [draft, setDraft] = useState<typeof EMPTY_DRAFT | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [busy, setBusy] = useState('')

  const load = useCallback(async () => {
    try { setEntries((await listJobPreferences()).preferences) }
    catch (err) { toast((err as Error).message, 'error') }
  }, [])
  useEffect(() => { void load() }, [load])

  function run(key: string, action: () => Promise<void>) {
    setBusy(key)
    void action().catch(err => toast((err as Error).message, 'error')).finally(() => setBusy(''))
  }

  function startCreate() { setEditingId(null); setDraft({ ...EMPTY_DRAFT }) }
  function startEdit(item: JobPreference) {
    setEditingId(item.id)
    setDraft({ dimension: item.dimension, statement: item.statement, enabled: item.enabled })
  }

  function submitDraft() {
    if (!draft) return
    if (draft.statement.trim().length < 2) { toast('请填写申报内容', 'error'); return }
    const body = { dimension: draft.dimension, statement: draft.statement.trim(), enabled: draft.enabled }
    run('save', async () => {
      if (editingId) await updateJobPreference(editingId, body)
      else await createJobPreference(body)
      toast(editingId ? '申报已更新。' : '申报已保存，下次评估适配自动生效。')
      setDraft(null); setEditingId(null)
      await load()
    })
  }

  const toggleEnabled = (item: JobPreference) => run(`toggle-${item.id}`, async () => {
    await updateJobPreference(item.id, { enabled: !item.enabled })
    await load()
  })
  const removeEntry = (item: JobPreference) => run(`delete-${item.id}`, async () => {
    await deleteJobPreference(item.id)
    if (editingId === item.id) { setDraft(null); setEditingId(null) }
    toast('申报已删除。')
    await load()
  })

  const visible = (entries ?? []).filter(item => !filter || item.dimension === filter)
  const grouped = DIMENSIONS.map(dimension => ({ dimension, items: visible.filter(item => item.dimension === dimension) })).filter(group => group.items.length)

  return <section className="profile-view" aria-label="求职偏好">
    <section className="page-intro"><div><h1>求职偏好</h1><p>提前申报硬性条件的答案：工作地点与到岗方式、可接受的排班作息、薪资期望、学历与资质门槛。「评估适配」时启用中的申报会作为已确认事实参与逐项匹配——你写明的立场，不再留给模型猜。</p></div>
      <span className="revision-label">{entries?.length ?? '…'} 条申报 · 启用 {(entries ?? []).filter(item => item.enabled).length} 条</span></section>
    <section className="paper-panel profile-panel">
      <div className="profile-toolbar">
        <div className="pipeline-filter" role="group" aria-label="维度筛选">
          <button type="button" className={`pipeline-filter-chip${filter === '' ? ' active' : ''}`} onClick={() => setFilter('')}>全部</button>
          {DIMENSIONS.map(dimension => <button key={dimension} type="button" className={`pipeline-filter-chip${filter === dimension ? ' active' : ''}`} onClick={() => setFilter(dimension)}>{DIMENSION_NAMES[dimension]}</button>)}
        </div>
        <div className="profile-toolbar-actions">
          <button type="button" className="button primary small" onClick={startCreate} disabled={Boolean(draft)}><Icon name="plus" size={14} />新增申报</button>
        </div>
      </div>

      {draft && <div className="profile-editor" role="form" aria-label={editingId ? '编辑申报' : '新增申报'}>
        <label>维度<select value={draft.dimension} onChange={event => setDraft({ ...draft, dimension: event.target.value })}>
          {DIMENSIONS.map(dimension => <option key={dimension} value={dimension}>{DIMENSION_NAMES[dimension]}</option>)}
        </select></label>
        <label>申报内容 *（一句话立场，评估时按原文对照 JD 硬性要求）<textarea value={draft.statement} onChange={event => setDraft({ ...draft, statement: event.target.value })} rows={3} maxLength={1000} placeholder={'例：当前在杭州，可线下办公并能随时参加线下面试；接受做六休一、周日固定休息；期望薪资 15k-30k，底薪+绩效可接受。'} autoFocus /></label>
        <label className="agent-checkbox" title="停用的申报不参与评估匹配，但保留在列表里">
          <input type="checkbox" checked={draft.enabled} onChange={event => setDraft({ ...draft, enabled: event.target.checked })} />启用
        </label>
        <div className="modal-actions">
          <button type="button" className="button secondary" onClick={() => { setDraft(null); setEditingId(null) }} disabled={busy === 'save'}>取消</button>
          <button type="button" className="button primary" disabled={busy === 'save'} onClick={() => void submitDraft()}>{busy === 'save' ? <span className="spinner" /> : null}{editingId ? '保存修改' : '保存申报'}</button>
        </div>
      </div>}

      {entries === null
        ? <div className="loading-state"><span className="spinner" />正在读取申报…</div>
        : grouped.length === 0
          ? <div className="empty"><div className="empty-icon"><Icon name="resume" /></div><h3>{filter ? '该维度下还没有申报' : '还没有求职偏好申报'}</h3><p>把你的硬性底线写成一一句话立场：地点与到岗、排班作息、薪资期望、学历资质。评估适配时它们会替你回答 JD 里的硬性问题。</p></div>
          : grouped.map(group => <div key={group.dimension} className="profile-group">
            <h3 className="profile-group-title">{DIMENSION_NAMES[group.dimension]}<span>{group.items.length}</span></h3>
            {group.items.map(item => <article key={item.id} className={`profile-entry${item.enabled ? '' : ' disabled'}`}>
              <div className="profile-entry-head">
                <div><p className="profile-preference-statement">{item.statement}</p></div>
                <div className="profile-entry-actions">
                  <label className="agent-checkbox" title="停用的申报不参与评估匹配">
                    <input type="checkbox" checked={item.enabled} disabled={busy === `toggle-${item.id}`} onChange={() => void toggleEnabled(item)} />启用
                  </label>
                  <button type="button" className="text-button" onClick={() => startEdit(item)}>编辑</button>
                  <button type="button" className="text-button danger-text" disabled={busy === `delete-${item.id}`} onClick={() => void removeEntry(item)}>删除</button>
                </div>
              </div>
            </article>)}
          </div>)}
      <p className="tiny-label list-bottom">申报只在「评估适配」时作为已确认事实参与匹配；规则分析（未绑定分析模型）不使用偏好。评估报告里改判某条状态的手动确认独立于本页，两者都跨重评保留。</p>
    </section>
  </section>
}
