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
// 布局（2026-11-22 用户反馈「乱」后重排）：单面板上下结构——状态三块等宽对齐、
// 提示合并一块、表单 2×2 网格全宽控件、隐私说明与保存键同行收底。
// 样式全部作用域化（.runway-*）：全局没有裸 label/input 样式，行内流会散架。
const CLAIM_LABELS: Record<string, string> = { '': '没填', not_claimed: '未申领', claimed: '申领中', ineligible: '不符合条件' }
const CLAIM_OPTIONS = Object.entries(CLAIM_LABELS).map(([value, label]) => ({ value: value as RunwayClaim, label }))

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

  const profile = data?.profile || null
  const derived = data?.derived || null
  const hints = derived ? [derived.claim_hint, derived.medical_hint].filter(Boolean) : []
  return <section className="runway-view" aria-label="生存跑道">
    <section className="page-intro compact"><h1 className="sr-only">生存跑道</h1>
      <span className="revision-label">{profile ? `更新于 ${date(profile.updated_at)}` : '还没填过'} · 数据只存本机，不进模型分析</span></section>
    <section className="paper-panel runway-panel" aria-label="生存跑道面板">
      <div className="section-heading"><div><h2>跑道现状</h2>
        <p className="body-small muted">只在两个时刻有用：决定「还能挑多久」和「Offer 能不能接」。不做提醒、不做催办。</p></div></div>
      {profile && derived ? <>
        <div className="runway-figures">
          <div className="runway-stat" title="可用存款 ÷ 月支出；两个数都填了才算得出来">
            <b>{monthsText(derived.runway_months) || '—'}</b><span>跑道（按当前月支出）</span></div>
          <div className="runway-stat" title="社保最后缴纳月到今天的整月数；断缴影响以当地政策为准">
            <b>{derived.paused_months === null ? '—' : `${derived.paused_months} 个月`}</b><span>社保已停缴</span></div>
          <div className="runway-stat" title="失业金申领状态；提示见下方，均为通用口径">
            <b>{CLAIM_LABELS[profile.unemployment_claim] || '没填'}</b><span>失业金状态</span></div>
        </div>
        {hints.length > 0 && <div className="runway-hints">{hints.map(hint => <p key={hint}>{hint}</p>)}</div>}
        {profile.notes && <p className="tiny-label runway-note">备注：{profile.notes}</p>}
        <p className="tiny-label runway-disclaimer">政策类提示均为通用口径，以当地人社部门为准。</p>
      </> : <div className="inline-empty">填一下下面的参数，这里会算出跑道与社保状态。</div>}
      <div className="runway-divider" />
      <div className="section-heading runway-form-heading"><div><h2>我的参数</h2>
        <p className="body-small muted">全部选填；金额只在本页可见。</p></div></div>
      <form className="runway-form" onSubmit={submit}>
        <label className="runway-field"><span>月支出</span>
          <input type="number" min={0} step={100} value={draft.monthly_expense} spellCheck={false}
            onChange={event => setDraft({ ...draft, monthly_expense: event.target.value })} placeholder="如 8000" /></label>
        <label className="runway-field"><span>可用存款</span>
          <input type="number" min={0} step={1000} value={draft.savings} spellCheck={false}
            onChange={event => setDraft({ ...draft, savings: event.target.value })} placeholder="如 100000" /></label>
        <label className="runway-field"><span>社保最后缴纳月</span>
          <input type="month" value={draft.social_security_last_month}
            onChange={event => setDraft({ ...draft, social_security_last_month: event.target.value })} /></label>
        <label className="runway-field"><span>失业金状态</span>
          <select value={draft.unemployment_claim}
            onChange={event => setDraft({ ...draft, unemployment_claim: event.target.value as RunwayClaim })}>
            {CLAIM_OPTIONS.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select></label>
        <label className="runway-field runway-field-wide"><span>备注</span>
          <textarea rows={3} maxLength={500} value={draft.notes} spellCheck={false}
            onChange={event => setDraft({ ...draft, notes: event.target.value })}
            placeholder="如：房租年底到期 / 有副业收入每月约 2000" /></label>
        <div className="runway-footer">
          <p className="tiny-label runway-privacy"><Icon name="check" size={13} />
            这些数字只存进本机数据库；不进简历、不进评估、不发给任何模型。其他页面只会出现「跑道约 N 个月」这类推导值。</p>
          <Button className="button primary" type="submit" busy={busy} disabled={busy}>保存<Icon name="check" size={16} /></Button>
        </div>
      </form>
    </section>
  </section>
}
