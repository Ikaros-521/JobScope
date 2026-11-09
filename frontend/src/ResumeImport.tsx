import { useRef, useState } from 'react'
import { upload } from './api'

type Extracted = { filename: string; text: string; format: string; characters: number; pages: number | null; warnings: string[] }
const extensions = ['pdf', 'docx', 'txt', 'md', 'yaml', 'yml']

export function ResumeImport({ disabled, hasText, onApply }: { disabled: boolean; hasText: boolean; onApply: (text: string, format: string) => void }) {
  const input = useRef<HTMLInputElement>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [result, setResult] = useState<Extracted | null>(null)
  const [dragging, setDragging] = useState(false)
  async function extract(files: File[]) {
    if (busy || disabled) return
    setError(''); setResult(null)
    if (files.length !== 1) { setError('请一次导入一份简历文件。'); return }
    const file = files[0]
    const extension = file.name.split('.').pop()?.toLowerCase() || ''
    if (extension === 'doc') { setError('旧版 .doc 当前不能直接解析，请在 Word / WPS 另存为 .docx 后导入。'); return }
    if (!extensions.includes(extension)) { setError('不支持此格式，请选择 PDF、Word .docx、TXT、Markdown 或 facts.yaml。'); return }
    if (file.size > 10 * 1024 * 1024) { setError('文件超过 10 MB，请压缩或缩减后导入。'); return }
    setBusy(true)
    try { setResult(await upload<Extracted>(`/resume/import?filename=${encodeURIComponent(file.name)}`, file)) }
    catch (err) { setError((err as Error).message) }
    finally { setBusy(false); if (input.current) input.current.value = '' }
  }
  return <section className="resume-import" aria-label="导入简历文件" aria-busy={busy}>
    <div className={`resume-dropzone ${dragging ? 'dragging' : ''}`} onDragOver={event => { event.preventDefault(); if (!disabled && !busy) setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={event => { event.preventDefault(); setDragging(false); void extract(Array.from(event.dataTransfer.files)) }}>
      <div><h3>从文件开始</h3><p>拖入 PDF 或 Word .docx，自动提取文字</p><span>也支持 TXT、Markdown、facts.yaml · 单文件不超过 10 MB</span></div>
      <button className="button secondary" type="button" disabled={disabled || busy} onClick={() => input.current?.click()}>{busy && <span className="spinner" />}{busy ? '正在提取文字…' : '选择简历文件'}</button>
      <input ref={input} className="sr-only" type="file" tabIndex={-1} accept=".pdf,.docx,.txt,.md,.yaml,.yml" aria-label="简历文件" disabled={disabled || busy} onChange={event => { const files = Array.from(event.target.files || []); event.target.value = ''; if (files.length) void extract(files) }} />
    </div>
    {error && <p className="resume-import-error" role="alert">{error}</p>}
    {result && <div className="resume-import-preview"><div className="resume-import-meta"><strong>{result.filename}</strong><span>{result.characters.toLocaleString()} 字符{result.pages != null ? ` · ${result.pages} 页` : ''} · 提取完成，待确认</span></div>
      <label className="field-label" htmlFor="resume-import-preview">提取文本预览</label><textarea id="resume-import-preview" readOnly value={result.text} rows={6} spellCheck={false} />
      {result.warnings.map(warning => <p className="field-help" key={warning}>{warning}</p>)}
      <div className="resume-import-actions"><p>{hasText ? '应用后将替换编辑区内容；已保存版本会保留到你再次保存。' : '应用到编辑区后，可修改内容，再点击“保存到本地”。'}</p><button className="button secondary" type="button" disabled={disabled} onClick={() => setResult(null)}>取消导入</button><button className="button primary" type="button" disabled={disabled} onClick={() => { onApply(result.text, result.format); setResult(null) }}>{hasText ? '替换编辑区内容' : '应用到编辑区'}</button></div>
    </div>}
  </section>
}
