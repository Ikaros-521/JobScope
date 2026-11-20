import { useEffect, useRef, useState } from 'react'
import { toast } from './toast'
import { post, startCompanyResearch } from './api'
import type { BossTab, BasketItem, Task } from './api'
import { listBossImportTabs, captureBossPage, parseImportLinks, getImportBasket, clearImportBasket, confirmImport,
  getTask, startFavoritesImport, getBrowseStatus, startBrowseCapture, stopBrowseCapture } from './api'
import type { BrowseCaptureStatus } from './api'

// 通道名（与后端 boss_import.imported_via 对应）。
const viaNames: Record<string, string> = { link: '链接', current_page: '当前页', favorite: '收藏', browse: '逛即采集', script: '油猴脚本' }

// 「Boss 导入中心」面板（docs/design/boss-import.md）：
// 捕获当前页 + 一键收藏导入 + 逛即采集 + 粘贴链接 → 捕获篮 → 确认入库（可选加星）→ 一键后处理。
export function BossImport({ onClose, onImported }: { onClose: () => void; onImported: () => void }) {
  const [tabs, setTabs] = useState<BossTab[]>([])
  const [tabsNote, setTabsNote] = useState('')
  const [paste, setPaste] = useState('')
  const [items, setItems] = useState<BasketItem[]>([])
  const [unchecked, setUnchecked] = useState<Set<string>>(new Set())
  const [autoStar, setAutoStar] = useState(false)
  const [busy, setBusy] = useState('')
  const [done, setDone] = useState<{ ids: string[]; count: number; companies: string[] } | null>(null)
  const [browse, setBrowse] = useState<BrowseCaptureStatus | null>(null)
  const capturedRef = useRef(0)

  async function refresh() {
    try {
      const [tab, basket] = await Promise.all([listBossImportTabs(), getImportBasket()])
      setTabs(tab.tabs)
      setTabsNote(tab.message)
      setItems(basket.items)
    } catch (error) {
      toast((error as Error).message, 'error')
    }
  }

  useEffect(() => {
    void refresh()
    getBrowseStatus().then(setBrowse).catch(() => {})
  }, [])

  // 逛即采集运行中：轮询状态，捕获数变化时同步篮子。
  useEffect(() => {
    if (!browse?.running) return
    let active = true
    const timer = setInterval(async () => {
      try {
        const next = await getBrowseStatus()
        if (!active) return
        setBrowse(next)
        if (next.captured !== capturedRef.current) {
          capturedRef.current = next.captured
          const basket = await getImportBasket()
          if (active) setItems(basket.items)
        }
      } catch { /* 轮询失败静默，下个周期再试 */ }
    }, 5000)
    return () => { active = false; clearInterval(timer) }
  }, [browse?.running])

  const selected = items.filter(item => !unchecked.has(item.external_id))
  const newCount = selected.filter(item => !item.in_library).length

  async function capture(tab: BossTab) {
    setBusy(`capture-${tab.id}`)
    try {
      const result = await captureBossPage(tab.id)
      if (result.status === 'ok') { toast(result.message); setTabsNote('') } else toast(result.message, 'error')
      const basket = await getImportBasket()
      setItems(basket.items)
    } catch (error) {
      toast((error as Error).message, 'error')
    } finally {
      setBusy('')
    }
  }

  // 一键收藏导入：后台任务自动翻页捕获，前端轮询到终态；完成默认勾选「导入后加星」。
  async function runFavorites() {
    setBusy('favorites')
    try {
      let current = (await startFavoritesImport()).task
      while (current.status === 'running') {
        await new Promise(resolve => setTimeout(resolve, 2500))
        current = (await getTask(current.id)).task
      }
      // 不依赖 toast 增强（taskSucceeded/failedTaskToast 尚未合入），状态判断就地内联。
      if (current.status === 'completed') { toast(current.message); setAutoStar(true) }
      else toast(current.message || '收藏导入未完成', 'error')
      const basket = await getImportBasket()
      setItems(basket.items)
    } catch (error) {
      toast((error as Error).message, 'error')
    } finally {
      setBusy('')
    }
  }

  async function toggleBrowse(next: boolean) {
    setBusy('browse')
    try {
      const result = next ? await startBrowseCapture() : await stopBrowseCapture()
      setBrowse(result)
      if (result.status === 'ok') toast(result.message)
      else toast(result.message, 'error')
    } catch (error) {
      toast((error as Error).message, 'error')
    } finally {
      setBusy('')
    }
  }

  async function submitPaste() {
    if (!paste.trim()) return
    setBusy('paste')
    try {
      const result = await parseImportLinks(paste)
      toast(result.message)
      setPaste('')
      const basket = await getImportBasket()
      setItems(basket.items)
    } catch (error) {
      toast((error as Error).message, 'error')
    } finally {
      setBusy('')
    }
  }

  async function confirmBasket() {
    if (!selected.length) return
    setBusy('confirm')
    try {
      const result = await confirmImport(selected.map(item => item.external_id), autoStar)
      if (result.status !== 'ok') { toast(result.message, 'error'); return }
      toast(result.message)
      setDone({
        ids: result.jobs.map(job => job.id),
        count: result.imported,
        companies: [...new Set(result.jobs.map(job => job.company).filter(Boolean))],
      })
      onImported()
      const basket = await getImportBasket()
      setItems(basket.items)
    } catch (error) {
      toast((error as Error).message, 'error')
    } finally {
      setBusy('')
    }
  }

  async function clearBasket() {
    setBusy('clear')
    try {
      await clearImportBasket()
      setItems([]); setUnchecked(new Set())
    } catch (error) {
      toast((error as Error).message, 'error')
    } finally {
      setBusy('')
    }
  }

  // 入库完成后处理一：链接导入的岗位往往只有链接没有正文，直接引导接「补全详情」后台批量读取。
  async function runDetailBatch() {
    if (!done) return
    try {
      await post<{ task: Task }>('/browser/details', { ids: done.ids })
      toast(`已开始补全 ${done.count} 条完整职责（后台逐条读取，可在任务记录查看进度）`)
      onClose()
    } catch (error) {
      toast((error as Error).message, 'error')
    }
  }

  // 入库完成后处理二：对新增公司跑多平台研究（公告+快查+岗位渠道，后台逐渠道执行）。
  async function runCompanyResearch() {
    if (!done?.companies.length) return
    setBusy('research')
    try {
      await Promise.all(done.companies.map(company => startCompanyResearch(company)))
      toast(`已为 ${done.companies.length} 家公司启动后台研究（公告、快查与岗位渠道），进度见「数据与采集 → 任务记录」`)
    } catch (error) {
      toast((error as Error).message, 'error')
    } finally {
      setBusy('')
    }
  }

  return <div className="boss-import">
    <div className="boss-import-browse">
      <label className="agent-checkbox" title="开启后，你在项目浏览器里浏览 Boss 岗位列表时，页面自己发出的检索响应会自动入篮——只接住响应，不代替你发请求">
        <input type="checkbox" checked={Boolean(browse?.running)} disabled={Boolean(busy)} onChange={event => void toggleBrowse(event.target.checked)} />逛即采集
      </label>
      <span className="tiny-label">{browse?.running ? `已捕获 ${browse.captured} 条 · ${browse.message}` : browse?.message || '正在读取逛即采集状态…'}</span>
    </div>

    <section className="boss-import-section" aria-labelledby="boss-import-page-heading">
      <div className="boss-import-heading">
        <h3 id="boss-import-page-heading">捕获当前页</h3>
        <div className="boss-import-heading-buttons">
          <button className="button secondary small" disabled={Boolean(busy)} onClick={() => void runFavorites()}>{busy === 'favorites' ? '收藏导入中…' : '一键导入收藏'}</button>
          <button className="text-button" disabled={Boolean(busy)} onClick={() => void refresh()}>刷新标签页</button>
        </div>
      </div>
      <p className="tiny-label">在项目浏览器里自己逛 Boss（搜索、公司页、筛选“最近”排序都随你），翻到哪页就捕获哪页——所见即所得，不模拟定位。</p>
      {tabs.length ? <div className="boss-import-tabs">{tabs.map(tab => <div className="boss-import-tab" key={tab.id}>
        <div className="boss-import-tab-info"><strong>{tab.title || 'Boss 页面'}</strong><span>{tab.url}</span></div>
        <button className="button secondary small" disabled={Boolean(busy)} onClick={() => void capture(tab)}>{busy === `capture-${tab.id}` ? '捕获中…' : '捕获本页'}</button>
      </div>)}</div> : <p className="tiny-label boss-import-note">{tabsNote || '正在读取浏览器标签页…'}</p>}
    </section>

    <section className="boss-import-section" aria-labelledby="boss-import-links-heading">
      <div className="boss-import-heading"><h3 id="boss-import-links-heading">粘贴 Boss 链接</h3></div>
      <textarea value={paste} onChange={event => setPaste(event.target.value)} rows={4} maxLength={50000}
        placeholder={'每行一条，或整段粘贴 App 分享文本：\n【后端开发】20-30K·14薪 经验3-5年 本科 杭州\nhttps://www.zhipin.com/job_detail/xxxx.html'} />
      <div className="boss-import-actions">
        <button className="button secondary" disabled={Boolean(busy) || !paste.trim()} onClick={() => void submitPaste()}>{busy === 'paste' ? '解析中…' : '解析入篮'}</button>
      </div>
    </section>

    <section className="boss-import-section" aria-labelledby="boss-import-basket-heading">
      <div className="boss-import-heading"><h3 id="boss-import-basket-heading">捕获篮 <span>{items.length}</span></h3>
        <label className="agent-checkbox"><input type="checkbox" checked={selected.length === items.length && items.length > 0}
          onChange={event => setUnchecked(event.target.checked ? new Set() : new Set(items.map(item => item.external_id)))} />全选</label>
      </div>
      {items.length ? <>
        <div className="boss-import-basket">{items.map(item => <label className="boss-import-item" key={item.external_id}>
          <input type="checkbox" checked={!unchecked.has(item.external_id)}
            onChange={() => { const next = new Set(unchecked); if (next.has(item.external_id)) next.delete(item.external_id); else next.add(item.external_id); setUnchecked(next) }} />
          <span className="sr-only">选择：{item.title} · {item.company}</span>
          <div className="boss-import-item-body">
            <strong>{item.title}</strong>
            <span>{[item.company || '公司待补全', item.salary, item.location].filter(Boolean).join(' · ')}</span>
            <span className="boss-import-item-meta">{viaNames[item.imported_via] || item.imported_via}{item.in_library ? ' · 已在库（导入会刷新归并）' : ''}</span>
          </div>
        </label>)}</div>
        <div className="boss-import-footer">
          <label className="agent-checkbox" title="导入后在岗位档案里统一标记收藏（收藏导入默认勾选，与 Boss 侧收藏意图对齐）"><input type="checkbox" checked={autoStar} onChange={event => setAutoStar(event.target.checked)} />导入后加星</label>
          <span className="tiny-label">{selected.length > 0 ? `将导入 ${selected.length} 条（${newCount} 条新增）` : '未选择任何条目'}</span>
          <div className="boss-import-footer-buttons">
            <button className="text-button" disabled={Boolean(busy) || !items.length} onClick={() => void clearBasket()}>清空篮子</button>
            <button className="button primary" disabled={Boolean(busy) || !selected.length} onClick={() => void confirmBasket()}>{busy === 'confirm' ? '导入中…' : `导入 ${selected.length} 条`}</button>
          </div>
        </div>
      </> : <p className="tiny-label boss-import-note">篮子是空的：先「捕获本页」「一键导入收藏」或粘贴链接；重复捕获自动按岗位去重，重启服务后篮子清空（已导入的不受影响）。</p>}
    </section>

    {done && <div className="boss-import-done">
      <div><strong>已导入 {done.count} 条</strong><p className="tiny-label">重复岗位已自动归并；链接导入的可能缺公司与正文。</p></div>
      <div className="boss-import-footer-buttons">
        {done.companies.length > 0 && <button className="button secondary" disabled={Boolean(busy)} onClick={() => void runCompanyResearch()}>{busy === 'research' ? '启动中…' : `公司研究（${done.companies.length} 家）`}</button>}
        <button className="button secondary" onClick={() => void runDetailBatch()}>补全详情（{done.count} 条）</button>
        <button className="button secondary" onClick={onClose}>完成</button>
      </div>
    </div>}
  </div>
}
