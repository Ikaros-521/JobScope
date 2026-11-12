import { useEffect, useState } from 'react'
import { getSystemSettings, saveSystemSettings } from './api'
import type { SystemSettings } from './api'

// 系统设置的模块级共享 store：整站只拉取一次，设置页、聊天工作区、输入框共享同一份缓存。
// 写入走乐观更新 + 后台 PUT；失败回滚并抛错，由调用方 toast。
const DEFAULTS: SystemSettings = {
  chat_display_mode: 'merged',
  show_reasoning: true,
  agent_limits: { max_model_calls: 60, max_tool_calls: 50, context_characters: 160000, max_seconds: 900 },
}

let current: SystemSettings = { ...DEFAULTS, agent_limits: { ...DEFAULTS.agent_limits } }
const listeners = new Set<() => void>()
let loadPromise: Promise<void> | null = null

function emit() { listeners.forEach(listener => listener()) }

export function ensureSystemSettings(): Promise<void> {
  if (!loadPromise) {
    loadPromise = getSystemSettings().then(settings => { current = settings; emit() }).catch(() => { /* 拉取失败保持默认值，不阻塞渲染 */ })
  }
  return loadPromise
}

export function useSystemSettings(): SystemSettings {
  const [settings, setSettings] = useState(current)
  useEffect(() => {
    const listener = () => setSettings(current)
    listeners.add(listener)
    void ensureSystemSettings()
    return () => { listeners.delete(listener) }
  }, [])
  return settings
}

export async function updateSystemSettings(patch: Partial<SystemSettings>): Promise<SystemSettings> {
  const previous = current
  current = { ...current, ...patch, agent_limits: { ...current.agent_limits, ...(patch.agent_limits || {}) } }
  emit()
  try {
    current = await saveSystemSettings(current)
  } catch (err) {
    current = previous
    emit()
    throw err
  }
  emit()
  return current
}
