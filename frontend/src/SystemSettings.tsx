import { useEffect, useRef, useState } from 'react'
import { updateSystemSettings, useSystemSettings } from './systemSettingsStore'
import { createBackup, date, deleteBackup, downloadBackup, getBackupStatus, restoreBackup } from './api'
import type { AgentLimits, BackupStatus, SystemSettings as SystemSettingsPayload } from './api'
import { toast } from './toast'
import { Icon } from './Icon'

const LIMIT_FIELDS: { key: keyof AgentLimits; label: string; help: string; min: number; max: number; unit: string }[] = [
  { key: 'max_model_calls', label: '模型轮数上限', help: '一次回答最多进行多少轮模型调用，达到即收尾', min: 1, max: 200, unit: '轮' },
  { key: 'max_tool_calls', label: '工具调用上限', help: '一次回答最多执行多少次工具（含 MCP）', min: 1, max: 300, unit: '次' },
  { key: 'context_characters', label: '上下文字符上限', help: '工具结果累计占用上下文的额度，超出后不再执行新工具', min: 10000, max: 1000000, unit: '字符' },
  { key: 'max_seconds', label: '执行时长上限', help: '一次回答的最长运行时间', min: 60, max: 7200, unit: '秒' },
]

const DEFAULT_LIMITS: AgentLimits = { max_model_calls: 60, max_tool_calls: 50, context_characters: 160000, max_seconds: 900 }

const DISPLAY_MODES: { value: SystemSettingsPayload['chat_display_mode']; label: string; desc: string }[] = [
  { value: 'merged', label: '合并显示', desc: '思维链聚成一段、工具汇总成「执行过程」折叠条，正文压轴' },
  { value: 'sequential', label: '顺序显示', desc: '像 AI IDE：思维链、工具、正文按发生顺序穿插；思维链被打断即换新卡片' },
]

function formatSize(size: number) {
  if (size >= 1024 * 1024) return `${(size / 1024 / 1024).toFixed(1)} MB`
  if (size >= 1024) return `${Math.round(size / 1024)} KB`
  return `${size} B`
}

// 数据备份（2026-11-18）：在线快照 → zip 存 .data/backups（保留最近 10 份）；
// 恢复前自动落「恢复前」安全备份，选错也能退回。浏览器工作目录可再生，不入备份。
function BackupPanel() {
  const [status, setStatus] = useState<BackupStatus | null>(null)
  const [busy, setBusy] = useState('')
  const [pendingRestore, setPendingRestore] = useState<{ file: File } | null>(null)
  const fileInput = useRef<HTMLInputElement | null>(null)

  async function load() {
    try { setStatus(await getBackupStatus()) } catch { /* 面板失败不阻塞其他设置 */ }
  }
  useEffect(() => { void load() }, [])

  function backupNow() {
    setBusy('create')
    void createBackup().then(result => { toast(`已备份：${result.backup.name}`); return load() })
      .catch((err: Error) => toast(err.message, 'error'))
      .finally(() => setBusy(''))
  }

  function remove(id: string, name: string) {
    setBusy(`delete-${id}`)
    void deleteBackup(id).then(() => { toast(`已删除 ${name}`); return load() })
      .catch((err: Error) => toast(err.message, 'error'))
      .finally(() => setBusy(''))
  }

  function pickRestore(event: { target: { files: FileList | null; value: string } }) {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (file) setPendingRestore({ file })
  }

  function runRestore() {
    if (!pendingRestore) return
    setBusy('restore')
    void restoreBackup(pendingRestore.file)
      .then(result => {
        toast(`已恢复（${result.jobs} 份岗位、${result.evidence} 条证据）；已自动留存恢复前备份 ${result.safety_backup}，建议刷新页面。`, 'notice', false, { label: '刷新页面', onClick: () => window.location.reload() })
        setPendingRestore(null)
        return load()
      })
      .catch((err: Error) => toast(err.message, 'error'))
      .finally(() => setBusy(''))
  }

  const last = status?.last_backup_at ? `最近备份 ${date(status.last_backup_at)}${status.days_since !== null ? ` · ${status.days_since < 1 ? '不到 1 天前' : `${Math.floor(status.days_since)} 天前`}` : ''}` : '还没有备份过'
  return <div className="paper-panel system-panel" aria-label="数据备份">
    <div className="section-heading"><div><h2>数据备份与恢复</h2><p className="body-small muted">岗位、证据、报告与对话都在本机一个数据库文件里。备份打包成 zip 存在数据目录的 backups 文件夹（保留最近 {status?.retention ?? 10} 份），可下载另行保管；恢复会整体替换当前数据，并先自动留存一份「恢复前」备份。</p></div></div>
    <div className="backup-actions">
      <span className="body-small muted">{last}</span>
      <div className="backup-action-buttons">
        <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={backupNow}>{busy === 'create' ? '正在备份…' : '立即备份'}</button>
        <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={() => fileInput.current?.click()}>从备份恢复…</button>
        <input ref={fileInput} type="file" accept=".zip" className="sr-only" aria-label="选择备份包" onChange={pickRestore} />
      </div>
    </div>
    {status && status.backups.length > 0 && <ul className="backup-list">
      {status.backups.map(item => <li key={item.id}>
        <div className="backup-item-name">
          <span>{item.name}</span>
          {item.pre_restore && <span className="tiny-label">恢复前安全备份</span>}
        </div>
        <span className="tiny-label">{formatSize(item.size)} · {date(item.created_at)}</span>
        <span className="backup-item-actions">
          <button type="button" className="text-button" disabled={Boolean(busy)} onClick={() => void downloadBackup(item)}>下载</button>
          <button type="button" className="text-button danger-text" disabled={Boolean(busy)} onClick={() => remove(item.id, item.name)}>删除</button>
        </span>
      </li>)}
    </ul>}
    {pendingRestore && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) setPendingRestore(null) }}>
      <section className="modal paper-panel" role="dialog" aria-modal="true" aria-labelledby="restore-title" onKeyDown={event => { if (event.key === 'Escape' && !busy) setPendingRestore(null) }}>
        <div className="section-heading"><div><h2 id="restore-title">确认恢复备份</h2></div><button className="icon-button" aria-label="关闭弹窗" onClick={() => setPendingRestore(null)} disabled={Boolean(busy)}><Icon name="close" /></button></div>
        <p>将用 <b>{pendingRestore.file.name}</b>（{formatSize(pendingRestore.file.size)}）整体替换当前全部数据。恢复前会自动留存一份「恢复前」备份，误选也能退回。恢复完成后建议刷新页面。</p>
        <div className="modal-actions">
          <button type="button" className="button secondary" onClick={() => setPendingRestore(null)} disabled={Boolean(busy)}>取消</button>
          <button type="button" className="button danger" disabled={Boolean(busy)} onClick={runRestore}>{busy === 'restore' ? '正在恢复…' : '确认恢复'}</button>
        </div>
      </section>
    </div>}
  </div>
}

export function SystemSettings() {
  const settings = useSystemSettings()
  const [limits, setLimits] = useState<AgentLimits>(settings.agent_limits)
  const [savingLimits, setSavingLimits] = useState(false)
  useEffect(() => { setLimits(settings.agent_limits) }, [settings.agent_limits])
  const limitsDirty = JSON.stringify(limits) !== JSON.stringify(settings.agent_limits)

  function pickMode(mode: SystemSettingsPayload['chat_display_mode']) {
    if (settings.chat_display_mode === mode) return
    void updateSystemSettings({ chat_display_mode: mode })
      .then(() => toast(`已切换为「${mode === 'sequential' ? '顺序显示' : '合并显示'}」，新对话即时生效`))
      .catch(err => toast((err as Error).message || '设置保存失败，请稍后重试', 'error'))
  }

  function toggleReasoning(value: boolean) {
    void updateSystemSettings({ show_reasoning: value })
      .catch(err => toast((err as Error).message || '设置保存失败，请稍后重试', 'error'))
  }

  async function saveLimits() {
    setSavingLimits(true)
    try {
      const saved = await updateSystemSettings({ agent_limits: limits })
      setLimits(saved.agent_limits)
      toast('Agent 执行限额已保存，下一次提问生效')
    } catch (err) {
      toast((err as Error).message || '设置保存失败，请稍后重试', 'error')
    } finally {
      setSavingLimits(false)
    }
  }

  return <section className="system-settings" aria-label="系统设置">
    <div className="paper-panel system-panel">
      <div className="section-heading"><div><h2>聊天显示</h2><p className="body-small muted">对全局、公司与岗位对话同时生效；随时切换，历史消息按所选模式重新呈现。</p></div></div>
      <div className="mode-picks display-mode-picks" role="group" aria-label="聊天显示模式">
        {DISPLAY_MODES.map(mode => <button type="button" key={mode.value} aria-pressed={settings.chat_display_mode === mode.value} onClick={() => pickMode(mode.value)}>
          <strong>{mode.label}</strong>
          <span>{mode.desc}</span>
        </button>)}
      </div>
      <label className="agent-checkbox system-reasoning-toggle">
        <input type="checkbox" checked={settings.show_reasoning} onChange={event => toggleReasoning(event.target.checked)} />
        <span>显示思维链 <span className="muted">关闭后对话不再展示「思考过程」内容；输入框旁的开关与此处同步</span></span>
      </label>
    </div>

    <div className="paper-panel system-panel">
      <div className="section-heading"><div><h2>Agent 执行限额</h2><p className="body-small muted">防止单次回答无休止调用；多步浏览器类任务可以调高。保存后对下一次提问生效。</p></div></div>
      <div className="system-limits-grid">
        {LIMIT_FIELDS.map(field => <div className="system-limit-field" key={field.key}>
          <label className="field-label" htmlFor={`limit-${field.key}`}>{field.label} <span className="muted">{field.unit}</span></label>
          <input id={`limit-${field.key}`} type="number" inputMode="numeric" min={field.min} max={field.max}
            value={limits[field.key]}
            onChange={event => setLimits(current => ({ ...current, [field.key]: Math.max(field.min, Math.min(field.max, Number(event.target.value) || field.min)) }))} />
          <p className="field-help">{field.help} · 允许 {field.min.toLocaleString()}–{field.max.toLocaleString()}</p>
        </div>)}
      </div>
      <div className="model-settings-actions system-limits-actions">
        <span className="body-small muted">{limitsDirty ? '有未保存的更改' : '与已保存设置一致'}</span>
        <button type="button" className="button secondary" disabled={!limitsDirty && JSON.stringify(settings.agent_limits) === JSON.stringify(DEFAULT_LIMITS)} onClick={() => setLimits({ ...DEFAULT_LIMITS })}>恢复默认</button>
        <button type="button" className="button primary" disabled={!limitsDirty || savingLimits} onClick={() => void saveLimits()}>{savingLimits ? '正在保存…' : '保存限额'}</button>
      </div>
    </div>

    <BackupPanel />
  </section>
}
