import { useEffect, useRef, useState } from 'react'
import { api, download, post, upload, type Skill } from './api'

type Act = (key: string, action: () => Promise<void>, message: string) => Promise<boolean>
const SCRIPT_SUFFIX = ['.py', '.sh', '.bash', '.js', '.mjs', '.cjs', '.ts', '.tsx', '.ps1', '.bat', '.cmd', '.exe', '.dll', '.bin']

export function SkillManager({ skills, busy, act, prefill, onPrefillConsumed }: { skills: Skill[]; busy: string; act: Act; prefill: string; onPrefillConsumed: () => void }) {
  const [selected, setSelected] = useState<string | null>(skills[0]?.id ?? null)
  const [search, setSearch] = useState('')
  const [showImport, setShowImport] = useState(false)
  const [content, setContent] = useState('')
  const [importError, setImportError] = useState('')
  const [preview, setPreview] = useState<Record<string, string>>({})
  const zipInput = useRef<HTMLInputElement>(null)
  const folderInput = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (!prefill) return
    setShowImport(true); setContent(prefill); setSelected(null)
    onPrefillConsumed()
  }, [prefill]) // eslint-disable-line react-hooks/exhaustive-deps
  const skill = skills.find(item => item.id === selected) || null
  const filtered = skills.filter(item => `${item.name} ${item.description} ${item.category || ''}`.toLowerCase().includes(search.toLowerCase()))
  async function importPaste() {
    const ok = await act('skill', async () => { await post('/agent/skills', { content }) }, 'Skill 已导入并启用。同名导入会覆盖旧版本。')
    if (ok) { setContent(''); setShowImport(false); setImportError('') }
  }
  async function importArchive(file: File) {
    const ok = await act('skill-zip', async () => { await upload(`/agent/skills/archive?filename=${encodeURIComponent(file.name)}`, file) }, 'zip 包已导入，文本资源随 Skill 保存。')
    if (ok) setShowImport(false)
  }
  async function importFolder(files: FileList) {
    const rows: { path: string; text: string | null }[] = []
    for (const file of Array.from(files)) {
      const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath || file.name
      const path = relative.split('/').slice(1).join('/') || relative
      const lower = file.name.toLowerCase()
      const isScript = SCRIPT_SUFFIX.some(suffix => lower.endsWith(suffix))
      let text: string | null = null
      if (!isScript && file.size > 0 && file.size <= 400000) {
        try {
          const decoded = await file.text()
          text = decoded.includes('\uFFFD') ? null : decoded // 解码出现替换符视为二进制
        } catch { text = null }
      }
      rows.push({ path, text })
    }
    const ok = await act('skill-folder', async () => { await post('/agent/skills/files', { files: rows }) }, '文件夹已导入，文本资源随 Skill 保存。')
    if (ok) setShowImport(false)
  }
  async function loadPreview(item: Skill, path: string) {
    if (preview[path] !== undefined) { setPreview(current => { const next = { ...current }; delete next[path]; return next }); return }
    try {
      const result = await post<{ path: string; text: string }>(`/agent/skills/${item.id}/resource`, { path })
      setPreview(current => ({ ...current, [path]: result.text }))
    } catch (err) { setImportError((err as Error).message) }
  }
  return <div className="ext-shell">
    <aside className="ext-list" aria-label="Skill 列表">
      <input type="search" aria-label="搜索 Skills" placeholder="搜索 Skills" value={search} onChange={event => setSearch(event.target.value)} />
      <div className="ext-list-items">{filtered.map(item => <button key={item.id} type="button" className={`ext-item ${selected === item.id ? 'active' : ''}`} onClick={() => { setSelected(item.id); setImportError('') }}>
        <span className={`ext-dot ${item.enabled ? 'ready' : ''}`} aria-hidden="true" />
        <span className="ext-item-body"><strong>{item.icon ? item.icon + ' ' : ''}{item.name}</strong><small>{item.category || '未分类'} · {item.resources.length} 资源 · {item.scripts.length} 脚本</small></span>
      </button>)}{!skills.length && <div className="extensions-empty"><span className="extensions-empty-mark" aria-hidden="true">≡</span><h3>把常用方法交给 Agent</h3><p>从市场安装精选 Skills，或导入你自己的 SKILL.md</p></div>}{Boolean(skills.length) && !filtered.length && <p className="muted">没有匹配的 Skill</p>}</div>
      <button type="button" className="button secondary small" onClick={() => { setShowImport(value => !value); setSelected(null) }}>导入 Skill</button>
    </aside>
    <section className="ext-detail" aria-label="Skill 详情">
      {showImport ? <>
        <div className="ext-detail-head"><h2>导入 Skill</h2><button type="button" className="inline-button" onClick={() => { setShowImport(false); setContent(''); setImportError('') }}>返回列表</button></div>
        <p className="field-help">三种方式：粘贴 SKILL.md 文本、上传 .zip 压缩包、或选择本地 Skill 文件夹。SKILL.md 须含 name 和 description；文本资源（references、示例等）会一并保存供 Agent 按需读取；脚本只登记路径，不会执行。</p>
        <div className="agent-skill-form"><label htmlFor="agent-skill-content">SKILL.md 内容</label><textarea id="agent-skill-content" rows={9} value={content} maxLength={60000} onChange={event => setContent(event.target.value)} placeholder={'---\nname: interview-coach\ndescription: 面试演练与反馈\ncategory: 面试\n---\n每次只问一个问题，按 STAR 结构反馈。'} />
          <div className="agent-form-actions"><input ref={zipInput} type="file" accept=".zip" className="sr-only" aria-label="Skill zip 压缩包" onChange={event => { const file = event.target.files?.[0]; event.target.value = ''; if (file) void importArchive(file) }} /><input ref={folderInput} type="file" multiple className="sr-only" aria-label="Skill 文件夹" onChange={event => { const files = event.target.files; event.target.value = ''; if (files?.length) void importFolder(files) }} />
            <button type="button" className="button secondary" onClick={() => zipInput.current?.click()}>上传 .zip</button>
            <button type="button" className="button secondary" onClick={() => folderInput.current?.click()}>选择文件夹</button>
            <button className="button primary" type="button" disabled={!content.trim() || Boolean(busy)} onClick={() => void importPaste()}>{busy === 'skill' ? '导入中…' : '粘贴导入'}</button></div></div>
        {importError && <p role="alert" className="mcp-import-error">{importError}</p>}
      </> : skill ? <>
        <div className="ext-detail-head"><h2>{skill.icon ? skill.icon + ' ' : ''}{skill.name}<span className={`extension-state ${skill.enabled ? 'ready' : ''}`}>{skill.enabled ? '已启用' : '已停用'}</span></h2><code className="market-endpoint">{skill.category || '未分类'}{skill.author ? ` · ${skill.author}` : ''}</code></div>
        <p className="skill-description">{skill.description}</p>
        {Boolean(skill.keywords?.length) && <div className="ext-detail-meta">{skill.keywords?.map(keyword => <span key={keyword} className="market-badge">{keyword}</span>)}</div>}
        <div className="ext-detail-actions">
          <label className="agent-checkbox"><input type="checkbox" checked={skill.enabled} disabled={Boolean(busy)} onChange={event => void act(skill.id, async () => { await api(`/agent/skills/${skill.id}`, { method: 'PATCH', body: JSON.stringify({ enabled: event.target.checked }) }) }, 'Skill 状态已更新。')} />启用</label>
          <button type="button" className="button secondary small" disabled={Boolean(busy)} onClick={() => void download(`/agent/skills/${skill.id}/export`, `${skill.name}.zip`).catch((err: unknown) => setImportError((err as Error).message))}>导出 .zip</button>
          <button type="button" className="inline-button" disabled={Boolean(busy)} onClick={() => void act(skill.id, async () => { setSelected(null); await api(`/agent/skills/${skill.id}`, { method: 'DELETE' }) }, 'Skill 已移除。')}>移除</button>
        </div>
        <h3 className="ext-section-title">文本资源 · {skill.resources.length}（Agent 可按清单读取）</h3>
        <div className="skill-resources">{skill.resources.map(resource => <div key={resource.path} className="skill-resource">
          <button type="button" className="text-button" aria-expanded={preview[resource.path] !== undefined} onClick={() => void loadPreview(skill, resource.path)}>{preview[resource.path] !== undefined ? '收起' : '查看'} · {resource.path} <span className="muted">({resource.characters} 字符)</span></button>
          {preview[resource.path] !== undefined && <pre className="skill-resource-text">{preview[resource.path]}</pre>}
        </div>)}{!skill.resources.length && <p className="muted">没有文本资源</p>}</div>
        <h3 className="ext-section-title">脚本文件 · {skill.scripts.length}（仅登记路径，不执行）</h3>
        {Boolean(skill.scripts.length) ? <ul className="skill-scripts">{skill.scripts.map(path => <li key={path}><code>{path}</code></li>)}</ul> : <p className="muted">没有脚本文件</p>}
        <p className="field-help">同名重新导入会覆盖此 Skill；导出的 zip 不含脚本内容，可在其他宿主粘贴 SKILL.md 使用。</p>
        {importError && <p role="alert" className="mcp-import-error">{importError}</p>}
      </> : <div className="extensions-empty"><span className="extensions-empty-mark" aria-hidden="true">≡</span><h3>选择左侧 Skill 查看详情</h3><p>启用、查看资源、导出与移除都在这里完成</p></div>}
    </section>
  </div>
}
