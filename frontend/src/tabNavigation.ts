import type { KeyboardEvent } from 'react'

export function navigateTabs(event: KeyboardEvent<HTMLDivElement>) {
  const vertical = event.currentTarget.getAttribute('aria-orientation') === 'vertical'
  const previous = vertical ? 'ArrowUp' : 'ArrowLeft'
  const next = vertical ? 'ArrowDown' : 'ArrowRight'
  if (![previous, next, 'Home', 'End'].includes(event.key)) return
  const tabs = Array.from(event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="tab"]'))
  const index = tabs.indexOf(document.activeElement as HTMLButtonElement)
  if (index < 0) return
  event.preventDefault()
  const target = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
    : (index + (event.key === next ? 1 : -1) + tabs.length) % tabs.length
  tabs[target]?.focus()
  tabs[target]?.click()
}
