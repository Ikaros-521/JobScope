import { useCallback, useEffect, useState } from 'react'
import { PROFILE_CATEGORY_NAMES, createProfileEntry, deleteProfileEntry, listProfileEntries, patchProfileEntry, replaceProfileEntry } from './api'
import type { ProfileCategory, ProfileEntry } from './api'
import { toast } from './toast'
import { Icon } from './Icon'

// 个人资料库（2026-11-17）：完整版个人材料。与「我的简历」分离——简历因匹配度与篇幅
// 常是阉割版；这里存完整的项目经历等工作事实，简历工坊优化时勾选取材扩写充实。
// preference 类（2026-11-18 并入，原「求职偏好」独立页退役）注入方向不同：
// 启用条目评估适配时自动进事实池判上，不进工坊取材。
// 本视图只做手写材料的增删改查，不抓取、不联网、不打分。笔记导入（2026-11-18 改版）走
// 全局对话 Agent：本页只收路径并跳转（预填指令），由 Agent 的 vault_list/vault_read/
// profile_import_write 工具完成提炼与幂等写入——无模型的本地规则解析已退役。
const CATEGORIES = Object.keys(PROFILE_CATEGORY_NAMES) as ProfileCategory[]
const EMPTY_DRAFT = { category: 'project' as ProfileCategory, title: '', org: '', period: '', content: '', tags: '' }

// 跳转全局对话的预填指令：工作法写进指令，Agent 按工具协作流程完成导入（可中途追问确认）。
function importPrompt(path: string) {
  return `请把我的笔记库导入个人资料库，目录：${path}。\n` +
    '工作法：先用 vault_list 列出该目录的 .md 笔记清单；挑与个人经历（项目/工作/教育/技能/证书/奖项）相关的笔记用 vault_read 读取；' +
    '把每篇提炼为一条资料条目——背景、做了什么、怎么做、结果与数字，忠于笔记原文不虚构，标题/组织/时间段从原文归纳；' +
    '先向我展示提炼要点，确认后用 profile_import_write 写入（rel_path 用清单里的相对路径，同一笔记重导会原位更新），最后给导入汇总。'
}

export function ProfileView({ onImportViaChat }: { onImportViaChat?: (prompt: string) => void }) {
  const [entries, setEntries] = useState<ProfileEntry[] | null>(null)
  const [filter, setFilter] = useState<'' | ProfileCategory>('')
  const [draft, setDraft] = useState<typeof EMPTY_DRAFT | null>(null)
  const [editingId, setEditingId] = useState<string | null>(null)
  const [busy, setBusy] = useState('')
  // 笔记导入：路径记在 localStorage（本机库路径，换机器无意义）；导入动作发生在全局对话。
  const [obsidianOpen, setObsidianOpen] = useState(false)
  const [vaultPath, setVaultPath] = useState(() => { try { return localStorage.getItem('jobscope-obsidian-path') || '' } catch { return '' } })

  const load = useCallback(async () => {
    try { setEntries((await listProfileEntries()).entries) }
    catch (err) { toast((err as Error).message, 'error') }
  }, [])
  useEffect(() => { void load() }, [load])

  function run(key: string, action: () => Promise<void>) {
    setBusy(key)
    void action().catch(err => toast((err as Error).message, 'error')).finally(() => setBusy(''))
  }

  function startAgentImport() {
    const path = vaultPath.trim()
    if (!path) { toast('请先填写 Obsidian 库（或任意 markdown 目录）路径', 'error'); return }
    if (!onImportViaChat) { toast('当前入口未接入全局对话', 'error'); return }
    try { localStorage.setItem('jobscope-obsidian-path', path) } catch { /* 路径记忆失败无碍 */ }
    onImportViaChat(importPrompt(path))
  }

  function startCreate() { setEditingId(null); setDraft({ ...EMPTY_DRAFT }) }
  function startEdit(item: ProfileEntry) {
    setEditingId(item.id)
    setDraft({ category: item.category, title: item.title, org: item.org, period: item.period, content: item.content, tags: item.tags.join(', ') })
  }

  function submitDraft() {
    if (!draft) return
    if (!draft.title.trim()) { toast('请填写条目标题', 'error'); return }
    if (!draft.content.trim()) { toast('请填写条目内容', 'error'); return }
    const tags = draft.tags.split(/[,，]/).map(item => item.trim()).filter(Boolean)
    const body = { category: draft.category, title: draft.title.trim(), org: draft.org.trim(), period: draft.period.trim(), content: draft.content.trim(), tags }
    run('save', async () => {
      if (editingId) await replaceProfileEntry(editingId, body)
      else await createProfileEntry(body)
      toast(editingId ? '条目已更新。' : draft.category === 'preference' ? '偏好已保存，启用状态下评估适配会自动判上。' : '条目已保存，可在简历工坊勾选取材。')
      setDraft(null); setEditingId(null)
      await load()
    })
  }

  const toggleEnabled = (item: ProfileEntry) => run(`toggle-${item.id}`, async () => {
    await patchProfileEntry(item.id, { enabled: !item.enabled })
    await load()
  })
  const removeEntry = (item: ProfileEntry) => run(`delete-${item.id}`, async () => {
    await deleteProfileEntry(item.id)
    if (editingId === item.id) { setDraft(null); setEditingId(null) }
    toast('条目已删除。')
    await load()
  })

  const visible = (entries ?? []).filter(item => !filter || item.category === filter)
  const grouped = CATEGORIES.map(category => ({ category, items: visible.filter(item => item.category === category) })).filter(group => group.items.length)
  const charTotal = (entries ?? []).reduce((sum, item) => sum + item.content.length, 0)

  return <section className="profile-view" aria-label="个人资料库">
    <section className="page-intro compact"><h1 className="sr-only">个人资料库</h1>
      <span className="revision-label">{entries?.length ?? '…'} 条材料 · 约 {charTotal.toLocaleString()} 字</span></section>
    <section className="paper-panel profile-panel">
      <div className="profile-toolbar">
        <div className="pipeline-filter" role="group" aria-label="类别筛选">
          <button type="button" className={`pipeline-filter-chip${filter === '' ? ' active' : ''}`} onClick={() => setFilter('')}>全部</button>
          {CATEGORIES.map(category => <button key={category} type="button" className={`pipeline-filter-chip${filter === category ? ' active' : ''}`} onClick={() => setFilter(category)}>{PROFILE_CATEGORY_NAMES[category]}</button>)}
        </div>
        <div className="profile-toolbar-actions">
          <button type="button" className="button secondary small" onClick={() => setObsidianOpen(value => !value)}>从笔记导入</button>
          <button type="button" className="button primary small" onClick={startCreate} disabled={Boolean(draft)}><Icon name="plus" size={14} />新增条目</button>
        </div>
      </div>

      {obsidianOpen && <div className="obsidian-import">
        <p className="tiny-label">导入由全局对话的 Agent 用模型完成（需在「设置 → 模型」配置对话模型）：填本机 Obsidian 库（或任意 markdown 目录）的完整路径，点击后跳转全局对话并预填指令——Agent 会先列出笔记清单，挑与个人经历相关的逐篇提炼为条目，展示要点确认后写入。只读目录、不改动笔记；同一笔记路径重导会在原位更新条目。</p>
        <div className="obsidian-import-row">
          <input value={vaultPath} onChange={event => setVaultPath(event.target.value)} maxLength={500}
            placeholder="如 D:\\Notes\\MyVault" aria-label="Obsidian 库路径" spellCheck={false} />
          <button type="button" className="button primary small" onClick={startAgentImport}>让 Agent 导入</button>
        </div>
      </div>}

      {draft && <div className="profile-editor" role="form" aria-label={editingId ? '编辑条目' : '新增条目'}>
        <div className="form-two">
          <label>类别<select value={draft.category} onChange={event => setDraft({ ...draft, category: event.target.value as ProfileCategory })}>
            {CATEGORIES.map(category => <option key={category} value={category}>{PROFILE_CATEGORY_NAMES[category]}</option>)}
          </select></label>
          <label>标题 *<input value={draft.title} onChange={event => setDraft({ ...draft, title: event.target.value })} maxLength={200} placeholder="如：电商大促网关重构" autoFocus /></label>
        </div>
        <div className="form-two">
          <label>组织 / 公司（可选）<input value={draft.org} onChange={event => setDraft({ ...draft, org: event.target.value })} maxLength={200} placeholder="如：甲科技 · 平台架构组" /></label>
          <label>时间段（可选）<input value={draft.period} onChange={event => setDraft({ ...draft, period: event.target.value })} maxLength={100} placeholder="如：2023-2024" /></label>
        </div>
        <label>{draft.category === 'preference' ? '申报内容 *（写清你的硬性答案：到岗方式、排班作息、薪资期望、学历门槛等——启用后评估适配会作为已确认事实自动判上）' : '完整内容 *（写清背景、你做了什么、怎么做的、结果与数字——工坊优化时会从中取材）'}<textarea value={draft.content} onChange={event => setDraft({ ...draft, content: event.target.value })} rows={8} maxLength={20000} placeholder={draft.category === 'preference' ? '例：接受做六休一，周日固定休息；期望薪资 18k×15；可接受短期出差。' : '例：负责网关限流与灰度方案设计；推导容量模型并落地三级限流；大促峰值 4.2 万 QPS 零故障；沉淀为公司级组件，3 个团队复用。'} spellCheck={false} /></label>
        <label>个人标签（逗号分隔，可选）<input value={draft.tags} onChange={event => setDraft({ ...draft, tags: event.target.value })} maxLength={500} placeholder="后端, 高并发, 网关" /></label>
        <div className="modal-actions">
          <button type="button" className="button secondary" onClick={() => { setDraft(null); setEditingId(null) }} disabled={busy === 'save'}>取消</button>
          <button type="button" className="button primary" disabled={busy === 'save'} onClick={() => void submitDraft()}>{busy === 'save' ? <span className="spinner" /> : null}{editingId ? '保存修改' : '保存条目'}</button>
        </div>
      </div>}

      {entries === null
        ? <div className="loading-state"><span className="spinner" />正在读取资料库…</div>
        : grouped.length === 0
          ? <div className="empty"><div className="empty-icon"><Icon name="book" /></div><h3>{filter ? '该类别下还没有条目' : '还没有个人材料'}</h3><p>把完整的项目经历、工作细节写进来：背景、你做了什么、怎么做的、结果与数字。简历投递版放不下的内容，都适合存在这里。</p></div>
          : grouped.map(group => <div key={group.category} className="profile-group">
            <h3 className="profile-group-title">{PROFILE_CATEGORY_NAMES[group.category]}<span>{group.items.length}</span></h3>
            {group.items.map(item => <article key={item.id} className={`profile-entry${item.enabled ? '' : ' disabled'}`}>
              <div className="profile-entry-head">
                <div><strong>{item.title}</strong><span className="tiny-label">{[item.org, item.period].filter(Boolean).join(' · ')}</span></div>
                <div className="profile-entry-actions">
                  <label className="agent-checkbox" title={item.category === 'preference' ? '停用的偏好不参与评估适配的自动判上' : '停用的条目不会出现在简历工坊的取材列表里'}>
                    <input type="checkbox" checked={item.enabled} disabled={busy === `toggle-${item.id}`} onChange={() => void toggleEnabled(item)} />启用
                  </label>
                  <button type="button" className="text-button" onClick={() => startEdit(item)}>编辑</button>
                  <button type="button" className="text-button danger-text" disabled={busy === `delete-${item.id}`} onClick={() => void removeEntry(item)}>删除</button>
                </div>
              </div>
              <details><summary className="tiny-label">完整内容（{item.content.length} 字）</summary><p className="profile-entry-content">{item.content}</p></details>
              {item.tags.length > 0 && <div className="job-card-meta">{item.tags.map(tag => <span className="job-tags" key={tag}><span>{tag}</span></span>)}</div>}
            </article>)}
          </div>)}
    </section>
  </section>
}
