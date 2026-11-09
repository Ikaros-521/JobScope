export type Job = {
  id: string; title: string; company: string; location: string; salary: string;
  url: string; source: string; updated_at: string; description: string; evidence_ids: string[]; tags: string[]
  description_status?: string; detail_collected_at?: string; detail_source_url?: string
}
export type Evidence = { id: string; title: string; url: string; source: string; kind: string; company: string; text: string; collected_at: string; published_at: string | null; content_hash: string }
export type Source = { id: string; name: string; transport_mode: string; status: string; description: string }
export type Task = { id: string; source: string; query: string; status: string; message: string; created_at: string; count: number; kind?: string; total?: number; processed?: number; failed?: number }
export type PageInfo = { page: number; page_size: number; total: number; pages: number }
export type ResearchRun = { id: string; status: string; strategy: string; duration_ms: number; context_characters: number; limits?: { context_characters: number }; scope: { kind: string; company?: string }; plan?: { query: string; planner: string; concepts: string[]; companies: string[] }; degraded: string[]; events: { step: number; tool: string; duration_ms: number; selected?: number; matched_jobs?: number; label?: string; status?: string }[] }
export type SalaryMetrics = { matched_jobs: number; known_monthly_jobs: number; unknown_monthly_jobs: number; threshold: number | null; threshold_basis: string; location: string; skills?: string[] }
export type ChatResult = { answer: string; mode: string; citations: Citation[]; conversation_id: string; trace?: ResearchRun; metrics?: SalaryMetrics | null; pending_approval?: { approval_id: string; tool: string; arguments: Record<string, unknown>; reason: string } | null }
export type Citation = { id: string; title: string; url: string; excerpt: string; company?: string; source?: string; locators?: { start: number; end: number; page: string }[] }
export type ChatMessage = { role: 'user' | 'assistant'; content: string; mode?: string; citations?: Citation[]; trace?: ResearchRun; metrics?: SalaryMetrics | null }
export type Analysis = {
  job_id: string; resume_revision: number; score: number | null; coverage: number; method: string;
  requirements: { requirement: string; status: string; evidence: unknown; dimension: string; rationale?: string; jd_quote?: string; hard?: boolean }[];
  strengths: string[]; gaps: string[]; questions: string[]; salary: unknown; company_summary: unknown; evidence_ids: string[]; limitations?: string | string[]
  hard_conditions?: unknown[]; dimensions?: Record<string, unknown>
}
export type BrowserStatus = { installed: boolean; running: boolean; message: string }
export type Transport = 'http' | 'sse' | 'stdio'
export type Tool = { name: string; description: string; annotations?: { readOnlyHint?: boolean } }
export type Server = { id: string; name: string; transport: Transport; url: string; command: string; args: string[]; enabled: boolean; tested: boolean; token_set: boolean; env_keys: string[]; headers_set: string[]; tools: Tool[]; allowed_tools: string[]; auto_tools: string[] }
export type SkillResource = { path: string; characters: number }
export type Skill = { id: string; name: string; description: string; enabled: boolean; category?: string; author?: string; keywords?: string[]; icon?: string; resources: SkillResource[]; scripts: string[] }
export type Extensions = { servers: Server[]; skills: Skill[] }
export type MarketMcp = { id: string; name: string; icon: string; category: string; desc: string; official?: boolean; transport: Transport; url?: string; command?: string; args?: string[]; repo?: string; needs_key?: string; needs_config?: boolean; installed?: boolean }
export type MarketSkill = { id: string; name: string; title?: string; icon: string; category: string; desc?: string; content?: string; installed?: boolean; repo?: string; path?: string; official?: boolean; stars?: number }
export type MarketPayload = { curated_mcp: MarketMcp[]; curated_skills: MarketSkill[]; online: { skills: MarketSkill[]; mcp: MarketMcp[]; fetched_at?: number } | null; fetched_at: number | null }

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, { ...options, headers: { 'Content-Type': 'application/json', ...options?.headers } })
  return readResponse<T>(response)
}
export async function download(path: string, filename: string): Promise<void> {
  const response = await fetch(`/api${path}`)
  if (!response.ok) throw new Error('导出失败，请稍后重试。')
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a')
  link.href = url; link.download = filename; link.click()
  URL.revokeObjectURL(url)
}
async function readResponse<T>(response: Response): Promise<T> {
  const text = await response.text()
  let data: Record<string, unknown>
  try { data = JSON.parse(text) } catch { throw new Error(response.ok ? '服务返回了无法读取的数据。' : '无法连接后端服务，请检查服务是否已启动。') }
  if (!response.ok) {
    const detail = data.detail
    const message = typeof detail === 'string' ? detail : Array.isArray(detail) ? detail.map((item: { msg?: string }) => item.msg).join('；') : `请求失败（${response.status}）`
    throw new Error(message)
  }
  return data as T
}
export async function upload<T>(path: string, file: File): Promise<T> {
  return readResponse<T>(await fetch(`/api${path}`, { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream' } }))
}
export const post = <T,>(path: string, body: unknown = {}) => api<T>(path, { method: 'POST', body: JSON.stringify(body) })
export function readable(value: unknown): string {
  if (value == null) return '暂无资料'
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.map(readable).join('；')
  if (typeof value === 'object') return Object.entries(value).map(([key, val]) => `${key}：${readable(val)}`).join('\n')
  return String(value)
}
export function plain(text: string) { return new DOMParser().parseFromString(text || '', 'text/html').body.textContent || '' }
export function date(value: string | null) {
  if (!value) return '时间未知'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })
}
export function safeUrl(url: string) { try { const parsed = new URL(url); return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : undefined } catch { return undefined } }
