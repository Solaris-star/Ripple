import { securedFetchOptions } from './security';

function getBasePath(): string {
  const path = window.location.pathname;
  const cleaned = path.replace(/\/index\.html$/, '').replace(/\/$/, '');
  return cleaned;
}

const BASE = getBasePath();

const inflightRequests = new Map<string, Promise<unknown>>();

async function request<T>(url: string, options?: RequestInit): Promise<T> {
  const method = String(options?.method || 'GET').toUpperCase();
  const execute = async (): Promise<T> => {
    const res = await fetch(`${BASE}${url}`, securedFetchOptions(options));
    if (!res.ok) {
      // 优先显示后端返回的实质错误信息（FastAPI 的 {detail}），而不是无意义的 "API error: 400"
      let detail = '';
      try {
        const j = await res.json();
        detail = (j && (j.detail || j.message)) || '';
      } catch {
        /* 响应体不是 JSON，忽略 */
      }
      throw new Error(detail || `请求失败（${res.status} ${res.statusText}）`);
    }
    return res.json() as Promise<T>;
  };
  // Requests with an AbortSignal stay caller-owned; one cancelled view must not
  // cancel another view that happens to read the same URL.
  if (method !== 'GET' || options?.signal) return execute();
  const existing = inflightRequests.get(url) as Promise<T> | undefined;
  if (existing) return existing;
  const pending = execute().finally(() => { if (inflightRequests.get(url) === pending) inflightRequests.delete(url); });
  inflightRequests.set(url, pending);
  return pending;
}

export interface StatusResponse {
  agentReady: boolean;
  recommendationAi: boolean;
  recommendationProvider?: string;
  agentRuntime?: string;
  agentVersion?: string;
  agentModel?: string;
  agentDetail?: string;
  features?: { campaigns?: boolean; campaign_sources_v2?: boolean; ai_providers_v2?: boolean };
  skills: SkillItem[];
  personas: PersonaItem[];
}

export interface PersonaItem {
  name: string;
  description: string;
}

export interface PersonaDetail {
  name: string;
  content: string;
}
export interface ContentProfileSummary {
  id: string; workspace_id: string; display_name: string; legacy_name: string;
  current_revision: number; state: string; account_count?: number; created_at: string; updated_at: string;
}
export interface ContentProfileBinding {
  workspace_id: string; target_kind: 'account' | 'blog'; account_id: string; profile_id: string;
  profile_revision: number; overrides: Record<string, unknown>; binding_revision: number;
  created_at: string; updated_at: string;
}
export interface ContentTargetSummary {
  id: string; target_kind: 'account' | 'blog'; platform: string; label: string; status: string;
  identity: { logged_in?: boolean; name?: string; remote_id?: string } | null;
  live_verified: boolean; auth_revision: number;
}
export interface ContentProfileContext {
  profiles: ContentProfileSummary[];
  bindings: ContentProfileBinding[];
  accounts: ContentTargetSummary[];
  blogs: ContentTargetSummary[];
  resolved: {
    workspace_id: string;
    profile: Pick<ContentProfileSummary, 'id' | 'display_name' | 'legacy_name' | 'current_revision' | 'state'> | null;
    binding: ContentProfileBinding | null;
    effective_files: Record<string, string>;
    overrides?: Record<string, unknown>;
    scope: 'generic' | 'profile' | 'account';
  };
}
export interface ContentProfileDetail extends ContentProfileSummary {
  files: Record<string, string>; revision_source: string; bindings: ContentProfileBinding[];
}
export interface ProfileAnalysisCapability {
  target_kind: 'account' | 'blog'; account_id: string; platform: string;
  automatic_history_supported: boolean; automatic_history_planned: boolean;
  automatic_history_fields?: string[];
  import_samples_supported: boolean; accepted_sample_fields: string[]; note: string;
}
export interface ProfileAnalysisRun {
  id: string; workspace_id: string; target_kind: 'account' | 'blog'; account_id: string; profile_id: string;
  status: string; capability: ProfileAnalysisCapability | Record<string, unknown>;
  samples: Array<Record<string, unknown>>; proposal: {
    files?: Record<string, string>; observations?: string[]; assumptions?: string[];
    open_questions?: string[]; sample_summary?: string;
  }; model: Record<string, unknown>; error: string; created_at: string; updated_at: string;
}

export function fetchContentProfileContext(params: { profile_name?: string; profile_id?: string; account_id?: string; target_kind?: string } = {}): Promise<ContentProfileContext> {
  const q = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) q.set(key, value);
  return request<ContentProfileContext>(`/api/content-profile-context${q.size ? `?${q}` : ''}`);
}
export function fetchContentProfiles(): Promise<{ items: ContentProfileSummary[] }> {
  return request('/api/content-profiles');
}
export function fetchContentProfile(profileId: string): Promise<ContentProfileDetail> {
  return request(`/api/content-profiles/${encodeURIComponent(profileId)}`);
}
export function renameContentProfile(profileId: string, displayName: string): Promise<ContentProfileDetail> {
  return request(`/api/content-profiles/${encodeURIComponent(profileId)}`, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ display_name: displayName }),
  });
}
export function copyContentProfile(profileId: string, displayName: string): Promise<ContentProfileDetail> {
  return request(`/api/content-profiles/${encodeURIComponent(profileId)}/copy`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ display_name: displayName }),
  });
}
export function saveContentProfileRevision(profileId: string, payload: { expected_revision: number; files: Record<string, string>; note?: string; confirm?: boolean }): Promise<ContentProfileDetail> {
  return request(`/api/content-profiles/${encodeURIComponent(profileId)}/revision`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}
export function bindContentProfile(targetKind: 'account' | 'blog', accountId: string, payload: { profile_id: string; overrides?: Record<string, unknown>; expected_binding_revision?: number }): Promise<ContentProfileBinding> {
  return request(`/api/account-profile-bindings/${targetKind}/${encodeURIComponent(accountId)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}
export function unbindContentProfile(targetKind: 'account' | 'blog', accountId: string): Promise<{ ok: boolean }> {
  return request(`/api/account-profile-bindings/${targetKind}/${encodeURIComponent(accountId)}`, { method: 'DELETE' });
}
export function fetchProfileAnalysisCapability(targetKind: 'account' | 'blog', accountId: string): Promise<ProfileAnalysisCapability> {
  return request(`/api/profile-analysis/capability/${targetKind}/${encodeURIComponent(accountId)}`);
}
export function startProfileAnalysis(payload: {
  target_kind: 'account' | 'blog'; account_id: string; profile_id?: string; display_name?: string;
  samples: Array<Record<string, unknown>>; use_account_history?: boolean; history_limit?: number; confirmed: boolean;
}): Promise<ProfileAnalysisRun> {
  return request('/api/profile-analysis', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}
export function fetchProfileAnalysis(runId: string): Promise<ProfileAnalysisRun> {
  return request(`/api/profile-analysis/${encodeURIComponent(runId)}`);
}
export function applyProfileAnalysis(runId: string, payload: {
  profile_id?: string; display_name?: string; expected_revision?: number; bind_target?: boolean;
}): Promise<{ profile: ContentProfileDetail; binding: ContentProfileBinding | null }> {
  return request(`/api/profile-analysis/${encodeURIComponent(runId)}/apply`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
  });
}


export interface StructuredSkillOperation {
  id: string; version: string; label: string; module: string; risk: string; kind: string;
  ready: boolean; requires_model: boolean; description: string;
}

export interface SkillItem {
  name: string;
  description: string;
  layer: string;
  needsApi: boolean;
  apiConfigured: boolean;
  structuredOperation?: StructuredSkillOperation | null;
}

export interface ApiKeySpec {
  env: string;
  label: string;
  required: boolean;
  secret: boolean;
  configured: boolean;
  masked: string;   // 脱敏值或非 secret 明文
  choices: string[];
}

export interface ApiProviderSpec {
  id: string;
  name: string;
  keys: ApiKeySpec[];
}

export interface ApiSpec {
  label: string;
  settings: ApiKeySpec[];
  providers: ApiProviderSpec[];
}

export interface SkillDetail {
  name: string;
  layer: string;
  description: string;
  body: string;
  needsApi: boolean;
  apiConfigured: boolean;
  apiSpec: ApiSpec | null;
  managedInSettings?: boolean;
  structuredOperation?: StructuredSkillOperation | null;
}

export type FileKind = 'text' | 'image' | 'video' | 'audio' | 'binary';

/** 内容关联产物目录的 .ripple.json 展示头。 */
export interface OutputMeta {
  title?: string;
  summary?: string;
  platform?: string;
  kind?: string;             // article|xhs-note|video|cards|poster|audio|other
  status?: string;           // draft|ready|published
  tags?: string[];
  cover?: string;            // outputs 下相对路径（后端已解析存在性）
  deliverables?: string[];   // 成品文件名（项目根相对）
  deliverablePaths?: string[];  // 成品的 outputs 相对路径（后端已解析存在性）
  content_id?: string;
  source_version_id?: string;
  contentId?: string;
  sourceVersionId?: string;
}

/** 产物树节点：文件或目录（目录带 children，可无限嵌套点开）。 */
export interface OutputNode {
  name: string;
  type: 'dir' | 'file';
  path: string;              // outputs 下的相对路径，如 "short-drama/监控诡影/episodes/ep01"
  mtime?: number;
  kind?: FileKind;           // 仅 file
  size?: number;             // 仅 file
  children?: OutputNode[];   // 仅 dir
  fileCount?: number;        // 仅 dir：递归文件数
  meta?: OutputMeta;         // 仅顶层项目 dir
  synthetic?: boolean;
  content_id?: string;
  legacy_unlinked?: boolean;
}

/** @deprecated 用 OutputNode（type==='file'）。保留别名减少改动面。 */
export type OutputFile = OutputNode;

export interface OutputContent {
  path: string;
  content: string;
  kind: FileKind;
  isBinary: boolean;
}

export interface SkillResponse {
  response: string;
}

export interface ProfileBuildResponse {
  created: boolean;
  name: string;
  async?: boolean;
  status?: string;
  log?: string;
}

export interface ProfileBuildStatus {
  state: 'running' | 'done' | 'failed' | 'unknown';
  log: string;
}

export function fetchStatus(): Promise<StatusResponse> {
  return request<StatusResponse>('/api/status');
}

export function fetchPersonas(): Promise<PersonaItem[]> {
  return request<PersonaItem[]>('/api/personas');
}

export function fetchPersonaDetail(name: string): Promise<PersonaDetail> {
  return request<PersonaDetail>(`/api/persona/${encodeURIComponent(name)}`);
}

export interface PersonaFile {
  filename: string;
  content: string;
}

export function fetchPersonaFiles(name: string): Promise<{ name: string; files: PersonaFile[] }> {
  return request(`/api/persona/${encodeURIComponent(name)}/files`);
}

export function savePersonaFile(name: string, filename: string, content: string): Promise<{ ok: boolean }> {
  return request(`/api/persona/${encodeURIComponent(name)}/file`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename, content }),
  });
}

export function deletePersona(name: string): Promise<{ ok: boolean; deleted: string }> {
  return request(`/api/persona/${encodeURIComponent(name)}`, { method: 'DELETE' });
}

// ---- 热点雷达 ----
export interface TrendItem { title: string; hot: string; url: string; }
export interface TrendAttempt { provider: string; status: string; }
export interface TrendGroup {
  platform: string; label: string; items: TrendItem[];
  status: 'fresh' | 'stale' | 'error'; source: string; fetched_at: number;
  error: string; attempts: TrendAttempt[]; cached?: boolean;
}
export function fetchTrends(platforms: string, limit = 12, options: { refresh?: boolean; xiaohongshuAccountId?: string; xiaohongshuProbe?: boolean } = {}): Promise<{ trends: TrendGroup[]; updated: number }> {
  const params = new URLSearchParams({ platforms, limit: String(limit) });
  if (options.refresh) params.set('refresh', 'true');
  if (options.xiaohongshuProbe) params.set('xiaohongshu_probe', 'true');
  if (options.xiaohongshuAccountId) params.set('xiaohongshu_account_id', options.xiaohongshuAccountId);
  return request(`/api/trends?${params.toString()}`);
}

// ---- 内容排期 ----
export interface ScheduleItem {
  id: string; title: string; date: string; platform: string;
  time: string; status: string; note: string;
  kind?: string;          // content（内容/发布）| event（平台活动/节日/特殊日期）
  url?: string;           // 已发布链接
  source?: string;        // manual | publish-page | chat | scheduler | campaign
  event_type?: string;    // event 专属：节日/电商/平台活动/行业
  end_date?: string;      // event 专属：活动区间结束日
  campaign_id?: string;
  campaign_rule_version?: number;
}
export type ScheduleInput = Omit<ScheduleItem, 'id'>;
export interface ScheduleContext {
  today?: string; window_days?: number; published_recent?: number;
  per_platform?: { platform: string; recent_count: number; last_date: string | null; days_since_last: number | null }[];
  upcoming_schedules?: ScheduleItem[];
  upcoming_events?: ScheduleItem[];
  suggestions?: string[];
}
export function fetchSchedule(): Promise<ScheduleItem[]> {
  return request('/api/schedule');
}
export function fetchScheduleContext(days = 14): Promise<ScheduleContext> {
  return request(`/api/schedule/context?days=${days}`);
}
export function createSchedule(item: ScheduleInput): Promise<ScheduleItem> {
  return request('/api/schedule', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item),
  });
}
export function updateSchedule(id: string, item: ScheduleInput): Promise<ScheduleItem> {
  return request(`/api/schedule/${encodeURIComponent(id)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item),
  });
}
export function deleteSchedule(id: string): Promise<{ ok: boolean }> {
  return request(`/api/schedule/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

// ---- 创作活动 ----
export type CampaignPlatform = 'x' | 'xiaohongshu' | 'douyin' | 'bilibili' | 'wechat' | 'weixin-channels';
export type CampaignQualification = 'eligible' | 'ineligible' | 'unknown';
export interface CampaignSubmissionSpec {
  formats: string[];
  content_directions: string[];
  style_requirements: string[];
  duration_seconds: { min: number | null; max: number | null };
  aspect_ratios: string[];
  resolutions: string[];
  orientation: 'vertical' | 'horizontal' | 'square' | null;
  image_count: { min: number | null; max: number | null };
  text_length: { min: number | null; max: number | null };
  live: { min_duration_seconds: number | null; required_category: string; title_keywords: string[] };
  original_required: boolean | null;
  first_publish_required: boolean | null;
  exclusive_required: boolean | null;
  min_entries: number | null;
  max_entries: number | null;
  submission_method: string;
  required_mentions: string[];
  required_music: string[];
}

export interface CampaignRuleSnapshot {
  version: number;
  archived_at?: number;
  title?: string;
  platform?: CampaignPlatform;
  platform_label?: string;
  organizer?: string;
  activity_type?: string;
  reward_type?: string;
  reward_summary?: string;
  summary?: string;
  starts_at?: string;
  signup_deadline?: string;
  submit_deadline?: string;
  stats_deadline?: string;
  timezone?: string;
  eligibility?: string[];
  qualification_state?: CampaignQualification;
  content_requirements?: string[];
  prizes?: string[];
  winning_conditions?: string[];
  reward_rules?: string[];
  required_topics?: string[];
  submission_spec?: CampaignSubmissionSpec;
  ai_policy?: string;
  source_url?: string;
  source_status?: string;
  note?: string;
}

export interface Campaign {
  id: string;
  title: string;
  platform: CampaignPlatform;
  platform_label: string;
  organizer: string;
  organizer_type: string;
  activity_type: string;
  reward_type: string;
  reward_summary: string;
  summary: string;
  starts_at: string;
  signup_deadline: string;
  submit_deadline: string;
  stats_deadline: string;
  timezone: string;
  eligibility: string[];
  qualification_state: CampaignQualification;
  qualification_basis?: string;
  content_requirements: string[];
  prizes: string[];
  winning_conditions: string[];
  reward_rules: string[];
  required_topics: string[];
  submission_spec: CampaignSubmissionSpec;
  ai_policy: string;
  source_url: string;
  source_type: string;
  source_status: 'imported' | 'verified' | 'stale' | 'unavailable' | string;
  last_verified_at: number;
  discovered_at?: number;
  last_seen_at?: number;
  note: string;
  status: 'upcoming' | 'active' | 'ended' | 'cancelled' | 'unknown';
  account_id: string;
  saved: boolean;
  rule_version: number;
  rule_history?: CampaignRuleSnapshot[];
  created_at: number;
  updated_at: number;
  external_ids?: Record<string, string>;
  source_evidence?: { provider_id?: string; external_id?: string; source_url?: string; fetched_at?: number; status?: string; evidence?: Record<string, unknown> }[];
  account_states?: Record<string, { visible?: boolean; qualification_state?: CampaignQualification; last_seen_at?: number; provider_id?: string }>;
  rule_evidence_fingerprint?: string;
  last_agent_fingerprint?: string;
  last_agent_enriched_at?: number;
  agent_model?: string;
  agent_run_id?: string;
  enrichment_status?: 'incomplete' | 'running' | 'partial' | 'complete' | 'failed' | string;
  missing_fields?: string[];
  agent_error?: string;
  x_enrichment_version?: number;
  x_enriched_at?: number;
  x_enrichment_model?: string;
  x_enrichment_run_id?: string;
  x_enrichment_status?: 'incomplete' | 'running' | 'partial' | 'complete' | 'failed' | string;
  x_enrichment_error?: string;
  x_enrichment_attempt_count?: number;
  x_enrichment_next_retry_at?: number;
  field_evidence?: Record<string, { source?: string; paths?: string[]; originals?: string[]; note?: string }>;
  xhs_detail_status?: 'not_fetched' | 'parsed' | 'no_structured_rules' | 'needs_visual_review' | 'failed' | string;
  xhs_detail_version?: number;
  xhs_detail_fetched_at?: number;
  xhs_detail_error?: string;
  douyin_listing?: {
    display_starts_at: string; display_ends_at: string; detail_status: string;
    scope_note: string; challenge_ids: string[]; source_order: number; fetched_at: number;
  } | null;
}
export type CampaignInput = Pick<Campaign, 'title' | 'platform'> & Partial<Pick<Campaign,
  'organizer' | 'organizer_type' | 'activity_type' | 'reward_type' | 'reward_summary' | 'summary' |
  'starts_at' | 'signup_deadline' | 'submit_deadline' | 'stats_deadline' | 'timezone' |
  'eligibility' | 'qualification_state' | 'content_requirements' | 'prizes' | 'winning_conditions' | 'reward_rules' |
  'required_topics' | 'submission_spec' | 'ai_policy' | 'source_url' | 'note' | 'status' | 'account_id'>>;
export type CampaignListSort = 'recommend' | 'new' | 'deadline' | 'saved' | 'default' | 'latest';
export interface CampaignPageResponse {
  items: Campaign[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  range_start: number;
  range_end: number;
  sort: CampaignListSort;
  platform: string;
  account_id: string;
  snapshot_id: string;
  fetched_at: string;
  source_status: string;
  source_total: number;
  truncated: boolean;
  missing_source_items: number;
  stats: { active: number; soon: number; saved: number };
  filter_options: { activity_types: string[]; reward_types: string[] };
}
export interface CampaignPageParams {
  platform?: string;
  account_id?: string;
  sort?: CampaignListSort;
  page?: number;
  activity_type?: string;
  reward_type?: string;
  deadline?: string;
  qualification?: string;
  snapshot_id?: string;
}
export interface CampaignSourceSyncState {
  at?: number; last_attempt_at?: number; last_success_at?: number; last_success_count?: number; next_run_at?: number;
  status?: string; count?: number; error?: string; provider?: string; fallback_used?: boolean;
}
export interface CampaignCollectionSchedule {
  mode: 'interval' | 'daily_slots' | 'manual';
  timezone: string;
  times: string[];
  interval_seconds: number;
  next_run_at: number;
  cost: 'paid' | 'free_primary';
  paid_note?: string;
}
export interface CampaignSourceCapability {
  id?: string;
  platform: CampaignPlatform;
  label: string;
  mode: string;
  automatic: boolean;
  detail: string;
  scope_note?: string;
  status?: 'ready' | 'ready_fallback' | 'needs_config' | 'needs_login' | 'stale' | 'error' | 'manual' | string;
  billing?: 'free' | 'paid_or_plan_dependent' | 'free_primary_paid_fallback' | 'unknown' | string;
  cost_note?: string;
  method?: string;
  x_api_account_id?: string;
  fallback_method?: string;
  fallback_enabled?: boolean;
  account_id?: string;
  tikhub_enabled?: boolean;
  tikhub_api_key_set?: boolean;
  last_sync?: CampaignSourceSyncState;
  sync_interval_seconds?: number;
  schedule?: CampaignCollectionSchedule;
  next_sync_at?: number;
}
export interface CampaignSourceState { items: CampaignSourceCapability[]; automatic_count: number; revision?: number; server_now?: number; }
export interface CampaignRefreshResult {
  results: { platform: CampaignPlatform; status: string; count: number; provider?: string; fallback_used?: boolean; error?: string }[];
  sources: CampaignSourceState;
  merged: number;
  total: number;
  stale_platforms: string[];
  campaigns: Campaign[];
}
function normalizeCampaign(item: Campaign): Campaign {
  return {
    ...item,
    summary: item.summary || '',
    eligibility: Array.isArray(item.eligibility) ? item.eligibility : [],
    content_requirements: Array.isArray(item.content_requirements) ? item.content_requirements : [],
    prizes: Array.isArray(item.prizes) ? item.prizes : [],
    winning_conditions: Array.isArray(item.winning_conditions) ? item.winning_conditions : [],
    reward_rules: Array.isArray(item.reward_rules) ? item.reward_rules : [],
    required_topics: Array.isArray(item.required_topics) ? item.required_topics : [],
    submission_spec: item.submission_spec || {
      formats: [], content_directions: [], style_requirements: [], duration_seconds: { min: null, max: null },
      aspect_ratios: [], resolutions: [], orientation: null, image_count: { min: null, max: null },
      text_length: { min: null, max: null }, live: { min_duration_seconds: null, required_category: '', title_keywords: [] },
      original_required: null, first_publish_required: null, exclusive_required: null,
      min_entries: null, max_entries: null, submission_method: '', required_mentions: [], required_music: [],
    },
  };
}
export function fetchCampaigns(): Promise<Campaign[]> {
  return request<Campaign[]>('/api/campaigns').then((items) => items.map(normalizeCampaign));
}
export function fetchCampaignPage(params: CampaignPageParams = {}): Promise<CampaignPageResponse> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '' && value !== 'all') query.set(key, String(value));
  }
  return request<CampaignPageResponse>(`/api/campaigns/page?${query.toString()}`).then((value) => ({
    ...value,
    items: (value.items || []).map(normalizeCampaign),
    stats: value.stats || { active: 0, soon: 0, saved: 0 },
    filter_options: value.filter_options || { activity_types: [], reward_types: [] },
  }));
}
export function fetchCampaign(id: string): Promise<Campaign> {
  return request<Campaign>(`/api/campaigns/${encodeURIComponent(id)}`).then(normalizeCampaign);
}
export function verifyCampaign(id: string): Promise<Campaign> {
  return request<Campaign>(`/api/campaigns/${encodeURIComponent(id)}/verify`, { method: 'POST' }).then(normalizeCampaign);
}
export function refreshXhsCampaignDetail(id: string): Promise<Campaign> {
  return request<Campaign>(`/api/campaigns/${encodeURIComponent(id)}/xhs-detail`, { method: 'POST' }).then(normalizeCampaign);
}
export function enrichCampaign(id: string, force = true): Promise<{ called: boolean; reason: string; changed_fields: string[]; item: Campaign }> {
  return request<{ called: boolean; reason: string; changed_fields: string[]; item: Campaign }>(`/api/campaigns/${encodeURIComponent(id)}/enrich`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ force }),
  }).then((value) => ({ ...value, item: normalizeCampaign(value.item) }));
}
export interface CampaignEnrichmentPreview { platform: string; count: number; items: { id: string; title: string; missing_fields: string[]; reason: string }[]; model: string; ready: boolean; note: string; }
export interface CampaignEnrichmentStatus { status: string; id?: string; total: number; done: number; failed: number; current?: string; items?: string[]; }
export function fetchCampaignEnrichmentPreview(): Promise<CampaignEnrichmentPreview> { return request('/api/campaigns/enrichment/preview'); }
export function runCampaignEnrichment(): Promise<{ started: boolean; reason?: string; status: CampaignEnrichmentStatus }> { return request('/api/campaigns/enrichment/run', { method: 'POST' }); }
export function fetchCampaignEnrichmentStatus(): Promise<CampaignEnrichmentStatus> { return request('/api/campaigns/enrichment/status'); }
export function fetchXCampaignEnrichmentStatus(): Promise<CampaignEnrichmentStatus> { return request('/api/campaigns/x-enrichment/status'); }
export function cancelCampaignEnrichment(): Promise<CampaignEnrichmentStatus> { return request('/api/campaigns/enrichment/cancel', { method: 'POST' }); }
export interface CampaignImportPreviewInput {
  target_platform: CampaignPlatform;
  input_kind: 'url' | 'text';
  url?: string;
  text?: string;
  allow_agent?: boolean;
}
export interface CampaignImportPreviewResult {
  draft: CampaignInput & Partial<Campaign>;
  agent_used: boolean;
  model: string;
  warning: string;
  evidence_fingerprint: string;
  detected_platform?: CampaignPlatform | '';
  field_evidence?: Record<string, unknown>;
}
export function previewCampaignImport(input: CampaignImportPreviewInput): Promise<CampaignImportPreviewResult> {
  return request('/api/campaigns/import/preview', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  });
}
export function fetchCampaignSources(): Promise<CampaignSourceState> { return request('/api/campaigns/sources'); }
export function configureCampaignSource(platform: CampaignPlatform, input: {
  method?: string; x_api_account_id?: string; fallback_method?: string; fallback_enabled?: boolean;
  account_id?: string; tikhub_enabled?: boolean; tikhub_api_key?: string;
}): Promise<CampaignSourceState> {
  return request(`/api/campaigns/sources/${encodeURIComponent(platform)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  });
}
export function refreshCampaigns(platforms?: CampaignPlatform[], force = false, allowPaid = false): Promise<CampaignRefreshResult> {
  return request<CampaignRefreshResult>('/api/campaigns/refresh', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ platforms, force, allow_paid: allowPaid }),
  }).then((result) => ({ ...result, campaigns: result.campaigns.map(normalizeCampaign) }));
}
export function createCampaign(item: CampaignInput): Promise<Campaign> {
  return request('/api/campaigns', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item) });
}
export function updateCampaign(id: string, item: CampaignInput): Promise<Campaign> {
  return request(`/api/campaigns/${encodeURIComponent(id)}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item) });
}
export function saveCampaign(id: string, saved: boolean): Promise<Campaign> {
  return request(`/api/campaigns/${encodeURIComponent(id)}/saved`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ saved }) });
}
export function deleteCampaign(id: string): Promise<{ ok: boolean }> {
  return request(`/api/campaigns/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

// ---- 选题库 ----
export type IdeaStage = 'candidate' | 'saved' | 'developing' | 'review' | 'ready' | 'production' | 'done' | 'rejected' | 'archived';
export interface IdeaPlatformPlan {
  platform: string; angle: string; format: string; hook: string; adaptation: string;
}
export interface IdeaBriefData {
  audience: string; objective: string; core_thesis: string; differentiation: string;
  title_directions: string[]; hook: string;
  outline: { title: string; purpose: string; evidence_needed: string[] }[];
  platform_plans: { platform: string; title: string; hook: string; format: string; adaptation: string }[];
  evidence_checks: string[]; production_tasks: string[]; open_questions: string[]; source_refs: string[];
}
export interface IdeaBrief {
  idea_id: string; revision: number; data: IdeaBriefData; locked_fields: string[];
  source: string; status: string; created_at: string;
}
export interface Idea {
  id: string; run_id?: string; title: string; note: string; source: string; status: string; stage?: IdeaStage;
  persona?: string; created: number; updated?: number;
  angle?: string; reason?: string; campaign_id?: string; campaign_rule_version?: number;
  trend_refs?: string[]; target_platforms?: string[]; requirements?: string[]; pending_checks?: string[];
  platform_plans?: IdeaPlatformPlan[]; source_refs?: string[]; score?: number;
  origin?: 'manual' | 'automatic' | string; opportunity_key?: string; discovered_at?: number;
  validity?: 'active' | 'expired' | string; unread?: boolean; trigger_summary?: string;
  brief_revision?: number; brief_status?: string; content_id?: string; plan_id?: string; brief?: IdeaBrief | null;
}
export type IdeaInput = {
  title: string; note?: string; source?: string; status?: string; stage?: IdeaStage; persona?: string;
  angle?: string; reason?: string; campaign_id?: string; campaign_rule_version?: number;
  trend_refs?: string[]; target_platforms?: string[]; requirements?: string[]; pending_checks?: string[];
  platform_plans?: IdeaPlatformPlan[]; source_refs?: string[]; score?: number;
};
export interface TopicUseContext {
  title: string;
  ideaId?: string;
  contentId?: string;
  brief?: IdeaBriefData;
  autoStart?: boolean;
  angle?: string;
  reason?: string;
  campaignId?: string;
  campaignTitle?: string;
  campaignRuleVersion?: number;
  trendRefs?: string[];
  targetPlatforms?: string[];
  requirements?: string[];
  pendingChecks?: string[];
  campaignRequirements?: string[];
  campaignRequiredTopics?: string[];
  campaignAiPolicy?: string;
  campaignSubmitDeadline?: string;
  campaignSourceUrl?: string;
  campaignQualification?: CampaignQualification;
  campaignCurrentRuleVersion?: number;
  source?: string;
}
export function fetchIdeas(persona = '', includeRejected = false): Promise<Idea[]> {
  const params = new URLSearchParams();
  if (persona) params.set('persona', persona);
  if (includeRejected) params.set('include_rejected', 'true');
  return request('/api/ideas' + (params.size ? '?' + params.toString() : ''));
}
export function fetchIdea(id: string): Promise<{ idea: Idea; campaign: Campaign | null; campaign_rule_snapshot?: CampaignRuleSnapshot | null; brief?: IdeaBrief | null }> { return request('/api/ideas/' + encodeURIComponent(id)); }
export function createIdea(item: IdeaInput): Promise<Idea> {
  return request('/api/ideas', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item) });
}
export function updateIdea(id: string, item: IdeaInput): Promise<Idea> {
  return request('/api/ideas/' + encodeURIComponent(id), { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(item) });
}
export function deleteIdea(id: string): Promise<{ ok: boolean }> {
  return request('/api/ideas/' + encodeURIComponent(id), { method: 'DELETE' });
}

export interface IdeaRecommendation {
  title: string; angle: string; reason: string; score: number;
  platforms: string[]; trend_refs: string[];
  requirements?: string[]; pending_checks?: string[];
  campaign_id?: string; campaign_rule_version?: number;
  platform_plans?: IdeaPlatformPlan[]; source_refs?: string[];
}
export interface IdeaRecommendResponse {
  persona: string; platforms: string[]; generated_at: number; existing_count: number;
  trend_sources?: string[]; target_platforms?: string[];
  campaign?: Pick<Campaign, 'id' | 'title' | 'platform' | 'platform_label' | 'rule_version' | 'submit_deadline' | 'qualification_state' | 'source_status'> | null;
  trend_summary: { platform: string; label: string; status: string; count: number }[];
  recommendations: IdeaRecommendation[];
}
export function recommendIdeas(input: {
  persona: string; platforms?: string[]; trend_sources?: string[]; target_platforms?: string[]; account_ids?: string[];
  campaign_id?: string; limit?: number;
}): Promise<IdeaRecommendResponse> {
  return request('/api/ideas/recommend', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  });
}

export interface IdeaSourceSnapshot {
  id: string; kind: 'trend' | 'campaign' | string; source_id: string; title: string; platform: string;
  url: string; fetched_at: number; version: string; access_scope: string; data: Record<string, unknown>;
}
export interface IdeaRun {
  id: string; kind: 'recommend' | 'develop'; persona: string; target_platforms: string[]; trend_sources: string[];
  origin?: 'manual' | 'automatic' | string; policy_id?: string; opportunity_key?: string;
  status: 'queued' | 'running' | 'waiting_user' | 'partial' | 'succeeded' | 'failed' | 'cancelled' | 'interrupted';
  stage: string; error: string; result: Record<string, unknown>; event_seq: number; cancelled: boolean;
  request: Record<string, unknown>; created_at: string; updated_at: string;
  sources?: IdeaSourceSnapshot[]; ideas?: Idea[];
}
export function createIdeaRun(input: {
  persona: string; target_platforms: string[]; account_ids?: string[]; trend_sources: string[]; trend_titles?: string[];
  include_trends: boolean; include_campaigns: boolean; campaign_ids?: string[];
  instruction?: string; goal?: string; effort_minutes?: number; limit?: number; idempotency_key: string;
}): Promise<IdeaRun> {
  return request('/api/idea-runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) });
}
export function fetchIdeaRuns(limit = 20, persona = '', origin: '' | 'manual' | 'automatic' = 'manual'): Promise<{ items: IdeaRun[] }> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (persona) params.set('persona', persona);
  if (origin) params.set('origin', origin);
  return request('/api/idea-runs?' + params.toString());
}
export function fetchIdeaRun(id: string): Promise<IdeaRun> { return request('/api/idea-runs/' + encodeURIComponent(id)); }
export function cancelIdeaRun(id: string): Promise<IdeaRun> { return request('/api/idea-runs/' + encodeURIComponent(id) + '/cancel', { method: 'POST' }); }
export function feedbackIdea(id: string, action: 'stash' | 'select' | 'reject' | 'reopen', reason = ''): Promise<Idea> {
  return request('/api/ideas/' + encodeURIComponent(id) + '/feedback', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action, reason }),
  });
}
export function markIdeaSeen(id: string): Promise<Idea> {
  return request('/api/ideas/' + encodeURIComponent(id) + '/seen', { method: 'POST' });
}
export function developIdea(id: string, input: { scope?: string; instruction?: string; idempotency_key: string }): Promise<IdeaRun> {
  return request('/api/ideas/' + encodeURIComponent(id) + '/develop', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  });
}
export function fetchIdeaBrief(id: string): Promise<{ idea: Idea; brief: IdeaBrief | null }> { return request('/api/ideas/' + encodeURIComponent(id) + '/brief'); }
export function updateIdeaBrief(id: string, input: { expected_revision: number; data: IdeaBriefData; locked_fields: string[] }): Promise<IdeaBrief> {
  return request('/api/ideas/' + encodeURIComponent(id) + '/brief', {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  });
}
export function confirmIdeaBrief(id: string, expectedRevision: number): Promise<IdeaBrief> {
  return request('/api/ideas/' + encodeURIComponent(id) + '/brief/confirm', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ expected_revision: expectedRevision }),
  });
}
export function startIdeaContent(id: string, expectedRevision: number, idempotencyKey: string): Promise<{ idea: Idea; content: { id: string; version_id: string; content: { title: string } }; created: boolean }> {
  return request('/api/ideas/' + encodeURIComponent(id) + '/start-content', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ expected_revision: expectedRevision, idempotency_key: idempotencyKey }),
  });
}
export function planIdea(id: string, input: { scheduled_local: string; timezone: string; fold?: 0 | 1 | null; idempotency_key: string }): Promise<{ idea: Idea; plan: { id: string; version: number; scheduled_local: string; timezone: string } }> {
  return request('/api/ideas/' + encodeURIComponent(id) + '/plan', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  });
}

export interface IdeaDiscoveryAccount {
  id: string; platform: string; label: string; status: string; live_verified?: boolean;
}
export function fetchIdeaDiscoveryAccounts(): Promise<IdeaDiscoveryAccount[]> {
  return request('/api/ripple/accounts');
}

export interface IdeaDiscoveryPolicy {
  id: string; persona: string; enabled: boolean; revision: number;
  target_platforms: string[]; trend_sources: string[]; account_ids: string[];
  profile_id: string; profile_revision: number; current_profile_revision?: number;
  requires_review?: boolean; review_reason?: string;
  mode: 'balanced' | 'combo_only'; focus_keywords: string[]; effort_minutes: number;
  max_daily_runs: number; max_daily_candidates: number; max_daily_notices: number;
  min_candidate_score: number; timezone: string; quiet_start: string; quiet_end: string;
  important_notifications: boolean; last_scan_at: number; last_daily_review: string;
  last_source_digest: string; source_health: Record<string, unknown>; created_at: string; updated_at: string;
}
export interface IdeaDiscoveryJob {
  id: string; policy_id: string; policy_revision: number; trigger_key: string; trigger_type: string;
  trigger: Record<string, unknown>; opportunity_key: string; source_digest: string;
  status: string; run_id: string; lease_until: number; provider_started: boolean;
  budget_date: string; reserved_runs: number; reserved_candidates: number;
  usage: Record<string, unknown>; error: string; created_at: string; updated_at: string;
}
export interface IdeaDiscoveryState {
  policy: IdeaDiscoveryPolicy | null;
  today: { runs: number; candidates: number; notices: number };
  unread: number; waiting: number; jobs: IdeaDiscoveryJob[];
  source_health: Record<string, unknown>; server_now: number;
}
export type IdeaDiscoveryPolicyInput = {
  persona: string; enabled: boolean; target_platforms: string[]; trend_sources: string[]; account_ids?: string[];
  mode: 'balanced' | 'combo_only'; focus_keywords: string[]; effort_minutes: number;
  max_daily_runs: number; max_daily_candidates: number; max_daily_notices: number; min_candidate_score?: number;
  timezone: string; quiet_start: string; quiet_end: string; important_notifications: boolean;
};
export function fetchIdeaDiscovery(persona: string): Promise<IdeaDiscoveryState> {
  return request('/api/idea-discovery?persona=' + encodeURIComponent(persona));
}
export function saveIdeaDiscoveryPolicy(input: IdeaDiscoveryPolicyInput): Promise<IdeaDiscoveryState> {
  return request('/api/idea-discovery/policy', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input),
  });
}

// ---- AI / Agent Providers ----
export type AIRoutePurpose = 'default_agent' | 'idea_generation' | 'research' | 'x_campaign_discovery';
export interface AIProviderModel { id: string; name: string; effort_levels?: string[]; source?: string; supports_reasoning?: boolean; }
export interface AIProvider {
  id: string; name: string; kind: 'openai-compatible' | 'xai'; base_url: string;
  models: AIProviderModel[]; default_model: string; enabled: boolean; api_key_set: boolean;
  capabilities: Record<string, string>; migrated_from_legacy?: boolean; updated_at?: string;
}
export interface AIProviderState {
  schema: number; revision: number; providers: AIProvider[];
  routes: Partial<Record<AIRoutePurpose, { provider_id: string; model_id: string }>>;
  purposes: { id: AIRoutePurpose; label: string }[];
}
export function fetchAIProviders(): Promise<AIProviderState> { return request('/api/ai-providers'); }
export function saveAIProvider(input: {
  provider_id?: string; name: string; kind: 'openai-compatible' | 'xai'; base_url: string; api_key?: string;
  models: (AIProviderModel | string)[]; default_model: string; enabled?: boolean;
}): Promise<AIProviderState> {
  return request('/api/ai-providers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) });
}
export function deleteAIProvider(id: string): Promise<AIProviderState> {
  return request(`/api/ai-providers/${encodeURIComponent(id)}`, { method: 'DELETE' });
}
export function discoverAIProviderModels(input: {
  provider_id?: string; kind?: 'openai-compatible' | 'xai'; base_url?: string; api_key?: string;
}): Promise<{ items: AIProviderModel[] }> {
  return request('/api/ai-providers/discover', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(input) });
}
export function setAIProviderRoute(purpose: AIRoutePurpose, provider_id: string, model_id: string): Promise<AIProviderState> {
  return request(`/api/ai-providers/routes/${encodeURIComponent(purpose)}`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ provider_id, model_id }),
  });
}
export function probeAIProvider(id: string, model_id: string, capability: 'chat' | 'x_search' | 'web_search'): Promise<{ ok: boolean; capability: string; state: AIProviderState }> {
  return request(`/api/ai-providers/${encodeURIComponent(id)}/probe`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model_id, capability }),
  });
}

export function fetchSkills(): Promise<SkillItem[]> {
  return request<SkillItem[]>('/api/skills');
}

export function fetchSkillDetail(name: string): Promise<SkillDetail> {
  return request<SkillDetail>(`/api/skill/${encodeURIComponent(name)}`);
}

/** 保存 API key 到项目根 .env（仅注册表内变量）。返回各需 API 的 skill 是否已就绪。 */
export function saveEnv(updates: Record<string, string>): Promise<{ ok: boolean; skills: Record<string, boolean> }> {
  return request('/api/env', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ updates }),
  });
}

export function executeSkill(skill: string, input: string, persona?: string): Promise<SkillResponse> {
  return request<SkillResponse>('/api/skill', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ skill, input, persona: persona || undefined }),
  });
}

/** 一次性 agent 任务（返回整段 markdown）——用于一稿多改 / 爆款拆解 / 发布预检等工具型调用。 */
export function runAgent(message: string, persona?: string): Promise<{ response: string }> {
  return request('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message, persona: persona || undefined }),
  });
}

/** 从表单构建画像（首次引导）。后端写基线 + agent 分析社媒链接增强。 */
export function buildProfile(name: string, form: Record<string, unknown>): Promise<ProfileBuildResponse> {
  return request<ProfileBuildResponse>('/api/profile/build', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, form }),
  });
}

/** 轮询画像 AI 增强进度（构建异步化后用）。 */
export function profileBuildStatus(name: string): Promise<ProfileBuildStatus> {
  return request<ProfileBuildStatus>(`/api/profile/build/status/${encodeURIComponent(name)}`);
}

export function fetchOutputs(): Promise<OutputNode[]> {
  return request<OutputNode[]>('/api/outputs');
}

export function fetchOutputContent(path: string): Promise<OutputContent> {
  return request<OutputContent>(`/api/output/${path.split('/').map(encodeURIComponent).join('/')}`);
}

/** 媒体文件（图片/视频/音频/HTML/PDF）的原样 URL，用于 <img>/<video>/iframe/下载。 */
export function mediaUrl(path: string): string {
  return `${BASE}/api/media/${path.split('/').map(encodeURIComponent).join('/')}`;
}

/** 删除内容库里的文件或整个项目目录（系统数据受保护，后端会拒）。 */
export function deleteOutput(path: string, confirmed: boolean): Promise<{ ok: boolean; deleted: string }> {
  return request(`/api/output/${path.split('/').map(encodeURIComponent).join('/')}`, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ confirmed }) });
}

export interface UploadedFile { id: string; name: string; path: string; }
/** 上传素材到当前会话的隔离 inbox，返回后端可校验的附件引用。 */
export async function uploadFiles(files: File[], sessionId: string): Promise<UploadedFile[]> {
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('sessionId', sessionId);
  const r = await request<{ ok: boolean; files: UploadedFile[] }>('/api/upload', { method: 'POST', body: fd });
  return r.files;
}

export function deleteSession(sessionKey: string): Promise<{ deleted: boolean }> {
  return request<{ deleted: boolean }>(`/api/session/${encodeURIComponent(sessionKey)}`, {
    method: 'DELETE',
  });
}

// ---- 账号登录 ----
export interface AccountItem {
  platform: string;
  name: string;
  backend: string;      // xhs | web | biliup | unsupported
  supported: boolean;
  loggedIn: boolean;
  note: string;
}

export interface LoginStart {
  mode: 'qr' | 'terminal';
  state?: string;       // starting | qr_ready | success | expired | error | unknown
  message?: string;
  qr?: string;          // outputs 下相对路径，用 mediaUrl() 取图
}

export interface LoginStatus {
  mode: 'qr';
  state: string;
  message: string;
  qr: string;
  qrTs?: number;
}

export function fetchAccounts(): Promise<AccountItem[]> {
  return request<AccountItem[]>('/api/accounts');
}

export interface AccountWhoami {
  loggedIn: boolean;
  name: string;
  avatar: string;   // 头像 URL（http）或空
}

/** 真校验某平台登录态 + 拉昵称/头像（后端起 headless 浏览器，数秒）。 */
export function accountWhoami(platform: string): Promise<AccountWhoami> {
  return request<AccountWhoami>(`/api/accounts/${encodeURIComponent(platform)}/whoami`);
}

/** 退出登录：删该平台持久化登录态。 */
export function logoutAccount(platform: string): Promise<{ ok: boolean; deleted: string[] }> {
  return request(`/api/logout/${encodeURIComponent(platform)}`, { method: 'POST' });
}

export interface PublishResult {
  ok?: boolean;
  message: string;
  detail?: string;
  async?: boolean;   // true = 异步发布（抖音，可能触发短信验证），需轮询 publishStatus
  pending?: boolean;
}

/** 一键发布到某平台（真发布，--exec）。media 为 outputs 相对路径数组。
 * 抖音返回 {async:true}，需轮询 publishStatus；其他平台同步返回结果。 */
export function publishNow(
  platform: string,
  payload: { title: string; body: string; media: string[]; tags?: string },
): Promise<PublishResult> {
  return request<PublishResult>(`/api/publish/${encodeURIComponent(platform)}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  });
}

export interface PublishStatus {
  mode: 'publish';
  state: string;     // starting | sms_required | verifying | success | error | unknown
  message: string;
}

/** 轮询异步发布状态（抖音）。 */
export function publishStatus(platform: string): Promise<PublishStatus> {
  return request<PublishStatus>(`/api/publish/${encodeURIComponent(platform)}/status`);
}

/** 发布触发短信墙时回填验证码。 */
export function submitPublishSms(platform: string, code: string): Promise<{ ok: boolean }> {
  return request(`/api/publish/${encodeURIComponent(platform)}/sms`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
}

export function startLogin(platform: string): Promise<LoginStart> {
  return request<LoginStart>(`/api/login/${encodeURIComponent(platform)}`, { method: 'POST' });
}

export function loginStatus(platform: string): Promise<LoginStatus> {
  return request<LoginStatus>(`/api/login/${encodeURIComponent(platform)}/status`);
}

// ---- 归因层：账号创作数据 ----
export interface AnalyticsPlatform {
  platform: string;
  name: string;
  loggedIn: boolean;
}

export interface AccountAnalytics {
  platform: string;
  name: string;
  nickname: string;
  loggedIn: boolean;
  followers: number | null;
  likes: number | null;
  following: number | null;
  posts: number | null;
  metrics: { label: string; value: string; vs: string }[];
  notes: { title: string; url: string; cover?: string; stat?: string }[];
  growth: Record<'last' | 'day' | 'week' | 'month' | 'year',
    { followers: number | null; likes: number | null; posts: number | null; since_days: number | null } | null>;
  fetched_at: number;
}

/** 支持抓数据的平台 + 各自登录态。 */
export function fetchAnalyticsPlatforms(): Promise<AnalyticsPlatform[]> {
  return request<AnalyticsPlatform[]>('/api/analytics/platforms');
}

/** 抓某平台已登录账号的创作数据（后端起 headless 浏览器，数秒）。 */
export function fetchAccountAnalytics(platform: string): Promise<AccountAnalytics> {
  return request<AccountAnalytics>(`/api/analytics/${encodeURIComponent(platform)}`);
}

/** 回填短信验证码（登录风控短信墙）：提交后 runner 读走填码继续登录。 */
export function submitLoginSms(platform: string, code: string): Promise<{ ok: boolean }> {
  return request(`/api/login/${encodeURIComponent(platform)}/sms`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
}

/** 用户显式停止：终止后端正在跑的对话 agent，释放会话锁，令下一句能立刻发。 */
export function stopChat(sessionId: string): Promise<{ stopped: boolean }> {
  return request('/api/chat/stop', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ sessionId }),
  });
}

export interface ChatArtifactRef {
  kind: 'content_draft'; id: string; version_id: string; title: string; status: 'draft';
}

export interface AgentTurnInjection { skills?: string[]; }
export interface ChatContentScope {
  profileId: string;
  profileRevision: number;
  accountId: string;
  bindingRevision: number;
  overrides: Record<string, unknown>;
}

function parseChatArtifact(data: string): ChatArtifactRef | null {
  try {
    let value: unknown = JSON.parse(data);
    if (typeof value === 'string') value = JSON.parse(value);
    if (!value || typeof value !== 'object') return null;
    const row = value as Record<string, unknown>;
    if (row.kind !== 'content_draft' || typeof row.id !== 'string' || !/^[a-f0-9]{32}$/.test(row.id)
        || typeof row.version_id !== 'string' || !/^[a-f0-9]{64}$/.test(row.version_id)) return null;
    return { kind: 'content_draft', id: row.id, version_id: row.version_id,
      title: typeof row.title === 'string' ? row.title.slice(0, 200) : '', status: 'draft' };
  } catch { return null; }
}

export function streamChat(
  message: string,
  persona: string | undefined,
  sessionId: string,
  onToken: (chunk: string) => void,
  onDone: (sessionKey?: string) => void,
  onError: (err: Error) => void,
  onThinking?: (chunk: string) => void,
  onActivity?: (status: string) => void,
  onInterrupted?: () => void,
  turnId?: string,
  resumeOnly = false,
  onRecoveryUnavailable?: () => void,
  attachments: UploadedFile[] = [],
  onArtifact?: (artifact: ChatArtifactRef) => void,
  injection: AgentTurnInjection = {},
  contentScope?: ChatContentScope,
): AbortController {
  const controller = new AbortController();
  let lastEventId = 0;

  const consume = async (res: Response): Promise<boolean> => {
      if (!res.ok) {
        let detail = `Stream error: ${res.status}`;
        try {
          const payload = await res.json() as { detail?: string };
          if (payload.detail) detail = payload.detail;
        } catch { /* non-JSON error */ }
        const error = new Error(detail) as Error & { status?: number };
        error.status = res.status;
        throw error;
      }
      const reader = res.body?.getReader();
      if (!reader) throw new Error('No response body');

      const decoder = new TextDecoder();
      let buffer = '';
      let currentEvent = '';
      let currentId = 0;
      let dataLines: string[] = [];

      const dispatch = () => {
        if (!currentEvent || dataLines.length === 0) return false;
        if (currentId > lastEventId) lastEventId = currentId;
        const data = dataLines.join('\n');
        if (currentEvent === 'token') {
          try { onToken(JSON.parse(data) as string); } catch { onToken(data); }
        } else if (currentEvent === 'thinking' && onThinking) {
          try { onThinking(JSON.parse(data) as string); } catch { onThinking(data); }
        } else if (currentEvent === 'activity' && onActivity) {
          try { onActivity(JSON.parse(data) as string); } catch { onActivity(data); }
        } else if (currentEvent === 'artifact' && onArtifact) {
          const artifact = parseChatArtifact(data);
          if (artifact) onArtifact(artifact);
        } else if (currentEvent === 'error') {
          let msg = '执行失败';
          try { msg = JSON.parse(data) as string; } catch { msg = data; }
          onError(new Error(msg));
          return true;
        } else if (currentEvent === 'done') {
          try { onDone((JSON.parse(data) as { sessionKey?: string }).sessionKey); } catch { onDone(); }
          return true;
        }
        return false;
      };

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split(/\r?\n/);
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line === '') {
            if (dispatch()) return true;
            currentEvent = ''; currentId = 0; dataLines = [];
          } else if (line.startsWith('id:')) currentId = Number(line.slice(3).trim()) || 0;
          else if (line.startsWith('event:')) currentEvent = line.slice(6).trim();
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
        }
      }
      buffer += decoder.decode();
      if (buffer) {
        for (const line of buffer.split(/\r?\n/)) {
          if (line.startsWith('id:')) currentId = Number(line.slice(3).trim()) || 0;
          else if (line.startsWith('event:')) currentEvent = line.slice(6).trim();
          else if (line.startsWith('data:')) dataLines.push(line.slice(5).replace(/^ /, ''));
        }
      }
      return dispatch();
  };

  void (async () => {
    const started = Date.now();
    let first = !resumeOnly;
    while (!controller.signal.aborted) {
      try {
        const res = first
          ? await fetch(`${BASE}/api/chat/stream`, securedFetchOptions({
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                message, persona: persona || undefined, sessionId, turnId, attachments, turnSkills: injection.skills || [],
                ...(contentScope ? {
                  contentProfileId: contentScope.profileId,
                  contentProfileRevision: contentScope.profileRevision,
                  contentAccountId: contentScope.accountId,
                  bindingRevision: contentScope.bindingRevision,
                  accountOverrides: contentScope.overrides || {},
                } : {}),
              }),
              signal: controller.signal,
            }))
          : await fetch(`${BASE}/api/chat/jobs/${encodeURIComponent(turnId || '')}/stream?after=${lastEventId}`, securedFetchOptions({
              signal: controller.signal,
            }));
        if (!first && res.status === 404 && onRecoveryUnavailable) {
          onRecoveryUnavailable();
          return;
        }
        if (await consume(res)) return;
      } catch (err: unknown) {
        if (err instanceof Error && err.name === 'AbortError') return;
        const status = (err as Error & { status?: number })?.status;
        if (first && status && status >= 400 && status < 500) {
          onError(err instanceof Error ? err : new Error('请求失败'));
          return;
        }
      }
      first = false;
      // Backend chat runs may spend 5 minutes waiting for a session lock and then
      // run for 2 hours. Keep reconnecting for the same end-to-end budget.
      if (!turnId || Date.now() - started >= 130 * 60 * 1000) {
        onError(new Error('连接中断，自动重连超时'));
        return;
      }
      onInterrupted?.();
      await new Promise((resolve) => setTimeout(resolve, 1000));
    }
  })();

  return controller;
}

/** 取某会话最近一轮的完整结果（SSE 断线后据此取回）。 */
export function fetchLastTurn(sessionId: string, turnId?: string): Promise<{ status: string; text: string; turn_id?: string; artifacts?: ChatArtifactRef[] }> {
  const query = turnId ? `?turn_id=${encodeURIComponent(turnId)}` : '';
  return request(`/api/chat/last/${encodeURIComponent(sessionId)}${query}`);
}
