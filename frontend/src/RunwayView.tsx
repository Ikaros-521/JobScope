import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { date, getRunway, safeUrl, saveRunway } from './api'
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

// 失业金申领指引（2026-11-22）：静态通用口径——只写社会保险法框架内稳定的事实
// （缴费满 1 年 / 最长 24 个月 / 官方渠道），城市差异全部收口到「以参保地口径为准」。
// 纯前端常量：不进模型素材池、不做城市专属 URL 硬编（会过期），只给稳定官方入口。
const SI_PLATFORM_URL = 'https://si.12333.gov.cn'
const CLAIM_GUIDE = {
  conditions: [
    '失业前用人单位和本人已缴纳失业保险费满 1 年（含视同缴费年限）',
    '非因本人意愿中断就业：合同期满、被单位解除/裁员都算；主动辞职一般不符合——但因单位拖欠工资、未缴社保等原因被迫解除劳动合同的属例外（劳动合同法第 38 条）',
    '已办理失业登记，并有求职要求',
  ],
  steps: [
    '办失业登记：线上可在「掌上 12333」/电子社保卡/当地人社平台办理，也可去街道（乡镇）服务大厅',
    '提交申领：任选下面任一渠道，按提示选参保地、填收款账户（一般是社保卡金融账户——先确认金融功能已激活）',
    '等审核：经办机构审核通过后按月发放',
    '按月认证：领取期间按当地要求做月度资格认证；重新就业/恢复参保要如实申报，否则可能追回',
  ],
  channels: [
    { label: '国家社会保险公共服务平台', url: SI_PLATFORM_URL },
    { label: '「掌上 12333」APP（手机应用商店）', url: '' },
    { label: '电子社保卡（微信 / 支付宝小程序）', url: '' },
    { label: '线下：参保地社保经办机构或街道（乡镇）服务大厅', url: '' },
  ],
  notes: [
    '领取期限按累计缴费年限确定（最长 24 个月），金额与当地最低工资等挂钩——具体以参保地人社部门口径为准',
    '有申领时限，失业后越早办越稳；停缴的月份不补缴，已算好的年限不受影响',
    '「非因本人意愿中断就业」是审核重点：离职证明 / 解除劳动合同证明务必留好',
  ],
}

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
        <details className="runway-guide">
          <summary>失业金怎么领：通用申领指引（点开看）</summary>
          <div className="runway-guide-body">
            <h4>三个条件</h4>
            <ul>{CLAIM_GUIDE.conditions.map(item => <li key={item}>{item}</li>)}</ul>
            <h4>四步流程</h4>
            <ol>{CLAIM_GUIDE.steps.map(item => <li key={item}>{item}</li>)}</ol>
            <h4>申领渠道（任选其一）</h4>
            <ul>{CLAIM_GUIDE.channels.map(item => <li key={item.label}>
              {item.url && safeUrl(item.url)
                ? <>「{item.label}」：<a href={safeUrl(item.url)} target="_blank" rel="noreferrer">{safeUrl(item.url)?.replace(/^https?:\/\//, '')}</a></>
                : item.label}</li>)}</ul>
            <h4>几个坑</h4>
            <ul>{CLAIM_GUIDE.notes.map(item => <li key={item}>{item}</li>)}</ul>
          </div>
        </details>
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
