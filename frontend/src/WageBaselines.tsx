import { useCallback, useEffect, useState } from 'react'
import { deleteWageBaseline, importWageBaselines, listWageBaselines } from './api'
import type { WageBaseline } from './api'
import { toast } from './toast'

// 工资基线（2026-11-17）：人社局工资指导价位的手动导入。作为 pay 维度同城同层对照样本
// 不足时的兜底基线；与招聘报价口径不同，页面与报告都会写明出处与口径差异。
// 只做手动导入（用户自己下载官方发布物），不做任何官网抓取。
const SAMPLE_CSV = '城市,职类,学历,月薪中位数,年份,备注\n杭州,技术研发,本科,18000,2025,2025年人力资源市场工资价位\n杭州,技术研发,大专,12000,2025,'
const ROLE_HINTS = ['审核认证', '技术研发', '产品与设计', '销售与市场', '人事与财务', '运营与客服']
const EDU_HINTS = ['不限', '博士', '硕士', '本科', '大专', '高中及以下']

export function WageBaselines({ refreshMetadata }: { refreshMetadata: () => Promise<void> }) {
  const [rows, setRows] = useState<WageBaseline[] | null>(null)
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [errors, setErrors] = useState<string[]>([])
  const [expanded, setExpanded] = useState(false)

  const load = useCallback(async () => {
    try { setRows((await listWageBaselines()).baselines) }
    catch (err) { toast((err as Error).message, 'error') }
  }, [])
  useEffect(() => { void load() }, [load])

  async function submit() {
    if (!draft.trim()) { toast('先粘贴 CSV 内容或选择文件', 'error'); return }
    setBusy(true)
    setErrors([])
    try {
      const result = await importWageBaselines(draft)
      if (result.saved) toast(`已导入 ${result.saved} 条工资基线（同键同年在原位更新）。`)
      setErrors(result.errors)
      setDraft('')
      await load()
      await refreshMetadata()
    } catch (err) { toast((err as Error).message, 'error') }
    finally { setBusy(false) }
  }

  async function remove(row: WageBaseline) {
    try {
      await deleteWageBaseline(row.id)
      await load()
    } catch (err) { toast((err as Error).message, 'error') }
  }

  return <section className="paper-panel wage-baseline-panel">
    <div className="section-heading">
      <div><h2>工资基线（人社局指导价位）</h2>
        <p className="muted body-small">把官方发布的工资指导价位按「城市 × 职类 × 学历」导入本机；某公司同城同层的招聘报价样本不足 5 条时，报告自动改用基线作对照并写明口径差异。只做手动导入，本项目不抓取官网。</p></div>
      <button className="button secondary small" type="button" onClick={() => setExpanded(value => !value)}>{expanded ? '收起导入' : rows?.length ? '导入更多' : '导入'}</button>
    </div>
    {expanded && <div className="wage-import">
      <ol className="wage-import-steps">
        <li>从当地人社局官网下载工资价位表（常见为 职业×学历/分位值 的表格），整理成下方 CSV；「职类」请归入：{ROLE_HINTS.join('、')}；「学历」请归入：{EDU_HINTS.join('、')}。</li>
        <li>粘贴 CSV（表头：城市,职类,学历,月薪中位数,年份,备注）或选择文件，导入后即可在报告中生效。</li>
      </ol>
      <textarea value={draft} onChange={event => setDraft(event.target.value)} rows={5}
        placeholder={SAMPLE_CSV} aria-label="工资基线 CSV 内容" maxLength={300000} />
      <div className="wage-import-actions">
        <input type="file" accept=".csv,.txt" aria-label="选择 CSV 文件" onChange={async event => {
          const file = event.target.files?.[0]
          if (!file) return
          if (file.size > 5 * 1024 * 1024) { toast('文件超过 5 MB，请拆分后导入', 'error'); return }
          setDraft(await file.text())
          event.target.value = ''
        }} />
        <button className="button primary small" type="button" disabled={busy} onClick={() => void submit()}>{busy ? '导入中…' : '导入'}</button>
        <button className="button secondary small" type="button" disabled={busy} onClick={() => setDraft(SAMPLE_CSV)}>填入示例</button>
      </div>
      {errors.length > 0 && <div className="wage-import-errors" role="alert">
        <strong>{errors.length} 行未导入：</strong>
        <ul>{errors.slice(0, 20).map((line, index) => <li key={index}>{line}</li>)}</ul>
        {errors.length > 20 && <p className="tiny-label">其余 {errors.length - 20} 条错误已省略，修正后可重新导入。</p>}
      </div>}
    </div>}
    {rows === null
      ? <div className="loading-state"><span className="spinner" />正在读取工资基线…</div>
      : rows.length
        ? <div className="task-table-wrap"><table>
          <thead><tr><th scope="col">城市</th><th scope="col">职类</th><th scope="col">学历</th><th scope="col">月薪中位数</th><th scope="col">年份</th><th scope="col">备注</th><th scope="col"><span className="sr-only">操作</span></th></tr></thead>
          <tbody>{rows.map(row => <tr key={row.id}>
            <td data-label="城市">{row.city}</td>
            <td data-label="职类">{row.role}</td>
            <td data-label="学历">{row.education}</td>
            <td data-label="月薪中位数">{row.monthly_median.toLocaleString('zh-CN')} 元/月</td>
            <td data-label="年份" className="nowrap">{row.year}</td>
            <td data-label="备注" className="task-message">{row.note || '—'}</td>
            <td data-label="操作" className="nowrap"><button className="text-button danger-text" onClick={() => void remove(row)}>删除</button></td>
          </tr>)}</tbody>
        </table></div>
        : <p className="inline-empty">还没有导入工资基线。没有基线时，薪资对比完全依赖已采集的同城同层报价样本。</p>}
    <p className="tiny-label">口径说明：官方指导价位是人社部门调查发布的分位值，招聘平台报价是在招岗位的要价，两者口径不同；报告会在用到基线的分层里逐条写明，比值只作方向性参考。重复导入同一「城市 × 职类 × 学历 × 年份」会在原位更新。</p>
  </section>
}
