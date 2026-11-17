export type Job = {
  id: string; title: string; company: string; location: string; salary: string;
  url: string; source: string; updated_at: string; description: string; evidence_ids: string[]; tags: string[]
  platform_tags?: string[]; personal_tags?: string[]; description_source?: string; description_source_note?: string
  description_status?: string; detail_collected_at?: string; detail_source_url?: string
  description_truncated?: boolean // 列表端点裁剪过正文；全文走 /jobs/{id}
  starred?: boolean; starred_at?: string // 收藏标记：重采集幸存，见 repository.save_job
  pipeline_status?: 'interested' | 'applied' | 'interviewing' | 'offer' | 'archived' // 机会管线：与收藏同篮，重采集幸存
  pipeline_status_changed_at?: string
}
// 机会管线回顾（收藏夹顶部）：纯本地统计，各列表截前 8 条。
export type PipelineReview = {
  totals: Record<string, number>; stale_days: number
  new_favorites: { id: string; title: string; company: string; status: string; changed_at: string; starred_at: string }[]
  recent_changes: { id: string; title: string; company: string; status: string; changed_at: string; starred_at: string }[]
  follow_ups: { id: string; title: string; company: string; status: string; changed_at: string; starred_at: string }[]
}
export function getPipelineReview() { return api<PipelineReview>('/jobs/pipeline/review') }
// 列表端点的 text 可能被裁剪（text_truncated），text_hit 是搜索命中处的全文摘要窗口。
export type Evidence = { id: string; title: string; url: string; source: string; kind: string; company: string; text: string; collected_at: string; published_at: string | null; content_hash: string; text_truncated?: boolean; text_hit?: string }
export type Source = { id: string; name: string; kind: string; transport_mode: string; status: string; description: string; login?: string; cities: string[]; hot: string[]; browser_collectable?: boolean }
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
// step 段有两种来源：流式增量（step 键）与落库回放（event 键，general_agent._event），渲染层两个键都兼容。
export type ChatSegment = { kind: 'reasoning' | 'text'; text: string } | { kind: 'step'; step?: ToolStep; event?: ToolStep }
// 系统设置：存服务端（跨浏览器一致），含聊天显示模式、思维链显隐、Agent 执行限额与市场检索源。
export type AgentLimits = { max_model_calls: number; max_tool_calls: number; context_characters: number; max_seconds: number }
export type SystemSettings = { chat_display_mode: 'merged' | 'sequential'; show_reasoning: boolean; agent_limits: AgentLimits; market_source: 'official' | 'china' }
export type ApprovalRequest = { approval_id: string; tool: string; arguments: Record<string, unknown>; reason: string; run_id?: string }
export type ChatRunStatus = { run_id: string; conversation_id: string; done: boolean; cancelled: boolean; awaiting_approval: boolean; pending_approval: ApprovalRequest | null }
export type ChatScope = { kind: 'global' } | { kind: 'company'; company: string } | { kind: 'job'; jobId: string; company: string; title?: string }
export type ConversationSummary = { id: string; title: string; mode: ChatMode; model: string; llm_channel: string; permission_mode: PermissionMode; updated_at: string; message_count: number; preview: string }
export type ConversationConditions = { location: string; threshold: number | null; operator: string; threshold_basis: string; exclude_outsourcing: boolean; clarification: string }
export type ConversationDetail = { conditions?: ConversationConditions; id: string; scope: { kind: string; company?: string; job_id?: string }; title: string; mode: ChatMode; model: string; llm_channel: string; permission_mode: PermissionMode; created_at: string; updated_at: string; messages: StoredMessage[]; active_run: ChatRunStatus | null; scope_expansions?: ScopeExpansion[]; lineage?: { source_conversation_id: string; action_id: string } | null }
export type ChatRunStart = { run_id: string; conversation_id: string; user_message_id: string; title: string | null }
export type ModelProvider = { mode: string; channel: string; protocol: 'openai' | 'anthropic'; url: string; model: string; local_model: string; key_set: boolean; source: string; context_tokens?: number | null; saved_channel_id?: string }
export type Channel = { id: string; name: string; url: string; protocol: 'openai' | 'anthropic'; models: string[]; embeddings: string[] | null; note?: string }
export type ModelMetadata = { context_tokens: number; source: string; checked_at: string }
// 已保存的对话渠道：每条自带地址/密钥/默认模型/模型列表，可激活为全局默认
export type ChannelTestRecord = { target: 'llm' | 'embedding'; model: string; ok: boolean; duration_ms: number; at: string }
export type LlmChannel = { id: string; name: string; channel: string; protocol: 'openai' | 'anthropic'; url: string; model: string; models: string[]; embeddings?: string[]; context_tokens: number | null; key_set: boolean; last_test?: ChannelTestRecord | null }
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
export type SignalCollectResult = SignalActionResult & { signals: unknown[]; task?: Task }
export type SignalExtractResult = SignalActionResult & { accepted: number; signals: unknown[]; evidence: number; task: Task }
export type SignalFieldMapping = Record<string, Record<string, string[]>[]>
export type SignalSourceDraft = { mode: 'disabled' | 'enabled'; key?: string; url?: string; auth_header?: string; auth_prefix?: string; company_param?: string; fields?: SignalFieldMapping }
export type SignalSource = {
  name: string; tier: 'free' | 'paid'; transport: string; status: string; rules: string[]; description: string;
  mode: 'disabled' | 'enabled'; implemented: boolean; ready: boolean; unavailable_reason: string;
  manual_only?: boolean;
  url?: string; updated_at?: string; key_set?: boolean; auth_header?: string; auth_prefix?: string;
  company_param?: string; fields?: SignalFieldMapping; last_run?: Task | null
}

export function getReliability(company: string) { return api<{ report: ReliabilityReport | null }>(`/companies/${encodeURIComponent(company)}/reliability`) }
// 横评候选：实体表全量（含未算过的公司），分数照实 null——没算过就是没算过；按名称排序，不带名次语义。
// jobs_count 是实体口径（名称+别名归并）下的已存岗位数，检索时判断哪家有数据用。
export type CompareCompany = { company: string; credit_code: string; score: number | null; confidence: number | null; computed_at: string | null; jobs_count: number }
export function getCompareCompanies(q = '') { return api<{ companies: CompareCompany[] }>(`/reliability/compare?q=${encodeURIComponent(q)}`) }
// 岗位分层概要（compare 端点 companies 模式）：职类构成 + 职类×经验×学历逐层报价对照。
// 中位数只在过闸层给出（本公司 ≥2 条、同城同层 ≥5 条），与报告 pay_vs_city_median 同源同闸门。
export type CompareLayer = { role: string; experience: string; education: string; company_jobs: number; city_jobs: number; company_median?: number; city_median?: number; ratio?: number; gate: 'pass' | 'few_company' | 'few_city' }
export type CompareLayers = { city: string; total_jobs: number; parseable: number; mix: { role: string; jobs: number }[]; layers: CompareLayer[] }
export function getCompanyLayers(companies: string[], gate?: { own_min: number; city_min: number }) {
  const suffix = gate ? `&own_min=${gate.own_min}&city_min=${gate.city_min}` : ''
  return api<{ companies: (CompareLayers & { company: string })[] }>(`/reliability/compare?companies=${encodeURIComponent(companies.join('|'))}${suffix}`)
}
// 岗位收藏与反查：PATCH 支持部分字段（starred 显式 false = 取消收藏）；批量反查 ids 与完整JD补采共用一条任务管道。
export function patchJobs(ids: string[], patch: { starred?: boolean; location?: string; salary?: string; tags?: string[]; pipeline_status?: 'interested' | 'applied' | 'interviewing' | 'offer' | 'archived' | '' }) { return api<{ updated: number }>('/jobs', { method: 'PATCH', body: JSON.stringify({ ids, ...patch }) }) }
export function startDetailBatch(ids?: string[]) { return post<{ task: Task }>('/browser/details', ids ? { ids } : {}) }
// ---- Boss 捕获篮导入（docs/design/boss-import.md）：当前页捕获 + 链接粘贴 → 篮子 → 确认入库 ----
export type BossTab = { id: string; url: string; title: string }
// 篮子条目：in_library 表示库里已有同一条岗位（source=boss + encryptJobId 幂等归并）。
export type BasketItem = {
  external_id: string; title: string; company: string; location: string; salary: string; url: string;
  tags: string[]; imported_via: 'link' | 'current_page' | 'favorite' | 'browse' | 'script'; captured_at: string; in_library: boolean
}
export function listBossImportTabs() { return api<{ status: string; tabs: BossTab[]; message: string }>('/import/boss/tabs') }
export function captureBossPage(tabId: string) { return post<{ status: string; added: number; updated?: number; message: string }>('/import/boss/page', { tab_id: tabId }) }
export function parseImportLinks(text: string) { return post<{ added: number; updated: number; message: string }>('/import/links', { text }) }
export function getImportBasket() { return api<{ items: BasketItem[]; total: number }>('/import/basket') }
export function clearImportBasket() { return post<{ cleared: number }>('/import/basket/clear', {}) }
export function confirmImport(ids: string[] | null, autoStar: boolean) {
  return post<{ status: string; imported: number; message: string; jobs: Job[] }>('/import/confirm', { ...(ids ? { ids } : {}), auto_star: autoStar })
}
// P1：一键收藏导入（后台任务，轮询 /tasks/{id}）；逛即采集开关；公司快查联动「在 Boss 打开」。
export function getTask(id: string) { return api<{ task: Task }>(`/tasks/${encodeURIComponent(id)}`) }
export function startFavoritesImport() { return post<{ task: Task }>('/import/boss/favorites', {}) }
export type BrowseCaptureStatus = { status: string; running: boolean; captured: number; message: string }
export function getBrowseStatus() { return api<BrowseCaptureStatus>('/import/boss/browse') }
export function startBrowseCapture() { return post<BrowseCaptureStatus>('/import/boss/browse/start', {}) }
export function stopBrowseCapture() { return post<BrowseCaptureStatus>('/import/boss/browse/stop', {}) }
export function openBossSearch(company: string) { return post<{ status: string; message: string }>('/import/boss/open', { company }) }
// 主体归并：规则+LLM 扫描产候选，人工确认/手动合并/拆分是最终事实源；合并即别名并集，
// jobs/evidence 的原始名不动，读路径按「名称+别名」集合聚数据。
export type MergeEntity = { id: string; name: string; credit_code: string; aliases: string[]; jobs_count: number }
export type MergeProposal = { id: string; company_a: string; company_b: string; reason: string; status: 'pending' | 'accepted' | 'dismissed'; created_at: string; resolved_at: string }
export type MergeOverview = { entities: MergeEntity[]; proposals: MergeProposal[] }
export function getMergeOverview() { return api<MergeOverview>('/companies/merge/overview') }
export function startMergeScan() { return post<{ task: Task }>('/companies/merge/scan', {}) }
export function mergeCompanies(company_a: string, company_b: string) {
  return post<{ company: MergeEntity } & MergeOverview>('/companies/merge', { company_a, company_b, primary: 'a' })
}
export function acceptMergeProposal(id: string, primary: 'a' | 'b' | 'auto') {
  return post<{ company: MergeEntity } & MergeOverview>(`/companies/merge/proposals/${encodeURIComponent(id)}/accept`, { primary })
}
export function dismissMergeProposal(id: string) {
  return post<{ proposals: MergeProposal[] }>(`/companies/merge/proposals/${encodeURIComponent(id)}/dismiss`, {})
}
export function splitCompanyAlias(companyId: string, alias: string) {
  return post<{ company: MergeEntity } & MergeOverview>(`/companies/${encodeURIComponent(companyId)}/split`, { alias })
}
export function getReliabilityHistory(company: string) { return api<{ history: ReliabilityHistory | null }>(`/companies/${encodeURIComponent(company)}/reliability/history`) }
export function computeReliability(company: string) { return post<ReliabilityReport>(`/companies/${encodeURIComponent(company)}/reliability`, { credit_code: '' }) }
// 多平台公司检索：公告+企查查/天眼查+岗位渠道逐渠道后台串行跑，返回各渠道任务由调用方轮询；
// limit 控制岗位渠道的采集上限，横评补数据传更大值（默认 10 与岗位详情页「多平台检索」一致）。
export function startCompanyResearch(company: string, limit = 10, signalSources?: string[]) { return post<{ tasks: Task[] }>('/company-research', { company, limit, ...(signalSources ? { signal_sources: signalSources } : {}) }) }
export function collectCompanySignals(company: string, source: string) { return post<SignalCollectResult>(`/companies/${encodeURIComponent(company)}/signals/collect`, { source }) }
export function extractCompanySignals(company: string, limit = 12) { return post<SignalExtractResult>(`/companies/${encodeURIComponent(company)}/signals/extract`, { limit }) }
export function listSignalSources() { return api<{ sources: Record<string, SignalSource> }>('/reliability/sources') }
export function saveSignalSource(id: string, body: SignalSourceDraft) {
  return api<SignalSource>(`/reliability/sources/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(body) })
}
// 来源健康度（数据与采集 → 来源健康）：后端按最近 100 次任务聚合，不产出诊断结论。
export type SourceHealth = {
  source: string; total: number; ok: number; attention: number; failed: number;
  last_run_at: string; last_ok_at: string; last_status: string; last_message: string
}
export function getSourceHealth() { return api<{ window: string; sources: SourceHealth[] }>('/sources/health') }
// 运行日志页：后端进程内环形缓冲（最多 2000 条），服务重启清空、不落盘；对 /api/logs 自身的轮询访问行已被后端滤掉。
export type LogEntry = { seq: number; ts: string; level: string; logger: string; message: string }
export type LogPage = { entries: LogEntry[]; latest: number }
export function getLogs(after = 0, limit = 1000) { return api<LogPage>(`/logs?after=${after}&limit=${limit}`) }
// 工资基线（数据与采集 → 数据源）：人社局指导价位手动导入，pay 维度同城对照的兜底。
export type WageBaseline = { id: string; city: string; role: string; education: string; year: string; monthly_median: number; note: string; created_at: string }
export function listWageBaselines() { return api<{ baselines: WageBaseline[] }>('/wage-baselines') }
export function importWageBaselines(csv: string) { return post<{ saved: number; errors: string[] }>('/wage-baselines/import', { csv }) }
export function deleteWageBaseline(id: string) { return api<{ deleted: string }>(`/wage-baselines/${encodeURIComponent(id)}`, { method: 'DELETE' }) }

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
export function getChannels() { return api<{ channels: Channel[]; model_metadata: Record<string, ModelMetadata> }>('/settings/channels') }
export function listLlmChannels() { return api<{ channels: LlmChannel[] }>('/settings/llm-channels') }
export function saveLlmChannel(draft: LlmChannelDraft) { return post<{ channel: LlmChannel; channels: LlmChannel[] }>('/settings/llm-channels', draft) }
export function deleteLlmChannel(id: string) { return api<{ ok: boolean; channels: LlmChannel[]; bindings: BindingsPayload }>(`/settings/llm-channels/${encodeURIComponent(id)}`, { method: 'DELETE' }) }
export function activateLlmChannel(id: string) { return post<{ settings: Record<'llm' | 'embedding', ModelProvider>; bindings: BindingsPayload }>(`/settings/llm-channels/${encodeURIComponent(id)}/activate`) }
// 按功能当前生效配置实测：服务端从绑定取渠道/模型/密钥，密钥不下发前端。
export function testModelPurpose(purpose: ModelPurpose) {
  return post<{ ok: boolean; dimensions: number | null; duration_ms: number; message: string }>(`/settings/models/test-purpose/${purpose}`)
}
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
// 数据新鲜度（2026-11-17）：岗位/证据的采集时间超过阈值时，卡片给出「数据较旧」提示。
// 求职数据半衰期短：岗位 30 天、公司证据 90 天；只提示不拦截，时间解析失败不判旧。
export function isStale(value: string | null | undefined, days: number) {
  if (!value) return false
  const parsed = new Date(value)
  return !Number.isNaN(parsed.getTime()) && Date.now() - parsed.getTime() > days * 86400000
}
export function safeUrl(url: string) { try { const parsed = new URL(url); return ['http:', 'https:'].includes(parsed.protocol) ? parsed.href : undefined } catch { return undefined } }
// ---- 简历工坊：JD 定向优化、版本管理、模板预览与多格式导出 ----
export type ResumeTemplate = { id: string; name: string; description: string }
export type ResumeVersionSummary = { id: number; title: string; company: string; job_title: string; template_id: string; created_at: string; updated_at: string; characters: number }
export type ResumeVersion = ResumeVersionSummary & { job_id: string; jd_digest: string; base_text: string; markdown: string; notes: string[] }
export type ResumeVersionDraft = { title: string; markdown: string; template_id?: string; job_id?: string; company?: string; job_title?: string; jd_digest?: string; base_text?: string; notes?: string[] }
export function listResumeTemplates() { return api<{ templates: ResumeTemplate[] }>('/resume-templates') }
export function previewResumeDocument(markdown: string, templateId: string) {
  return post<{ html: string }>('/resume-templates/preview', { markdown, template_id: templateId })
}
export function listResumeVersions() { return api<{ versions: ResumeVersionSummary[] }>('/resume-versions') }
export function getResumeVersion(id: number) { return api<ResumeVersion>(`/resume-versions/${id}`) }
export function createResumeVersion(draft: ResumeVersionDraft) { return post<ResumeVersion>('/resume-versions', draft) }
export function updateResumeVersion(id: number, patch: Partial<Pick<ResumeVersionDraft, 'title' | 'markdown' | 'template_id' | 'notes'>>) {
  return api<ResumeVersion>(`/resume-versions/${id}`, { method: 'PUT', body: JSON.stringify(patch) })
}
export function deleteResumeVersion(id: number) { return api<{ ok: boolean }>(`/resume-versions/${id}`, { method: 'DELETE' }) }
export function optimizeResume(payload: { resume_text?: string; job_id?: string; jd_text?: string; company?: string; job_title?: string; instructions?: string; profile_entry_ids?: string[] }) {
  return post<ResumeVersion>('/resume-versions/optimize', payload)
}
// 个人资料库（2026-11-17）：完整版个人材料，与「我的简历」（可为篇幅而删减）分离；
// 简历工坊优化时勾选条目取材，扩写充实阉割版内容。
export type ProfileCategory = 'project' | 'work' | 'education' | 'skill' | 'cert' | 'award' | 'other'
export const PROFILE_CATEGORY_NAMES: Record<ProfileCategory, string> = {
  project: '项目经历', work: '工作经历', education: '教育背景', skill: '专业技能',
  cert: '证书', award: '奖项荣誉', other: '其他材料',
}
export type ProfileEntry = {
  id: string; category: ProfileCategory; title: string; org: string; period: string;
  content: string; tags: string[]; enabled: boolean; created_at: string; updated_at: string
}
export function listProfileEntries(enabledOnly = false) {
  return api<{ entries: ProfileEntry[] }>(`/profile-entries${enabledOnly ? '?enabled_only=true' : ''}`)
}
export function createProfileEntry(body: { category: ProfileCategory; title: string; org?: string; period?: string; content: string; tags?: string[]; enabled?: boolean }) {
  return post<ProfileEntry>('/profile-entries', body)
}
export function replaceProfileEntry(id: string, body: { category: ProfileCategory; title: string; org?: string; period?: string; content: string; tags?: string[]; enabled?: boolean }) {
  return api<ProfileEntry>(`/profile-entries/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(body) })
}
export function patchProfileEntry(id: string, patch: Partial<{ category: ProfileCategory; title: string; org: string; period: string; content: string; tags: string[]; enabled: boolean }>) {
  return api<ProfileEntry>(`/profile-entries/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(patch) })
}
export function deleteProfileEntry(id: string) {
  return api<{ deleted: string }>(`/profile-entries/${encodeURIComponent(id)}`, { method: 'DELETE' })
}
export function exportResumeVersion(id: number, format: 'pdf' | 'docx' | 'html' | 'md', filename: string) {
  return download(`/resume-versions/${id}/export?format=${format}`, filename)
}
// ---- 知识库：分块文档模型——手写条目与上传文档统一入库，Agent 经 knowledge_search 检索引用（K-…） ----
export type KnowledgeStrategy = 'recursive' | 'hard' | 'punct' | 'semantic' | 'llm'
export type KnowledgeRetrievalMode = 'lexical' | 'hybrid'
export type KnowledgeDoc = {
  id: string; title: string; content: string; tags: string[]; created_at: string; updated_at: string
  source: 'manual' | 'upload'; file_name?: string; file_size?: number
  chunk_strategy: KnowledgeStrategy; chunk_size: number; chunk_overlap: number; chunk_count: number
  content_truncated?: boolean; characters?: number; hit_excerpt?: string
}
export type KnowledgeChunk = { ordinal: number; text: string; characters: number }
export type KnowledgeSettings = { strategy: KnowledgeStrategy; size: number; overlap: number; separators: string[]; retrieval_mode: KnowledgeRetrievalMode }
export type KnowledgeStatus = { docs: number; chunks: number; embedded: number; pending: number; stale: boolean; embedding_model: string | null; embedding_ready: boolean }
export type KnowledgeSettingsPayload = { settings: KnowledgeSettings } & KnowledgeStatus
export type KnowledgeList = { entries: KnowledgeDoc[]; tags: string[]; total: number; page: number; page_size: number; pages: number } & KnowledgeStatus
// 批量导入任务：登记→逐文件上传暂存→后台导入（可取消）；Task 基础字段复用采集记录类型。
export type KnowledgeImportFile = {
  index: number; name: string; size: number | null
  state: 'pending' | 'staged' | 'processing' | 'imported' | 'failed' | 'skipped' | 'cancelled'
  entry_id?: string | null; chunks?: number | null; error?: string | null; warnings?: string[]
}
export type KnowledgeImportTask = Task & { kind: 'knowledge_import'; files: KnowledgeImportFile[] }
export function listKnowledge(q = '', tag = '', page = 1, pageSize = 20) {
  const params = new URLSearchParams({ q, tag, page: String(page), page_size: String(pageSize) })
  return api<KnowledgeList>(`/knowledge?${params.toString()}`)
}
export function getKnowledge(id: string) { return api<KnowledgeDoc>(`/knowledge/${encodeURIComponent(id)}`) }
export function getKnowledgeChunks(id: string) { return api<{ chunks: KnowledgeChunk[] }>(`/knowledge/${encodeURIComponent(id)}/chunks`) }
export function createKnowledge(draft: { title: string; content: string; tags: string[] }) { return post<KnowledgeDoc>('/knowledge', draft) }
export function updateKnowledge(id: string, patch: { title?: string; content?: string; tags?: string[] }) {
  return api<KnowledgeDoc>(`/knowledge/${encodeURIComponent(id)}`, { method: 'PUT', body: JSON.stringify(patch) })
}
export function deleteKnowledge(id: string) { return api<{ deleted: boolean }>(`/knowledge/${encodeURIComponent(id)}`, { method: 'DELETE' }) }
// 导入任务：创建（登记文件名）→ 逐个上传暂存 → start 触发后台导入 → 轮询进度 → 可取消。
export function createKnowledgeImport(names: string[]) { return post<KnowledgeImportTask>('/knowledge/imports', { names }) }
export function stageKnowledgeImportFile(taskId: string, index: number, filename: string, file: File) {
  return upload<KnowledgeImportTask>(`/knowledge/imports/${encodeURIComponent(taskId)}/files?index=${index}&filename=${encodeURIComponent(filename)}`, file)
}
export function startKnowledgeImport(taskId: string) { return post<KnowledgeImportTask>(`/knowledge/imports/${encodeURIComponent(taskId)}/start`) }
export function cancelKnowledgeImport(taskId: string) { return post<KnowledgeImportTask>(`/knowledge/imports/${encodeURIComponent(taskId)}/cancel`) }
export function getKnowledgeImport(taskId: string) { return api<KnowledgeImportTask>(`/knowledge/imports/${encodeURIComponent(taskId)}`) }
export function getLatestKnowledgeImport() { return api<KnowledgeImportTask | null>('/knowledge/imports/latest') }
export function getKnowledgeSettings() { return api<KnowledgeSettingsPayload>('/knowledge/settings') }
export function updateKnowledgeSettings(patch: Partial<Pick<KnowledgeSettings, 'strategy' | 'size' | 'overlap' | 'separators' | 'retrieval_mode'>>) {
  return api<KnowledgeSettingsPayload>('/knowledge/settings', { method: 'PUT', body: JSON.stringify(patch) })
}
export type KnowledgeReembedResult = KnowledgeStatus & { model: string }
export function reembedKnowledge() { return post<KnowledgeReembedResult>('/knowledge/reembed') }
