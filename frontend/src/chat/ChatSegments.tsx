import type { ChatSegment, ToolStep } from '../api'
import { Markdown } from './Markdown'
import { ReasoningBlock } from './ReasoningBlock'
import { StepRow } from './ToolTimeline'

// 顺序显示模式：思维链/正文/工具步骤按发生顺序穿插；每段思维链独立成卡（被打断即换卡）。
// 流式时只有最后一段是 live（自动展开跟随），更早的段随 live 失效自动折叠。
export function ChatSegments({ segments, live, showReasoning }: { segments: ChatSegment[]; live?: boolean; showReasoning: boolean }) {
  if (!segments.length) return null
  const streamingIndex = live ? segments.length - 1 : -1
  return <div className="chat-segments">
    {segments.map((segment, index) => {
      if (segment.kind === 'step') {
        // 落库的 step 段把 ToolStep 挂在 event 键下（general_agent._event），流式增量的 step 段才是 step 键：两种形状都认。
        const step = (segment.step ?? segment.event) as ToolStep | undefined
        if (!step) return null
        return <StepRow key={`step-${step.step}`} step={step} open={false} />
      }
      if (segment.kind === 'reasoning') {
        if (!showReasoning) return null
        return <ReasoningBlock key={`reasoning-${index}`} text={segment.text} live={index === streamingIndex} />
      }
      return <div key={`text-${index}`} className="message-content segment-text"><Markdown content={segment.text} /></div>
    })}
  </div>
}
