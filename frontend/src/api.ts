export type Job = {
  id: string; title: string; company: string; location: string; salary: string;
  url: string; source: string; updated_at: string; description: string; evidence_ids: string[]; tags: string[]
  description_status?: string; detail_collected_at?: string; detail_source_url?: string
}
export type Evidence = { id: string; title: string; url: string; source: string; kind: string; company: string; text: string; collected_at: string; published_at: string | null; content_hash: string }
export type Source = { id: string; name: string; kind: string; transport_mode: string; status: string; description: string; cities: string[]; hot: string[]; browser_collectable?: boolean }
export type Task = { id: string; source: string; query: string; status: string; message: string; created_at: string; count: number; kind?: string; total?: number; processed?: number; failed?: number }
export type PageInfo = { page: number; page_size: number; total: number; pages: number }
export type ResearchRun = { id: string; status: string; strategy: string; duration_ms: number; context_characters: number; limits?: { context_characters: number }; scope: { kind: string; company?: string }; plan?: { query: string; planner: string; concepts: string[]; companies: string[] }; degraded: string[]; events: ToolStep[] }
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
// 会话级工具权限档位：confirm=外部工具逐次确认；autonomous=只读/自动执行白名单直跑；full=全部自动执行
export type PermissionMode = 'confirm' | 'autonomous' | 'full'
export type StoredMessage = {
  id: string; ts: string; role: 'user' | 'assistant'; content: string;
  reasoning?: string; mode?: string; interrupted?: boolean;
  // kind='compaction' 是压缩检查点；compacted 表示该消息已被压缩折叠（仅 UI 呈现，不进模型上下文）
  kind?: string; compacted_count?: number; compacted?: boolean;
  citations?: Citation[]; trace?: ResearchRun; metrics?: SalaryMetrics | null; actions?: ScopeExpansion[]
  // 顺序显示模式的分段日志：思维链/正文/工具步骤按发生顺序穿插（旧消息没有，回落合并显示）
  timeline?: ChatSegment[]
}
export type ConversationScope = { kind: 'global' } | { kind: 'company'; company: string } | { kind: 'job'; company: string; job_id: string }
export type ScopeExpansion = { id: string; type: 'scope_expansion'; source_scope: ConversationScope; target_scope: ConversationScope; reason: string; target_conversation_id: string | null }
export type ConversationNavigation = { requestId: string; conversationId: string; scope: ConversationScope }
export type ScopeExpansionResult = { conversation_id: string; scope: ConversationScope; source_conversation_id: string; action_id: string }
export type ToolStep = { step: number; tool: string; duration_ms: number; label?: string; status?: string; arguments?: Record<string, unknown>; result_preview?: string; selected?: number; matched_jobs?: number; calls?: number; action?: ScopeExpansion }
// 顺序显示模式的分段：思维链/正文按段追加，工具步骤穿插其间，与发生顺序一致。
export type ChatSegment = { kind: 'reasoning' | 'text'; text: string } | { kind: 'step'; step: ToolStep }
// 系统设置：存服务端（跨浏览器一致），含聊天显示模式、思维链显隐与 Agent 执行限额。
export type AgentLimits = { max_model_calls: number; max_tool_calls: number; context_characters: number; max_seconds: number }
export type SystemSettings = { chat_display_mode: 'merged' | 'sequential'; show_reasoning: boolean; agent_limits: AgentLimits }
export type ApprovalRequest = { approval_id: string; tool: string; arguments: Record<string, unknown>; reason: string; run_id?: string }
export type ChatRunStatus = { run_id: string; conversation_id: string; done: boolean; cancelled: boolean; awaiting_approval: boolean; pending_approval: ApprovalRequest | null }
export type ChatScope = { kind: 'global' } | { kind: 'company'; company: string } | { kind: 'job'; jobId: string; company: string; title?: string }
export type ConversationSummary = { id: string; title: string; mode: ChatMode; model: string; llm_channel: string; permission_mode: PermissionMode; updated_at: string; message_count: number; preview: string }
export type ConversationDetail = { id: string; scope: { kind: string; company?: string; job_id?: string }; title: string; mode: ChatMode; model: string; llm_channel: string; permission_mode: PermissionMode; created_at: string; updated_at: string; messages: StoredMessage[]; active_run: ChatRunStatus | null; scope_expansions?: ScopeExpansion[]; lineage?: { source_conversation_id: string; action_id: string } | null }
export type ChatRunStart = { run_id: string; conversation_id: string; user_message_id: string; title: string | null }
export type ModelProvider = { mode: string; channel: string; protocol: 'openai' | 'anthropic'; url: string; model: string; local_model: string; key_set: boolean; source: string; context_tokens?: number | null; saved_channel_id?: string }
export type Channel = { id: string; name: string; url: string; protocol: 'openai' | 'anthropic'; models: string[]; embeddings: string[] }
// 已保存的对话渠道：每条自带地址/密钥/默认模型/模型列表，可激活为全局默认
export type LlmChannel = { id: string; name: string; channel: string; protocol: 'openai' | 'anthropic'; url: string; model: string; models: string[]; embeddings?: string[]; context_tokens: number | null; key_set: boolean }
export type LlmChannelDraft = { id?: string; name: string; channel: string; protocol: 'openai' | 'anthropic'; url: string; model: string; models?: string[]; embeddings?: string[]; key?: string; clear_key?: boolean; context_tokens?: number | null }
// 已保存渠道的测试/拉列表：带上 channel_id，后端才能用该渠道已保存的密钥而不是全局配置。
export type ModelConnectionInput = { target: 'llm' | 'embedding'; channel_id?: string; config: { mode: 'disabled' | 'remote' | 'local'; channel: string; protocol: 'openai' | 'anthropic'; url: string; model: string; local_model?: string; key?: string; clear_key?: boolean; context_tokens?: number | null } }
export type RunEvent = { event: string; data: Record<string, unknown> }

// 求职参考分析：字段口径以后端 reliability/models.py 与 main.py 的端点返回为准。
export type ReliabilityFinding = {
  text: string; rule: string; kind: 'assessment' | 'material';
  // material 类的 score 恒为 null：那个数字量的是本项目存了几条材料，不是对公司的评价。
  score: number | null; source_class: string; evidence_id: string;
  // 信号本身的采集时刻；过期判定发生在重算时，过期依据会落进 missing 不进 findings。
  collected_at?: string;
  // 句子里那组原始数字（旧报告没有这个字段）：事实小图只画这里声明过的数字，不从文字反解析。
  data?: Record<string, number>
}
export type ReliabilityDimension = { dimension: string; label: string; score: number | null; confidence: number; findings: ReliabilityFinding[]; evidence_ids: string[]; missing: string[] }
export type ReliabilityReport = {
  company_id: string; company: string; nature: 'subjective_reference'; score: number | null;
  // 顶层 confidence 是「按权重算出的维度覆盖度」，不是单维度的依据充分度，别和维度里那个混称。
  confidence: number; summary: string; disclaimer: string; dimensions: ReliabilityDimension[];
  rubric_version: string; wording_version: string; disclaimer_version: string; computed_at: string;
  input_hash: string; evidence_ids: string[]; limitations: string[];
  // 重新计算时自动刷新的本地统计（不联网）；来源被用户关闭时为 null。
  collection?: { source: string; status: string; message: string; count: number } | null
}
// 单公司历史趋势：只对比这家公司自己的相邻两次快照，没有任何跨公司对照。
export type ReliabilitySnapshot = {
  computed_at: string; input_hash: string; score: number | null; confidence: number;
  dimensions: Record<string, number | null>; rules: Record<string, number | null>
}
export type ReliabilityDiff = {
  before: string; after: string; score_before: number | null; score: number | null;
  rules: { rule: string; before: number | null; after: number | null }[];
  // material 的 score 恒为 null，它的「变化」只能体现为登记出现/消失，不带任何数字。
  materials: { rule: string; appeared: boolean }[]
}
export type ReliabilityHistory = { company: string; company_id: string; items: ReliabilitySnapshot[]; diffs: ReliabilityDiff[] }
// 采集类端点的 status 词表（collectors/local）；抽取端点另有 skipped、degraded，所以这里不收成闭集。
export type SignalActionResult = { status: string; message: string }
export type SignalCollectResult = SignalActionResult & { signals: unknown[] }
export type SignalExtractResult = SignalActionResult & { accepted: number; signals: unknown[]; evidence: number; task: Task }
export type SignalSource = { name: string; tier: 'free' | 'paid'; transport: string; status: string; rules: string[]; description: string; mode: 'disabled' | 'enabled'; url?: string; updated_at?: string; key_set?: boolean }

export function getReliability(company: string) { return api<{ report: ReliabilityReport | null }>(`/companies/${encodeURIComponent(company)}/reliability`) }
// 横评候选：实体表全量（含未算过的公司），分数照实 null——没算过就是没算过；按名称排序，不带名次语义。
export type CompareCompany = { company: string; credit_code: string; score: number | null; confidence: number | null; computed_at: string | null }
export function getCompareCompanies(q = '') { return api<{ companies: CompareCompany[] }>(`/reliability/compare?q=${encodeURIComponent(q)}`) }
export function getReliabilityHistory(company: string) { return api<{ history: ReliabilityHistory | null }>(`/companies/${encodeURIComponent(company)}/reliability/history`) }
export function computeReliability(company: string) { return post<ReliabilityReport>(`/companies/${encodeURIComponent(company)}/reliability`, { credit_code: '' }) }
export function collectCompanySignals(company: string, source: string) { return post<SignalCollectResult>(`/companies/${encodeURIComponent(company)}/signals/collect`, { source }) }
export function extractCompanySignals(company: string, limit = 12) { return post<SignalExtractResult>(`/companies/${encodeURIComponent(company)}/signals/extract`, { limit }) }
export function listSignalSources() { return api<{ sources: Record<string, SignalSource> }>('/reliability/sources') }
export function saveSignalSource(id: string, body: { mode: 'disabled' | 'enabled'; key?: string; url?: string }) {
  return api<SignalSource>(`/reliability/sources/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(body) })
}

export function listConversations(scope: ChatScope = { kind: 'global' }) {
  const params = new URLSearchParams({ scope: scope.kind })
  if (scope.kind === 'company') params.set('company', scope.company)
  if (scope.kind === 'job') params.set('job_id', scope.jobId)
  return api<{ conversations: ConversationSummary[] }>(`/conversations?${params}`)
}
export function getConversation(id: string) { return api<ConversationDetail>(`/conversations/${encodeURIComponent(id)}`) }
export function proposeScopeExpansion(id: string, target: 'company' | 'global') {
  return post<ScopeExpansion>(`/conversations/${encodeURIComponent(id)}/scope-expansions`, { target })
}
export function acceptScopeExpansion(id: string, actionId: string) {
  return post<ScopeExpansionResult>(`/conversations/${encodeURIComponent(id)}/scope-expansions/${encodeURIComponent(actionId)}/accept`)
}
export function patchConversation(id: string, body: { title?: string; mode?: ChatMode; model?: string; llm_channel?: string; permission_mode?: PermissionMode }) {
  return api<ConversationSummary>(`/conversations/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) })
}
export function deleteConversation(id: string) { return api<{ ok: boolean }>(`/conversations/${encodeURIComponent(id)}`, { method: 'DELETE' }) }
export function deleteConversationMessage(conversationId: string, messageId: string) {
  return api<{ ok: boolean }>(`/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}`, { method: 'DELETE' })
}
export function startChatRun(body: { message: string; conversation_id?: string; agent_mode?: ChatMode; model?: string; llm_channel?: string; permission_mode?: PermissionMode; edit_message_id?: string; company?: string; job_id?: string }) {
  return post<ChatRunStart>('/chat/runs', body)
}
export function cancelChatRun(runId: string) { return post<ChatRunStatus>(`/chat/runs/${encodeURIComponent(runId)}/cancel`) }
export function approveChatRun(runId: string, approved: boolean, approvalId: string, grant = false) {
  return post<ChatRunStatus>(`/chat/runs/${encodeURIComponent(runId)}/approval`, { approved, approval_id: approvalId, grant })
}
export function getModelSettings() { return api<Record<'llm' | 'embedding', ModelProvider>>('/settings/models') }
export function getSystemSettings() { return api<SystemSettings>('/settings/system') }
export function saveSystemSettings(settings: SystemSettings) {
  return api<SystemSettings>('/settings/system', { method: 'PUT', body: JSON.stringify(settings) })
}
export function getChannels() { return api<{ channels: Channel[] }>('/settings/channels') }
export function listLlmChannels() { return api<{ channels: LlmChannel[] }>('/settings/llm-channels') }
export function saveLlmChannel(draft: LlmChannelDraft) { return post<{ channel: LlmChannel; channels: LlmChannel[] }>('/settings/llm-channels', draft) }
export function deleteLlmChannel(id: string) { return api<{ ok: boolean; channels: LlmChannel[]; bindings: BindingsPayload }>(`/settings/llm-channels/${encodeURIComponent(id)}`, { method: 'DELETE' }) }
export function activateLlmChannel(id: string) { return post<{ settings: Record<'llm' | 'embedding', ModelProvider>; bindings: BindingsPayload }>(`/settings/llm-channels/${encodeURIComponent(id)}/activate`) }
export type CompressResult = { compacted_count: number; summary_message_id: string; message_count: number }
export function compressConversation(id: string) { return post<CompressResult>(`/conversations/${encodeURIComponent(id)}/compress`) }
// 功能级模型绑定：渠道是唯一事实源，绑定是指向渠道+模型的指针。
export type ModelPurpose = 'agent' | 'embedding' | 'extraction' | 'analysis'
export type ModelBinding = { kind: 'channel' | 'local' | 'disabled' | 'inherit'; channel_id?: string; model?: string; local_model?: string }
export type BindingEntry = { binding: ModelBinding | null; inherited: boolean; channel_name?: string; channel_missing?: boolean; resolved: { configured: boolean; mode: string; model: string; context_tokens: number | null } }
export type BindingsPayload = Record<ModelPurpose, BindingEntry>
export function getBindings() { return api<BindingsPayload>('/settings/bindings') }
export function saveBinding(purpose: ModelPurpose, binding: ModelBinding | { kind: 'inherit' }) {
  return api<BindingsPayload>('/settings/bindings', { method: 'PUT', body: JSON.stringify({ purpose, ...binding }) })
}
// 上下文用量估算：中文为主的文本约 3.2 字符/token，叠加 system 提示与工具声明的固定基数。
export function estimateTokens(text: string) { return Math.ceil((text || '').length / 3.2) }

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
