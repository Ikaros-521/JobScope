import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { date, getRunway, saveRunway } from './api'
import type { RunwayClaim, RunwayPayload } from './api'
import { toast } from './toast'
import { Button } from './ui'
import { Icon } from './Icon'

// 生存跑道（2026-11-22 P2）：长期求职者比较 Offer 的真实约束是现金流——
// 「这个 offer 能不能接」往往是「跑道还剩几个月」。本页是唯一的录入与展示入口：
// 数据只存本机、绝不进模型素材池（不进资料库取材/评估事实池，后端硬隔离），
// 其他页面只出现推导值（跑道月数），金额原值不出本页。
const CLAIM_OPTIONS: { value: RunwayClaim; label: string }[] = [
  { value: '', label: '没填' },
  { value: 'not_claimed', label: '未申领' },
  { value: 'claimed', label: '申领中' },
  { value: 'ineligible', label: '不符合条件' },
]

const EMPTY_DRAFT = { monthly_expense: '', savings: '', social_security_last_month: '', unemployment_claim: '' as RunwayClaim, notes: '' }

function monthsText(months: number | null) {
  if (months === null) return null
  if (months === 0) return '不足 1 个月'
  if (months < 1) return `约 ${Math.round(months * 30)} 天`
  return `约 ${Math.round(months)} 个月`
}

export function RunwayView() {
  const [data, setData] = useState<RunwayPayload | null>(null)
  const [draft, setDraft] = useState(EMPTY_DRAFT)
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    try {
      const payload = await getRunway()
      setData(payload)
      const profile = payload.profile
      if (profile) {
        setDraft({
          monthly_expense: profile.monthly_expense === null ? '' : String(profile.monthly_expense),
          savings: profile.savings === null ? '' : String(profile.savings),
          social_security_last_month: profile.social_security_last_month || '',
          unemployment_claim: profile.unemployment_claim || '',
          notes: profile.notes || '',
        })
      }
    } catch (err) { toast((err as Error).message, 'error') }
  }, [])
  useEffect(() => { void load() }, [load])

  function submit(event: FormEvent) {
    event.preventDefault()
    const expense = draft.monthly_expense.trim() === '' ? null : Number(draft.monthly_expense)
    const savings = draft.savings.trim() === '' ? null : Number(draft.savings)
    if ((expense !== null && (!Number.isFinite(expense) || expense < 0)) || (savings !== null && (!Number.isFinite(savings) || savings < 0))) {
      toast('金额请填非负数字（留空表示不填）', 'error'); return
    }
    setBusy(true)
    void saveRunway({
      monthly_expense: expense, savings, social_security_last_month: draft.social_security_last_month,
      unemployment_claim: draft.unemployment_claim, notes: draft.notes.trim(),
    })
      .then(payload => { setData(payload); toast('已保存——只写进本机数据库', 'notice') })
      .catch((err: Error) => toast(err.message, 'error'))
      .finally(() => setBusy(false))
  }

  const derived = data?.derived || null
  return <section className="runway-view" aria-label="生存跑道">
    <section className="page-intro compact"><h1 className="sr-only">生存跑道</h1>
      <span className="revision-label">{data?.profile ? `更新于 ${date(data.profile.updated_at)}` : '还没填过'} · 数据只存本机，不进模型分析</span></section>
    <div className="runway-layout">
      <section className="paper-panel runway-summary" aria-label="跑道现状">
        <div className="section-heading"><div><h2>跑道现状</h2>
          <p className="body-small muted">只在两个时刻有用：决定「还能挑多久」和「Offer 能不能接」。不做提醒、不做催办。</p></div></div>
        {!data?.profile || derived === null ? <div className="inline-empty">填一下右边的数字，这里会算出跑道与社保状态。</div> : <>
          <div className="runway-figures">
            <div className="runway-figure" title="可用存款 ÷ 月支出；两个数都填了才算得出来">
              <b>{monthsText(derived.runway_months) || '—'}</b><span>跑道（按当前月支出）</span></div>
            <div className="runway-figure" title="社保最后缴纳月到今天的整月数；断缴影响以当地政策为准">
              <b>{derived.paused_months === null ? '—' : `${derived.paused_months} 个月`}</b><span>社保已停缴</span></div>
          </div>
          {derived.claim_hint && <p className="runway-hint">{derived.claim_hint}</p>}
          {derived.medical_hint && <p className="runway-hint">{derived.medical_hint}</p>}
          {data.profile.notes && <p className="tiny-label">备注：{data.profile.notes}</p>}
          <p className="tiny-label">政策类提示均为通用口径，以当地人社部门为准。</p>
        </>}
      </section>
      <section className="paper-panel runway-editor" aria-label="跑道参数">
        <div className="section-heading"><div><h2>我的参数</h2></div></div>
        <form onSubmit={submit}>
          <div className="form-two">
            <label>月支出（选填）<input type="number" min={0} step="100" value={draft.monthly_expense}
              onChange={event => setDraft({ ...draft, monthly_expense: event.target.value })} placeholder="如 8000" spellCheck={false} /></label>
            <label>可用存款（选填）<input type="number" min={0} step="1000" value={draft.savings}
              onChange={event => setDraft({ ...draft, savings: event.target.value })} placeholder="如 100000" spellCheck={false} /></label>
          </div>
          <div className="form-two">
            <label>社保最后缴纳月（选填）<input type="month" value={draft.social_security_last_month}
              onChange={event => setDraft({ ...draft, social_security_last_month: event.target.value })} /></label>
            <label>失业金状态（选填）<select value={draft.unemployment_claim}
              onChange={event => setDraft({ ...draft, unemployment_claim: event.target.value as RunwayClaim })}>
              {CLAIM_OPTIONS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select></label>
          </div>
          <label>备注（选填）<textarea value={draft.notes} rows={3} maxLength={500}
            onChange={event => setDraft({ ...draft, notes: event.target.value })} placeholder="如：房租年底到期 / 有副业收入每月约 2000" spellCheck={false} /></label>
          <p className="tiny-label runway-privacy"><Icon name="check" size={13} />这些数字只存进本机数据库；不进简历、不进评估、不发给任何模型。其他页面只会出现「跑道约 N 个月」这类推导值。</p>
          <div className="modal-actions">
            <Button className="button primary" type="submit" busy={busy} disabled={busy}>保存<Icon name="check" size={16} /></Button>
          </div>
        </form>
      </section>
    </div>
  </section>
}
