import { useEffect, useState } from 'react'

// 公司横评的已选列表：模块级共享 store + localStorage 持久化。
// 横评页是条件渲染（切页即卸载），选择放这里才能跨页面、跨刷新保留；
// 公司资料库/岗位详情的「加入横评」按钮也直接写这份 store，不经过页面跳转传参。
const STORAGE_KEY = 'jobscope.compare-selected'
// 雷达图再多家就糊成一团、条形图也放不下：横评上限的唯一事实源。
export const MAX_COMPANIES = 8

function load(): string[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw)
    if (!Array.isArray(parsed)) return []
    return parsed.filter((item): item is string => typeof item === 'string' && item.trim() !== '').slice(0, MAX_COMPANIES)
  } catch { return [] }
}
function persist() {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(current)) } catch { /* 隐私模式等写入失败时只留在内存 */ }
}

let current: string[] = load()
const listeners = new Set<() => void>()

function emit() { listeners.forEach(listener => listener()) }

export function useCompareSelection(): string[] {
  const [names, setNames] = useState(current)
  useEffect(() => {
    const listener = () => setNames(current)
    listeners.add(listener)
    return () => { listeners.delete(listener) }
  }, [])
  return names
}

export type AddCompareResult = 'added' | 'exists' | 'full'

export function addCompareCompany(name: string): AddCompareResult {
  const trimmed = name.trim()
  if (!trimmed) return 'exists'
  if (current.includes(trimmed)) return 'exists'
  if (current.length >= MAX_COMPANIES) return 'full'
  current = [...current, trimmed]
  persist(); emit()
  return 'added'
}

export function removeCompareCompany(name: string) {
  current = current.filter(item => item !== name)
  persist(); emit()
}
