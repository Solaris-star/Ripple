import { csrfToken, securedFetchOptions } from './security';

export interface ChannelConnectionOption {
  id: string; label: string; status: 'available' | 'planned' | 'unavailable';
  execution_scope: 'server' | 'browser_node' | 'local'; requirements: string;
}
export interface Channel {
  id: string; name: string; connected: boolean; direct_publish: boolean;
  status: string; local_export: boolean; reason: string; formats: string[];
  adapter_available: boolean; adapter: string; environment_ready: boolean; account_count: number;
  live_verified: boolean; scope_note: string;
  connection_methods?: string[]; connection_options?: ChannelConnectionOption[];
}
export interface ExecutionNode {
  id: string; name: string; kind: 'local' | 'remote'; platform: string; online: boolean;
  capabilities: string[]; browsers: string[]; interactive_browsers: string[]; last_seen: string;
}
export interface Account {
  id: string; platform: string; label: string; status: string; auth_revision: number;
  identity: { logged_in: boolean; name: string; remote_id: string } | null;
  checked_at: string | null; message: string; live_verified: boolean;
  operation: { id: string; kind: string; state: string; started_at: string; task_id?: string; browser_channel?: string; execution_node_id?: string; command_id?: string } | null;
  login_state?: string; qr_available?: boolean; login_url?: string;
  execution_node_id?: string; profile_id?: string; browser_channel?: string | null;
  adapter?: string; external_id?: string; bridge_revision?: string; capabilities?: string[];
}
export interface XhsMetrics { views?: number | null; likes?: number | null; collects?: number | null; comments?: number | null; shares?: number | null; }
export interface XhsNoteSummary {
  note_id: string; title: string; author?: string; url: string; scope: string; metrics: XhsMetrics;
}
export interface XhsNoteDetail extends XhsNoteSummary { body: string; images: string[]; }
export interface XhsReadResult {
  source: string; sample_scope: string; fetched_at: string; query?: string;
  items?: XhsNoteSummary[]; note?: XhsNoteDetail; count?: number;
}
export interface XhsComment {
  id: string; nickname: string; content: string; time: number; time_str: string; like: string; parent: string;
}
export interface XhsCommentsResult extends XhsReadResult { note_id: string; count: number; comments: XhsComment[]; }
export interface XhsInteractionItem { id?: string; nickname?: string; content?: string; reply?: string; }
export interface XhsInteraction {
  id: string; account_id: string; account_label: string; note_id: string; note_url: string;
  kind: 'reply' | 'delete' | 'comment'; payload: { items?: XhsInteractionItem[]; text?: string };
  status: 'draft' | 'dispatching' | 'verified' | 'partial' | 'unknown_result' | 'not_submitted';
  attempts: number; operation_id: string | null; result?: { results?: { id: string; status: string; reason: string }[]; status?: string; reason?: string } | null;
  created_at: string; updated_at: string;
}
export interface InteractionCapability {
  platform: string; name: string; read_contents: boolean; read_comments: boolean;
  reply: boolean; comment: boolean; delete: boolean; refresh_result: boolean; platform_verify: boolean;
  account_count: number; connected_count: number; note: string;
}
export interface InteractionComment {
  id: string; nickname: string; content: string; time: number; time_str: string; like: string; parent: string;
}
export interface InteractionSource {
  id: string; kind: 'import' | 'remote'; platform: string; label: string; account_id: string; account_label: string;
  target_id: string; target_url: string; count: number; comments?: InteractionComment[]; created_at: string; updated_at: string;
}
export interface InteractionInsight {
  source_id: string; platform: string; label: string; engine: string; approximate: boolean; total: number; unique_authors: number;
  sentiment: { distribution: Record<string, number>; ratio: Record<string, number>; positive_examples: string[]; negative_examples: string[] };
  keywords: { word: string; count: number }[];
  demands: { count: number; examples: string[] }; complaints: { count: number; examples: string[] }; questions: { count: number; examples: string[] };
  comment_labels: Record<string, ('positive' | 'negative' | 'demand' | 'question')[]>;
  warning: string;
}
export interface InteractionContentSummary { id: string; title: string; url: string; metrics: XhsMetrics; }
export interface InteractionItem { id?: string; nickname?: string; content?: string; reply?: string; }
export interface Interaction {
  id: string; platform: string; source_kind: 'import' | 'remote'; source_id: string; delivery: 'local' | 'remote';
  account_id: string; account_label: string; target_id: string; target_url: string; note_id?: string; note_url?: string;
  kind: 'reply' | 'delete' | 'comment'; payload: { items?: InteractionItem[]; text?: string };
  status: 'draft' | 'dispatching' | 'verified' | 'partial' | 'unknown_result' | 'not_submitted' | 'cancelled';
  attempts: number; operation_id: string | null; result?: { results?: { id: string; status: string; reason: string }[]; status?: string; reason?: string } | null;
  created_at: string; updated_at: string; legacy_origin?: string;
  resolution?: { source: 'manual_platform_check'; result: 'verified' | 'not_submitted'; note: string; at: string };
}
export interface InteractionRefreshResult { source: 'local_ledger' | 'local_worker'; platform_verified: boolean; interaction: Interaction; note?: string; }
export type StructuredOperationId = 'topic_evaluate' | 'text_polish' | 'comment_analysis' | 'template_apply' | 'publish_checklist';
export interface StructuredOperationMeta {
  id: StructuredOperationId; version: string; label: string; module: string; skills: string[];
  risk: string; kind: 'model' | 'deterministic' | 'hybrid'; requires_model: boolean; ready: boolean; model_ready: boolean;
  advisory_ready?: boolean; description: string; input_schema: Record<string, string>; output_schema: Record<string, string>;
}
export interface OperationSource { kind: string; ref: string; version: string; digest: string; }
export interface OperationResult<T = Record<string, unknown>> {
  id: string; operation_id: StructuredOperationId; operation_version: string; source: OperationSource;
  input_digest: string; output: T; created_at: string;
}
export interface OperationTemplate { id: string; name: string; category: string; description: string; variables: string[]; }
export interface TopicEvaluationOutput {
  dimensions: { name: string; score: number; reason: string }[]; score: number; decision: '做' | '改方向' | '不做';
  summary: string; optimizations: string[]; alternatives: string[]; assumptions: string[];
}
export interface TextPolishOutput { revised_text: string; changes: string[]; warnings: string[]; }
export interface TemplateApplyOutput { template: { id: string; name: string; category: string; description: string }; variables: string[]; missing: string[]; preview: string; }
export interface PublishChecklistOutput {
  ready: boolean; blocking_issues: string[]; checks: { item: string; status: 'pass' | 'fail' | 'warn'; detail: string }[];
  advisories: { item: string; detail: string }[]; advisory_summary: string; advisory_available: boolean; advisory_error?: string; note: string;
}
export interface Content {
  project_id: string; title: string; body: string; platform: string; account_id: string;
  mode: 'simulation' | 'blog' | 'real'; media: string[]; scheduled_local: string | null;
  timezone: string; fold: 0 | 1 | null; source_id: string; source_version_id: string; tags: string;
  simulation_result: 'success' | 'retryable' | 'unknown' | 'verification' | 'accepted';
  options?: VariantOptions;
}
export interface Task {
  id: string; version: number; version_id: string; status: string;
  content: Content & { scheduled_at: string | null };
  attempts: number; created_at: string; updated_at: string;
  approval: { version_id: string; at: string; real_publish_confirmed?: boolean; auth_revision?: number } | null;
  review_context?: { account_id?: string; auth_revision?: number; label: string; identity?: Account['identity']; adapter?: string; connector_id?: string };
  variant_id?: string; variant_version_id?: string;
  receipt: { adapter: string; simulated: boolean; artifact_url?: string; public_url: string | null; result: string; verification?: string; not_submitted?: boolean; flow_id?: string; remote_status?: number; candidate_url?: string; draft_media_id?: string; publish_id?: string; publish_status?: number; draft_only?: boolean } | null;
  events: { at: string; status: string; note: string; version_id?: string }[];
}
export type WatermarkStatus = 'evidence' | 'unknown' | 'unsupported' | 'unavailable' | 'error';
export interface WatermarkCapability {
  inspection_ready: boolean; deep_clean_ready: boolean; reason: string;
}
export interface WatermarkInspection extends WatermarkCapability {
  path: string; status: WatermarkStatus; provider: string | null; signals: string[];
  supported: boolean; note: string;
}
export interface WatermarkCleanResult {
  source: string; output: string;
  media: { path: string; bytes: number; sha256: string; kind: string };
  inspection: WatermarkInspection;
}
export interface WatermarkTaskCleanResult { task: Task; cleaned: WatermarkCleanResult[]; }
export interface PreflightResult {
  ok: boolean; problems: string[]; task: Task;
  watermarks: WatermarkInspection[]; watermark_capability: WatermarkCapability;
}
export interface GeneratedMedia {
  kind: 'image' | 'video'; path: string; bytes: number;
  width?: number; height?: number; provider_id?: string | null; model?: string | null; adapter?: string; alternatives?: string[];
}
export interface MediaModelCapability { id: string; name: string; capabilities?: Record<string, boolean | string | number>; }
export interface MediaProviderCapability { id: string; name: string; protocol: string; configured: boolean; models: MediaModelCapability[]; }
export interface MediaCapability {
  available: boolean; default_provider_id?: string | null; default_model?: string | null;
  providers?: MediaProviderCapability[]; label: string;
  text_to_image?: boolean; image_to_image?: boolean; variations?: boolean;
  text_to_video?: boolean; image_to_video?: boolean;
}
export interface MediaCapabilities { image: MediaCapability; video: MediaCapability; }
export interface VariantOptions {
  content_type?: 'article' | 'thought'; slug?: string; excerpt?: string; date?: string;
  cover?: string; thought_tag?: string; [key: string]: unknown;
  wechat_action?: 'draft' | 'publish'; wechat_author?: string; wechat_digest?: string; wechat_source_url?: string; wechat_cover?: string;
}
export interface VariantContent {
  project_id: string; title: string; body: string; media: string[]; tags: string; target_id: string;
  delivery: 'remote' | 'export'; scheduled_local: string | null; timezone: string; fold: 0 | 1 | null; options: VariantOptions;
}
export interface PlatformVariant {
  id: string; source_id: string; source_version_id: string; platform: string; version: number; version_id: string;
  content: VariantContent; created_at: string; updated_at: string;
  bindings?: Record<string, { connector_id: string; kind: string; remote_id: string; revision: number; slug?: string; public_url?: string }>;
  legacy_task_id?: string;
}
export interface Mother {
  id: string; version: number; version_id: string; created_at: string; updated_at: string;
  content: { project_id: string; title: string; body: string; media: string[]; tags: string };
  variants?: { id: string; platform: string; target_id: string; delivery: 'remote' | 'export'; version: number; version_id: string; stale: boolean }[];
}
export interface BlogConnector {
  id: string; label: string; openapi_url: string; protocol: string; capabilities: string[]; content_types: string[];
  server_url?: string; public_url_templates?: Record<string, string>; status: string; updated_at: string;
}
export interface CalendarItem {
  id: string; title: string; platform: string; account: string; scheduled_at: string; timezone: string; status: string; mode: string;
}
export interface RippleStatus {
  brand: string; version: string; local_only: boolean; live_publishing: boolean; ai_enabled: boolean; native_adapters: boolean;
  environment: { browser: string | null; browsers?: string[]; biliup: boolean; requires_extension: boolean };
  aitoearn: { server_relay: string; ai_relay: string; enabled: boolean };
}
export const base = window.location.pathname.replace(/\/index\.html$/, '').replace(/\/$/, '');
export function errorText(error: unknown): string { return error instanceof Error ? error.message : '操作未完成，请重试。'; }

function detail(payload: { detail?: string | { msg: string }[] }, fallback: string): string {
  if (typeof payload.detail === 'string') return payload.detail;
  if (Array.isArray(payload.detail)) return payload.detail.map(v => v.msg).join('；');
  return fallback;
}
export async function api<T>(path: string, method = 'GET', data?: unknown): Promise<T> {
  const response = await fetch(`${base}${path}`, securedFetchOptions({
    method, headers: data === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: data === undefined ? undefined : JSON.stringify(data),
  }));
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    try { message = detail(await response.json(), message); } catch { /* Non-JSON server response. */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export interface AgentModelMeta { id: string; name: string; effort_levels: string[]; supports_reasoning?: boolean; source?: string; }
export interface AgentCapabilityMeta { id: string; category: string; label: string; description: string; risk: string; source: string; default_enabled: boolean; ready: boolean; user_selectable?: boolean; server_id?: string; tool_id?: string; }
export interface AgentSkillMeta { id: string; name: string; description: string; layer: string; ready: boolean; }
export interface AgentPluginMeta { id: string; name: string; description: string; skills: string[]; mcp_tools: string[]; }
export interface AgentCapabilityCatalog { mcp_tools: AgentCapabilityMeta[]; skills: AgentSkillMeta[]; plugins: AgentPluginMeta[]; models: AgentModelMeta[]; default_model: string; }
export interface AgentRuntimeCapabilities { restricted_mode?: boolean; native_model_selection?: boolean; native_effort?: boolean; profile_import?: boolean; streaming?: boolean; attachments?: boolean; mcp?: boolean; [key: string]: unknown; }
export interface AgentAdapterAction { id: 'install' | 'repair' | 'verify' | 'uninstall'; label: string; danger?: boolean; }
export interface AgentAdapterOption {
  id: string;
  runtime_id: string;
  label: string;
  kind: 'builtin' | 'npm_acp' | 'unavailable';
  protocol: string;
  description: string;
  source_url?: string;
  state: 'missing' | 'present_unmanaged' | 'installed_unverified' | 'ready' | 'verification_failed' | 'unavailable';
  state_label: string;
  detail: string;
  managed: boolean;
  package?: string;
  version?: string;
  installed_version?: string;
  actions: AgentAdapterAction[];
}
export interface AgentRuntimeMeta {
  runtime: string;
  name: string;
  installed: boolean;
  native_detected?: boolean;
  configured?: boolean;
  ready?: boolean;
  selectable?: boolean;
  healthy: boolean;
  running?: boolean;
  version?: string;
  detail?: string;
  dependency?: string;
  connection_state?: string;
  current_model?: string;
  current_effort?: string;
  profile_id?: string;
  models?: AgentModelMeta[];
  capabilities: AgentRuntimeCapabilities;
  adapters?: AgentAdapterOption[];
}
export interface AgentRuntimeCatalog { default_runtime: string; items: AgentRuntimeMeta[]; }
export interface AgentAdapterActionResult {
  ok: boolean;
  result: { ok?: boolean; state?: string; managed?: boolean; version?: string; adapter_id?: string; detail?: string };
  runtimes: AgentRuntimeCatalog;
}
export interface AgentProfileComponent { count: number; mode: string; items: Array<{ name?: string; path?: string; size?: number; sha256?: string }>; }
export interface AgentProfileInheritance { rules: boolean; skills: boolean; agents: boolean; commands: boolean; mcp: boolean; plugins: boolean; model: boolean; effort: boolean; memory: boolean; }
export interface AgentProfileMeta { id: string; runtime_id: string; name: string; installed: boolean; source_root: string; inherit_mode: string; model: string; effort: string; components: Record<string, AgentProfileComponent>; fingerprints: Record<string, string>; blocked_detected: Record<string, boolean | number>; blocked_inheritance: string[]; inheritance: AgentProfileInheritance; accepted_fingerprint: string; changed_since_review: boolean; reviewed_at?: string; }
export interface AgentProfileState { schema: number; default_profile: string; scanned_at?: string; profiles: AgentProfileMeta[]; }
export interface AgentSessionConfig { session_id: string; runtime_id: string; profile_id: string; runtime_locked: boolean; model: string; effort: string; effort_mode: 'auto' | 'instruction_fallback'; pinned_skills: string[]; enabled_mcp_tools: string[]; enabled_plugins: string[]; }
export interface McpToolMeta { id: string; name: string; description: string; risk: string; }
export interface McpServerMeta { id: string; name: string; transport: 'http' | 'sse' | 'streamable_http' | 'mock'; endpoint: string; enabled: boolean; status: 'ready' | 'not_connected'; tools: McpToolMeta[]; }
export interface AgentExtensions { schema: number; mcp_servers: McpServerMeta[]; plugins?: AgentPluginMeta[]; }
export const fetchAgentCapabilities = () => api<AgentCapabilityCatalog>('/api/agent/capabilities');
export const fetchAgentSessionConfig = (sessionId: string) => api<AgentSessionConfig>(`/api/agent/sessions/${encodeURIComponent(sessionId)}/config`);
export const saveAgentSessionConfig = (sessionId: string, patch: Partial<AgentSessionConfig>) => api<AgentSessionConfig>(`/api/agent/sessions/${encodeURIComponent(sessionId)}/config`, 'PUT', patch);
export const fetchAgentRuntimes = () => api<AgentRuntimeCatalog>('/api/agent/runtimes');
export const runAgentAdapterAction = (runtimeId: string, adapterId: string, action: AgentAdapterAction['id']) => api<AgentAdapterActionResult>(
  `/api/agent/runtimes/${encodeURIComponent(runtimeId)}/adapters/${encodeURIComponent(adapterId)}/actions`,
  'POST',
  { action },
);
export const fetchAgentProfiles = () => api<AgentProfileState>('/api/agent/profiles');
export const scanAgentProfiles = () => api<AgentProfileState>('/api/agent/profiles/scan', 'POST', {});
export const setDefaultAgentProfile = (profileId: string) => api<AgentProfileState>('/api/agent/profiles/default', 'PUT', { profile_id: profileId });
export const saveAgentProfileInheritance = (profileId: string, inheritance: Partial<AgentProfileInheritance>) => api<AgentProfileState>(`/api/agent/profiles/${encodeURIComponent(profileId)}/inheritance`, 'PUT', { inheritance });
export const acknowledgeAgentProfile = (profileId: string) => api<AgentProfileState>(`/api/agent/profiles/${encodeURIComponent(profileId)}/acknowledge`, 'POST', {});
export const fetchAgentExtensions = () => api<AgentExtensions>('/api/agent/extensions');
export const saveAgentExtensions = (mcp_servers: McpServerMeta[]) => api<AgentExtensions>('/api/agent/extensions', 'PUT', { mcp_servers });
export const taskAction = (task: Task, action: string, extra?: object) => api<Task>(
  `/api/ripple/tasks/${task.id}/${action}`, 'POST', { expected_version: task.version_id, ...extra },
);
export const xhsFeed = (accountId: string, limit = 12) => api<XhsReadResult>(`/api/ripple/xiaohongshu/accounts/${encodeURIComponent(accountId)}/feed?limit=${limit}`);
export const xhsSearch = (accountId: string, query: string, limit = 12) => api<XhsReadResult>(`/api/ripple/xiaohongshu/accounts/${encodeURIComponent(accountId)}/search?q=${encodeURIComponent(query)}&limit=${limit}`);
export const xhsNotes = (accountId: string, limit = 20) => api<XhsReadResult>(`/api/ripple/xiaohongshu/accounts/${encodeURIComponent(accountId)}/notes?limit=${limit}`);
export const xhsNote = (accountId: string, locator: { note_id?: string; url?: string }) => api<XhsReadResult>(`/api/ripple/xiaohongshu/accounts/${encodeURIComponent(accountId)}/note`, 'POST', { note_id: locator.note_id || '', url: locator.url || '' });
export const xhsComments = (accountId: string, locator: { note_id?: string; url?: string }, limit = 50) => api<XhsCommentsResult>(`/api/ripple/xiaohongshu/accounts/${encodeURIComponent(accountId)}/comments`, 'POST', { note_id: locator.note_id || '', url: locator.url || '', limit });
export const xhsInteractions = (accountId = '', limit = 100) => api<{ items: XhsInteraction[] }>(`/api/ripple/xiaohongshu/interactions?account_id=${encodeURIComponent(accountId)}&limit=${limit}`);
export const createXhsInteraction = (payload: {
  account_id: string; note_id?: string; url?: string; kind: 'reply' | 'delete' | 'comment'; items?: XhsInteractionItem[]; text?: string; idempotency_key: string;
}) => api<XhsInteraction>('/api/ripple/xiaohongshu/interactions', 'POST', { note_id: '', url: '', items: [], text: '', ...payload });
export const executeXhsInteraction = (id: string) => api<XhsInteraction>(`/api/ripple/xiaohongshu/interactions/${encodeURIComponent(id)}/execute`, 'POST', { confirmed: true });
export const queryXhsInteraction = (id: string) => api<XhsInteraction>(`/api/ripple/xiaohongshu/interactions/${encodeURIComponent(id)}/query`, 'POST', {});
export const fetchInteractionCapabilities = () => api<{ items: InteractionCapability[] }>('/api/ripple/interactions/capabilities');
export const fetchStructuredOperations = () => api<{ items: StructuredOperationMeta[] }>('/api/ripple/operations');
export const fetchOperationTemplates = () => api<{ items: OperationTemplate[] }>('/api/ripple/operations/templates');
export const executeStructuredOperation = <T>(operation: StructuredOperationId, input: Record<string, unknown>, source: Record<string, unknown> = {}) => api<OperationResult<T>>(`/api/ripple/operations/${encodeURIComponent(operation)}/execute`, 'POST', { input, source });
export const fetchOperationResults = <T = Record<string, unknown>>(operation: StructuredOperationId, sourceRef = '', limit = 30) => api<{ items: OperationResult<T>[] }>(`/api/ripple/operations/results?operation_id=${encodeURIComponent(operation)}&source_ref=${encodeURIComponent(sourceRef)}&limit=${limit}`);
export const fetchInteractionSources = (platform = '', limit = 24) => api<{ items: InteractionSource[] }>(`/api/ripple/interactions/sources?platform=${encodeURIComponent(platform)}&limit=${limit}`);
export const fetchInteractionSource = (id: string) => api<InteractionSource>(`/api/ripple/interactions/sources/${encodeURIComponent(id)}`);
export const analyzeInteractionSource = (id: string) => api<InteractionInsight>(`/api/ripple/interactions/sources/${encodeURIComponent(id)}/analysis`);
export const fetchInteractionContents = (accountId: string, limit = 30) => api<{ platform: string; source: string; fetched_at: string; items: InteractionContentSummary[] }>(`/api/ripple/interactions/accounts/${encodeURIComponent(accountId)}/contents?limit=${limit}`);
export const syncInteractionComments = (accountId: string, payload: { target_id?: string; target_url?: string; target_label?: string; limit?: number }) => api<InteractionSource>(`/api/ripple/interactions/accounts/${encodeURIComponent(accountId)}/comments`, 'POST', { target_id: '', target_url: '', target_label: '', limit: 100, ...payload });
export const fetchInteractions = (filters: { platform?: string; account_id?: string; status?: string; limit?: number } = {}) => {
  const q = new URLSearchParams(); if (filters.platform) q.set('platform', filters.platform); if (filters.account_id) q.set('account_id', filters.account_id); if (filters.status) q.set('status', filters.status); q.set('limit', String(filters.limit || 200));
  return api<{ items: Interaction[] }>(`/api/ripple/interactions?${q.toString()}`);
};
export const createInteraction = (payload: { platform: string; account_id?: string; source_id?: string; target_id?: string; target_url?: string; kind: 'reply' | 'delete' | 'comment'; items?: InteractionItem[]; text?: string; idempotency_key: string }) => api<Interaction>('/api/ripple/interactions', 'POST', { account_id: '', source_id: '', target_id: '', target_url: '', items: [], text: '', ...payload });
export const updateInteraction = (interaction: Interaction, payload: { items?: InteractionItem[]; text?: string }) => api<Interaction>(`/api/ripple/interactions/${encodeURIComponent(interaction.id)}`, 'PATCH', { expected_updated_at: interaction.updated_at, items: payload.items || [], text: payload.text || '' });
export const cancelInteraction = (interaction: Interaction) => api<Interaction>(`/api/ripple/interactions/${encodeURIComponent(interaction.id)}/cancel`, 'POST', { expected_updated_at: interaction.updated_at });
export const executeInteraction = (id: string) => api<Interaction>(`/api/ripple/interactions/${encodeURIComponent(id)}/execute`, 'POST', { confirmed: true });
export const refreshInteractionResult = (id: string) => api<InteractionRefreshResult>(`/api/ripple/interactions/${encodeURIComponent(id)}/refresh-result`, 'POST', {});
export const resolveUnknownInteraction = (id: string, result: 'verified' | 'not_submitted', note = '') => api<Interaction>(`/api/ripple/interactions/${encodeURIComponent(id)}/resolve-unknown`, 'POST', { result, confirmed: true, note });
export const inspectImplicitWatermark = (path: string) => api<WatermarkInspection>(
  '/api/ripple/media/watermark/inspect', 'POST', { path },
);
export const cleanImplicitWatermark = (path: string) => api<WatermarkCleanResult>(
  '/api/ripple/media/watermark/clean', 'POST', { path, confirmed: true },
);
export const cleanTaskImplicitWatermarks = (task: Pick<Task, 'id' | 'version_id'>) => api<WatermarkTaskCleanResult>(
  `/api/ripple/tasks/${task.id}/watermark-clean`, 'POST', { expected_version: task.version_id, confirmed: true },
);
export const fetchMediaCapabilities = () => api<MediaCapabilities>('/api/ripple/media/capabilities');
export const generateImage = (prompt: string, options: { size?: string; resolution?: string; input_image?: string; provider_id?: string; model?: string } = {}) => api<GeneratedMedia>(
  '/api/ripple/media/generate/image', 'POST', { prompt, size: options.size || '1024x1024', resolution: options.resolution || '2k', input_image: options.input_image, provider_id: options.provider_id, model: options.model },
);
export const generateVideo = (prompt: string, options: { ratio?: string; duration?: number; input_image?: string; provider_id?: string; model?: string } = {}) => api<GeneratedMedia>(
  '/api/ripple/media/generate/video', 'POST', { prompt, ratio: options.ratio || '9:16', duration: options.duration, input_image: options.input_image, provider_id: options.provider_id, model: options.model },
);
export function uploadMedia(file: File, onProgress?: (percent: number) => void): Promise<{ path: string; name: string; bytes: number; kind: string }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', `${base}/api/ripple/media`);
    const csrf = csrfToken(); if (csrf) xhr.setRequestHeader('X-Ripple-CSRF', csrf);
    xhr.withCredentials = true;
    xhr.timeout = 15 * 60 * 1000;
    xhr.upload.onprogress = e => { if (e.lengthComputable) onProgress?.(Math.round(e.loaded / e.total * 100)); };
    xhr.onerror = () => reject(new Error('上传连接中断。'));
    xhr.ontimeout = () => reject(new Error('上传超时，请检查本地服务。'));
    xhr.onload = () => {
      try { const result = JSON.parse(xhr.responseText); if (xhr.status >= 200 && xhr.status < 300) resolve(result); else reject(new Error(detail(result, '上传失败'))); }
      catch { reject(new Error('上传响应无效。')); }
    };
    const data = new FormData(); data.append('file', file); xhr.send(data);
  });
}
export const LABELS: Record<string, string> = {
  draft: '草稿', review_ready: '待审核', approved: '已审核', scheduled: '已排期', dispatching: '执行中',
  accepted: '已提交 · 待核对', published: '已发布 · 人工核对', simulated: '模拟完成', exported: '已导出', verification_required: '需人工处理',
  failed_retryable: '可重试', failed_terminal: '执行失败', unknown_result: '结果待核对', cancelled: '已取消',
};
export const ACCOUNT_LABELS: Record<string, string> = { disconnected: '未连接', connecting: '等待登录', connected: '已连接', expired: '需重新登录', error: '连接失败', deleting: '待清理' };
export const GLYPHS: Record<string, string> = { x: '𝕏', xiaohongshu: '红', wechat: '微', 'weixin-channels': '视', zhihu: '知', bilibili: 'b', blog: 'B', douyin: '♪', tiktok: '♪', kuaishou: '快' };
export const freshContent = (): Content => ({
  project_id: 'local', title: '', body: '', platform: 'xiaohongshu', account_id: 'unselected', mode: 'real', media: [],
  scheduled_local: null, timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC', fold: null, simulation_result: 'success',
  source_id: '', source_version_id: '', tags: '', options: {},
});
export function editableContent(content: Content): Content {
  return { ...freshContent(), ...Object.fromEntries(Object.keys(freshContent()).filter(k => content[k as keyof Content] !== undefined).map(k => [k, content[k as keyof Content]])) } as Content;
}
export function dateText(value?: string | null): string {
  if (!value) return '—';
  return new Date(value).toLocaleString(undefined, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false });
}
