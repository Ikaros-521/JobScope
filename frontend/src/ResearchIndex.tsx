import { useEffect, useState } from 'react'
import { api, post } from './api'
import { toast } from './toast'

type IndexStatus = { documents: number; chunks: number; jobs: number; cached_vectors: number; embedding_configured: boolean; embedding_local?: boolean; model_configured: boolean; version: string }
export function ResearchIndex() {
  const [status, setStatus] = useState<IndexStatus | null>(null)
  const [busy, setBusy] = useState('')
  async function load() { setStatus(await api<IndexStatus>('/research/index')) }
  useEffect(() => { void load().catch(err => toast((err as Error).message, 'error')) }, [])
  async function update(operation: string) {
    setBusy(operation)
    try { const result = await post<{ embedded?: number }>('/research/index', { operation }); await load(); toast(operation === 'embed' ? `已新增${result.embedded || 0}个语义向量，未变化内容复用缓存` : '检索索引已更新') }
    catch (err) { toast((err as Error).message, 'error') } finally { setBusy('') }
  }
  return <section className="paper-panel research-index"><div className="section-heading"><div><h2>研究检索引擎</h2><p className="body-small muted">增量分块索引、当前岗位版本、结构化薪资与可选语义向量</p></div><button className="button secondary" disabled={Boolean(busy)} onClick={() => void update('sync')}>同步索引</button></div>
    <div className="index-statistics"><span><b>{status?.documents ?? '—'}</b> 当前来源</span><span><b>{status?.chunks ?? '—'}</b> 原文分块</span><span><b>{status?.jobs ?? '—'}</b> 岗位事实</span><span><b>{status?.cached_vectors ?? '—'}</b> 缓存向量</span></div>
    <p className="body-small">全文检索：SQLite FTS5 / BM25 · 语义召回：{status?.embedding_configured ? '已配置，需构建向量' : '未配置，当前使用BM25及领域概念扩展'} · 问题计划：{status?.model_configured ? '模型辅助' : '领域规则'}</p>
    <div className="index-actions"><button className="button secondary small" disabled={Boolean(busy)} onClick={() => void update('rebuild')}>重建全文索引</button><button className="button secondary small" disabled={Boolean(busy) || !status?.embedding_configured} onClick={() => void update('embed')}>构建语义向量</button>{busy && <span role="status"><span className="spinner" />正在{busy === 'embed' ? '分批生成向量，可复用已保存进度' : '更新索引'}…</span>}</div>
    <p className="tiny-label">历史证据保留，检索优先使用当前岗位版本。{status?.embedding_local ? '当前使用本机已缓存模型生成向量，正文不发送至外部Embedding服务。' : '配置远端Embedding时，构建向量会把索引正文发送至你配置的服务。'}向量服务未配置或不可用时明确降级。</p>
  </section>
}
