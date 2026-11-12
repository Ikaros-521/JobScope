import { useEffect, useState } from 'react'
import { updateSystemSettings, useSystemSettings } from './systemSettingsStore'
import type { AgentLimits, SystemSettings as SystemSettingsPayload } from './api'
import { toast } from './toast'

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
  </section>
}
