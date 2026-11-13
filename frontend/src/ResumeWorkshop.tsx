import { useEffect, useRef, useState } from 'react'
import {
  api, date, listResumeTemplates, previewResumeDocument, listResumeVersions, getResumeVersion,
  createResumeVersion, updateResumeVersion, deleteResumeVersion, optimizeResume, exportResumeVersion,
} from './api'
import type { Job, PageInfo, ResumeTemplate, ResumeVersion, ResumeVersionSummary } from './api'
import { toast } from './toast'

// 岗位详情页「优化简历」跳转预填：带上岗位定位，进入后自动选中该岗位。
export type WorkshopPrefill = { jobId: string; company: string; jobTitle: string }

const EXPORT_ITEMS = [
  { format: 'pdf', label: 'PDF' },
  { format: 'docx', label: 'Word' },
  { format: 'html', label: 'HTML' },
  { format: 'md', label: 'Markdown' },
] as const
const EXPORT_EXTENSIONS: Record<string, string> = { pdf: 'pdf', docx: 'docx', html: 'html', md: 'md' }

function Empty({ title, children }: { title: string; children: React.ReactNode }) {
  return <div className="workshop-empty"><h3>{title}</h3><p>{children}</p></div>
}

export function ResumeWorkshop({ prefill, onPrefillConsumed, onOpenResume }: { prefill: WorkshopPrefill | null; onPrefillConsumed: () => void; onOpenResume: () => void }) {
  const [templates, setTemplates] = useState<ResumeTemplate[]>([])
  const [versions, setVersions] = useState<ResumeVersionSummary[]>([])
  const [resumeCharacters, setResumeCharacters] = useState<number | null>(null)
  const [current, setCurrent] = useState<ResumeVersion | null>(null)
  const [title, setTitle] = useState('')
  const [markdown, setMarkdown] = useState('')
  const [templateId, setTemplateId] = useState('classic')
  const [dirty, setDirty] = useState(false)
  const [notes, setNotes] = useState<string[]>([])
  const [previewHtml, setPreviewHtml] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [formError, setFormError] = useState('')
  const [deletingId, setDeletingId] = useState<number | null>(null)
  // 优化配置表单
  const [baseMode, setBaseMode] = useState<'saved' | 'paste'>('saved')
  const [pasteResume, setPasteResume] = useState('')
  const [targetMode, setTargetMode] = useState<'job' | 'paste'>('job')
  const [jobQuery, setJobQuery] = useState('')
  const [jobResults, setJobResults] = useState<Job[]>([])
  const [jobSearching, setJobSearching] = useState(false)
  const [selectedJob, setSelectedJob] = useState<Job | null>(null)
  const [jdText, setJdText] = useState('')
  const [pasteCompany, setPasteCompany] = useState('')
  const [pasteJobTitle, setPasteJobTitle] = useState('')
  const [instructions, setInstructions] = useState('')
  const jobSearchToken = useRef(0)
  const previewToken = useRef(0)

  useEffect(() => {
    listResumeTemplates().then(data => setTemplates(data.templates)).catch(() => setTemplates([]))
    void refreshVersions()
    api<{ text: string }>('/resume').then(data => setResumeCharacters(data.text.trim().length)).catch(() => setResumeCharacters(null))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function refreshVersions() {
    try { setVersions((await listResumeVersions()).versions) } catch { /* 列表加载失败保持原状，操作反馈走 toast */ }
  }

  // 岗位详情跳转预填：拉取该岗位并自动选中。
  useEffect(() => {
    if (!prefill) return
    api<Job>(`/jobs/${encodeURIComponent(prefill.jobId)}`)
      .then(job => { setSelectedJob(job); setTargetMode('job'); setJobQuery(`${prefill.jobTitle} ${prefill.company}`.trim()) })
      .catch(() => toast('岗位档案读取失败，请手动选择目标岗位'))
      .finally(onPrefillConsumed)
  }, [prefill, onPrefillConsumed])

  // 岗位 typeahead：两字符起检索本地档案，序号防过期响应回写。
  useEffect(() => {
    const query = jobQuery.trim()
    if (targetMode !== 'job' || query.length < 2) { setJobResults([]); setJobSearching(false); return }
    const token = ++jobSearchToken.current
    setJobSearching(true)
    api<PageInfo & { jobs: Job[] }>(`/jobs?q=${encodeURIComponent(query)}&page=1`)
      .then(data => { if (token === jobSearchToken.current) setJobResults(data.jobs.slice(0, 8)) })
      .catch(() => { if (token === jobSearchToken.current) setJobResults([]) })
      .finally(() => { if (token === jobSearchToken.current) setJobSearching(false) })
  }, [jobQuery, targetMode])

  // 编辑/模板切换后 800ms 防抖刷新预览；与导出共用后端渲染管线，所见即所得。
  useEffect(() => {
    if (!current || !markdown.trim()) { setPreviewHtml(''); return }
    const token = ++previewToken.current
    const timer = setTimeout(() => {
      previewResumeDocument(markdown, templateId)
        .then(data => { if (token === previewToken.current) setPreviewHtml(data.html) })
        .catch(() => { if (token === previewToken.current) setPreviewHtml('') })
    }, 800)
    return () => clearTimeout(timer)
  }, [current, markdown, templateId])

  function loadVersion(version: ResumeVersion) {
    setCurrent(version)
    setTitle(version.title)
    setMarkdown(version.markdown)
    setTemplateId(version.template_id || 'classic')
    setNotes(version.notes || [])
    setDirty(false)
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
      })
      loadVersion(version)
      await refreshVersions()
      toast('优化完成，已生成新版本；可直接二次编辑或导出。')
    } catch (err) { setFormError((err as Error).message) } finally { setBusy(null) }
  }

  async function saveVersion() {
    if (!current || busy || !markdown.trim()) return
    setBusy('save')
    try {
      const version = await updateResumeVersion(current.id, { title: title.trim() || current.title, markdown, template_id: templateId })
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
      // 导出前先把未保存的编辑落库，保证文件与所见一致。
      if (dirty || markdown.trim() !== current.markdown || templateId !== current.template_id || title.trim() !== current.title) {
        setBusy('save')
        const version = await updateResumeVersion(current.id, { title: title.trim() || current.title, markdown, template_id: templateId })
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
    <section className="page-intro"><div><h1>简历工坊</h1><p>针对不同公司的岗位要求定制简历：AI 优化 → 二次编辑 → 模板排版 → 导出投递。</p></div>
      <div className="workshop-intro-actions">
        <button type="button" className="button secondary" disabled={Boolean(busy)} onClick={() => { setCurrent(null); setMarkdown(''); setPreviewHtml(''); setDirty(false) }}>新建优化</button>
        <button type="button" className="button secondary" disabled={Boolean(busy) || resumeCharacters === null || !resumeCharacters} onClick={() => void createFromSaved()}>从我的简历复制</button>
      </div>
    </section>
    <div className="workshop-layout">
      <aside className="workshop-panel workshop-versions" aria-label="简历版本列表">
        <div className="panel-title"><h2>投递版本 <span>{versions.length}</span></h2></div>
        <div className="workshop-version-list">
          {versions.length === 0 ? <Empty title="还没有定制版本">在右侧选择目标岗位并开始优化，每次优化都会保存为一个可二次编辑的版本。</Empty>
            : versions.map(version => <div className={`workshop-version ${current?.id === version.id ? 'active' : ''}`} key={version.id}>
              <button type="button" className="workshop-version-main" onClick={() => void selectVersion(version.id)} title={version.title}>
                <strong>{version.title}</strong>
                <span>{version.company || version.job_title || '未绑定岗位'}</span>
                <time>{date(version.updated_at)} · {version.characters.toLocaleString()} 字</time>
              </button>
              <button type="button" className={`workshop-version-delete ${deletingId === version.id ? 'confirm' : ''}`} disabled={Boolean(busy)}
                onClick={() => void removeVersion(version.id)}>{deletingId === version.id ? '确认' : '删除'}</button>
            </div>)}
        </div>
        <p className="workshop-footnote">版本保存在本机；优化说明随版本保留，方便对照修改。</p>
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
                    <button type="button" className="text-button" onClick={() => { setSelectedJob(null); setJobQuery('') }} disabled={optimizing}>重选</button>
                  </div>
                  : <div className="workshop-job-search">
                    <input value={jobQuery} onChange={event => setJobQuery(event.target.value)} placeholder="搜索本地岗位档案，如：后端 字节" maxLength={200} disabled={optimizing} aria-label="搜索岗位档案" />
                    {jobSearching && <span className="spinner" aria-hidden="true" />}
                    {jobResults.length > 0 && <ul className="workshop-job-results">
                      {jobResults.map(job => <li key={job.id}><button type="button" onClick={() => { setSelectedJob(job); setJobResults([]) }}>
                        <strong>{job.title}</strong><span>{job.company}{job.location ? ` · ${job.location}` : ''}</span>
                        {job.description_status !== 'complete' && <em>仅列表摘要</em>}
                      </button></li>)}
                    </ul>}
                    <p className="workshop-hint">检索本地已保存岗位；标注「仅列表摘要」的建议先在岗位研究页补全职责。</p>
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
          <label className="field-label" htmlFor="workshop-markdown">简历正文 · Markdown，可直接编辑</label>
          <textarea id="workshop-markdown" className="workshop-markdown" value={markdown} onChange={event => { setMarkdown(event.target.value); setDirty(true) }} maxLength={60000} spellCheck={false} placeholder="# 姓名&#10;&#10;联系方式一行&#10;&#10;## 专业技能&#10;…" />
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
          <div className="workshop-preview-frame-wrap">
            {previewHtml ? <iframe className="workshop-preview-frame" title="简历预览" srcDoc={previewHtml} sandbox="" /> : <div className="loading-state"><span className="spinner" />正在生成预览…</div>}
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
