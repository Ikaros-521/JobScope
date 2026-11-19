import { useEffect, useState } from 'react'
import { api, date, getSourceHealth, post } from './api'
import type { BrowserStatus, PageInfo, Source, SourceHealth, Task } from './api'
import { toast, dismissToast, failedTaskToast, taskSucceeded } from './toast'
import { Badge, Button, Empty } from './ui'
import { Icon } from './Icon'
import { CityInput } from './CityInput'
import type { SavedLocation } from './CityInput'
import { DetailBatch } from './DetailBatch'
import { CompanyLookup } from './CompanyLookup'
import { ResearchIndex } from './ResearchIndex'
import { SignalSourceSettings } from './SignalSourceSettings'
import { WageBaselines } from './WageBaselines'
import { Pagination } from './Pagination'
import { navigateTabs } from './tabNavigation'

// 数据与采集整视图（2026-11-17 自 App.tsx 拆出）：采集工作台 / 公司快查 / 数据源 / 来源健康 / 任务记录。
// 纯视图 + 视图内状态；跨视图共享的采集条件（query/location）与来源登记仍在 App，经 props 进出，
// 行为与拆分前完全一致——这是视图搬移，不是功能改写。

export type SourcesTab = 'collect' | 'lookup' | 'catalog' | 'health' | 'history'

export function SourcesView({ active, tab, onTabChange, sources, loading, jobsTotal, browser, onBrowserStatus, tasks, tasksInfo,
                              loadTasks, busy, perform, refreshMetadata, onJobsChanged, onOpenJobs,
                              query, onQueryChange, location, onLocationChange,
                              browserSource, onBrowserSourceChange, browserLimit, onBrowserLimitChange,
                              cityPinyin, locations, sourceNames }: {
  active: boolean
  tab: SourcesTab
  onTabChange: (tab: SourcesTab) => void
  sources: Source[]
  loading: boolean
  jobsTotal: number
  browser: BrowserStatus | null
  onBrowserStatus: (status: BrowserStatus) => void
  tasks: Task[]
  tasksInfo: PageInfo
  loadTasks: (page: number) => Promise<void>
  busy: string
  perform: (key: string, action: () => Promise<void>) => Promise<void>
  refreshMetadata: () => Promise<void>
  // 采集落库后的岗位列表刷新：App 的 loadJobs 返回 Promise<Job[]>，这里只负责等待。
  onJobsChanged: () => Promise<unknown>
  onOpenJobs: () => void
  query: string
  onQueryChange: (value: string) => void
  location: string
  onLocationChange: (value: string) => void
  browserSource: string
  onBrowserSourceChange: (value: string) => void
  browserLimit: number
  onBrowserLimitChange: (value: number) => void
  cityPinyin: Record<string, string[]>
  locations: SavedLocation[]
  sourceNames: Record<string, string>
}) {
  // 来源健康度：首次切到该页时拉一次，之后手动刷新（不自动轮询，与「任务记录」同一节奏）。
  const [sourceHealth, setSourceHealth] = useState<SourceHealth[] | null>(null)
  useEffect(() => {
    if (!active || tab !== 'health' || sourceHealth !== null) return
    void getSourceHealth().then(data => setSourceHealth(data.sources)).catch(err => toast((err as Error).message, 'error'))
  }, [active, tab, sourceHealth])

  const browserSources = sources.filter(item => item.browser_collectable)
  const browserSourceItem = browserSources.find(item => item.id === browserSource)
  const browserCities = browserSourceItem?.cities || []
  const [browserCollecting, setBrowserCollecting] = useState(false)

  async function watchTask(id: string): Promise<Task> {
    // 后台采集最多约 30 分钟；轮询直到任务离开运行态。
    for (let attempt = 0; attempt < 900; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 2000))
      const { task } = await api<{ task: Task }>(`/tasks/${encodeURIComponent(id)}`)
      if (task.status !== 'running' && task.status !== 'pending') return task
    }
    throw new Error('等待采集超时，请稍后在采集记录里查看结果')
  }

  async function collectBrowser() {
    if (browserCollecting) return
    setBrowserCollecting(true)
    const progress = toast('已开始后台采集，可继续处理其他工作', 'notice', true)
    try {
      const started = await post<{ task: Task }>('/browser/collect', { source: browserSource, query, location, limit: browserLimit })
      const task = await watchTask(started.task.id)
      await Promise.all([onJobsChanged(), refreshMetadata()])
      const name = sources.find(item => item.id === browserSource)?.name || browserSource
      if (taskSucceeded(task)) {
        toast(task.count > 0 ? `已保存或更新 ${task.count} 条岗位。${task.message || ''}` : task.message || '本次未保存岗位', 'notice', false, {
          label: '查看岗位', onClick: onOpenJobs,
        })
      } else if (task.status === 'needs_login' || task.status === 'needs_verification') {
        // 登录/验证要用户在浏览器窗口里动手：后端会把页面留在前台。提示写清平台与两步动作，
        // 红色 20 秒长提示；后端的技术细节留在任务记录里，不堆进这条 toast。
        const what = task.status === 'needs_login' ? '登录' : '人工验证'
        toast(`${name}采集未完成：需要${what}。请在弹出的项目浏览器窗口里完成${what}（浏览器没开就先点「打开专用浏览器」），完成后回到这里再点一次「开始采集」。`, 'error', false, undefined, 20000)
      }
      else failedTaskToast(task, name)
    } catch (error) { toast((error as Error).message, 'error') }
    finally { setBrowserCollecting(false); dismissToast(progress) }
  }

  return <div className="sources-workspace">
    <section className="page-intro compact"><h1 className="sr-only">数据与采集</h1><Button className="button secondary" busy={busy === 'metadata'} disabled={Boolean(busy)} onClick={() => void perform('metadata', refreshMetadata)}><Icon name="source" size={16} />刷新状态</Button></section>
    <div className="management-overview" aria-label="数据概况">
      <div><span className="overview-icon"><Icon name="jobs" /></span><div><span>本地岗位</span><strong>{jobsTotal.toLocaleString()}<small>份档案</small></strong></div></div>
      <div><span className="overview-icon"><Icon name="globe" /></span><div><span>专用浏览器</span><strong>{browser ? browser.running ? '运行中' : '未启动' : '状态未知'}<small>登录后采集</small></strong></div></div>
      <div><span className="overview-icon"><Icon name="book" /></span><div><span>采集记录</span><strong>{tasksInfo.total.toLocaleString()}<small>次任务</small></strong></div></div>
    </div>
    <div className="management-tabs" role="tablist" aria-label="数据管理分区" onKeyDown={navigateTabs}>{([{ id: 'collect', label: '采集工作台' }, { id: 'lookup', label: '公司快查' }, { id: 'catalog', label: '数据源' }, { id: 'health', label: '来源健康' }, { id: 'history', label: '任务记录' }] as const).map(item => <button key={item.id} id={`sources-${item.id}-tab`} role="tab" aria-controls={`sources-${item.id}-panel`} aria-selected={tab === item.id} tabIndex={tab === item.id ? 0 : -1} onClick={() => onTabChange(item.id)}>{item.label}{item.id === 'catalog' && <span>{sources.length}</span>}</button>)}</div>
    <div id="sources-collect-panel" role="tabpanel" aria-labelledby="sources-collect-tab" hidden={tab !== 'collect'}>
      <section className="browser-panel paper-panel">
        <div className="browser-guide"><span className="section-label">浏览器采集</span><h2>从已登录的平台<br />带回真实岗位</h2><p>使用项目专属 Chrome，登录状态与日常浏览器分开保存。</p><ol className="collection-steps"><li><span>1</span><div><strong>选择平台</strong><p>在右侧填写关键词与城市</p></div></li><li><span>2</span><div><strong>打开浏览器并登录</strong><p>遇到验证时，请在浏览器内手动完成</p></div></li><li><span>3</span><div><strong>开始采集</strong><p>结果自动存档，任务记录保留真实状态</p></div></li></ol><div className="browser-status"><span className={`connection-dot ${browser?.running ? 'connected' : ''}`} />{browser?.message || '浏览器状态未知'}</div></div>
        <div className="browser-actions"><form onSubmit={event => { event.preventDefault(); void collectBrowser() }}>
          <div className="browser-form-heading"><h3>采集条件</h3><span>仅采集公开可见的资料</span></div>
          <div className="browser-fields"><div><label className="field-label" htmlFor="browser-source">采集平台</label><select id="browser-source" value={browserSource} onChange={event => onBrowserSourceChange(event.target.value)}>{browserSources.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></div><div><label className="field-label" htmlFor="browser-query">岗位关键词</label><input id="browser-query" value={query} onChange={event => onQueryChange(event.target.value)} placeholder="例如：Python 后端" required maxLength={200} /></div><div><label className="field-label" htmlFor="browser-location">城市（可选）</label><CityInput id="browser-location" cities={browserCities} hot={browserSourceItem?.hot || []} pinyin={cityPinyin} locations={locations} value={location} onChange={onLocationChange} placeholder={browserCities.length ? '输入或点选城市' : '例如：北京'} maxLength={100} /></div><div><label className="field-label" htmlFor="browser-limit">采集数量 <span>· 条</span></label><input id="browser-limit" type="number" min={1} max={1000} value={browserLimit} onChange={event => onBrowserLimitChange(Math.max(1, Math.min(1000, Number(event.target.value) || 20)))} inputMode="numeric" /></div></div>
          <div className="browser-form-actions"><Button className="button secondary" type="button" title="启动/连接项目专属 Chrome（登录态与日常浏览器隔离）并打开所选平台首页；适合平时先登录、过验证" busy={busy === 'browser-start'} disabled={Boolean(busy) || !browserSources.length} onClick={() => void perform('browser-start', async () => { const result = await post<BrowserStatus>(`/browser/start?source=${encodeURIComponent(browserSource)}`); onBrowserStatus(result); toast(result.message); await refreshMetadata() })}><Icon name="globe" size={17} />打开专用浏览器</Button><Button className="button primary" title="在项目浏览器会话里采集当前平台；浏览器没开会自动拉起——未登录时在弹出的窗口里完成登录/验证，再点一次即可" busy={browserCollecting} disabled={browserCollecting || !browserSources.length} type="submit">开始采集<Icon name="arrow" size={16} /></Button></div>
          <p className="browser-form-note">点「开始采集」没开浏览器会自动拉起；实际数量以平台可返回的结果为准。</p>
          {/* 登录前置说明随所选平台切换：需要登录的平台未登录采集必失败，提交前就写明白。 */}
          {browserSourceItem?.login === 'required' && <p className="browser-form-note login-required"><strong>「{browserSourceItem.name}」需要登录：</strong>未登录采集拿不到岗位。先点「打开专用浏览器」，在弹出的窗口里登录（遇到滑块、扫码就手动完成），再点「开始采集」。</p>}
          {browserSourceItem?.login === 'recommended' && <p className="browser-form-note login-recommended"><strong>「{browserSourceItem.name}」建议先登录：</strong>未登录可能遇到人工验证或结果受限，遇到时在弹出的窗口里完成后重试。</p>}
        </form></div>
      </section>
      <div className="collection-maintenance"><DetailBatch onUpdate={refreshMetadata} /><ResearchIndex /></div>
    </div>
    <div id="sources-lookup-panel" role="tabpanel" aria-labelledby="sources-lookup-tab" hidden={tab !== 'lookup'}>
      <CompanyLookup refreshMetadata={refreshMetadata} />
    </div>
    <div id="sources-catalog-panel" role="tabpanel" aria-labelledby="sources-catalog-tab" hidden={tab !== 'catalog'}>
      <div className="management-section-heading"><div><h2>岗位与公司数据源</h2><p>接口优先，浏览器补充；可用状态不代表已登录。</p></div></div>
      <div className="sources-grid">{loading ? <div className="paper-panel"><div className="loading-state"><span className="spinner" />正在读取数据源状态…</div></div> : sources.length ? sources.map(item => <article className="source-card paper-panel" key={item.id}><div className="source-card-heading"><span className="source-icon"><Icon name={item.id === 'cninfo' ? 'book' : 'globe'} /></span><h3>{item.name}</h3><Badge value={item.status} /></div><p>{item.description}</p><div className="source-card-footer"><span>{{ http_api: 'HTTP 接口', browser_page: '浏览器会话', company_web: '网页快查', manual: '用户材料' }[item.transport_mode] || item.transport_mode}</span><span>{item.id}</span></div></article>) : <div className="paper-panel"><Empty title="等待数据源状态">请确认本地后端服务已启动，然后刷新状态。</Empty></div>}</div>
      <SignalSourceSettings />
      <WageBaselines refreshMetadata={refreshMetadata} />
    </div>
    <div id="sources-health-panel" role="tabpanel" aria-labelledby="sources-health-tab" hidden={tab !== 'health'}>
      {/* 来源健康度：成败分布只是任务记录的聚合视图，消息引用任务原文，不做任何新的诊断结论。 */}
      <section className="paper-panel tasks-panel">
        <div className="section-heading">
          <div><h2>来源健康度</h2><p className="body-small muted">按最近 100 次任务统计每个来源的成功 / 未完成 / 失败分布。某来源连续失败通常是站点改版或登录态失效——先去「采集工作台」处理，不要把零条结果当作采集成功。</p></div>
          <Button className="button secondary small" busy={busy === 'health'} disabled={Boolean(busy)} onClick={() => void perform('health', async () => {
            const data = await getSourceHealth()
            setSourceHealth(data.sources)
          })}>刷新</Button>
        </div>
        {sourceHealth === null
          ? <div className="loading-state"><span className="spinner" />正在读取来源健康度…</div>
          : sourceHealth.length
            ? <div className="task-table-wrap"><table>
              <thead><tr><th scope="col">来源</th><th scope="col">成功</th><th scope="col">未完成</th><th scope="col">失败</th><th scope="col">最近成功</th><th scope="col">最近结果</th></tr></thead>
              <tbody>{sourceHealth.map(item => <tr key={item.source}>
                <td data-label="来源"><strong>{sourceNames[item.source] || item.source}</strong><small>{item.source}</small></td>
                <td data-label="成功" className="nowrap">{item.ok}</td>
                <td data-label="未完成" className="nowrap">{item.attention}</td>
                <td data-label="失败" className="nowrap">{item.failed}</td>
                <td data-label="最近成功" className="nowrap">{item.last_ok_at ? date(item.last_ok_at) : '从未成功'}</td>
                <td data-label="最近结果"><Badge value={item.last_status} /><span className="task-message">{item.last_message || '—'}</span></td>
              </tr>)}</tbody>
            </table></div>
            : <Empty title="还没有可统计的任务">发起一次岗位或公司采集后，各来源的健康度会出现在这里。</Empty>}
        <p className="tiny-label">「未完成」指需要登录/人工验证、浏览器正忙、部分成功或被跳过的任务；判断口径与采集记录的状态完全一致。没有任何任务的来源不会出现在表里。</p>
      </section>
    </div>
    <div id="sources-history-panel" role="tabpanel" aria-labelledby="sources-history-tab" hidden={tab !== 'history'}>
      <section className="paper-panel tasks-panel"><div className="section-heading"><div><h2>采集记录</h2><p className="body-small muted">成功、失败与验证请求都留有记录。</p></div><span className="muted body-small">{tasksInfo.total} 次任务</span></div>{loading ? <div className="loading-state"><span className="spinner" />正在读取采集记录…</div> : tasks.length ? <div className="task-table-wrap"><table><thead><tr><th scope="col">查询 / 来源</th><th scope="col">状态</th><th scope="col">资料数</th><th scope="col">时间</th><th scope="col">说明</th></tr></thead><tbody>{tasks.map(task => <tr key={task.id}><td data-label="查询 / 来源"><strong>{task.query || '未指定'}</strong><small>{sourceNames[task.source] || task.source}</small></td><td data-label="状态"><Badge value={task.status} /></td><td data-label="资料数">{task.count ?? '—'}</td><td data-label="时间" className="nowrap">{date(task.created_at)}</td><td data-label="说明" className="task-message">{task.message || '—'}</td></tr>)}</tbody></table></div> : <Empty title="还没有采集记录">发起一次岗位或公司检索，结果与失败原因都会记录在这里。</Empty>}<Pagination info={tasksInfo} label="采集记录" busy={Boolean(busy)} onPage={page => void perform('page-tasks', () => loadTasks(page))} /></section>
    </div>
  </div>
}
