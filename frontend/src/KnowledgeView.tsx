import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { createKnowledge, date, deleteKnowledge, getKnowledge, listKnowledge, updateKnowledge } from './api'
import type { KnowledgeEntry, PageInfo } from './api'
import { toast } from './toast'
import { Pagination, emptyPage } from './Pagination'
import { Icon } from './Icon'

// 知识库页面：录入、检索与维护用户知识条目。搜索与 Agent 的 knowledge_search 走同一条
// FTS 通道，所以这里的搜索结果就是对话检索的预览；为空或命中差时，对话检索也不会更好。
export function KnowledgeView({ onCountChange }: { onCountChange?: (total: number) => void }) {
  const [query, setQuery] = useState('')
  const [queryDraft, setQueryDraft] = useState('')
  const [tag, setTag] = useState('')
  const [entries, setEntries] = useState<KnowledgeEntry[]>([])
  const [tags, setTags] = useState<string[]>([])
  const [info, setInfo] = useState<PageInfo>(emptyPage)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState('')
  const [editor, setEditor] = useState<{ id: string | null; title: string; tags: string; content: string } | null>(null)
  const [armedDelete, setArmedDelete] = useState<string | null>(null)
  const request = useRef(0)

  async function load(q = query, t = tag, page = 1) {
    const token = ++request.current
    setLoading(true)
    try {
      const data = await listKnowledge(q, t, page)
      if (token !== request.current) return
      setEntries(data.entries); setTags(data.tags); setInfo(data)
      onCountChange?.(data.total)
    } catch (err) { toast((err as Error).message, 'error') }
    finally { if (token === request.current) setLoading(false) }
  }
  useEffect(() => { void load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  function search(event: FormEvent) {
    event.preventDefault()
    setQuery(queryDraft)
    void load(queryDraft, tag, 1)
  }
  function pickTag(next: string) {
    setTag(next)
    void load(query, next, 1)
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
      toast(id ? '知识条目已更新，对话中可继续检索引用' : '知识已录入，对话里可以直接检索引用')
      await load(query, tag)
    } catch (err) { toast((err as Error).message, 'error') }
    finally { setBusy('') }
  }
  async function remove(entry: KnowledgeEntry) {
    // 两步删除：第一次点只进入待确认态，3 秒内再点才真删；不弹全局弹窗打断列表浏览。
    if (armedDelete !== entry.id) {
      setArmedDelete(entry.id)
      window.setTimeout(() => setArmedDelete(current => current === entry.id ? null : current), 3000)
      return
    }
    setArmedDelete(null)
    try {
      await deleteKnowledge(entry.id)
      toast('知识条目已删除')
      await load(query, tag, info.page)
    } catch (err) { toast((err as Error).message, 'error') }
  }

  const editorTitle = editor?.id ? '编辑知识条目' : '录入知识条目'

  return <div className="knowledge-view">
    <section className="page-intro"><div><h1>知识库</h1><p>录入你的笔记与自述知识（面试复盘、行业认知、个人备忘），全局对话等 Agent 会话会检索并引用它们。</p></div>
      <button className="button primary" onClick={() => void openEditor(null)}><Icon name="plus" size={16} />新建知识</button></section>

    <div className="management-overview" aria-label="知识库概况">
      <div><span className="overview-icon"><Icon name="knowledge" /></span><div><span>知识条目</span><strong>{info.total.toLocaleString()}<small>条</small></strong></div></div>
      <div><span className="overview-icon"><Icon name="chat" /></span><div><span>对话检索</span><strong>已启用<small>全部分对话范围</small></strong></div></div>
    </div>

    <section className="paper-panel knowledge-panel">
      <form className="knowledge-toolbar" onSubmit={search}>
        <label className="search-input"><Icon name="search" size={16} /><span className="sr-only">知识检索</span>
          <input value={queryDraft} onChange={event => setQueryDraft(event.target.value)} placeholder="搜索知识标题与正文（与对话检索同通道）" maxLength={300} /></label>
        <button className="button secondary small" type="submit" disabled={loading}>搜索</button>
        {(query || tag) && <button type="button" className="text-button" onClick={() => { setQueryDraft(''); setQuery(''); setTag(''); void load('', '', 1) }}>清除</button>}
      </form>
      {tags.length > 0 && <div className="knowledge-tags" aria-label="按标签筛选">
        {tags.map(item => <button key={item} type="button" className={`knowledge-tag${tag === item ? ' active' : ''}`} aria-pressed={tag === item} onClick={() => pickTag(tag === item ? '' : item)}>{item}</button>)}
      </div>}
      {loading ? <div className="loading-state"><span className="spinner" />正在读取知识库…</div>
        : entries.length === 0 ? <EmptyState title={query || tag ? '没有匹配的知识条目' : '知识库还是空的'} query={Boolean(query || tag)} onNew={() => void openEditor(null)} />
          : <div className="knowledge-list">{entries.map(entry => <KnowledgeCard key={entry.id} entry={entry} armed={armedDelete === entry.id}
            onEdit={() => void openEditor(entry.id)} onDelete={() => void remove(entry)} onDisarmDelete={() => setArmedDelete(null)} />)}</div>}
      <Pagination info={info} label="知识条目" busy={loading} onPage={page => void load(query, tag, page)} />
    </section>

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
          <textarea id="knowledge-content" value={editor.content} onChange={event => setEditor({ ...editor, content: event.target.value })} required rows={9} maxLength={50000} spellCheck={false}
            placeholder={'记录事实与结论，保留具体细节。例如：\n· 二面重点是 RAG 评测与成本控制\n· 团队 12 人，技术栈以 Python 为主'} />
          <div className="editor-footer"><span>{editor.content.length.toLocaleString()} 字符</span></div>
          <div className="modal-actions"><button type="button" className="button secondary" onClick={() => setEditor(null)} disabled={Boolean(busy)}>取消</button>
            <button className="button primary" type="submit" disabled={Boolean(busy)}>{busy ? <span className="spinner" /> : null}{editor.id ? '保存修改' : '保存知识'}</button></div>
        </form>
      </section>
    </div>}
  </div>
}

function KnowledgeCard({ entry, armed, onEdit, onDelete, onDisarmDelete }: {
  entry: KnowledgeEntry; armed: boolean; onEdit: () => void; onDelete: () => void; onDisarmDelete: () => void
}) {
  return <article className="knowledge-card">
    <div className="knowledge-card-head">
      <h3>{entry.title}</h3>
      <div className="knowledge-card-actions">
        <button className="text-button" onClick={onEdit}><Icon name="settings" size={14} />编辑</button>
        <button className={`text-button${armed ? ' danger-text' : ''}`} onClick={armed ? onDelete : onDisarmDelete}
          onBlur={() => armed && onDisarmDelete()}>{armed ? '再点一次确认删除' : '删除'}</button>
      </div>
    </div>
    <p className="knowledge-card-body">{entry.content}{entry.content_truncated && '…'}</p>
    <div className="knowledge-card-meta">
      {entry.tags.map(item => <span key={item} className="knowledge-tag">{item}</span>)}
      <span className="muted body-small">更新于 {date(entry.updated_at)}</span>
    </div>
  </article>
}

function EmptyState({ title, query, onNew }: { title: string; query: boolean; onNew: () => void }) {
  return <div className="empty"><div className="empty-icon"><Icon name="knowledge" /></div><h3>{title}</h3>
    <p>{query ? <>换个关键词试试，或<button className="inline-button" onClick={onNew}>新建一条</button>知识。</>
      : <>把你面试后的复盘、对行业与公司的判断写在这里，对话时 Agent 会检索引用；<button className="inline-button" onClick={onNew}>录入第一条</button>只需要标题和正文。</>}</p></div>
}
