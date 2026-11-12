import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent as ReactKeyboardEvent, TouchEvent as ReactTouchEvent, WheelEvent as ReactWheelEvent } from 'react'

// 与主聊天线程同一套阈值吸底策略：距底 ≤80px 跟随，上翻解除，滚回恢复。
const REASONING_STICK_THRESHOLD = 80

export function ReasoningBlock({ text, live }: { text: string; live?: boolean }) {
  const [open, setOpen] = useState(Boolean(live))
  const [showJump, setShowJump] = useState(false)
  const wasLive = useRef(Boolean(live))
  const bodyRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)
  const touchYRef = useRef<number | null>(null)
  useEffect(() => {
    if (live && !wasLive.current) setOpen(true)
    if (!live && wasLive.current) setOpen(false)
    wasLive.current = Boolean(live)
  }, [live])

  const measureStick = () => {
    const body = bodyRef.current
    if (!body) return
    const distance = body.scrollHeight - body.scrollTop - body.clientHeight
    const stick = distance <= REASONING_STICK_THRESHOLD
    stickRef.current = stick
    // 只在流式思维链里提供回到底部；历史内容用户自由阅读。
    setShowJump(Boolean(live) && open && !stick)
  }
  const scrollToBottom = () => {
    const body = bodyRef.current
    // 即时滚动：内嵌浏览器后台面板会冻结 smooth 动画。
    if (body) body.scrollTop = body.scrollHeight
  }
  const pauseStick = () => { stickRef.current = false; setShowJump(true) }
  const measureDeferred = () => window.setTimeout(measureStick, 60)
  const onWheel = (event: ReactWheelEvent<HTMLDivElement>) => {
    if (event.deltaY < 0) pauseStick()
    else measureDeferred()
  }
  const onTouchStart = (event: ReactTouchEvent<HTMLDivElement>) => { touchYRef.current = event.touches[0]?.clientY ?? null }
  const onTouchMove = (event: ReactTouchEvent<HTMLDivElement>) => {
    const current = event.touches[0]?.clientY ?? null
    if (touchYRef.current !== null && current !== null && current > touchYRef.current) pauseStick()
    touchYRef.current = current
    measureDeferred()
  }
  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (['PageUp', 'Home', 'ArrowUp'].includes(event.key)) pauseStick()
    else if (['PageDown', 'End', 'ArrowDown'].includes(event.key)) measureDeferred()
  }

  // 流式文本增长时跟随；details 从折叠到展开后高度才真实存在，也要落一次底。
  useEffect(() => {
    if (!open || !live || !stickRef.current) return
    scrollToBottom()
    const timer = window.setTimeout(() => { if (stickRef.current && live) scrollToBottom() }, 120)
    return () => window.clearTimeout(timer)
  }, [text, open, live])

  if (!text) return null
  return <details className="reasoning-block" open={open} onToggle={event => setOpen((event.target as HTMLDetailsElement).open)}>
    <summary>{live ? '正在思考…' : '思考过程'}<span className="reasoning-meta">{text.length.toLocaleString()} 字符</span></summary>
    <div className="reasoning-body-wrap">
      <div className="reasoning-body" ref={bodyRef} tabIndex={-1}
        onScroll={measureStick} onWheel={onWheel} onTouchStart={onTouchStart} onTouchMove={onTouchMove} onKeyDown={onKeyDown}
      >{text}</div>
      {showJump && <button type="button" className="reasoning-jump" onClick={() => { stickRef.current = true; setShowJump(false); scrollToBottom() }}>↓ 回到底部</button>}
    </div>
  </details>
}
