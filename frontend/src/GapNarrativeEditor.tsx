import { useEffect, useState } from 'react'
import { date, generateGapNarrative, listGapNarratives } from './api'
import type { GapNarrative } from './api'
import { toast } from './toast'
import { Button } from './ui'
import { Icon } from './Icon'

// 空窗期叙事面板（2026-11-22）：把资料库「空窗经历」条目翻译成可直接使用的三份产物——
// 电梯陈述 / 简历空窗期段落 / 高频追问应答。只使用资料库已申报的事实，不编造；
// 模型不可用时后端退规则骨架（origin 如实标注）。生成即落版本，保留最近 8 版供回看。
const ORIGIN_NAMES: Record<string, string> = { model: '模型生成', rules: '规则骨架' }

const BLOCKS = [
  { key: 'pitch', label: '一句话电梯陈述', hint: '向面试官解释这段空窗，先事实后姿态' },
  { key: 'resume_block', label: '简历空窗期段落', hint: '可直接粘进简历的时间段 bullet' },
  { key: 'interview_block', label: '高频追问应答', hint: '四个必被问到的问题，口语化第一人称' },
] as const

function NarrativeBlock({ label, hint, text }: { label: string; hint: string; text: string }) {
  const copy = async () => {
    await navigator.clipboard.writeText(text)
    toast('已复制')
  }
  return <div className="gap-block">
    <div className="gap-block-head">
      <b>{label}</b>
      <span className="tiny-label">{hint}</span>
      <button type="button" className="text-button" disabled={!text} onClick={() => void copy()}>复制</button>
    </div>
    {text ? <p className="gap-block-text">{text}</p> : <p className="tiny-label">（空）</p>}
  </div>
}

export function GapNarrativeEditor({ onClose }: { onClose: () => void }) {
  const [narratives, setNarratives] = useState<GapNarrative[] | null>(null)
  const [busy, setBusy] = useState('')
  const [target, setTarget] = useState('')

  useEffect(() => {
    void listGapNarratives().then(data => setNarratives(data.narratives)).catch(() => setNarratives([]))
  }, [])

  function run(key: string, action: () => Promise<void>) {
    setBusy(key)
    void action().catch((err: Error) => toast(err.message, 'error')).finally(() => setBusy(''))
  }

  const generate = () => run('generate', async () => {
    const narrative = await generateGapNarrative(target.trim() ? { job_title: target.trim() } : {})
    toast('已生成空窗期叙事，三段产物可直接取用', 'notice')
    setNarratives(previous => [narrative, ...(previous || [])])
  })

  const latest = narratives?.[0] || null
  const older = (narratives || []).slice(1)
  return <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) onClose() }}>
    <section className="modal paper-panel outreach-editor gap-editor" role="dialog" aria-modal="true" aria-labelledby="gap-title"
      onKeyDown={event => { if (event.key === 'Escape' && !busy) onClose() }}>
      <div className="section-heading"><div><h2 id="gap-title">空窗期叙事</h2></div>
        <button className="icon-button" aria-label="关闭弹窗" onClick={onClose} disabled={Boolean(busy)}><Icon name="close" /></button></div>
      <p className="muted body-small">素材只来自资料库启用的「空窗经历」条目——先补事实再生成；面试官问到空窗时，答案就在这里。</p>
      <div className="outreach-kinds">
        <input className="gap-target-input" value={target} onChange={event => setTarget(event.target.value)}
          maxLength={300} placeholder="目标岗位（可选，对齐 JD 用语）" aria-label="目标岗位" spellCheck={false} />
        <Button className="button primary small outreach-generate" type="button" busy={busy === 'generate'} disabled={Boolean(busy)}
          onClick={generate}>生成<Icon name="wand" size={14} /></Button>
      </div>
      {narratives === null ? <p className="muted body-small">正在读取历史版本…</p>
        : !latest ? <p className="muted body-small outreach-empty">还没有生成过。在资料库补 1–2 条「空窗经历」条目（时间段+做了什么+产出），点「生成」。</p>
        : <>
          <div className="gap-latest">
            {BLOCKS.map(block => <NarrativeBlock key={block.key} label={block.label} hint={block.hint} text={latest.payload[block.key]} />)}
            <span className="tiny-label">{ORIGIN_NAMES[latest.origin] || latest.origin}
              {latest.job_title ? ` · 目标 ${latest.job_title}` : ''} · {date(latest.created_at)} · 素材条目：{latest.payload.entry_titles.join('、') || '—'}</span>
          </div>
          {older.length > 0 && <details className="gap-history">
            <summary className="tiny-label">历史版本（{older.length}）</summary>
            {older.map(item => <details key={item.id} className="gap-history-item">
              <summary className="tiny-label">{date(item.created_at)} · {ORIGIN_NAMES[item.origin] || item.origin}{item.job_title ? ` · ${item.job_title}` : ''}</summary>
              <NarrativeBlock label="简历空窗期段落" hint="" text={item.payload.resume_block} />
              <NarrativeBlock label="高频追问应答" hint="" text={item.payload.interview_block} />
            </details>)}
          </details>}
        </>}
    </section>
  </div>
}
