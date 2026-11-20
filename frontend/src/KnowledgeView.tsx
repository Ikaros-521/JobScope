import { useEffect, useRef, useState } from 'react'
import type { DragEvent as ReactDragEvent, FormEvent } from 'react'
import {
  cancelKnowledgeImport, createKnowledge, createKnowledgeImport, date, deleteKnowledge, getKnowledge,
  getKnowledgeChunks, getKnowledgeImport, getKnowledgeSettings, getLatestKnowledgeImport, listKnowledge,
  reembedKnowledge, startKnowledgeImport, stageKnowledgeImportFile, updateKnowledge, updateKnowledgeSettings,
} from './api'
import type { KnowledgeChunk, KnowledgeDoc, KnowledgeImportTask, KnowledgeSettings, KnowledgeStatus, PageInfo } from './api'
import { toast } from './toast'
import { Pagination, emptyPage } from './Pagination'
import { Icon } from './Icon'

// 知识库页面：手写条目 + 文件上传统一为分块文档。搜索与 Agent 的 knowledge_search 走同一条
// FTS/向量通道，所以这里的搜索结果就是对话检索的预览；分块与检索模式在页内设置即可生效。
const KB_ACCEPT = '.pdf,.docx,.md,.markdown,.txt,.html,.htm,.csv,.tsv,.json,.jsonl,.log,.xml,.yaml,.yml,.ini,.conf,.properties,.srt,.vtt,.rst'
const STRATEGY_LABELS: Record<string, string> = { recursive: '递归', hard: '强切滑窗', punct: '分隔符', semantic: '语义嵌入', llm: '模型判边界' }
const STRATEGY_HINTS: Record<string, string> = {
  recursive: '中文分隔符层级递归；Markdown 自动按标题层级切块，块保留所属标题',
  hard: '固定窗口滑动直切（Dify「通用」分段同款），粗粒度均匀',
  punct: '按分隔符切段合并到块上限，超限滑窗兜底（FastGPT/Dify 分隔符分段同款）',
  semantic: '逐句嵌入找主题断点；需要已配置 Embedding，否则自动回退递归',
  llm: '对话模型判断主题边界；需要已配置对话模型，否则自动回退递归',
}
const RETRIEVAL_LABELS: Record<string, string> = { lexical: '词法（BM25）', hybrid: '混合（向量+词法）' }
const KB_DOCUMENTS_TIP = '支持 PDF / Word .docx / Markdown / 纯文本等格式，单文件 ≤100MB、单批 ≤20 个；上传完成后后台导入，可随时停止。'
const IMPORT_FILE_STATE_LABELS: Record<string, string> = { pending: '等待上传', staged: '已上传', processing: '导入中', imported: '已导入', failed: '失败', skipped: '重复跳过', cancelled: '已取消' }
const IMPORT_ACTIVE = new Set(['pending', 'running'])

export function KnowledgeView({ onCountChange }: { onCountChange?: (total: number) => void }) {
  const [query, setQuery] = useState('')
  const [queryDraft, setQueryDraft] = useState('')
  const [tag, setTag] = useState('')
  const [entries, setEntries] = useState<KnowledgeDoc[]>([])
  const [tags, setTags] = useState<string[]>([])
  const [info, setInfo] = useState<PageInfo>(emptyPage)
  const [settings, setSettings] = useState<KnowledgeSettings | null>(null)
  const [meta, setMeta] = useState<KnowledgeStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [editor, setEditor] = useState<{ id: string | null; title: string; tags: string; content: string } | null>(null)
  const [preview, setPreview] = useState<{ doc: KnowledgeDoc; mode: 'text' | 'chunks'; chunks: KnowledgeChunk[] } | null>(null)
  const [armedDelete, setArmedDelete] = useState<string | null>(null)
  const [settingsDraft, setSettingsDraft] = useState<{ strategy: string; size: number; overlap: number; separators: string; retrieval_mode: string } | null>(null)
  const [importTask, setImportTask] = useState<KnowledgeImportTask | null>(null)
  const [stagingText, setStagingText] = useState('')
  const [dragging, setDragging] = useState(false)
  const dragDepth = useRef(0)
  const request = useRef(0)
  const pollToken = useRef(0)
  const fileInput = useRef<HTMLInputElement>(null)

  async function load(q = query, t = tag, page = 1) {
    const token = ++request.current
    setLoading(true)
    try {
      const data = await listKnowledge(q, t, page)
      if (token !== request.current) return
      setEntries(data.entries); setTags(data.tags); setInfo(data)
      setMeta(current => current ? { ...current, ...data } : data)
      onCountChange?.(data.total)
    } catch (err) { toast((err as Error).message, 'error') }
    finally { if (token === request.current) setLoading(false) }
  }
  useEffect(() => {
    void load()
    getKnowledgeSettings().then(payload => { setSettings(payload.settings); setMeta(payload) }).catch(() => { /* 设置读取失败时面板显示未知状态 */ })
    // 页面重开时恢复未完成的导入任务（轮询继续，直到任务结束）
    getLatestKnowledgeImport().then(task => { if (task && IMPORT_ACTIVE.has(task.status)) { setImportTask(task); pollImport(task.id) } }).catch(() => { /* 查询失败忽略 */ })
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  function search(event: FormEvent) {
    event.preventDefault()
    setQuery(queryDraft)
    void load(queryDraft, tag, 1)
  }
  function pickTag(next: string) {
    setTag(next)
    void load(query, next, 1)
  }

  function pollImport(taskId: string) {
    pollToken.current += 1
    const token = pollToken.current
    const tick = async () => {
      if (pollToken.current !== token) return
      try {
        const task = await getKnowledgeImport(taskId)
        if (pollToken.current !== token) return
        setImportTask(task)
        if (IMPORT_ACTIVE.has(task.status)) { window.setTimeout(() => void tick(), 1500); return }
        // 终态：提示结果并刷新列表；轮询到此为止（保留卡片供查看，可手动关闭）
        if (task.status === 'completed') toast(task.message || '导入完成', task.files.some(f => f.state === 'failed') ? 'error' : 'notice')
        else if (task.status === 'cancelled') toast(task.message || '导入已停止', 'notice')
        else if (task.status === 'failed') toast(task.message || '导入失败', 'error', false, undefined, 10000)
        void load(query, tag)
      } catch { window.setTimeout(() => { if (pollToken.current === token) void tick() }, 3000) }
    }
    void tick()
  }

  async function handleUpload(files: File[] | FileList | null) {
    const list = files ? Array.from(files) : []
    if (!list.length) return
    if (importTask && IMPORT_ACTIVE.has(importTask.status)) { toast('已有导入任务在进行中，请等待完成或先停止', 'error'); return }
    setBusy('knowledge-upload')
    setStagingText('正在创建导入任务…')
    try {
      const task = await createKnowledgeImport(list.map(file => file.name))
      setImportTask(task)
      for (let index = 0; index < list.length; index++) {
        setStagingText(`正在上传 ${index + 1}/${list.length}：${list[index].name}`)
        await stageKnowledgeImportFile(task.id, index, list[index].name, list[index])
      }
      setStagingText('')
      const started = await startKnowledgeImport(task.id)
      setImportTask(started)
      pollImport(task.id)
      toast(`已开始后台导入 ${list.length} 个文件，完成后会提示结果`, 'notice')
    } catch (err) {
      toast((err as Error).message, 'error', false, undefined, 10000)
      setImportTask(current => current && IMPORT_ACTIVE.has(current.status) ? current : null)
      pollToken.current += 1
    } finally { setBusy(''); setStagingText(''); if (fileInput.current) fileInput.current.value = '' }
  }

  async function stopImport() {
    if (!importTask || !IMPORT_ACTIVE.has(importTask.status)) return
    try {
      const task = await cancelKnowledgeImport(importTask.id)
      setImportTask(task)
      toast(task.status === 'cancelled' ? '导入任务已停止' : '正在停止导入任务，当前文件完成后收尾', 'notice')
    } catch (err) { toast((err as Error).message, 'error') }
  }

  // 拖拽上传：进入知识库页面即高亮，松手走与文件选择相同的导入任务流程。
  function onDragEnter(event: ReactDragEvent) {
    if (!Array.from(event.dataTransfer.types || []).includes('Files')) return
    event.preventDefault()
    dragDepth.current += 1
    setDragging(true)
  }
  function onDragOver(event: ReactDragEvent) { event.preventDefault() }
  function onDragLeave(event: ReactDragEvent) {
    if (!Array.from(event.dataTransfer.types || []).includes('Files')) return
    dragDepth.current = Math.max(0, dragDepth.current - 1)
    if (dragDepth.current === 0) setDragging(false)
  }
  function onDrop(event: ReactDragEvent) {
    if (!Array.from(event.dataTransfer.types || []).includes('Files')) return
    event.preventDefault()
    dragDepth.current = 0
    setDragging(false)
    void handleUpload(event.dataTransfer.files)
  }

  async function openEditor(id: string | null) {
    if (!id) { setEditor({ id: null, title: '', tags: '', content: '' }); return }
    try {
      // 列表条目带截断标记，编辑前取全文，避免「保存一次就把长文截掉」。
      const entry = await getKnowledge(id)
      setEditor({ id, title: entry.title, tags: entry.tags.join(', '), content: entry.content })
    } catch (err) { toast((err as Error).message, 'error') }
  }
  async function saveEditor(event: FormEvent) {
    event.preventDefault()
    if (!editor || busy) return
    const tags = editor.tags.split(/[,，]/).map(item => item.trim()).filter(Boolean)
    const id = editor.id
    setBusy('knowledge-save')
    try {
      const payload = { title: editor.title.trim(), content: editor.content.trim(), tags }
      await (id ? updateKnowledge(id, payload) : createKnowledge(payload))
      setEditor(null)
      toast(id ? '知识文档已更新，内容变更按原分块策略重切' : '知识已录入，对话里可以直接检索引用')
      await load(query, tag)
    } catch (err) { toast((err as Error).message, 'error') }
    finally { setBusy('') }
  }
  async function remove(entry: KnowledgeDoc) {
    // 两步删除：第一次点只进入待确认态，3 秒内再点才真删；不弹全局弹窗打断列表浏览。
    if (armedDelete !== entry.id) {
      setArmedDelete(entry.id)
      window.setTimeout(() => setArmedDelete(current => current === entry.id ? null : current), 3000)
      return
    }
    setArmedDelete(null)
    try {
      await deleteKnowledge(entry.id)
      toast('知识文档已删除')
      await load(query, tag, info.page)
    } catch (err) { toast((err as Error).message, 'error') }
  }

  async function openPreview(id: string, mode: 'text' | 'chunks' = 'text') {
    setBusy('knowledge-preview')
    try {
      const doc = await getKnowledge(id)
      const { chunks } = await getKnowledgeChunks(id)
      setPreview({ doc, mode, chunks })
    } catch (err) { toast((err as Error).message, 'error') }
    finally { setBusy('') }
  }
  function switchPreviewMode(mode: 'text' | 'chunks') {
    if (preview && preview.mode !== mode) setPreview({ ...preview, mode })
  }

  async function saveSettings(event: FormEvent) {
    event.preventDefault()
    if (!settingsDraft) return
    setBusy('knowledge-settings')
    try {
      const payload = await updateKnowledgeSettings({
        strategy: settingsDraft.strategy as never, size: Number(settingsDraft.size), overlap: Number(settingsDraft.overlap),
        separators: settingsDraft.separators.split(/[,，]/).map(item => item.trim()).filter(Boolean),
        retrieval_mode: settingsDraft.retrieval_mode as never,
      })
      setSettings(payload.settings); setMeta(payload)
      setSettingsDraft(null)
      toast('设置已保存：之后导入/重切的文档按新策略分块，已入库文档保持原策略')
    } catch (err) { toast((err as Error).message, 'error') }
    finally { setBusy('') }
  }
  async function rebuildVectors() {
    if (busy) return
    setBusy('knowledge-reembed')
    try {
      const result = await reembedKnowledge()
      setMeta(result)
      toast(`向量已重建：${result.embedded} 个分块（${result.model}）`)
    } catch (err) { toast((err as Error).message, 'error') }
    finally { setBusy('') }
  }

  const editorTitle = editor?.id ? '编辑知识文档' : '录入知识条目'
  const overviewCards = meta ? [
    { icon: 'knowledge', label: '知识文档', main: `${meta.docs}`, sub: `${meta.chunks} 个分块` },
    { icon: 'chat', label: '对话检索', main: RETRIEVAL_LABELS[settings?.retrieval_mode || 'lexical'] || '词法', sub: settings?.retrieval_mode === 'hybrid' ? '向量+词法 RRF 融合' : '分块 BM25 检索' },
    { icon: 'source', label: '向量状态', main: meta.embedding_ready ? `${meta.embedded}/${meta.chunks}` : '未配置', sub: meta.stale ? '嵌入模型已变更，建议重建' : meta.pending > 0 ? `${meta.pending} 个分块待补` : meta.embedding_ready ? '向量已就绪' : '配置 Embedding 后可启用混合检索' },
  ] : []

  const importActive = Boolean(importTask && IMPORT_ACTIVE.has(importTask.status))
  return <div className={`knowledge-view${dragging ? ' dragging' : ''}`} onDragEnter={onDragEnter} onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}>
    {dragging && <div className="kb-drop-overlay" role="status"><Icon name="source" size={36} /><strong>松开鼠标，导入文件到知识库</strong><span>支持多选；单文件 ≤100MB，单批 ≤20 个</span></div>}
    <section className="page-intro compact"><h1 className="sr-only">知识库</h1>
      <div className="knowledge-intro-actions">
        <input ref={fileInput} type="file" multiple accept={KB_ACCEPT} hidden onChange={event => void handleUpload(event.target.files)} aria-hidden="true" tabIndex={-1} />
        <button className="button secondary" title={KB_DOCUMENTS_TIP} disabled={Boolean(busy) || importActive} onClick={() => fileInput.current?.click()}>
          {busy === 'knowledge-upload' ? <span className="spinner" /> : <Icon name="source" size={16} />}上传文档
        </button>
        <button className="button primary" onClick={() => void openEditor(null)}><Icon name="plus" size={16} />新建知识</button>
      </div></section>

    {stagingText && <div className="kb-import-card paper-panel" role="status"><span className="spinner" /><strong>{stagingText}</strong></div>}
    {importTask && !stagingText && <ImportTaskCard task={importTask} onStop={() => void stopImport()} onClose={() => { pollToken.current += 1; setImportTask(null) }} />}

    <div className="management-overview" aria-label="知识库概况">
      {overviewCards.map(card => <div key={card.label}><span className="overview-icon"><Icon name={card.icon} /></span><div><span>{card.label}</span><strong>{card.main}<small>{card.sub}</small></strong></div></div>)}
    </div>

    {settings && meta && <details className="knowledge-settings paper-panel" open={Boolean(settingsDraft)}>
      <summary onClick={event => { if (!settingsDraft) { event.preventDefault(); setSettingsDraft({ strategy: settings.strategy, size: settings.size, overlap: settings.overlap, separators: settings.separators.join(', '), retrieval_mode: settings.retrieval_mode }) } }}>
        <Icon name="settings" size={16} />分块与检索设置<span className="muted body-small">当前策略：{STRATEGY_LABELS[settings.strategy]} · {settings.size}/{settings.overlap} · 检索：{RETRIEVAL_LABELS[settings.retrieval_mode]}</span>
      </summary>
      <form onSubmit={saveSettings} className="knowledge-settings-body">
        <label className="field-label" htmlFor="kb-strategy">分块策略（新导入生效；已入库文档重切时沿用各自原策略）</label>
        <select id="kb-strategy" value={settingsDraft?.strategy || settings.strategy} onChange={event => setSettingsDraft({ ...settingsDraft!, strategy: event.target.value })}>
          {Object.keys(STRATEGY_LABELS).map(key => <option key={key} value={key}>{STRATEGY_LABELS[key]}</option>)}
        </select>
        <p className="muted body-small">{STRATEGY_HINTS[settingsDraft?.strategy || settings.strategy]}
          {(settingsDraft?.strategy || settings.strategy) === 'semantic' && !meta.embedding_ready && '（当前 Embedding 未配置：导入时将自动回退递归分块）'}</p>
        <div className="knowledge-settings-grid">
          <div><label className="field-label" htmlFor="kb-size">块大小 <span>· 字符</span></label><input id="kb-size" type="number" min={64} max={2000} value={settingsDraft?.size ?? settings.size} onChange={event => setSettingsDraft({ ...settingsDraft!, size: Number(event.target.value) })} inputMode="numeric" /></div>
          <div><label className="field-label" htmlFor="kb-overlap">相邻重叠 <span>· 字符</span></label><input id="kb-overlap" type="number" min={0} max={2000} value={settingsDraft?.overlap ?? settings.overlap} onChange={event => setSettingsDraft({ ...settingsDraft!, overlap: Number(event.target.value) })} inputMode="numeric" /></div>
          <div className="kb-separators"><label className="field-label" htmlFor="kb-separators">分隔符</label><input id="kb-separators" value={settingsDraft?.separators ?? settings.separators.join(', ')} onChange={event => setSettingsDraft({ ...settingsDraft!, separators: event.target.value })} placeholder="仅分隔符策略 · 逗号分隔，默认：\n\n, \n, 。, ！, ？, ；" /></div>
          <div><label className="field-label" htmlFor="kb-mode">检索模式</label><select id="kb-mode" value={settingsDraft?.retrieval_mode || settings.retrieval_mode} onChange={event => setSettingsDraft({ ...settingsDraft!, retrieval_mode: event.target.value })}><option value="lexical">词法（BM25）</option><option value="hybrid">混合（向量+词法）</option></select></div>
        </div>
        <div className="knowledge-settings-actions">
          <span className="muted body-small">{meta.embedding_ready
            ? `Embedding：${meta.embedding_model} · 已嵌入 ${meta.embedded}/${meta.chunks}${meta.stale ? ' · 嵌入模型已变更' : ''}`
            : 'Embedding 未配置：词法检索可用，混合检索与语义分块不可用'}
            {settings.retrieval_mode === 'hybrid' && meta.pending > 0 && !meta.stale && <button type="button" className="text-button" disabled={Boolean(busy)} onClick={() => void rebuildVectors()}>补齐向量</button>}
            {meta.stale && <button type="button" className="text-button" disabled={Boolean(busy)} onClick={() => void rebuildVectors()}>重建向量</button>}
          </span>
          {settingsDraft && <><button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={() => setSettingsDraft(null)}>取消</button>
            <button className="button primary small" type="submit" disabled={Boolean(busy)}>{busy === 'knowledge-settings' ? <span className="spinner" /> : null}保存设置</button></>}
        </div>
      </form>
    </details>}

    <section className="paper-panel knowledge-panel">
      <form className="knowledge-toolbar" onSubmit={search}>
        <label className="search-input"><Icon name="search" size={16} /><span className="sr-only">知识检索</span>
          <input value={queryDraft} onChange={event => setQueryDraft(event.target.value)} placeholder="搜索知识标题与正文分块（与对话检索同通道）" maxLength={300} /></label>
        <button className="button secondary small" type="submit" disabled={loading}>搜索</button>
        {(query || tag) && <button type="button" className="text-button" onClick={() => { setQueryDraft(''); setQuery(''); setTag(''); void load('', '', 1) }}>清除</button>}
      </form>
      {tags.length > 0 && <div className="knowledge-tags" aria-label="按标签筛选">
        {tags.map(item => <button key={item} type="button" className={`knowledge-tag${tag === item ? ' active' : ''}`} aria-pressed={tag === item} onClick={() => pickTag(tag === item ? '' : item)}>{item}</button>)}
      </div>}
      {loading ? <div className="loading-state"><span className="spinner" />正在读取知识库…</div>
        : entries.length === 0 ? <EmptyState title={query || tag ? '没有匹配的知识文档' : '知识库还是空的'} query={Boolean(query || tag)} onUpload={() => fileInput.current?.click()} onNew={() => void openEditor(null)} />
          : <div className="knowledge-list">{entries.map(entry => <KnowledgeCard key={entry.id} entry={entry} armed={armedDelete === entry.id}
            onPreview={() => void openPreview(entry.id)} onEdit={() => void openEditor(entry.id)} onDelete={() => void remove(entry)} onDisarmDelete={() => setArmedDelete(null)} />)}</div>}
      <Pagination info={info} label="知识文档" busy={loading} onPage={page => void load(query, tag, page)} />
    </section>

    {preview && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) setPreview(null) }}>
      <section className="modal paper-panel knowledge-preview" role="dialog" aria-modal="true" aria-labelledby="knowledge-preview-title" onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); setPreview(null) } }}>
        <div className="section-heading"><div><h2 id="knowledge-preview-title">{preview.doc.title}</h2></div><button className="icon-button" aria-label="关闭预览" onClick={() => setPreview(null)}><Icon name="close" /></button></div>
        <p className="muted body-small knowledge-preview-meta">
          {preview.doc.source === 'upload' ? `上传 · ${preview.doc.file_name} · ${(preview.doc.file_size! / 1024).toFixed(1)} KB · ` : '手动录入 · '}
          {STRATEGY_LABELS[preview.doc.chunk_strategy] || preview.doc.chunk_strategy} 分块 · {preview.doc.chunk_size}/{preview.doc.chunk_overlap} · {preview.doc.chunk_count} 块 · 更新于 {date(preview.doc.updated_at)}
        </p>
        <div className="knowledge-preview-tabs" role="tablist" aria-label="预览方式">
          <button type="button" role="tab" aria-selected={preview.mode === 'text'} className={preview.mode === 'text' ? 'active' : ''} onClick={() => void switchPreviewMode('text')}>全文</button>
          <button type="button" role="tab" aria-selected={preview.mode === 'chunks'} className={preview.mode === 'chunks' ? 'active' : ''} onClick={() => void switchPreviewMode('chunks')}>分块视图</button>
        </div>
        {preview.mode === 'text'
          ? <div className="knowledge-preview-body">{preview.doc.content}</div>
          : <div className="knowledge-preview-chunks">{preview.chunks.length ? preview.chunks.map(chunk => <article key={chunk.ordinal} className="knowledge-chunk-card">
            <span className="knowledge-chunk-index">块 {chunk.ordinal + 1}</span><span className="muted body-small">{chunk.characters} 字符</span>
            <p>{chunk.text}</p></article>) : <div className="loading-state"><span className="spinner" />正在读取分块…</div>}</div>}
        <div className="modal-actions">
          <button type="button" className="button secondary" onClick={() => { setPreview(null); void openEditor(preview.doc.id) }}><Icon name="settings" size={15} />编辑</button>
          <button type="button" className="button primary" onClick={() => setPreview(null)}>关闭</button>
        </div>
      </section>
    </div>}

    {editor && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) setEditor(null) }}>
      <section className="modal paper-panel" role="dialog" aria-modal="true" aria-labelledby="knowledge-editor-title" onKeyDown={event => {
        if (event.key === 'Escape' && !busy) { event.stopPropagation(); setEditor(null) }
        if (event.key === 'Tab') {
          const focusable = Array.from(event.currentTarget.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), textarea:not(:disabled)'))
          const first = focusable[0], last = focusable[focusable.length - 1]
          if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
          else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
        }
      }}>
        <div className="section-heading"><div><h2 id="knowledge-editor-title">{editorTitle}</h2></div><button className="icon-button" aria-label="关闭弹窗" onClick={() => setEditor(null)} disabled={Boolean(busy)}><Icon name="close" /></button></div>
        <p className="muted body-small">保存后即可在对话中检索引用；条目属于你本人的自述材料，Agent 会如实标注来源，不会当成外部事实。</p>
        <form onSubmit={saveEditor}>
          <label className="field-label" htmlFor="knowledge-title">标题 <span>*</span></label>
          <input id="knowledge-title" value={editor.title} onChange={event => setEditor({ ...editor, title: event.target.value })} required maxLength={200} autoFocus placeholder="如：甲公司二面复盘" />
          <label className="field-label" htmlFor="knowledge-tags">标签（逗号分隔，可选）</label>
          <input id="knowledge-tags" value={editor.tags} onChange={event => setEditor({ ...editor, tags: event.target.value })} maxLength={400} placeholder="面试, 甲公司" />
          <label className="field-label" htmlFor="knowledge-content">知识内容 <span>*</span></label>
          <textarea id="knowledge-content" value={editor.content} onChange={event => setEditor({ ...editor, content: event.target.value })} required rows={9} maxLength={200000} spellCheck={false}
            placeholder={'记录事实与结论，保留具体细节。例如：\n· 二面重点是 RAG 评测与成本控制\n· 团队 12 人，技术栈以 Python 为主'} />
          <div className="editor-footer"><span>{editor.content.length.toLocaleString()} 字符</span></div>
          <div className="modal-actions"><button type="button" className="button secondary" onClick={() => setEditor(null)} disabled={Boolean(busy)}>取消</button>
            <button className="button primary" type="submit" disabled={Boolean(busy)}>{busy ? <span className="spinner" /> : null}{editor.id ? '保存修改' : '保存知识'}</button></div>
        </form>
      </section>
    </div>}
  </div>
}

function KnowledgeCard({ entry, armed, onPreview, onEdit, onDelete, onDisarmDelete }: {
  entry: KnowledgeDoc; armed: boolean; onPreview: () => void; onEdit: () => void; onDelete: () => void; onDisarmDelete: () => void
}) {
  return <article className="knowledge-card">
    <div className="knowledge-card-head">
      <h3>{entry.title}{entry.source === 'upload' && <span className="knowledge-source-tag">上传</span>}</h3>
      <div className="knowledge-card-actions">
        <button className="text-button" onClick={onPreview}><Icon name="book" size={14} />预览</button>
        <button className="text-button" onClick={onEdit}><Icon name="settings" size={14} />编辑</button>
        <button className={`text-button${armed ? ' danger-text' : ''}`} onClick={armed ? onDelete : onDisarmDelete}
          onBlur={() => armed && onDisarmDelete()}>{armed ? '再点一次确认删除' : '删除'}</button>
      </div>
    </div>
    <p className="knowledge-card-body">{entry.hit_excerpt || entry.content}{(entry.content_truncated && !entry.hit_excerpt) && '…'}</p>
    <div className="knowledge-card-meta">
      <span className="knowledge-tag">{STRATEGY_LABELS[entry.chunk_strategy] || entry.chunk_strategy} · {entry.chunk_count} 块</span>
      {entry.file_name && <span className="muted body-small">{entry.file_name} · {((entry.file_size || 0) / 1024).toFixed(1)} KB</span>}
      {entry.tags.map(item => <span key={item} className="knowledge-tag">{item}</span>)}
      <span className="muted body-small">更新于 {date(entry.updated_at)}</span>
    </div>
  </article>
}

function EmptyState({ title, query, onUpload, onNew }: { title: string; query: boolean; onUpload: () => void; onNew: () => void }) {
  return <div className="empty"><div className="empty-icon"><Icon name="knowledge" /></div><h3>{title}</h3>
    <p>{query ? <>换个关键词试试，或<button className="inline-button" onClick={onNew}>新建一条</button>知识。</>
      : <>把面试后的复盘、对行业与公司的判断<button className="inline-button" onClick={onNew}>手动录入</button>，或<button className="inline-button" onClick={onUpload}>上传 PDF / Word / Markdown 文档</button>；入库后对话中的 Agent 会按分块检索引用。</>}</p></div>
}


function ImportTaskCard({ task, onStop, onClose }: { task: KnowledgeImportTask; onStop: () => void; onClose: () => void }) {
  const active = IMPORT_ACTIVE.has(task.status)
  const statusLabel = { pending: '上传中', running: '导入中', completed: '已完成', failed: '失败', cancelled: '已停止' }[task.status] || task.status
  return <div className="kb-import-card paper-panel" role="status">
    <div className="kb-import-head">
      <span className={`kb-import-state ${task.status}`}><span className={active ? 'spinner' : ''} />{statusLabel}</span>
      <strong>{task.message}</strong>
      <span className="muted body-small">{task.files.length} 个文件 · 成功 {task.files.filter(f => f.state === 'imported').length} · 失败 {task.files.filter(f => f.state === 'failed').length}</span>
      <div className="kb-import-actions">
        {active && <button className="button secondary small danger-text" onClick={onStop}>停止导入</button>}
        {!active && <button className="text-button" onClick={onClose}>关闭</button>}
      </div>
    </div>
    <div className="kb-import-files">
      {task.files.map(file => <div key={file.index} className={`kb-import-file ${file.state}`}>
        <span className="kb-file-state">{IMPORT_FILE_STATE_LABELS[file.state] || file.state}</span>
        <span className="kb-file-name" title={file.name}>{file.name}</span>
        <span className="muted body-small">{file.chunks ? `${file.chunks} 块` : file.size ? `${(file.size / 1024).toFixed(0)} KB` : ''}</span>
        <span className="kb-file-error">{file.error || (file.warnings || []).join('；')}</span>
      </div>)}
    </div>
  </div>
}
