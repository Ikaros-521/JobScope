export type Job = {
  id: string; title: string; company: string; location: string; salary: string;
  url: string; source: string; updated_at: string; description: string; evidence_ids: string[]; tags: string[]
  description_status?: string; detail_collected_at?: string; detail_source_url?: string
}
export type Evidence = { id: string; title: string; url: string; source: string; kind: string; company: string; text: string; collected_at: string; published_at: string | null; content_hash: string }
export type Source = { id: string; name: string; kind: string; transport_mode: string; status: string; description: string; cities: string[]; browser_collectable?: boolean }
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

// 全局对话：run-based 流式会话
export type ChatMode = 'research' | 'general'
export type StoredMessage = {
  id: string; ts: string; role: 'user' | 'assistant'; content: string;
  reasoning?: string; mode?: string; interrupted?: boolean;
  citations?: Citation[]; trace?: ResearchRun; metrics?: SalaryMetrics | null
}
export type ToolStep = { step: number; tool: string; duration_ms: number; label?: string; status?: string; arguments?: Record<string, unknown>; result_preview?: string; selected?: number; matched_jobs?: number; calls?: number }
export type ApprovalRequest = { approval_id: string; tool: string; arguments: Record<string, unknown>; reason: string; run_id?: string }
export type ChatRunStatus = { run_id: string; conversation_id: string; done: boolean; cancelled: boolean; awaiting_approval: boolean; pending_approval: ApprovalRequest | null }
export type ChatScope = { kind: 'global' } | { kind: 'company'; company: string } | { kind: 'job'; jobId: string; company: string; title?: string }
export type ConversationSummary = { id: string; title: string; mode: ChatMode; model: string; updated_at: string; message_count: number; preview: string }
export type ConversationDetail = { id: string; scope: { kind: string; company?: string; job_id?: string }; title: string; mode: ChatMode; model: string; created_at: string; updated_at: string; messages: StoredMessage[]; active_run: ChatRunStatus | null }
export type ChatRunStart = { run_id: string; conversation_id: string; user_message_id: string; title: string | null }
export type ModelProvider = { mode: string; channel: string; protocol: 'openai' | 'anthropic'; url: string; model: string; local_model: string; key_set: boolean; source: string }
export type Channel = { id: string; name: string; url: string; protocol: 'openai' | 'anthropic'; models: string[]; embeddings: string[] }
export type RunEvent = { event: string; data: Record<string, unknown> }

export function listConversations(scope: ChatScope = { kind: 'global' }) {
  const params = new URLSearchParams({ scope: scope.kind })
  if (scope.kind === 'company') params.set('company', scope.company)
  if (scope.kind === 'job') params.set('job_id', scope.jobId)
  return api<{ conversations: ConversationSummary[] }>(`/conversations?${params}`)
}
export function getConversation(id: string) { return api<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`) }
export function patchConversation(id: string, body: { title?: string; mode?: ChatMode; model?: string }) {
  return api<ConversationSummary>(`/conversations/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) })
}
export function deleteConversation(id: string) { return api<{ ok: boolean }>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }) }
export function deleteConversationMessage(conversationId: string, messageId: string) {
  return api<{ ok: boolean }>(`/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}`, { method: 'DELETE' })
}
export function startChatRun(body: { message: string; conversation_id?: string; agent_mode?: ChatMode; model?: string; edit_message_id?: string; company?: string; job_id?: string }) {
  return post<ChatRunStart>('/chat/runs', body)
}
export function cancelChatRun(runId: string) { return post<ChatRunStatus>(`/chat/runs/${encodeURIComponent(runId)}/cancel`) }
export function approveChatRun(runId: string, approved: boolean, approvalId: string) {
  return post<ChatRunStatus>(`/chat/runs/${encodeURIComponent(runId)}/approval`, { approved, approval_id: approvalId })
}
export function getModelSettings() { return api<Record<'llm' | 'embedding', ModelProvider>>('/settings/models') }
export function getChannels() { return api<{ channels: Channel[] }>('/settings/channels') }

const STREAM_EVENTS = ['start', 'resumed', 'step', 'delta', 'reasoning_delta', 'approval_required', 'done', 'error', 'cancelled', 'end'] as const
export function openRunStream(runId: string, onEvent: (event: string, data: Record<string, unknown>) => void): EventSource {
  const source = new EventSource(`/api/chat/runs/${encodeURIComponent(runId)}/stream`)
  for (const name of STREAM_EVENTS) {
    source.addEventListener(name, event => {
      try { onEvent(name, JSON.parse((event as MessageEvent).data)) } catch { /* 跳过无法解析的帧 */ }
    })
  }
  return source
}

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
