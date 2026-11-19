import { useEffect, useRef, useState } from 'react'
import {
  api, date, listResumeTemplates, previewResumeDocument, listResumeVersions, getResumeVersion,
  createResumeVersion, updateResumeVersion, deleteResumeVersion, optimizeResume, exportResumeVersion,
  pinResumeVersion, listProfileEntries, PROFILE_CATEGORY_NAMES, DEFAULT_TYPOGRAPHY,
} from './api'
import type { Job, PageInfo, ProfileEntry, ResumeTemplate, ResumeTypography, ResumeVersion, ResumeVersionSummary } from './api'
import { ResumeDiff } from './ResumeDiff'
import { toast } from './toast'

// 岗位详情页「优化简历」跳转预填：带上岗位定位，进入后自动选中该岗位。
// instructions（P1 闭环，2026-11-19）：评估报告「带着缺口优化」传入的重点说明。
export type WorkshopPrefill = { jobId: string; company: string; jobTitle: string; instructions?: string }

const EXPORT_ITEMS = [
  { format: 'pdf', label: 'PDF' },
  { format: 'docx', label: 'Word' },
  { format: 'html', label: 'HTML' },
  { format: 'md', label: 'Markdown' },
] as const
const EXPORT_EXTENSIONS: Record<string, string> = { pdf: 'pdf', docx: 'docx', html: 'html', md: 'md' }

// 排版选项：与后端 sanitize 白名单一致（resume_workshop.FONT_STACKS/MARGIN_PRESETS）。
const TYPOGRAPHY_FONTS: [string, string][] = [['', '字体 · 跟随模板'], ['yahei', '微软雅黑'], ['song', '宋体'], ['hei', '黑体'], ['kai', '楷体'], ['fangsong', '仿宋']]
const TYPOGRAPHY_SIZES = [9.5, 10, 10.5, 11, 11.5, 12, 12.5, 13]
const TYPOGRAPHY_LINE_HEIGHTS: [number | null, string][] = [[null, '行距 · 跟随模板'], [1.35, '紧凑 1.35'], [1.5, '标准 1.5'], [1.65, '宽松 1.65'], [1.8, '疏朗 1.8']]
const TYPOGRAPHY_MARGINS: [string, string][] = [['narrow', '窄'], ['standard', '标准'], ['wide', '宽']]

// A4 内容高（297mm @96dpi 减页边距），估算页数与一页纸缩放共用。
function shrinkCss(basePt: number, scale: number) {
  return `.resume-body{font-size:${(basePt * scale).toFixed(2)}pt}` +
    '.resume-body h1{font-size:1.85em}.resume-body h2{font-size:1.15em}.resume-body h3{font-size:1.02em}' +
    '.resume-body th,.resume-body td{font-size:.95em}.resume-body p.contact{font-size:.92em}'
}

function Empty({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="workshop-empty"><h3>{title}</h3><p>{children}</p></div>
}

export function ResumeWorkshop({ prefill, onPrefillConsumed, onOpenResume }: { prefill: WorkshopPrefill | null; onPrefillConsumed: () => void; onOpenResume: () => void }) {
  const [templates, setTemplates] = useState<ResumeTemplate[]>([])
  const [versions, setVersions] = useState<ResumeVersionSummary[]>([])
  const [resumeCharacters, setResumeCharacters] = useState<number | null>(null)
  const [current, setCurrent] = useState<ResumeVersion | null>(null)
  // 排版设置（字体/字号/行距/页边距/一页纸）：随版本保存，预览与导出共用。
  const [typography, setTypography] = useState<ResumeTypography>(DEFAULT_TYPOGRAPHY)
  const [pageFit, setPageFit] = useState<{ pages: number; shrink: number } | null>(null)
  const previewFrameRef = useRef<HTMLIFrameElement | null>(null)
  // 编辑区视图：编辑正文，或对照优化时基础简历快照看「改动对比」（git diff 风格）。
  const [editTab, setEditTab] = useState<'edit' | 'diff'>('edit')
  const [title, setTitle] = useState('')
  const [markdown, setMarkdown] = useState('')
  const [templateId, setTemplateId] = useState('classic')
  const [dirty, setDirty] = useState(false)
  const [notes, setNotes] = useState<string[]>([])
  const [previewHtml, setPreviewHtml] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [formError, setFormError] = useState('')
  const [deletingId, setDeletingId] = useState<number | null>(null)
  // 存档管理：按公司筛选 + 行内快捷导出菜单；pin 的版本是该岗位「评估用」简历。
  const [companyFilter, setCompanyFilter] = useState('')
  const [exportMenuId, setExportMenuId] = useState<number | null>(null)
  // 优化配置表单
  const [baseMode, setBaseMode] = useState<'saved' | 'paste'>('saved')
  const [pasteResume, setPasteResume] = useState('')
  const [targetMode, setTargetMode] = useState<'job' | 'paste'>('job')
  const [jobQuery, setJobQuery] = useState('')
  const [jobResults, setJobResults] = useState<Job[]>([])
  const [jobSearching, setJobSearching] = useState(false)
  const [jobSearchFocused, setJobSearchFocused] = useState(false)
  const [selectedJob, setSelectedJob] = useState<Job | null>(null)
  const [jdText, setJdText] = useState('')
  const [pasteCompany, setPasteCompany] = useState('')
  const [pasteJobTitle, setPasteJobTitle] = useState('')
  const [instructions, setInstructions] = useState('')
  // 个人资料库取材（2026-11-17）：完整版材料（项目经历等），勾选后优化会从中扩写阉割版内容。
  const [profileEntries, setProfileEntries] = useState<ProfileEntry[]>([])
  const [profileSelected, setProfileSelected] = useState<Set<string>>(new Set())
  const jobSearchToken = useRef(0)
  const jobResultsRef = useRef<HTMLUListElement | null>(null)
  const previewToken = useRef(0)

  useEffect(() => {
    listResumeTemplates().then(data => setTemplates(data.templates)).catch(() => setTemplates([]))
    void refreshVersions()
    api<{ text: string }>('/resume').then(data => setResumeCharacters(data.text.trim().length)).catch(() => setResumeCharacters(null))
    // 只取启用条目，求职偏好类除外（那类启用即进评估事实池，不作扩写素材）；
    // 加载失败静默——选材是可选增强，不该挡住优化主流程。
    listProfileEntries(true).then(data => setProfileEntries(data.entries.filter(entry => entry.category !== 'preference'))).catch(() => setProfileEntries([]))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function refreshVersions() {
    try { setVersions((await listResumeVersions()).versions) } catch { /* 列表加载失败保持原状，操作反馈走 toast */ }
  }

  async function togglePin(version: ResumeVersionSummary) {
    setBusy(`pin-${version.id}`)
    try {
      await pinResumeVersion(version.id, !version.pinned)
      toast(version.pinned ? '已取消评估用，该岗位评估将回退「我的简历」。' : '已设为该岗位评估用简历，评估适配自动使用。')
      await refreshVersions()
    } catch (err) { toast((err as Error).message, 'error') } finally { setBusy(null) }
  }

  function quickExport(version: ResumeVersionSummary, format: 'pdf' | 'docx' | 'html' | 'md') {
    setExportMenuId(null)
    const name = `${version.title.replace(/[\\/:*?"<>|]/g, '_')}.${EXPORT_EXTENSIONS[format]}`
    void exportResumeVersion(version.id, format, name).catch(err => toast((err as Error).message, 'error'))
  }

  // 岗位详情跳转预填：拉取该岗位并自动选中；评估报告带来的缺口说明一并预填到优化重点。
  useEffect(() => {
    if (!prefill) return
    api<Job>(`/jobs/${encodeURIComponent(prefill.jobId)}`)
      .then(job => { setSelectedJob(job); setTargetMode('job'); setJobQuery(`${prefill.jobTitle} ${prefill.company}`.trim()); setInstructions(prefill.instructions || '') })
      .catch(() => toast('岗位档案读取失败，请手动选择目标岗位'))
      .finally(onPrefillConsumed)
  }, [prefill, onPrefillConsumed])

  // 岗位候选：聚焦空词直接给最近档案（点开即选，不必先想关键词）；
  // 有输入就检索（本地子串匹配开销小，原先两字符门槛会静默无反馈）。
  // 序号防过期响应回写。
  useEffect(() => {
    if (targetMode !== 'job') { setJobResults([]); setJobSearching(false); return }
    const query = jobQuery.trim()
    if (!query && !jobSearchFocused) { setJobResults([]); setJobSearching(false); return }
    const path = query ? `/jobs?q=${encodeURIComponent(query)}&page=1` : '/jobs?page=1'
    const token = ++jobSearchToken.current
    setJobSearching(true)
    api<PageInfo & { jobs: Job[] }>(path)
      .then(data => { if (token === jobSearchToken.current) setJobResults(data.jobs.slice(0, 8)) })
      .catch(() => { if (token === jobSearchToken.current) setJobResults([]) })
      .finally(() => { if (token === jobSearchToken.current) setJobSearching(false) })
  }, [jobQuery, targetMode, jobSearchFocused])

  // 候选浮层在可滚动表单内，输入框贴近可视区底部时会被容器裁掉；
  // 结果到达时就近滚入视野，避免「明明检索了却看不见」。
  useEffect(() => {
    if (jobResults.length > 0) jobResultsRef.current?.scrollIntoView({ block: 'nearest' })
  }, [jobResults])

  // 编辑/模板/排版切换后 800ms 防抖刷新预览；与导出共用后端渲染管线，所见即所得。
  useEffect(() => {
    if (!current || !markdown.trim()) { setPreviewHtml(''); setPageFit(null); return }
    const token = ++previewToken.current
    const timer = setTimeout(() => {
      previewResumeDocument(markdown, templateId, typography)
        .then(data => { if (token === previewToken.current) setPreviewHtml(data.html) })
        .catch(() => { if (token === previewToken.current) setPreviewHtml('') })
    }, 800)
    return () => clearTimeout(timer)
  }, [current, markdown, templateId, typography])

  // 预览测量：估页数 + 一页纸时注入字号缩放（与 PDF 导出的阶梯缩放同机制，结果一致）。
  // iframe 同源（srcDoc + allow-same-origin，无脚本），父页直接读内容文档。
  function fitPreview() {
    const doc = previewFrameRef.current?.contentDocument
    const page = doc?.querySelector<HTMLElement>('.resume-page')
    const body = doc?.querySelector<HTMLElement>('.resume-body')
    if (!doc || !page || !body) { setPageFit(null); return }
    const run = () => {
      const view = doc.defaultView
      if (!view) return
      const cs = view.getComputedStyle(page)
      const avail = 1122.5 - parseFloat(cs.paddingTop || '0') - parseFloat(cs.paddingBottom || '0')
      let styleEl = doc.getElementById('preview-fit') as HTMLStyleElement | null
      if (!styleEl) { styleEl = doc.createElement('style'); styleEl.id = 'preview-fit'; doc.head.appendChild(styleEl) }
      styleEl.textContent = ''
      const natural = body.getBoundingClientRect().height
      const pages = Math.max(1, Math.ceil(natural / avail - 0.02))
      if (!typography.one_page || natural <= avail) { setPageFit({ pages, shrink: 1 }); return }
      const basePt = typography.font_size ?? templates.find(item => item.id === templateId)?.body_pt ?? 11
      let scale = Math.max(0.7, Math.min(1, avail / natural))
      for (let i = 0; i < 8; i++) {
        styleEl.textContent = shrinkCss(basePt, scale)
        if (body.getBoundingClientRect().height <= avail || scale <= 0.7) break
        scale = Math.max(0.7, scale - 0.04)
      }
      setPageFit({ pages: 1, shrink: Math.round(scale * 100) / 100 })
    }
    const fonts = doc.fonts
    if (fonts?.ready) fonts.ready.then(run).catch(run); else run()
  }

  // 仅切换排版设置（如一页纸开关）不改变服务端 HTML、iframe 不重载时，主动重测。
  useEffect(() => {
    if (!previewHtml) return
    const timer = setTimeout(() => fitPreview(), 60)
    return () => clearTimeout(timer)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [previewHtml, typography, templateId])

  // tab 缺省保持当前视图：普通打开/保存不劫持视图，仅优化完成时显式切到「改动对比」。
  function loadVersion(version: ResumeVersion, tab?: 'edit' | 'diff') {
    setCurrent(version)
    setTitle(version.title)
    setMarkdown(version.markdown)
    setTemplateId(version.template_id || 'classic')
    setNotes(version.notes || [])
    setTypography({ ...DEFAULT_TYPOGRAPHY, ...(version.typography || {}) })
    setDirty(false)
    if (tab) setEditTab(tab)
  }

  async function selectVersion(id: number) {
    if (busy) return
    try {
      loadVersion(await getResumeVersion(id))
    } catch (err) { toast((err as Error).message) }
  }

  async function createFromSaved() {
    if (busy || resumeCharacters === null) return
    if (!resumeCharacters) { setFormError('「我的简历」还没有内容，先去保存一份基础简历。'); return }
    setBusy('create')
    try {
      const data = await api<{ text: string }>('/resume')
      const version = await createResumeVersion({ title: `我的简历 · 副本 ${new Date().toLocaleDateString('zh-CN')}`, markdown: data.text })
      loadVersion(version)
      await refreshVersions()
      toast('已从「我的简历」创建副本，可直接编辑。')
    } catch (err) { toast((err as Error).message) } finally { setBusy(null) }
  }

  async function startOptimize() {
    if (busy) return
    setFormError('')
    if (baseMode === 'saved' && !resumeCharacters) { setFormError('「我的简历」还没有内容，请先保存，或改用粘贴简历文本。'); return }
    if (baseMode === 'paste' && !pasteResume.trim()) { setFormError('请粘贴基础简历文本。'); return }
    if (targetMode === 'job' && !selectedJob) { setFormError('请从岗位档案选择目标岗位，或改用粘贴 JD。'); return }
    if (targetMode === 'paste' && !jdText.trim()) { setFormError('请粘贴目标岗位的职责与要求（JD）。'); return }
    setBusy('optimize')
    try {
      const version = await optimizeResume({
        resume_text: baseMode === 'paste' ? pasteResume : '',
        job_id: targetMode === 'job' && selectedJob ? selectedJob.id : '',
        jd_text: targetMode === 'paste' ? jdText : '',
        company: targetMode === 'paste' ? pasteCompany : '',
        job_title: targetMode === 'paste' ? pasteJobTitle : '',
        instructions,
        profile_entry_ids: [...profileSelected],
      })
      loadVersion(version, 'diff')
      await refreshVersions()
      toast('优化完成，已生成新版本；先在「改动对比」核对改动，可直接二次编辑或导出。')
    } catch (err) { setFormError((err as Error).message) } finally { setBusy(null) }
  }

  async function saveVersion() {
    if (!current || busy || !markdown.trim()) return
    setBusy('save')
    try {
      const version = await updateResumeVersion(current.id, { title: title.trim() || current.title, markdown, template_id: templateId, typography })
      loadVersion(version)
      await refreshVersions()
      toast('版本已保存。')
    } catch (err) { toast((err as Error).message) } finally { setBusy(null) }
  }

  async function removeVersion(id: number) {
    if (busy) return
    if (deletingId !== id) { setDeletingId(id); return }
    try {
      await deleteResumeVersion(id)
      if (current?.id === id) { setCurrent(null); setMarkdown(''); setPreviewHtml('') }
      setDeletingId(null)
      await refreshVersions()
      toast('版本已删除。')
    } catch (err) { toast((err as Error).message) }
  }

  async function exportCurrent(format: string) {
    if (!current || busy) return
    try {
      // 导出前先把未保存的编辑与排版落库，保证文件与所见一致。
      const typographyDirty = JSON.stringify(typography) !== JSON.stringify(current.typography || DEFAULT_TYPOGRAPHY)
      if (dirty || typographyDirty || markdown.trim() !== current.markdown || templateId !== current.template_id || title.trim() !== current.title) {
        setBusy('save')
        const version = await updateResumeVersion(current.id, { title: title.trim() || current.title, markdown, template_id: templateId, typography })
        loadVersion(version)
        await refreshVersions()
      }
      setBusy(`export-${format}`)
      const name = (title.trim() || '简历').replace(/[\\/:*?"<>|]/g, ' ')
      await exportResumeVersion(current.id, format as 'pdf', `${name}.${EXPORT_EXTENSIONS[format]}`)
      toast('导出成功，已开始下载。')
    } catch (err) { toast((err as Error).message) } finally { setBusy(null) }
  }

  const optimizing = busy === 'optimize'
  const templateName = templates.find(item => item.id === templateId)?.name || templateId

  return <div className="workshop-view">
    <section className="page-intro compact"><h1 className="sr-only">简历工坊</h1>
      <div className="workshop-intro-actions">
        <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={() => { setCurrent(null); setMarkdown(''); setPreviewHtml(''); setPageFit(null); setTypography(DEFAULT_TYPOGRAPHY); setDirty(false) }}>新建优化</button>
        <button type="button" className="button secondary" disabled={Boolean(busy) || resumeCharacters === null || !resumeCharacters} onClick={() => void createFromSaved()}>从我的简历复制</button>
      </div>
    </section>
    <div className="workshop-layout">
      <aside className="workshop-panel workshop-versions" aria-label="简历版本列表">
        <div className="panel-title"><h2>投递存档 <span>{versions.length}</span></h2></div>
        {versions.length > 0 && <div className="workshop-company-filter">
          <select value={companyFilter} aria-label="按公司筛选存档" onChange={event => setCompanyFilter(event.target.value)}>
            <option value="">全部公司</option>
            {[...new Set(versions.map(version => version.company).filter(Boolean))].sort().map(company => <option key={company} value={company}>{company}</option>)}
          </select>
        </div>}
        <div className="workshop-version-list">
          {(() => {
            const visible = versions.filter(version => !companyFilter || version.company === companyFilter)
            return visible.length === 0 ? <Empty title={companyFilter ? '该公司还没有存档' : '还没有定制版本'}>在右侧选择目标岗位并开始优化，每次优化都会保存为一个可二次编辑的版本，并自动设为该岗位的评估用简历。</Empty>
              : visible.map(version => <div className={`workshop-version ${current?.id === version.id ? 'active' : ''}`} key={version.id}>
              <button type="button" className="workshop-version-main" onClick={() => void selectVersion(version.id)} title={version.title}>
                <strong><span className="workshop-version-title" title={version.title}>{version.title}</span>{version.pinned && <span className="pin-badge" title="该岗位评估适配当前使用的简历">评估用</span>}</strong>
                <span>{version.company || version.job_title || '未绑定岗位'}</span>
                <time>{date(version.updated_at)} · {version.characters.toLocaleString()} 字</time>
              </button>
              <div className="workshop-version-actions">
                {version.job_id && <button type="button" className="text-button" disabled={busy === `pin-${version.id}`}
                  title={version.pinned ? '取消评估用，该岗位评估回退「我的简历」' : '设为该岗位评估适配使用的简历'}
                  onClick={() => void togglePin(version)}>{version.pinned ? '解绑' : '设评估用'}</button>}
                <details className="workshop-version-export" open={exportMenuId === version.id}
                  onToggle={event => setExportMenuId((event.target as HTMLDetailsElement).open ? version.id : null)}>
                  <summary>导出</summary>
                  <div>{EXPORT_ITEMS.map(item => <button type="button" key={item.format} onClick={() => quickExport(version, item.format)}>{item.label}</button>)}</div>
                </details>
                <button type="button" className={`workshop-version-delete ${deletingId === version.id ? 'confirm' : ''}`} disabled={Boolean(busy)}
                  onClick={() => void removeVersion(version.id)}>{deletingId === version.id ? '确认' : '删除'}</button>
              </div>
            </div>)
          })()}
        </div>
        <p className="workshop-footnote">存档保存在本机；标「评估用」的版本会在该岗位评估适配时自动作为简历参与匹配。</p>
      </aside>

      <section className="workshop-panel workshop-editor" aria-label="简历编辑">
        {!current ? <div className="workshop-config">
          <div className="panel-title"><h2>针对岗位优化</h2></div>
          <div className="workshop-config-body">
            <fieldset className="workshop-fieldset">
              <legend>基础简历</legend>
              <div className="workshop-choice">
                <label className="agent-checkbox"><input type="radio" name="workshop-base" checked={baseMode === 'saved'} onChange={() => setBaseMode('saved')} disabled={optimizing} /><span>使用「我的简历」{resumeCharacters != null ? `（${resumeCharacters.toLocaleString()} 字）` : ''}</span></label>
                {resumeCharacters === 0 && baseMode === 'saved' && <p className="workshop-hint warn">「我的简历」为空，请先<button className="inline-button" onClick={onOpenResume}>去保存</button>，或改用粘贴文本。</p>}
                <label className="agent-checkbox"><input type="radio" name="workshop-base" checked={baseMode === 'paste'} onChange={() => setBaseMode('paste')} disabled={optimizing} /><span>粘贴简历文本</span></label>
                {baseMode === 'paste' && <textarea className="workshop-textarea" value={pasteResume} onChange={event => setPasteResume(event.target.value)} disabled={optimizing} rows={5} maxLength={100000} placeholder="粘贴完整简历文本；优化只重组其中已有的事实。" spellCheck={false} aria-label="基础简历文本" />}
              </div>
            </fieldset>
            <fieldset className="workshop-fieldset">
              <legend>目标岗位</legend>
              <div className="workshop-choice">
                <label className="agent-checkbox"><input type="radio" name="workshop-target" checked={targetMode === 'job'} onChange={() => setTargetMode('job')} disabled={optimizing} /><span>从岗位档案选择</span></label>
                {targetMode === 'job' && (selectedJob
                  ? <div className="workshop-job-chip">
                    <div><strong>{selectedJob.title}</strong><span>{selectedJob.company}{selectedJob.location ? ` · ${selectedJob.location}` : ''}{selectedJob.salary ? ` · ${selectedJob.salary}` : ''}</span></div>
                    <button type="button" className="text-button" onClick={() => { setSelectedJob(null); setJobQuery(''); setJobResults([]); setJobSearchFocused(false) }} disabled={optimizing}>重选</button>
                  </div>
                  : <div className="workshop-job-search">
                    <input value={jobQuery} onChange={event => setJobQuery(event.target.value)} onFocus={() => setJobSearchFocused(true)} onBlur={() => setJobSearchFocused(false)} placeholder="点击选最近档案，或输入关键词检索" maxLength={200} disabled={optimizing} aria-label="搜索岗位档案" />
                    {jobSearching && <span className="spinner" aria-hidden="true" />}
                    {(jobResults.length > 0 || (!jobSearching && jobQuery.trim())) && <ul className="workshop-job-results" ref={jobResultsRef} onMouseDown={event => event.preventDefault()}>
                      {jobResults.length > 0 ? jobResults.map(job => <li key={job.id}><button type="button" onClick={() => { setSelectedJob(job); setJobResults([]); setJobSearchFocused(false) }}>
                        <strong>{job.title}</strong><span>{job.company}{job.location ? ` · ${job.location}` : ''}</span>
                        {job.description_status !== 'complete' && <em>仅列表摘要</em>}
                      </button></li>) : <li className="workshop-job-none">没有匹配「{jobQuery.trim()}」的本地岗位：换个关键词，或改用「粘贴岗位 JD」。</li>}
                    </ul>}
                    <p className="workshop-hint">点击输入框直接选最近档案，输入即检索；标注「仅列表摘要」的建议先在岗位研究页补全职责。</p>
                  </div>)}
                <label className="agent-checkbox"><input type="radio" name="workshop-target" checked={targetMode === 'paste'} onChange={() => setTargetMode('paste')} disabled={optimizing} /><span>粘贴岗位 JD</span></label>
                {targetMode === 'paste' && <div className="workshop-paste-jd">
                  <div className="form-two">
                    <label>公司名称<input value={pasteCompany} onChange={event => setPasteCompany(event.target.value)} maxLength={300} disabled={optimizing} placeholder="用于版本命名" /></label>
                    <label>岗位名称<input value={pasteJobTitle} onChange={event => setPasteJobTitle(event.target.value)} maxLength={300} disabled={optimizing} placeholder="如：后端开发工程师" /></label>
                  </div>
                  <textarea className="workshop-textarea" value={jdText} onChange={event => setJdText(event.target.value)} disabled={optimizing} rows={7} maxLength={20000} placeholder="粘贴岗位职责与任职要求全文，优化会围绕这些要求定制表达与侧重。" spellCheck={false} aria-label="岗位 JD 全文" />
                </div>}
              </div>
            </fieldset>
            <label className="workshop-instructions">补充指示（可选）
              <input value={instructions} onChange={event => setInstructions(event.target.value)} maxLength={2000} disabled={optimizing} placeholder="如：更突出带团队经验；控制在 1 页；保留英文技能名" />
            </label>
            {/* 个人资料库取材：完整版材料（项目经历等）——勾选后优化从中扩写简历里的阉割版内容。 */}
            <div className="workshop-profile-picker">
              <div className="workshop-profile-head">
                <strong>个人资料库取材（可选）</strong>
                <span className="tiny-label">完整版材料：勾选与岗位相关的条目，优化会从中扩写简历里的删减版内容；只使用已写下的事实，不虚构。</span>
              </div>
              {profileEntries.length === 0
                ? <p className="tiny-label">资料库还没有启用的条目；可在「个人材料 → 个人资料库」里补充完整的项目经历等工作材料。</p>
                : <div className="workshop-profile-list">
                  {profileEntries.map(item => <label key={item.id} className="agent-checkbox workshop-profile-item">
                    <input type="checkbox" checked={profileSelected.has(item.id)} disabled={optimizing}
                      onChange={() => setProfileSelected(previous => {
                        const next = new Set(previous)
                        if (next.has(item.id)) next.delete(item.id); else next.add(item.id)
                        return next
                      })} />
                    <span><strong>{PROFILE_CATEGORY_NAMES[item.category]} · {item.title}</strong><small>{[item.org, item.period].filter(Boolean).join(' · ')} · {item.content.length} 字</small></span>
                  </label>)}
                  <button type="button" className="text-button" disabled={optimizing} onClick={() => setProfileSelected(new Set(profileEntries.map(item => item.id)))}>全选</button>
                  <button type="button" className="text-button" disabled={optimizing} onClick={() => setProfileSelected(new Set())}>清空</button>
                </div>}
            </div>
            {formError && <p className="resume-import-error" role="alert">{formError}</p>}
            <div className="workshop-config-actions">
              <button type="button" className="button primary" disabled={Boolean(busy)} onClick={() => void startOptimize()}>{optimizing && <span className="spinner" />}{optimizing ? '正在优化（约 30–90 秒）…' : '开始优化'}</button>
              <p className="workshop-hint">优化只重组你已有的事实并贴合岗位关键词，<strong>不会虚构经历、技能或数字</strong>；结果可再编辑。</p>
            </div>
          </div>
        </div> : <div className="workshop-edit">
          <div className="workshop-edit-heading">
            <label className="field-label" htmlFor="workshop-title">版本名称</label>
            <div className="workshop-title-row">
              <input id="workshop-title" value={title} onChange={event => { setTitle(event.target.value); setDirty(true) }} maxLength={100} disabled={busy === 'save'} />
              <button type="button" className="button primary" disabled={Boolean(busy) || !markdown.trim()} onClick={() => void saveVersion()}>{busy === 'save' ? <span className="spinner" /> : null}{dirty ? '保存修改' : '已保存'}</button>
            </div>
            {notes.length > 0 && <details className="workshop-notes"><summary>优化说明（{notes.length}）</summary><ul>{notes.map((note, index) => <li key={index}>{note}</li>)}</ul></details>}
          </div>
          <div className="workshop-edit-toolbar">
            <label className="field-label" htmlFor="workshop-markdown">简历正文 · Markdown，可直接编辑</label>
            {current.base_text?.trim() && <div className="workshop-edit-tabs" role="tablist" aria-label="正文查看方式">
              <button type="button" role="tab" aria-selected={editTab === 'edit'} className={editTab === 'edit' ? 'active' : ''} onClick={() => setEditTab('edit')}>编辑</button>
              <button type="button" role="tab" aria-selected={editTab === 'diff'} className={editTab === 'diff' ? 'active' : ''} onClick={() => setEditTab('diff')}>改动对比</button>
            </div>}
          </div>
          {editTab === 'diff' && current.base_text?.trim()
            ? <ResumeDiff base={current.base_text} current={markdown} />
            : <textarea id="workshop-markdown" className="workshop-markdown" value={markdown} onChange={event => { setMarkdown(event.target.value); setDirty(true) }} maxLength={60000} spellCheck={false} placeholder="# 姓名&#10;&#10;联系方式一行&#10;&#10;## 专业技能&#10;…" />}
          <div className="editor-footer">
            <span className={dirty ? 'editor-unsaved' : ''}>{markdown.length.toLocaleString()} 字符 · {dirty ? '未保存的修改' : '已保存'}{current.job_id && ' · 绑定岗位档案'}</span>
            <button type="button" className="text-button" onClick={() => void selectVersion(current.id)} disabled={Boolean(busy)}>放弃修改</button>
          </div>
        </div>}
      </section>

      <section className="workshop-panel workshop-preview" aria-label="模板与预览">
        <div className="panel-title"><h2>模板与预览</h2>{current && <span className="tiny-label">{templateName}</span>}</div>
        {current ? <>
          <div className="workshop-template-row" role="radiogroup" aria-label="选择简历模板">
            {templates.map(template => <button type="button" role="radio" aria-checked={templateId === template.id} key={template.id}
              className={`workshop-template-chip ${templateId === template.id ? 'active' : ''}`}
              title={template.description} disabled={busy === 'save'}
              onClick={() => { setTemplateId(template.id); setDirty(true) }}>{template.name}</button>)}
          </div>
          {/* 排版设置：随版本保存；页数为预览实测估算，一页纸与 PDF 导出的缩放机制一致 */}
          <div className="workshop-typography">
            <label>字体
              <select value={typography.font} disabled={busy === 'save'} aria-label="正文字体"
                onChange={event => { setTypography(previous => ({ ...previous, font: event.target.value })); setDirty(true) }}>
                {TYPOGRAPHY_FONTS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label>字号
              <select value={typography.font_size ?? ''} disabled={busy === 'save'} aria-label="正文字号"
                onChange={event => { const raw = event.target.value; setTypography(previous => ({ ...previous, font_size: raw ? Number(raw) : null })); setDirty(true) }}>
                <option value="">跟随模板</option>
                {TYPOGRAPHY_SIZES.map(size => <option key={size} value={size}>{size}pt</option>)}
              </select>
            </label>
            <label>行距
              <select value={typography.line_height ?? ''} disabled={busy === 'save'} aria-label="正文行距"
                onChange={event => { const raw = event.target.value; setTypography(previous => ({ ...previous, line_height: raw ? Number(raw) : null })); setDirty(true) }}>
                {TYPOGRAPHY_LINE_HEIGHTS.map(([value, label]) => <option key={String(value)} value={value ?? ''}>{label}</option>)}
              </select>
            </label>
            <label>页边距
              <select value={typography.margin} disabled={busy === 'save'} aria-label="页边距"
                onChange={event => { setTypography(previous => ({ ...previous, margin: event.target.value })); setDirty(true) }}>
                {TYPOGRAPHY_MARGINS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
              </select>
            </label>
            <label className="agent-checkbox workshop-onepage" title="超出 A4 一页时自动整体缩小字号压进单页（PDF 导出同步生效）">
              <input type="checkbox" checked={typography.one_page} disabled={busy === 'save'}
                onChange={event => { setTypography(previous => ({ ...previous, one_page: event.target.checked })); setDirty(true) }} />
              <span>一页纸</span>
            </label>
            {pageFit && <span className="workshop-pagefit">{typography.one_page
              ? (pageFit.shrink < 1 ? `一页纸 · 已缩至 ${Math.round(pageFit.shrink * 100)}%` : '一页纸 · 无需缩放')
              : `约 ${pageFit.pages} 页`}</span>}
            <button type="button" className="text-button" disabled={busy === 'save'}
              onClick={() => { setTypography(DEFAULT_TYPOGRAPHY); setDirty(true) }}>重置</button>
          </div>
          <div className="workshop-preview-frame-wrap">
            {previewHtml ? <iframe className="workshop-preview-frame" title="简历预览" srcDoc={previewHtml} sandbox="allow-same-origin" ref={previewFrameRef} onLoad={() => fitPreview()} /> : <div className="loading-state"><span className="spinner" />正在生成预览…</div>}
          </div>
          <div className="workshop-export">
            <span className="tiny-label">导出</span>
            {EXPORT_ITEMS.map(item => <button type="button" className="button secondary small" key={item.format}
              disabled={Boolean(busy)} onClick={() => void exportCurrent(item.format)}>{busy === `export-${item.format}` ? <span className="spinner" /> : null}{item.label}</button>)}
            <p className="workshop-hint">PDF/HTML 按当前模板排版；导出前会自动保存未存的修改。</p>
          </div>
        </> : <div className="workshop-template-gallery">
          {templates.map(template => <article className={`workshop-template-card ${templateId === template.id ? 'active' : ''}`} key={template.id} onClick={() => setTemplateId(template.id)}>
            <h3>{template.name}</h3><p>{template.description}</p>
          </article>)}
          <p className="workshop-footnote">优化或新建版本后，这里会显示所选模板的实时预览。</p>
        </div>}
      </section>
    </div>
  </div>
}
