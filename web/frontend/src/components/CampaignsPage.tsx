import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  cancelCampaignEnrichment, configureCampaignSource, createCampaign, createIdea, createSchedule, enrichCampaign,
  fetchCampaignEnrichmentStatus, fetchCampaignPage, fetchCampaignSources,
  fetchXCampaignEnrichmentStatus,
  fetchStatus, fetchTrends, previewCampaignImport, recommendIdeas, refreshCampaigns, refreshXhsCampaignDetail,
  saveCampaign, updateCampaign, verifyCampaign,
} from '../lib/api';
import type {
  Campaign, CampaignEnrichmentStatus, CampaignInput, CampaignListSort, CampaignPageResponse, CampaignPlatform,
  CampaignSourceCapability, CampaignSubmissionSpec, IdeaRecommendation, IdeaRecommendResponse,
  PersonaItem, TopicUseContext, TrendGroup,
} from '../lib/api';
import { loadTrendSelection } from '../lib/trendPrefs';
import { api as rippleApi } from '../lib/ripple';
import type { Account } from '../lib/ripple';
import {
  IconBookmark, IconCalendar, IconCheck, IconCompass, IconIdea, IconRefresh, IconSkills,
} from './icons';
import { PlatformBadge, PlatformIcon } from './PlatformBrand';
import { platformDisplayName } from '../lib/platforms';

interface CampaignsPageProps {
  onUseTopic: (input: string | TopicUseContext) => void;
  persona: string;
  aiReady: boolean;
  personas: PersonaItem[];
  onPersonaChange: (name: string) => void;
  onNewPersona: () => void;
  onOpenSettings: () => void;
}

const PLATFORMS: { key: CampaignPlatform; label: string }[] = [
  { key: 'x', label: 'X' },
  { key: 'xiaohongshu', label: '小红书' },
  { key: 'douyin', label: '抖音' },
  { key: 'bilibili', label: 'B站' },
  { key: 'wechat', label: '微信公众号' },
  { key: 'weixin-channels', label: '微信视频号' },
];

const STATUS: Record<string, string> = {
  active: '进行中',
  upcoming: '即将开始',
  ended: '已结束',
  cancelled: '已取消',
  unknown: '状态未知',
};

const SOURCE_STATUS: Record<string, string> = {
  imported: '未核验',
  verified: '已核验',
  model_reported: '模型检索',
  stale: '待复核',
  unavailable: '来源不可用',
};

const SOURCE_HEALTH: Record<string, string> = {
  ready: '自动同步', ready_fallback: '备用源可用', needs_config: '需要配置',
  needs_login: '需要登录', stale: '缓存/待复核', error: '读取异常', manual: '支持导入',
};

type SourceDraft = {
  method: string; x_api_account_id: string; fallback_method: string; fallback_enabled: boolean;
  account_id: string; tikhub_enabled: boolean; tikhub_api_key: string;
};

const emptySubmissionSpec = (): CampaignSubmissionSpec => ({
  formats: [], content_directions: [], style_requirements: [], duration_seconds: { min: null, max: null },
  aspect_ratios: [], resolutions: [], orientation: null, image_count: { min: null, max: null },
  text_length: { min: null, max: null }, live: { min_duration_seconds: null, required_category: '', title_keywords: [] },
  original_required: null, first_publish_required: null, exclusive_required: null,
  min_entries: null, max_entries: null, submission_method: '', required_mentions: [], required_music: [],
});

const FORMAT_LABELS: Record<string, string> = {
  video: '视频', short_video: '短视频', long_video: '长视频', image_text: '图文', text: '文字',
  image: '图片', live: '直播', audio: '音频', any: '不限形式',
};
const MISSING_LABELS: Record<string, string> = { eligibility: '参与条件', submission_spec: '参赛作品', prizes: '奖品', winning_conditions: '获奖条件' };

const emptyCampaign = (): CampaignInput => ({
  title: '',
  platform: 'xiaohongshu',
  organizer: '',
  organizer_type: 'unknown',
  activity_type: '征稿/活动',
  reward_type: '',
  reward_summary: '',
  summary: '',
  starts_at: '',
  signup_deadline: '',
  submit_deadline: '',
  stats_deadline: '',
  timezone: '',
  eligibility: [],
  qualification_state: 'unknown',
  content_requirements: [],
  prizes: [],
  winning_conditions: [],
  reward_rules: [],
  required_topics: [],
  submission_spec: emptySubmissionSpec(),
  ai_policy: 'unknown',
  source_url: '',
  note: '',
  status: 'unknown',
  account_id: '',
});

function splitLines(value: string): string[] {
  return value.split(/\r?\n|，|,/).map((x) => x.trim()).filter(Boolean);
}

function joined(values?: string[]): string {
  return (values || []).join('\n');
}

function dateOnly(value?: string): string {
  return (value || '').slice(0, 10);
}

function daysUntil(value?: string): number | null {
  const target = dateOnly(value);
  if (!/^\d{4}-\d{2}-\d{2}$/.test(target)) return null;
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  const end = new Date(`${target}T00:00:00`).getTime();
  if (!Number.isFinite(end)) return null;
  return Math.round((end - start) / 86400000);
}

function sourceLabel(campaign: Campaign): string {
  return SOURCE_STATUS[campaign.source_status] || campaign.source_status || '来源未知';
}

function sourceVerificationLabel(campaign: Campaign): string {
  const label = sourceLabel(campaign);
  return campaign.platform === 'xiaohongshu' && campaign.source_status === 'verified' ? '列表已同步' : label;
}

function sourceName(campaign: Campaign): string {
  if (campaign.source_type === 'user_import') return '用户导入';
  if (campaign.platform === 'bilibili' && campaign.source_type === 'platform_public') return 'B站官方活动页';
  if (campaign.source_type === 'x_model_prompt') return 'Grok / 搜索模型';
  if (campaign.source_type === 'x_search') return 'xAI · X Search';
  if (campaign.source_type === 'x_developer_api') return 'X Developer API';
  if (campaign.platform === 'xiaohongshu' && campaign.source_type.includes('creator')) return '小红书创作服务平台';
  if (campaign.platform === 'douyin' && campaign.source_type.includes('creator')) return '抖音创作者中心';
  if (campaign.source_type === 'third_party_api') return 'TikHub';
  if (campaign.source_type === 'wechat_public_official') return '微信官方公开规则';
  return (campaign.platform_label || platformDisplayName(campaign.platform)) + '活动来源';
}

function qualificationBadge(campaign: Campaign): { label: string; cls: string } | null {
  if (campaign.qualification_state === 'eligible') return { label: '可参与', cls: 'ok' };
  if (campaign.qualification_state === 'ineligible') return { label: '暂不符合', cls: 'bad' };
  if (campaign.eligibility?.length) return { label: '资格待核验', cls: 'warn' };
  return null;
}

function xhsDetailStatusText(campaign: Campaign): string {
  if (campaign.platform !== 'xiaohongshu') return '';
  if (campaign.xhs_detail_status === 'parsed') return '详情规则已读取';
  if (campaign.xhs_detail_status === 'no_structured_rules') return '详情已读取 · 未发现结构化规则';
  if (campaign.xhs_detail_status === 'needs_visual_review') return '详情已读取 · 图片物料待识别';
  if (campaign.xhs_detail_status === 'failed') return '详情读取失败';
  return '详情待读取';
}

function concise(values: string[] | undefined, fallback: string, limit = 2): string {
  const rows = (values || []).map((x) => x.trim()).filter(Boolean);
  return rows.length ? rows.slice(0, limit).join('；') + (rows.length > limit ? ' 等 ' + rows.length + ' 条' : '') : fallback;
}

function isLongTermProgram(campaign: Campaign): boolean {
  return campaign.source_type === 'wechat_public_official' || /长期.*(?:计划|变现)/.test(campaign.activity_type || '');
}

function campaignTime(campaign: Campaign): string {
  if (isLongTermProgram(campaign) && !dateOnly(campaign.starts_at) && !dateOnly(campaign.submit_deadline)) return '长期计划 · 未公布截止日期';
  const start = dateOnly(campaign.starts_at) || '开始时间未说明';
  const end = dateOnly(campaign.submit_deadline) || '截止时间未说明';
  const left = daysUntil(campaign.submit_deadline);
  const countdown = left !== null && left >= 0 && left <= 7 ? ' · 剩 ' + left + ' 天' : '';
  return start + ' → ' + end + countdown;
}

function syncIntervalLabel(seconds?: number): string {
  if (!seconds) return '';
  if (seconds % 3600 === 0) return `${seconds / 3600} 小时`;
  return `${Math.round(seconds / 60)} 分钟`;
}

function formatSeconds(value: number | null | undefined): string {
  if (value == null) return '';
  return value >= 60 && value % 60 === 0 ? `${value / 60} 分钟` : `${value} 秒`;
}

function submissionSummary(campaign: Campaign): string {
  const spec = campaign.submission_spec || emptySubmissionSpec();
  const formats = (spec.formats || []).map((key) => FORMAT_LABELS[key] || key).join(' / ');
  const min = formatSeconds(spec.duration_seconds?.min); const max = formatSeconds(spec.duration_seconds?.max);
  const duration = min && max ? `${min}～${max}` : min ? `≥ ${min}` : max ? `≤ ${max}` : '';
  const primary = [formats, duration].filter(Boolean).join(' · ');
  return primary || spec.submission_method || spec.content_directions?.[0] || '';
}

function hasSubmissionSpec(campaign: Campaign): boolean {
  const spec = campaign.submission_spec || emptySubmissionSpec();
  return Boolean(
    spec.formats.length || spec.content_directions.length || spec.style_requirements.length
    || spec.duration_seconds.min != null || spec.duration_seconds.max != null
    || spec.aspect_ratios.length || spec.resolutions.length || spec.orientation
    || spec.image_count.min != null || spec.image_count.max != null
    || spec.text_length.min != null || spec.text_length.max != null
    || spec.live.min_duration_seconds != null || spec.live.required_category || spec.live.title_keywords.length
    || spec.original_required != null || spec.first_publish_required != null || spec.exclusive_required != null
    || spec.min_entries != null || spec.max_entries != null || spec.submission_method
    || spec.required_mentions.length || spec.required_music.length
  );
}

function formatCountdown(seconds: number): string {
  if (seconds <= 0) return '即将自动检查';
  const h = Math.floor(seconds / 3600); const m = Math.floor((seconds % 3600) / 60); const s = seconds % 60;
  return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}` : `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function platformLabel(key: string): string {
  return platformDisplayName(key);
}

function isNetworkError(error: unknown): boolean {
  if (error instanceof TypeError) return true;
  const message = error instanceof Error ? error.message : String(error || '');
  return /failed to fetch|networkerror|load failed|network request failed/i.test(message);
}

function paginationItems(page: number, totalPages: number): Array<number | 'ellipsis'> {
  if (totalPages <= 7) return Array.from({ length: totalPages }, (_, index) => index + 1);
  const values = new Set<number>([1, totalPages, page - 1, page, page + 1]);
  if (page <= 4) [2, 3, 4, 5].forEach((value) => values.add(value));
  if (page >= totalPages - 3) [totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1].forEach((value) => values.add(value));
  const sorted = Array.from(values).filter((value) => value >= 1 && value <= totalPages).sort((a, b) => a - b);
  const out: Array<number | 'ellipsis'> = [];
  sorted.forEach((value, index) => {
    if (index && value - sorted[index - 1] > 1) out.push('ellipsis');
    out.push(value);
  });
  return out;
}

export default function CampaignsPage({
  onUseTopic, persona, aiReady, personas, onPersonaChange, onNewPersona, onOpenSettings,
}: CampaignsPageProps) {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [pageData, setPageData] = useState<CampaignPageResponse | null>(null);
  const [page, setPage] = useState(1);
  const [snapshotChanged, setSnapshotChanged] = useState(false);
  const [sources, setSources] = useState<CampaignSourceCapability[]>([]);
  const [automaticCount, setAutomaticCount] = useState(0);
  const [trendGroups, setTrendGroups] = useState<TrendGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [campaignsLoaded, setCampaignsLoaded] = useState(false);
  const [sourcesLoaded, setSourcesLoaded] = useState(false);
  const [connectionIssue, setConnectionIssue] = useState('');
  const [reconnecting, setReconnecting] = useState(false);
  const [error, setError] = useState('');
  const [toast, setToast] = useState('');
  const [selected, setSelected] = useState<Campaign | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [refreshingPlatforms, setRefreshingPlatforms] = useState<string[]>([]);
  const refreshInFlightRef = useRef(new Set<string>());
  const currentViewRef = useRef('');
  const [verifying, setVerifying] = useState(false);
  const [xhsDetailRefreshing, setXhsDetailRefreshing] = useState('');
  const [enrichingId, setEnrichingId] = useState('');
  const [enrichmentStatus, setEnrichmentStatus] = useState<CampaignEnrichmentStatus | null>(null);
  const [xEnrichmentStatus, setXEnrichmentStatus] = useState<CampaignEnrichmentStatus | null>(null);
  const enrichmentPhase = enrichmentStatus?.status || '';
  const agentBatchActive = ['running', 'queued', 'cancelling'].includes(enrichmentPhase);
  const xEnrichmentPhase = xEnrichmentStatus?.status || '';
  const xBatchActive = ['running', 'queued'].includes(xEnrichmentPhase);
  const [serverOffset, setServerOffset] = useState(0);
  const [clock, setClock] = useState(() => Math.floor(Date.now() / 1000));
  const [sourceEditor, setSourceEditor] = useState<CampaignSourceCapability | null>(null);
  const [sourceDraft, setSourceDraft] = useState<SourceDraft>({
    method: '', x_api_account_id: '', fallback_method: '', fallback_enabled: false,
    account_id: '', tikhub_enabled: false, tikhub_api_key: '',
  });
  const [sourceSaving, setSourceSaving] = useState(false);
  const loadRef = useRef<(initial?: boolean) => Promise<void>>(async () => undefined);
  const loadRequestRef = useRef(0);
  const snapshotIdRef = useRef('');
  const retryTimerRef = useRef<number | null>(null);
  const retryAttemptRef = useRef(0);
  const campaignsLoadedRef = useRef(false);
  const sourcesLoadedRef = useRef(false);

  const [platformFilter, setPlatformFilter] = useState('all');
  const [typeFilter, setTypeFilter] = useState('all');
  const [rewardFilter, setRewardFilter] = useState('all');
  const [deadlineFilter, setDeadlineFilter] = useState('all');
  const [qualificationFilter, setQualificationFilter] = useState('all');
  const [accountFilter, setAccountFilter] = useState('all');
  const [sortMode, setSortMode] = useState<CampaignListSort>('recommend');
  const currentSource = sources.find((source) => source.platform === platformFilter);
  const refreshTargets = sources.filter((source) => source.automatic && (platformFilter === 'all' || source.platform === platformFilter)).map((source) => source.platform);
  const refreshing = refreshTargets.some((platform) => refreshingPlatforms.includes(platform));
  const canRefresh = refreshTargets.length > 0;
  const refreshLabel = platformFilter === 'all' ? '刷新全部活动' : `刷新${platformLabel(platformFilter)}活动`;
  const paidRefresh = sources.some((source) => refreshTargets.includes(source.platform) && (source.schedule?.cost === 'paid' || source.platform === 'x' || source.status === 'ready_fallback'));
  const refreshHint = paidRefresh ? '当前范围包含收费来源，手动刷新会额外消耗 Token / 接口额度。' : '仅刷新当前范围的免费来源，不额外启动付费 Agent 或收费备用。';
  const viewKey = JSON.stringify([platformFilter, accountFilter, sortMode, page, typeFilter, rewardFilter, deadlineFilter, qualificationFilter]);
  currentViewRef.current = viewKey;

  const [form, setForm] = useState<CampaignInput | null>(null);
  const [editId, setEditId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [eligibilityText, setEligibilityText] = useState('');
  const [requirementsText, setRequirementsText] = useState('');
  const [prizesText, setPrizesText] = useState('');
  const [winningConditionsText, setWinningConditionsText] = useState('');
  const [rewardRulesText, setRewardRulesText] = useState('');
  const [topicsText, setTopicsText] = useState('');
  const [importUrlOpen, setImportUrlOpen] = useState(false);
  const [importUrl, setImportUrl] = useState('');
  const [importParsing, setImportParsing] = useState(false);
  const [importWarning, setImportWarning] = useState('');
  const [importPlatform, setImportPlatform] = useState<CampaignPlatform>('bilibili');
  const [importKind, setImportKind] = useState<'url' | 'text'>('url');
  const [importText, setImportText] = useState('');
  const [importUseAgent, setImportUseAgent] = useState(false);

  const [recommendOpen, setRecommendOpen] = useState(false);
  const [recommending, setRecommending] = useState(false);
  const [recommendError, setRecommendError] = useState('');
  const [recommendResult, setRecommendResult] = useState<IdeaRecommendResponse | null>(null);
  const [recommendCampaign, setRecommendCampaign] = useState<Campaign | null>(null);
  const [added, setAdded] = useState<Set<string>>(new Set());

  const showToast = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(''), 2300);
  };

  const clearRetry = useCallback(() => {
    if (retryTimerRef.current != null) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    retryAttemptRef.current = 0;
  }, []);

  const scheduleRetry = useCallback(() => {
    if (retryTimerRef.current != null) return;
    const delays = [1000, 2000, 5000];
    const delay = delays[Math.min(retryAttemptRef.current, delays.length - 1)];
    retryAttemptRef.current += 1;
    retryTimerRef.current = window.setTimeout(() => {
      retryTimerRef.current = null;
      void loadRef.current(false);
    }, delay);
  }, []);

  const markConnectionFailure = useCallback(() => {
    const hasSnapshot = campaignsLoadedRef.current || sourcesLoadedRef.current;
    setReconnecting(true);
    setConnectionIssue(hasSnapshot
      ? 'Ripple 后端正在重新连接，当前保留已成功读取的数据。'
      : '活动数据暂时无法读取，正在自动重新连接 Ripple 后端…');
    scheduleRetry();
  }, [scheduleRetry]);

  const load = useCallback(async (initial = false) => {
    const requestId = ++loadRequestRef.current;
    if (initial && !campaignsLoadedRef.current && !sourcesLoadedRef.current) setLoading(true);
    const listSort: CampaignListSort = platformFilter === 'xiaohongshu'
      ? (sortMode === 'latest' ? 'latest' : 'default')
      : (['recommend', 'new', 'deadline', 'saved'].includes(sortMode) ? sortMode as CampaignListSort : 'recommend');
    try {
      let runtime;
      try {
        runtime = await fetchStatus();
      } catch {
        if (requestId === loadRequestRef.current) markConnectionFailure();
        return;
      }
      if (!runtime.features?.campaigns || !runtime.features?.campaign_sources_v2) {
        if (requestId === loadRequestRef.current) {
          setReconnecting(false);
          setConnectionIssue('当前运行中的 Ripple 后端版本较旧，尚未加载活动中心 API。请重启 Ripple 服务后再试。');
          clearRetry();
        }
        return;
      }

      const [campaignResult, sourceResult, accountResult, agentResult, xRuleResult] = await Promise.allSettled([
        fetchCampaignPage({
          platform: platformFilter,
          account_id: accountFilter === 'all' ? '' : accountFilter,
          sort: listSort,
          page,
          activity_type: typeFilter,
          reward_type: rewardFilter,
          deadline: deadlineFilter,
          qualification: qualificationFilter,
          snapshot_id: platformFilter === 'xiaohongshu' ? snapshotIdRef.current : '',
        }),
        fetchCampaignSources(), rippleApi<Account[]>('/api/ripple/accounts'),
        fetchCampaignEnrichmentStatus(), fetchXCampaignEnrichmentStatus(),
      ]);
      if (requestId !== loadRequestRef.current) return;

      let coreFailed = false;
      let logicalError = '';
      if (campaignResult.status === 'fulfilled') {
        const value = campaignResult.value;
        setCampaigns(value.items || []);
        setPageData(value);
        if (value.page !== page) setPage(value.page);
        if (platformFilter === 'xiaohongshu' && value.snapshot_id) snapshotIdRef.current = value.snapshot_id;
        setSnapshotChanged(false);
        setCampaignsLoaded(true);
        campaignsLoadedRef.current = true;
        setSelected((current) => current ? value.items.find((x) => x.id === current.id) || current : null);
      } else {
        const reason = campaignResult.reason;
        const message = reason instanceof Error ? reason.message : '活动分页读取失败';
        if (isNetworkError(reason)) coreFailed = true;
        else {
          logicalError = message;
          if (message.includes('排序已更新')) setSnapshotChanged(true);
        }
      }

      if (sourceResult.status === 'fulfilled') {
        const sourceState = sourceResult.value;
        setSources(sourceState.items || []);
        setAutomaticCount(sourceState.automatic_count || 0);
        setSourcesLoaded(true);
        sourcesLoadedRef.current = true;
        if (sourceState.server_now) setServerOffset(sourceState.server_now - Math.floor(Date.now() / 1000));
      } else if (isNetworkError(sourceResult.reason)) {
        coreFailed = true;
      } else {
        logicalError ||= sourceResult.reason instanceof Error ? sourceResult.reason.message : '活动源状态读取失败';
      }

      if (accountResult.status === 'fulfilled') setAccounts(accountResult.value);
      if (agentResult.status === 'fulfilled') setEnrichmentStatus(agentResult.value);
      if (xRuleResult.status === 'fulfilled') setXEnrichmentStatus(xRuleResult.value);

      if (coreFailed) {
        markConnectionFailure();
      } else {
        clearRetry();
        setReconnecting(false);
        setConnectionIssue('');
        setError(logicalError);
      }
    } finally {
      if (requestId === loadRequestRef.current && initial) setLoading(false);
    }
  }, [
    accountFilter, clearRetry, deadlineFilter, markConnectionFailure, page, platformFilter,
    qualificationFilter, rewardFilter, sortMode, typeFilter,
  ]);

  useEffect(() => {
    loadRef.current = load;
  }, [load]);

  useEffect(() => {
    void load(true);
    const refreshTimer = window.setInterval(() => { void load(false); }, 60 * 1000);
    const clockTimer = window.setInterval(() => setClock(Math.floor(Date.now() / 1000)), 1000);
    return () => {
      window.clearInterval(refreshTimer);
      window.clearInterval(clockTimer);
      if (retryTimerRef.current != null) window.clearTimeout(retryTimerRef.current);
    };
  }, [load]);

  useEffect(() => {
    if (!['running', 'queued', 'cancelling'].includes(enrichmentPhase)) return;
    const timer = window.setInterval(() => {
      fetchCampaignEnrichmentStatus().then((status) => {
        setEnrichmentStatus(status);
        if (!['running', 'queued', 'cancelling'].includes(status.status)) void load(false);
      }).catch(() => undefined);
    }, 1800);
    return () => window.clearInterval(timer);
  }, [enrichmentPhase, load]);

  useEffect(() => {
    if (!xBatchActive) return;
    const timer = window.setInterval(() => {
      fetchXCampaignEnrichmentStatus().then((status) => {
        setXEnrichmentStatus(status);
        if (!['running', 'queued'].includes(status.status)) void load(false);
      }).catch(() => undefined);
    }, 1800);
    return () => window.clearInterval(timer);
  }, [xBatchActive, load]);

  useEffect(() => {
    const selectedTrends = loadTrendSelection();
    if (!selectedTrends.length) return;
    fetchTrends(selectedTrends.join(','), 6)
      .then((value) => setTrendGroups(value.trends || []))
      .catch(() => setTrendGroups([]));
  }, []);

  const refreshNow = async () => {
    const platforms = [...refreshTargets];
    const originView = viewKey;
    if (!platforms.length) { showToast(currentSource?.detail || '当前范围没有已就绪的活动源'); return; }
    if (platforms.some((platform) => refreshInFlightRef.current.has(platform))) return;
    platforms.forEach((platform) => refreshInFlightRef.current.add(platform));
    setRefreshingPlatforms(Array.from(refreshInFlightRef.current)); setError('');
    try {
      const result = await refreshCampaigns(platforms, true, paidRefresh);
      setSources(result.sources.items || []);
      setAutomaticCount(result.sources.automatic_count || 0);
      setSourcesLoaded(true); sourcesLoadedRef.current = true;
      if (currentViewRef.current !== originView) return;
      setSelected((current) => current ? result.campaigns.find((row) => row.id === current.id) || current : null);
      clearRetry(); setReconnecting(false); setConnectionIssue('');
      const fresh = result.results.filter((row) => row.status === 'fresh');
      const failed = result.results.filter((row) => !['fresh', 'cached'].includes(row.status));
      showToast(`${refreshLabel}：${fresh.length} 个来源完成${failed.length ? `，${failed.length} 个来源需要处理` : ''}`);
      if (failed.length) setError(failed.map((row) => `${platformLabel(row.platform)}：${row.error || row.status}`).join('；'));
      if (platformFilter === 'xiaohongshu' && fresh.some((row) => row.platform === 'xiaohongshu')) {
        snapshotIdRef.current = ''; setSnapshotChanged(false);
        if (page !== 1) setPage(1); else await loadRef.current(false);
      } else if (fresh.length) {
        await loadRef.current(false);
      }
    } catch (e) {
      if (currentViewRef.current !== originView) return;
      if (isNetworkError(e)) markConnectionFailure();
      else setError(e instanceof Error ? e.message : '活动刷新失败');
    } finally {
      platforms.forEach((platform) => refreshInFlightRef.current.delete(platform));
      setRefreshingPlatforms(Array.from(refreshInFlightRef.current));
    }
  };

  const verifySelected = async () => {
    if (!selected || verifying) return;
    setVerifying(true); setError('');
    try {
      const updated = await verifyCampaign(selected.id);
      setSelected(updated);
      setCampaigns((rows) => rows.map((row) => row.id === updated.id ? updated : row));
      showToast('活动规则已重新核验');
    } catch (e) {
      setError(e instanceof Error ? e.message : '活动规则核验失败');
    } finally {
      setVerifying(false);
    }
  };

  const refreshXhsDetail = async (campaign: Campaign) => {
    if (campaign.platform !== 'xiaohongshu' || xhsDetailRefreshing) return;
    setXhsDetailRefreshing(campaign.id); setError('');
    try {
      const updated = await refreshXhsCampaignDetail(campaign.id);
      setSelected((current) => current?.id === updated.id ? updated : current);
      setCampaigns((rows) => rows.map((row) => row.id === updated.id ? updated : row));
      if (updated.xhs_detail_status === 'parsed') showToast('已重新读取小红书规则详情');
      else if (updated.xhs_detail_status === 'no_structured_rules') showToast('详情已读取，当前页面未提供可结构化的完整规则');
      else showToast('详情读取完成，请查看规则状态');
    } catch (e) {
      setError(e instanceof Error ? e.message : '小红书规则详情读取失败');
    } finally {
      setXhsDetailRefreshing('');
    }
  };

  const enrichOne = async (campaign: Campaign) => {
    if (enrichingId) return;
    setEnrichingId(campaign.id); setError('');
    try {
      const result = await enrichCampaign(campaign.id, true);
      setCampaigns((rows) => rows.map((row) => row.id === result.item.id ? result.item : row));
      setSelected((current) => current?.id === result.item.id ? result.item : current);
      showToast(result.called ? 'Agent 已完成规则补全' : '当前证据无需重复调用 Agent');
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Agent 补全失败');
    } finally { setEnrichingId(''); }
  };

  const cancelGlobalEnrichment = async () => {
    try { setEnrichmentStatus(await cancelCampaignEnrichment()); }
    catch (e) { setError(e instanceof Error ? e.message : '取消失败'); }
  };

  const openSourceEditor = (source: CampaignSourceCapability) => {
    setSourceEditor(source);
    setSourceDraft({
      method: source.method || '',
      x_api_account_id: source.x_api_account_id || '',
      fallback_method: source.fallback_method || '',
      fallback_enabled: !!source.fallback_enabled,
      account_id: source.account_id || '',
      tikhub_enabled: !!source.tikhub_enabled,
      tikhub_api_key: '',
    });
  };

  const saveSourceEditor = async () => {
    if (!sourceEditor || sourceSaving) return;
    setSourceSaving(true); setError('');
    try {
      const state = await configureCampaignSource(sourceEditor.platform, sourceDraft);
      setSources(state.items || []); setAutomaticCount(state.automatic_count || 0);
      setSourceEditor(null); showToast('活动数据源配置已保存');
    } catch (e) { setError(e instanceof Error ? e.message : '活动数据源配置失败'); }
    finally { setSourceSaving(false); }
  };

  const changePlatformFilter = (value: string) => {
    snapshotIdRef.current = '';
    setSnapshotChanged(false);
    setPage(1);
    setPlatformFilter(value);
    setAccountFilter('all');
    setSortMode(value === 'xiaohongshu' ? 'default' : 'recommend');
  };

  const changeSortMode = (value: CampaignListSort) => {
    if (platformFilter === 'xiaohongshu') {
      snapshotIdRef.current = '';
      setSnapshotChanged(false);
    }
    setPage(1);
    setSortMode(value);
  };

  const acceptLatestSnapshot = () => {
    snapshotIdRef.current = '';
    setSnapshotChanged(false);
    setError('');
    if (page !== 1) setPage(1);
    else void load(false);
  };

  const activityTypes = pageData?.filter_options.activity_types || [];
  const rewardTypes = pageData?.filter_options.reward_types || [];
  const visible = campaigns;
  const stats = pageData?.stats || { active: 0, soon: 0, saved: 0 };
  const hasSourceRows = (pageData?.source_total || 0) > 0;
  const sortOptions: ReadonlyArray<readonly [CampaignListSort, string]> = platformFilter === 'xiaohongshu'
    ? [['default', '默认排序'], ['latest', '最新排序']]
    : [['recommend', '推荐排序'], ['new', '最近更新'], ['deadline', '即将截止'], ['saved', '已收藏']];
  const pageButtons = pageData ? paginationItems(pageData.page, pageData.total_pages) : [];
  const accountOptions = platformFilter === 'all'
    ? accounts
    : accounts.filter((account) => account.platform === platformFilter);

  const trends = useMemo(() => trendGroups.flatMap((group) =>
    group.items.slice(0, 4).map((item) => ({ ...item, platform: group.label, platformKey: group.platform, status: group.status }))).slice(0, 10), [trendGroups]);

  const fillDraft = (draft: CampaignInput) => {
    setEditId(null);
    setForm({ ...emptyCampaign(), ...draft, submission_spec: draft.submission_spec || emptySubmissionSpec() });
    setEligibilityText(joined(draft.eligibility)); setRequirementsText(joined(draft.content_requirements));
    setPrizesText(joined(draft.prizes)); setWinningConditionsText(joined(draft.winning_conditions));
    setRewardRulesText(joined(draft.reward_rules)); setTopicsText((draft.required_topics || []).join('，'));
  };

  const openNew = (platform?: CampaignPlatform) => {
    setImportWarning('');
    const draft = emptyCampaign();
    if (platform) draft.platform = platform;
    fillDraft(draft);
  };
  const updateSubmissionSpec = (patch: Partial<CampaignSubmissionSpec>) => {
    setForm((current) => current ? { ...current, submission_spec: { ...(current.submission_spec || emptySubmissionSpec()), ...patch } } : current);
  };
  const openImport = () => {
    const current = PLATFORMS.some((item) => item.key === platformFilter) ? platformFilter as CampaignPlatform : 'bilibili';
    setImportPlatform(current);
    setImportKind('url');
    setImportUrl('');
    setImportText('');
    setImportUseAgent(false);
    setImportWarning('');
    setImportUrlOpen(true);
  };

  const parseImport = async () => {
    const ready = importKind === 'url' ? !!importUrl.trim() : !!importText.trim();
    if (!ready || importParsing) return;
    setImportParsing(true); setError(''); setImportWarning('');
    try {
      const result = await previewCampaignImport({
        target_platform: importPlatform,
        input_kind: importKind,
        url: importKind === 'url' ? importUrl.trim() : '',
        text: importKind === 'text' ? importText.trim() : '',
        allow_agent: importUseAgent,
      });
      fillDraft({ ...result.draft, platform: result.draft.platform || importPlatform } as CampaignInput);
      const agentNote = result.agent_used ? ` 已由 ${result.model || '默认 Agent'} 基于现有原文辅助整理；请逐项确认。` : '';
      setImportWarning((result.warning || '已生成草稿；请确认后再创建。') + agentNote);
      setImportUrlOpen(false);
    } catch (e) { setError(e instanceof Error ? e.message : '活动 / 激励计划解析失败'); }
    finally { setImportParsing(false); }
  };

  const openEdit = (campaign: Campaign) => {
    setEditId(campaign.id);
    setForm({
      title: campaign.title, platform: campaign.platform, organizer: campaign.organizer,
      organizer_type: campaign.organizer_type, activity_type: campaign.activity_type,
      reward_type: campaign.reward_type, reward_summary: campaign.reward_summary, summary: campaign.summary,
      starts_at: campaign.starts_at, signup_deadline: campaign.signup_deadline,
      submit_deadline: campaign.submit_deadline, stats_deadline: campaign.stats_deadline,
      timezone: campaign.timezone, qualification_state: campaign.qualification_state,
      ai_policy: campaign.ai_policy, source_url: campaign.source_url, note: campaign.note,
      status: campaign.status, account_id: campaign.account_id,
      eligibility: campaign.eligibility, content_requirements: campaign.content_requirements,
      prizes: campaign.prizes, winning_conditions: campaign.winning_conditions,
      reward_rules: campaign.reward_rules, required_topics: campaign.required_topics,
      submission_spec: campaign.submission_spec || emptySubmissionSpec(),
    });
    setEligibilityText(joined(campaign.eligibility));
    setRequirementsText(joined(campaign.content_requirements));
    setPrizesText(joined(campaign.prizes));
    setWinningConditionsText(joined(campaign.winning_conditions));
    setRewardRulesText(joined(campaign.reward_rules));
    setTopicsText((campaign.required_topics || []).join('，'));
  };

  const submitCampaign = async () => {
    if (!form?.title.trim()) return;
    setSaving(true);
    setError('');
    try {
      const payload: CampaignInput = {
        ...form,
        eligibility: splitLines(eligibilityText),
        content_requirements: splitLines(requirementsText),
        prizes: splitLines(prizesText),
        winning_conditions: splitLines(winningConditionsText),
        reward_rules: splitLines(rewardRulesText),
        required_topics: splitLines(topicsText),
        submission_spec: form.submission_spec || emptySubmissionSpec(),
      };
      const saved = editId ? await updateCampaign(editId, payload) : await createCampaign(payload);
      setForm(null);
      setEditId(null);
      await load();
      setSelected(saved);
      showToast(editId ? '活动规则已更新并生成新版本' : '活动已导入');
    } catch (e) {
      setError(e instanceof Error ? e.message : '保存活动失败');
    } finally {
      setSaving(false);
    }
  };

  const toggleSaved = async (campaign: Campaign) => {
    try {
      const value = await saveCampaign(campaign.id, !campaign.saved);
      setCampaigns((rows) => rows.map((x) => x.id === value.id ? value : x));
      setSelected((current) => current?.id === value.id ? value : current);
      await load(false);
    } catch (e) {
      showToast(e instanceof Error ? e.message : '收藏失败');
    }
  };

  const addCalendar = async (campaign: Campaign) => {
    const today = new Date().toISOString().slice(0, 10);
    const start = dateOnly(campaign.starts_at) || dateOnly(campaign.signup_deadline) || today;
    await createSchedule({
      title: campaign.title,
      date: start,
      platform: campaign.platform_label,
      time: '',
      status: 'idea',
      note: [
        campaign.reward_summary,
        campaign.required_topics.length ? `指定话题：${campaign.required_topics.join('、')}` : '',
        `活动规则版本：v${campaign.rule_version}`,
      ].filter(Boolean).join('\n'),
      kind: 'event',
      url: campaign.source_url,
      source: 'campaign',
      event_type: '平台活动',
      end_date: dateOnly(campaign.submit_deadline),
      campaign_id: campaign.id,
      campaign_rule_version: campaign.rule_version,
    });
    showToast('已加入选题日历');
  };

  const generateIdeas = async (campaign: Campaign) => {
    setRecommendCampaign(campaign);
    setRecommendOpen(true);
    setRecommendError('');
    setRecommendResult(null);
    setAdded(new Set());
    if (!persona) { setRecommendError('先选择账号画像，再结合账号定位生成选题。'); return; }
    if (!aiReady) { setRecommendError('AI 推荐服务未配置或不可用。'); return; }
    setRecommending(true);
    try {
      setRecommendResult(await recommendIdeas({
        persona,
        campaign_id: campaign.id,
        trend_sources: loadTrendSelection(),
        target_platforms: [campaign.platform],
        limit: 3,
      }));
    } catch (e) {
      setRecommendError(e instanceof Error ? e.message : 'AI 选题生成失败');
    } finally {
      setRecommending(false);
    }
  };

  const addRecommendation = async (rec: IdeaRecommendation) => {
    const campaign = recommendCampaign;
    if (!campaign || added.has(rec.title)) return;
    const idea = await createIdea({
      title: rec.title,
      note: [rec.angle, `推荐理由：${rec.reason}`,
        rec.trend_refs.length ? `关联热点：${rec.trend_refs.join('、')}` : '',
        rec.pending_checks?.length ? `待确认：${rec.pending_checks.join('；')}` : '',
      ].filter(Boolean).join('\n\n'),
      source: `AI推荐 · ${persona} · ${campaign.title}`,
      status: 'pending',
      angle: rec.angle,
      reason: rec.reason,
      campaign_id: campaign.id,
      campaign_rule_version: campaign.rule_version,
      trend_refs: rec.trend_refs,
      target_platforms: rec.platforms?.length ? rec.platforms : [campaign.platform],
      requirements: rec.requirements?.length ? rec.requirements : campaign.content_requirements,
      pending_checks: rec.pending_checks || [],
    });
    setAdded((current) => new Set(current).add(rec.title));
    showToast('已加入选题库');
    return idea;
  };

  const startRecommendationContent = (rec: IdeaRecommendation) => {
    const campaign = recommendCampaign;
    onUseTopic({
      title: rec.title,
      angle: rec.angle,
      reason: rec.reason,
      campaignId: campaign?.id,
      campaignTitle: campaign?.title,
      campaignRuleVersion: campaign?.rule_version,
      trendRefs: rec.trend_refs,
      targetPlatforms: rec.platforms?.length ? rec.platforms : campaign ? [campaign.platform] : [],
      requirements: rec.requirements?.length ? rec.requirements : campaign?.content_requirements || [],
      pendingChecks: rec.pending_checks || [],
      campaignRequirements: campaign?.content_requirements || [],
      campaignRequiredTopics: campaign?.required_topics || [],
      campaignAiPolicy: campaign?.ai_policy,
      campaignSubmitDeadline: campaign?.submit_deadline,
      campaignSourceUrl: campaign?.source_url,
      campaignQualification: campaign?.qualification_state,
      campaignCurrentRuleVersion: campaign?.rule_version,
      source: campaign ? `活动广场 · ${campaign.title}` : '活动广场',
    });
  };

  const formSpec = form?.submission_spec || emptySubmissionSpec();

  return (
    <div className="page-scroll campaign-page">
      <div className="page-head campaign-head">
        <div>
          <h1 className="page-title"><IconCompass size={22} /> 活动广场</h1>
          <p className="page-subtitle">聚合 X、小红书、抖音、B站、微信公众号、微信视频号的创作活动与激励活动，结合热点生成选题灵感。</p>
        </div>
        <div className="campaign-head-actions">
          <button className="btn btn-sm btn-primary" title={canRefresh ? refreshHint : currentSource?.detail || '当前没有已就绪来源'} disabled={refreshing || loading || !sourcesLoaded || !canRefresh} onClick={() => void refreshNow()}>
            <IconRefresh size={13} /> {refreshing ? '刷新中…' : refreshLabel}
          </button>
          <button className="btn btn-sm" onClick={openImport}>+ 补充导入</button>
        </div>
      </div>

      <div className="campaign-stats">
        <div className="campaign-stat"><span className="campaign-stat-dot ok" /><span>进行中活动</span><strong>{campaignsLoaded ? stats.active : '—'}</strong></div>
        <div className="campaign-stat"><span className="campaign-stat-dot warn" /><span>即将截止</span><strong>{campaignsLoaded ? stats.soon : '—'}</strong></div>
        <div className="campaign-stat"><span className="campaign-stat-dot saved" /><span>已收藏</span><strong>{campaignsLoaded ? stats.saved : '—'}</strong></div>
      </div>

      <div className="campaign-source-note">
        <IconRefresh size={15} />
        <div>
          <strong>{sourcesLoaded ? `已就绪 ${automaticCount} 个自动活动源` : '活动源状态暂时无法读取'}</strong>
          <span>免费来源定期同步；消耗 Token / 接口额度的自动任务仅在北京时间 09:00、14:00、20:00 执行。手动刷新只处理当前平台范围，收费备用须明确启用。</span>
        </div>
        {enrichmentStatus && ['running', 'queued', 'cancelling'].includes(enrichmentStatus.status) && <div className="campaign-agent-progress"><strong>Agent 补全 {enrichmentStatus.done}/{enrichmentStatus.total}</strong><span>{enrichmentStatus.failed ? `失败 ${enrichmentStatus.failed} · ` : ''}按活动串行处理，避免重复 Token 消耗</span><button className="r2-text-button" onClick={() => void cancelGlobalEnrichment()}>取消</button></div>}
        {xEnrichmentStatus && ['running', 'queued'].includes(xEnrichmentStatus.status) && <div className="campaign-agent-progress"><strong>X 规则整理 {xEnrichmentStatus.done}/{xEnrichmentStatus.total}</strong><span>{xEnrichmentStatus.failed ? `失败 ${xEnrichmentStatus.failed} · ` : ''}逐条中文整理，已处理活动不会自动重复消耗 Token</span></div>}
      </div>
      {sourcesLoaded && <section className="campaign-source-status-grid">
        {sources.map((source) => {
          const sync = source.last_sync;
          const now = clock + serverOffset;
          const lastSuccess = sync?.last_success_at || (sync?.status === 'fresh' ? sync.at : 0) || 0;
          const lastAttempt = sync?.last_attempt_at || sync?.at || 0;
          const nextRun = source.schedule?.next_run_at || source.next_sync_at || sync?.next_run_at || 0;
          const remaining = nextRun ? Math.max(0, nextRun - now) : 0;
          const configurable = ['x', 'xiaohongshu', 'douyin'].includes(source.platform);
          const selectedSource = platformFilter === source.platform;
          const toggleSource = () => changePlatformFilter(selectedSource ? 'all' : source.platform);
          return <article
            key={source.id || source.platform}
            className={`campaign-source-status source-${source.status || 'unknown'} ${selectedSource ? 'selected' : ''}`.trim()}
          >
            <button
              type="button"
              className="campaign-source-hit"
              aria-pressed={selectedSource}
              aria-label={`${source.label}活动筛选`}
              onClick={toggleSource}
            />
            <div className="campaign-source-status-head">
              <div className="campaign-source-brand"><PlatformIcon platform={source.platform} size={18} /><strong>{source.label}</strong></div>
              <span>{SOURCE_HEALTH[source.status || ''] || source.status || (source.automatic ? '自动同步' : '支持导入')}</span>
            </div>
            <p>{source.detail}</p>
            {source.automatic && <small>自动频率：{!source.schedule ? '等待后端加载采集计划' : source.schedule.mode === 'daily_slots' ? `每日 ${(source.schedule.times || []).join(' / ')}（北京时间）` : `每 ${syncIntervalLabel(source.sync_interval_seconds)}（免费来源）`}</small>}
            {source.schedule?.paid_note && <small>{source.schedule.paid_note}</small>}
            {source.cost_note && <small>{source.cost_note}</small>}
            {lastSuccess > 0 && <small>最近成功采集：{new Date(lastSuccess * 1000).toLocaleString('zh-CN')}{sync?.last_success_count != null ? ` · ${sync.last_success_count} 条` : ''}</small>}
            {source.automatic && nextRun > 0 ? <small>下次采集：{new Date(nextRun * 1000).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}（北京时间） · <b className="campaign-sync-countdown">{formatCountdown(remaining)}</b></small> : null}
            {sync?.error && <small className="error">上次采集失败：{lastAttempt ? new Date(lastAttempt * 1000).toLocaleString('zh-CN') : '未知'} · {sync.error}</small>}
            <div className="campaign-source-status-actions">
              {configurable && <button className="r2-text-button" onClick={() => openSourceEditor(source)}>配置</button>}
            </div>
          </article>;
        })}
      </section>}

      <div className="campaign-filters">
        <select className="field" value={platformFilter} onChange={(e) => changePlatformFilter(e.target.value)}>
          <option value="all">全部平台</option>
          {PLATFORMS.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
        </select>
        <select className="field" value={typeFilter} onChange={(e) => { setTypeFilter(e.target.value); setPage(1); }}>
          <option value="all">全部活动类型</option>
          {activityTypes.map((x) => <option key={x}>{x}</option>)}
        </select>
        <select className="field" value={rewardFilter} onChange={(e) => { setRewardFilter(e.target.value); setPage(1); }}>
          <option value="all">全部奖励类型</option>
          {rewardTypes.map((x) => <option key={x}>{x}</option>)}
        </select>
        <select className="field" value={deadlineFilter} onChange={(e) => { setDeadlineFilter(e.target.value); setPage(1); }}>
          <option value="all">全部截止时间</option>
          <option value="7">7 天内截止</option>
          <option value="30">30 天内截止</option>
          <option value="none">未说明截止时间</option>
        </select>
        <select className="field" value={qualificationFilter} onChange={(e) => { setQualificationFilter(e.target.value); setPage(1); }}>
          <option value="all">全部资格状态</option>
          <option value="eligible">已知符合</option>
          <option value="unknown">资格信息不足</option>
          <option value="ineligible">已知不符合</option>
        </select>
        <select className="field" value={persona} onChange={(e) => {
          if (e.target.value === '__new__') onNewPersona(); else onPersonaChange(e.target.value);
        }}>
          <option value="">通用画像</option>
          {personas.map((p) => <option key={p.name} value={p.name}>{p.name}</option>)}
          <option value="__new__">+ 新建画像…</option>
        </select>
        <select className="field" value={accountFilter} onChange={(e) => {
          snapshotIdRef.current = '';
          setSnapshotChanged(false);
          setAccountFilter(e.target.value);
          setPage(1);
        }}>
          <option value="all">全部参与账号</option>
          {accountOptions.map((a) => <option key={a.id} value={a.id}>{a.label} · {platformLabel(a.platform)}</option>)}
        </select>
      </div>

      <div className="campaign-sort">
        {sortOptions.map(([key, label]) => (
          <button key={key} className={sortMode === key ? 'active' : ''} onClick={() => changeSortMode(key)}>{label}</button>
        ))}
        <span>{campaignsLoaded && pageData
          ? `共 ${pageData.total} 个活动 · 第 ${pageData.range_start}–${pageData.range_end} 条 · ${pageData.page}/${pageData.total_pages} 页${pageData.truncated ? ' · 来源结果达到同步上限' : ''}`
          : '活动数据未读取'}</span>
      </div>

      {connectionIssue && <div className={`campaign-connection-notice ${reconnecting ? 'reconnecting' : ''}`}>{connectionIssue}{reconnecting && <span>自动重试中…</span>}</div>}
      {snapshotChanged && <div className="campaign-snapshot-notice"><span>小红书活动排序已有新快照，当前页面没有混入新旧两批顺序。</span><button className="r2-text-button" onClick={acceptLatestSnapshot}>查看最新排序</button></div>}
      {platformFilter === 'xiaohongshu' && pageData?.source_status === 'stale' && <div className="campaign-snapshot-notice stale"><span>当前展示上次成功同步的小红书官方排序快照。</span></div>}
      {error && <div className="notice-error">{error}</div>}
      <div className="campaign-layout">
        <main className="campaign-list">
          {loading && !campaignsLoaded && <div className="campaign-empty">正在读取活动…</div>}
          {!loading && !campaignsLoaded && (
            <div className="campaign-empty">
              <IconCompass size={28} />
              <strong>活动数据暂时无法读取</strong>
              <p>Ripple 正在自动重新连接后端。连接恢复后会自动载入活动，不会把未读取状态当成 0 条数据。</p>
            </div>
          )}
          {campaignsLoaded && visible.length === 0 && (
            <div className="campaign-empty">
              <IconCompass size={28} />
              <strong>{hasSourceRows ? '当前筛选条件没有匹配活动' : currentSource && !currentSource.automatic ? `${currentSource.label}活动源尚未就绪` : '还没有已确认的创作活动'}</strong>
              <p>{hasSourceRows ? '调整筛选条件继续查看。' : currentSource && !currentSource.automatic ? currentSource.detail : '手动刷新只获取当前范围的活动，不会调用其他平台。'}</p>
              {!hasSourceRows && <div style={{ display: 'flex', gap: 8 }}>
                {canRefresh && <button className="btn btn-primary btn-sm" title={refreshHint} disabled={refreshing || !sourcesLoaded} onClick={() => void refreshNow()}>{refreshing ? '刷新中…' : refreshLabel}</button>}
                {currentSource && !currentSource.automatic && ['x', 'xiaohongshu', 'douyin'].includes(currentSource.platform) && <button className="btn btn-primary btn-sm" onClick={() => openSourceEditor(currentSource)}>配置{currentSource.label}活动源</button>}
                <button className="btn btn-sm" onClick={openImport}>补充导入</button>
              </div>}
            </div>
          )}
          <div className="campaign-grid">
            {visible.map((campaign) => {
              const qualification = qualificationBadge(campaign);
              const sourceText = sourceName(campaign) + ' · ' + sourceVerificationLabel(campaign);
              const specText = submissionSummary(campaign);
              const missing = campaign.missing_fields || [];
              const incomplete = missing.length > 0;
              const xhsRuleStatus = campaign.platform === 'xiaohongshu' ? xhsDetailStatusText(campaign) : '';
              const rewardText = campaign.prizes?.length || campaign.reward_summary
                ? concise(campaign.prizes, campaign.reward_summary || '')
                : campaign.reward_rules?.length ? concise(campaign.reward_rules, '') : '';
              const longTerm = isLongTermProgram(campaign);
              const conditionText = longTerm ? concise(campaign.reward_rules, '') : concise(campaign.winning_conditions, '');
              const canAgentEnrich = campaign.platform === 'bilibili' && incomplete;
              return (
                <article className="card campaign-card" key={campaign.id}>
                  <div className="campaign-card-top">
                    <div className={`campaign-platform platform-${campaign.platform}`} title={campaign.platform_label} aria-label={campaign.platform_label}><PlatformIcon platform={campaign.platform} size={30} /></div>
                    <div>
                      <h3>{campaign.title}</h3>
                      <p>主办方 · {campaign.organizer || '未说明'}</p>
                    </div>
                    <button className={`campaign-save ${campaign.saved ? 'saved' : ''}`} title={campaign.saved ? '取消收藏' : '收藏'}
                      onClick={() => void toggleSaved(campaign)}>{campaign.saved ? <IconCheck size={15} /> : <IconBookmark size={15} />}</button>
                  </div>
                  <div className="campaign-tags">
                    <span>{campaign.activity_type || '活动'}</span>
                    {campaign.reward_type && <span>{campaign.reward_type}</span>}
                    {qualification && <span className={qualification.cls}>{qualification.label}</span>}
                    {campaign.status === 'cancelled' && <span className="bad">已取消</span>}
                  </div>
                  <div className="campaign-card-facts">
                    <div><small>活动时间</small><strong>{campaignTime(campaign)}</strong></div>
                    <div><small>来源</small><strong>{sourceText}</strong></div>
                    <div><small>参赛作品</small><span className={!specText ? 'campaign-fact-empty' : ''}>{specText || '—'}</span></div>
                    <div><small>参与条件</small><span className={!campaign.eligibility?.length ? 'campaign-fact-empty' : ''}>{campaign.eligibility?.length ? concise(campaign.eligibility, '') : '—'}</span></div>
                    <div><small>{longTerm ? '收益方式' : '奖品 / 奖励'}</small><span className={!rewardText ? 'campaign-fact-empty' : ''}>{rewardText || '—'}</span></div>
                    <div><small>{longTerm ? '收益规则' : '获奖条件'}</small><span className={!conditionText ? 'campaign-fact-empty' : ''}>{conditionText || '—'}</span></div>
                    <div className={incomplete ? 'campaign-missing' : 'campaign-complete'}><small>规则信息</small><span>{campaign.platform === 'xiaohongshu' ? (xhsRuleStatus + (campaign.xhs_detail_status === 'parsed' && incomplete ? ' · 仍缺：' + missing.map((key) => MISSING_LABELS[key] || key).join('、') : '')) : campaign.platform === 'x' && xEnrichmentStatus?.current === campaign.id ? '整理中…' : campaign.platform === 'x' && campaign.x_enrichment_status === 'failed' ? '整理失败，等待重试' : incomplete ? '待补充：' + missing.map((key) => MISSING_LABELS[key] || key).join('、') : '已完整'}</span></div>
                  </div>
                  <div className="campaign-actions">
                    <button className="btn btn-sm" onClick={() => setSelected(campaign)}>查看详情</button>
                    {canAgentEnrich
                      ? <button className="btn btn-sm campaign-agent-button" disabled={!!enrichingId || agentBatchActive || campaign.enrichment_status === 'running'} onClick={() => void enrichOne(campaign)}>✦ {enrichingId === campaign.id || campaign.enrichment_status === 'running' ? 'Agent 补全中…' : 'Agent 补全'}</button>
                      : <span className="campaign-action-placeholder" aria-hidden="true" />}
                    <button className="btn btn-sm btn-primary" disabled={campaign.status === 'ended' || campaign.status === 'cancelled'}
                      onClick={() => void generateIdeas(campaign)}><IconSkills size={13} /> 生成选题</button>
                  </div>
                </article>
              );
            })}
          </div>

          {campaignsLoaded && pageData && pageData.total_pages > 1 && (
            <nav className="campaign-pagination" aria-label="活动分页">
              <span className="campaign-pagination-info">每页 10 条</span>
              <div className="campaign-pagination-pages">
                <button disabled={pageData.page <= 1} onClick={() => setPage(Math.max(1, pageData.page - 1))}>上一页</button>
                {pageButtons.map((item, index) => item === 'ellipsis'
                  ? <span className="campaign-pagination-ellipsis" key={`ellipsis-${index}`}>…</span>
                  : <button key={item} className={pageData.page === item ? 'active' : ''} aria-current={pageData.page === item ? 'page' : undefined} onClick={() => setPage(item)}>{item}</button>)}
                <button disabled={pageData.page >= pageData.total_pages} onClick={() => setPage(Math.min(pageData.total_pages, pageData.page + 1))}>下一页</button>
              </div>
            </nav>
          )}

        </main>

        <aside className="campaign-trends">
          <div className="campaign-trends-head"><IconIdea size={17} /><strong>相关热点</strong><span>来自热点雷达</span></div>
          {trends.length === 0 && <p className="campaign-trends-empty">当前没有可用热点。活动仍可单独生成选题。</p>}
          {trends.map((item, index) => (
            <a key={`${item.platform}-${item.title}-${index}`} href={item.url || undefined} target="_blank" rel="noreferrer">
              <b>{index + 1}</b><span><strong>{item.title}</strong><small><PlatformIcon platform={item.platformKey} size={12} /> {item.platform}{item.hot ? ` · ${item.hot}` : ''}</small></span>
            </a>
          ))}
          <p className="campaign-trends-note">AI 只会引用实际读取到的热点；没有自然关联时允许不绑定热点。</p>
        </aside>
      </div>

      {selected && (
        <div className="campaign-drawer-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) setSelected(null); }}>
          <aside className="campaign-drawer">
            <header><div><PlatformBadge platform={selected.platform} label={selected.platform_label} size="xs" /><h2>活动详情</h2></div><button onClick={() => setSelected(null)}>×</button></header>
            <div className="campaign-drawer-title">
              <h3>{selected.title}</h3>
              <p>主办方 · {selected.organizer || '未说明'} · {STATUS[selected.status] || selected.status}</p>
              <div className="campaign-tags">
                {qualificationBadge(selected) && <span className={qualificationBadge(selected)?.cls}>{qualificationBadge(selected)?.label}</span>}
                <span>{sourceName(selected)}</span><span>规则 v{selected.rule_version}</span>
              </div>
            </div>

            <section className="campaign-summary-section"><h4>活动摘要</h4><p className={selected.summary || selected.note ? 'campaign-copy' : 'campaign-unknown'}>{selected.summary || selected.note || '当前来源没有提供可结构化的活动摘要，建议打开原活动页查看。'}</p></section>
            <section>
              <h4>时间节点</h4>
              <div className="campaign-timeline">
                <span><small>活动开始</small>{dateOnly(selected.starts_at) || '未说明'}</span>
                <span><small>报名截止</small>{dateOnly(selected.signup_deadline) || '未说明'}</span>
                <span><small>投稿截止</small>{dateOnly(selected.submit_deadline) || '未说明'}</span>
                <span><small>统计截止</small>{dateOnly(selected.stats_deadline) || '未说明'}</span>
              </div>
            </section>
            <section><h4>参赛作品</h4>{hasSubmissionSpec(selected) ? <div className="campaign-submission-spec">
              <span><small>作品形式</small>{(selected.submission_spec?.formats || []).map((key) => FORMAT_LABELS[key] || key).join(' / ') || '未说明'}</span>
              {(selected.submission_spec?.content_directions || []).length > 0 && <span><small>内容方向</small>{selected.submission_spec.content_directions.join('、')}</span>}
              {(selected.submission_spec?.style_requirements || []).length > 0 && <span><small>风格要求</small>{selected.submission_spec.style_requirements.join('、')}</span>}
              {(selected.submission_spec?.duration_seconds?.min != null || selected.submission_spec?.duration_seconds?.max != null) && <span><small>视频时长</small>{submissionSummary(selected).split(' · ').slice(1).join(' · ') || '已说明'}</span>}
              {(selected.submission_spec?.aspect_ratios || []).length > 0 && <span><small>画面比例</small>{selected.submission_spec.aspect_ratios.join(' / ')}</span>}
              {(selected.submission_spec?.resolutions || []).length > 0 && <span><small>分辨率</small>{selected.submission_spec.resolutions.join(' / ')}</span>}
              {(selected.submission_spec?.image_count?.min != null || selected.submission_spec?.image_count?.max != null) && <span><small>图片数量</small>{selected.submission_spec.image_count.min ?? '未说明'} ～ {selected.submission_spec.image_count.max ?? '未说明'}</span>}
              {(selected.submission_spec?.text_length?.min != null || selected.submission_spec?.text_length?.max != null) && <span><small>文字长度</small>{selected.submission_spec.text_length.min ?? '未说明'} ～ {selected.submission_spec.text_length.max ?? '未说明'} 字</span>}
              {selected.submission_spec?.live?.min_duration_seconds != null && <span><small>直播时长</small>≥ {formatSeconds(selected.submission_spec.live.min_duration_seconds)}</span>}
              {selected.submission_spec?.live?.required_category && <span><small>直播分区</small>{selected.submission_spec.live.required_category}</span>}
              {(selected.submission_spec?.live?.title_keywords || []).length > 0 && <span><small>直播标题关键词</small>{selected.submission_spec.live.title_keywords.join('、')}</span>}
              {selected.submission_spec?.original_required != null && <span><small>原创</small>{selected.submission_spec.original_required ? '必须原创' : '明确不要求'}</span>}
              {selected.submission_spec?.first_publish_required != null && <span><small>首发</small>{selected.submission_spec.first_publish_required ? '要求首发' : '明确不要求'}</span>}
              {selected.submission_spec?.exclusive_required != null && <span><small>独家</small>{selected.submission_spec.exclusive_required ? '要求独家' : '明确不要求'}</span>}
              {(selected.submission_spec?.min_entries != null || selected.submission_spec?.max_entries != null) && <span><small>投稿数量</small>{selected.submission_spec.min_entries ?? '未说明'} ～ {selected.submission_spec.max_entries ?? '未说明'}</span>}
              {selected.submission_spec?.submission_method && <span><small>投稿方式</small>{selected.submission_spec.submission_method}</span>}
              {(selected.submission_spec?.required_mentions || []).length > 0 && <span><small>指定 @</small>{selected.submission_spec.required_mentions.join('、')}</span>}
              {(selected.submission_spec?.required_music || []).length > 0 && <span><small>指定音乐</small>{selected.submission_spec.required_music.join('、')}</span>}
            </div> : <p className="campaign-unknown">原活动页暂未解析到明确参赛作品格式与规格。</p>}</section>
            <section><h4>参与条件</h4>{selected.eligibility?.length ? <ul>{selected.eligibility.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">原活动页暂未解析到明确参与条件。</p>}</section>
            <section><h4>内容要求</h4>{selected.content_requirements?.length ? <ul>{selected.content_requirements.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">原活动页暂未解析到明确内容要求。</p>}</section>
            <section><h4>{isLongTermProgram(selected) ? '收益方式' : '奖品 / 奖励'}</h4>{selected.prizes?.length ? <ul>{selected.prizes.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">{selected.reward_summary || (isLongTermProgram(selected) ? '公开规则未说明固定收益金额。' : '原活动页暂未解析到明确奖品。')}</p>}</section>
            {!isLongTermProgram(selected) && <section><h4>获奖条件</h4>{selected.winning_conditions?.length ? <ul>{selected.winning_conditions.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">原活动页暂未解析到明确获奖门槛。</p>}</section>}
            <section><h4>{isLongTermProgram(selected) ? '收益规则' : '奖励规则'}</h4>{selected.reward_rules?.length ? <ul>{selected.reward_rules.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">{isLongTermProgram(selected) ? '公开规则未说明更多收益计算细节。' : '未解析到额外的奖励计算或发放规则。'}</p>}</section>
            <section><h4>指定话题 / 标签</h4>{selected.required_topics?.length ? <div className="campaign-topic-tags">{selected.required_topics.map((x) => <span key={x}>{x.startsWith('#') ? x : `# ${x}`}</span>)}</div> : <p className="campaign-unknown">未解析到指定话题。</p>}</section>
            <section><h4>AI 使用要求</h4><p className="campaign-copy">{selected.ai_policy === 'unknown' || !selected.ai_policy ? '原活动规则未说明 AI 使用要求。' : selected.ai_policy}</p></section>
            <section><h4>来源与核验</h4>
              <p className="campaign-source-name"><strong>{sourceName(selected)}</strong> · {sourceVerificationLabel(selected)}</p>
              {selected.source_url ? <a className="campaign-source-link" href={selected.source_url} target="_blank" rel="noreferrer">{selected.source_url}</a> : <p className="campaign-unknown">没有保存来源链接。</p>}
              <p className="campaign-verify">首次发现：{selected.discovered_at ? new Date(selected.discovered_at * 1000).toLocaleString('zh-CN') : '未知'} · 最后看到：{selected.last_seen_at ? new Date(selected.last_seen_at * 1000).toLocaleString('zh-CN') : '未知'}</p>
              <p className="campaign-verify">规则最近核验：{selected.last_verified_at ? new Date(selected.last_verified_at * 1000).toLocaleString('zh-CN') : '尚未完整核验'} · 规则版本 v{selected.rule_version}</p>
              {selected.platform === 'xiaohongshu' && <p className="campaign-verify">详情解析：{xhsDetailStatusText(selected)}{selected.xhs_detail_fetched_at ? ' · ' + new Date(selected.xhs_detail_fetched_at * 1000).toLocaleString('zh-CN') : ''}{selected.xhs_detail_error ? ' · ' + selected.xhs_detail_error : ''}</p>}
            </section>
            <footer>
              <button className="btn btn-primary campaign-generate" disabled={selected.status === 'ended' || selected.status === 'cancelled'}
                onClick={() => void generateIdeas(selected)}><IconSkills size={15} /> 活动 + 热点生成选题</button>
              <div>
                <button className="btn btn-sm" onClick={() => void addCalendar(selected)}><IconCalendar size={13} /> 加入选题日历</button>
                {selected.source_url && <a className="btn btn-sm" href={selected.source_url} target="_blank" rel="noreferrer">打开原活动页</a>}
                {selected.platform === 'bilibili' && <button className="btn btn-sm" disabled={verifying} onClick={() => void verifySelected()}><IconRefresh size={13} /> {verifying ? '核验中…' : '重新核验规则'}</button>}
                {selected.platform === 'xiaohongshu' && <button className="btn btn-sm" disabled={!!xhsDetailRefreshing} onClick={() => void refreshXhsDetail(selected)}><IconRefresh size={13} /> {xhsDetailRefreshing === selected.id ? '读取中…' : selected.xhs_detail_status === 'not_fetched' ? '读取小红书规则' : '重新读取小红书规则'}</button>}
                {selected.platform === 'bilibili' && (selected.missing_fields || []).length > 0 && <button className="btn btn-sm campaign-agent-button" disabled={!!enrichingId || agentBatchActive} onClick={() => void enrichOne(selected)}>✦ {enrichingId === selected.id ? 'Agent 补全中…' : 'Agent 补全'}</button>}
                {selected.source_type === 'user_import' && <button className="btn btn-sm" onClick={() => openEdit(selected)}>编辑规则</button>}
              </div>
            </footer>
          </aside>
        </div>
      )}

      {sourceEditor && (
        <div className="overlay">
          <div className="modal campaign-source-config-modal">
            <div className="campaign-modal-head">
              <div><h3>{sourceEditor.label} · 活动数据源</h3><p>{sourceEditor.detail}</p></div>
              <button onClick={() => setSourceEditor(null)}>×</button>
            </div>
            {sourceEditor.platform === 'x' && <>
              <label className="field-label">主检索方式</label>
              <select className="field" value={sourceDraft.method} onChange={(e) => {
                const method = e.target.value;
                setSourceDraft({ ...sourceDraft, method, fallback_method: sourceDraft.fallback_method === method ? '' : sourceDraft.fallback_method });
              }}>
                <option value="">未启用</option>
                <option value="prompt">Grok / 搜索模型 · Prompt</option>
                <option value="x_search">原生 X Search Tool</option>
                <option value="x_api">X Developer API</option>
              </select>
              {sourceDraft.method === 'prompt' && <div className="campaign-source-config-note">
                默认推荐：Ripple 每 2 小时把固定活动发现 Prompt 发给“X 活动发现”绑定的 Grok / 搜索模型，由模型或网关自行搜索 X 并返回 JSON。无需 X Search Tool 能力测试。
                <button className="r2-text-button" onClick={onOpenSettings}>打开模型设置</button>
              </div>}
              {sourceDraft.method === 'x_search' && <div className="campaign-source-config-note">
                高级模式：直接调用 Provider 的 <code>/responses + x_search</code>。只有严格 X Search 能力测试通过后才会自动采集，可能产生额外搜索费用。
                <button className="r2-text-button" onClick={onOpenSettings}>打开模型设置</button>
              </div>}
              {(sourceDraft.method === 'x_api' || sourceDraft.fallback_method === 'x_api') && <>
                <label className="field-label">X Developer API 账号</label>
                <select className="field" value={sourceDraft.x_api_account_id} onChange={(e) => setSourceDraft({ ...sourceDraft, x_api_account_id: e.target.value })}>
                  <option value="">请选择账号</option>
                  {accounts.filter((a) => a.platform === 'x' && a.adapter === 'x-api').map((a) => <option key={a.id} value={a.id}>{a.label} · {a.status}</option>)}
                </select>
              </>}
              {sourceDraft.method && <label className="r2-checkbox campaign-source-fallback"><input type="checkbox" checked={sourceDraft.fallback_enabled} onChange={(e) => setSourceDraft({
                ...sourceDraft,
                fallback_enabled: e.target.checked,
                fallback_method: e.target.checked ? (sourceDraft.method === 'x_api' ? 'prompt' : 'x_api') : '',
              })} />主来源失败时允许使用 {sourceDraft.method === 'x_api' ? 'Grok Prompt' : 'X Developer API'} 备用源</label>}
              {sourceDraft.fallback_enabled && <p className="campaign-source-config-warning">备用源只有在你显式启用后才会调用；涉及的 API/搜索费用按对应服务商规则计算。</p>}
            </>}
            {sourceEditor.platform === 'xiaohongshu' && <>
              <label className="field-label">创作者账号</label>
              <select className="field" value={sourceDraft.account_id} onChange={(e) => setSourceDraft({ ...sourceDraft, account_id: e.target.value })}>
                <option value="">自动选择唯一已连接账号</option>
                {accounts.filter((a) => a.platform === 'xiaohongshu').map((a) => <option key={a.id} value={a.id}>{a.label} · {a.status}</option>)}
              </select>
              <div className="campaign-source-config-note">读取 <code>creator.xiaohongshu.com/new/events</code>，复用该账号独立 XiaohongshuProfile。登录失效会显示“需要登录”，不会显示成“没有活动”。</div>
            </>}
            {sourceEditor.platform === 'douyin' && <>
              <label className="field-label">创作者账号（主来源）</label>
              <select className="field" value={sourceDraft.account_id} onChange={(e) => setSourceDraft({ ...sourceDraft, account_id: e.target.value })}>
                <option value="">自动选择唯一已连接账号</option>
                {accounts.filter((a) => a.platform === 'douyin').map((a) => <option key={a.id} value={a.id}>{a.label} · {a.status}</option>)}
              </select>
              <div className="campaign-source-config-note">优先使用 creator.douyin.com 的登录态，只读监听活动/任务接口。</div>
              <label className="r2-checkbox campaign-source-fallback"><input type="checkbox" checked={sourceDraft.tikhub_enabled} onChange={(e) => setSourceDraft({ ...sourceDraft, tikhub_enabled: e.target.checked })} />显式启用 TikHub 收费 fallback</label>
              {sourceDraft.tikhub_enabled && <>
                <label className="field-label">TikHub API Key</label>
                <input className="field" type="password" autoComplete="new-password" value={sourceDraft.tikhub_api_key} onChange={(e) => setSourceDraft({ ...sourceDraft, tikhub_api_key: e.target.value })} placeholder={sourceEditor.tikhub_api_key_set ? '已加密保存；留空保持原 Key' : '首次启用需要填写'} />
                <p className="campaign-source-config-warning">TikHub 的活动接口会计费。Ripple 不自动调用价格查询，也不会在未启用 fallback 时产生 TikHub 活动请求。</p>
              </>}
            </>}
            <div className="campaign-import-foot"><span>自动来源只读，不执行报名、投稿或领奖。</span><div><button className="btn btn-sm" disabled={sourceSaving} onClick={() => setSourceEditor(null)}>取消</button><button className="btn btn-sm btn-primary" disabled={sourceSaving} onClick={() => void saveSourceEditor()}>{sourceSaving ? '保存中…' : '保存数据源'}</button></div></div>
          </div>
        </div>
      )}

      {importUrlOpen && (
        <div className="overlay">
          <div className="modal campaign-import-url-modal">
            <div className="campaign-modal-head"><div><h3>补充导入活动 / 激励计划</h3><p>默认只做免费读取和结构化解析；确认前不会创建活动。目标平台默认继承当前频道，可在这里修改。</p></div><button onClick={() => setImportUrlOpen(false)}>×</button></div>
            <div className="campaign-import-grid">
              <label><span>目标平台</span><select className="field" value={importPlatform} onChange={(e) => setImportPlatform(e.target.value as CampaignPlatform)}>{PLATFORMS.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}</select></label>
              <label><span>导入方式</span><select className="field" value={importKind} onChange={(e) => setImportKind(e.target.value as 'url' | 'text')}><option value="url">活动 / 激励计划链接</option><option value="text">粘贴规则原文</option></select></label>
              {importKind === 'url'
                ? <label className="wide"><span>活动或激励计划链接</span><input className="field" type="url" value={importUrl} onChange={(e) => setImportUrl(e.target.value)} placeholder="https://…" autoFocus /></label>
                : <label className="wide"><span>规则原文</span><textarea className="field" value={importText} onChange={(e) => setImportText(e.target.value)} maxLength={24000} placeholder="粘贴主办方公告、官方规则或活动说明；缺失信息保持为空。" autoFocus /></label>}
              <label className="wide r2-checkbox"><input type="checkbox" checked={importUseAgent} onChange={(e) => setImportUseAgent(e.target.checked)} />可选：使用默认 Agent 基于当前原文辅助整理（会消耗 Token；不会让 Agent 联网或执行页面中的指令）</label>
            </div>
            <div className="campaign-import-foot"><span>已支持 B站、小红书官方详情和微信官方公开规则；其他链接会保留输入并转为手工确认，不会强制变成 B站。</span><div><button className="btn btn-sm" onClick={() => { setImportUrlOpen(false); openNew(importPlatform); }}>手动填写</button><button className="btn btn-sm btn-primary" disabled={importParsing || (importKind === 'url' ? !importUrl.trim() : !importText.trim())} onClick={() => void parseImport()}>{importParsing ? '解析中…' : '生成草稿'}</button></div></div>
          </div>
        </div>
      )}

      {form && (
        <div className="overlay">
          <div className="modal campaign-import-modal">
            <div className="campaign-modal-head"><div><h3>{editId ? '编辑活动规则' : '导入创作活动'}</h3><p>保存真实来源和已知规则；未说明的信息可以留空。</p></div><button onClick={() => setForm(null)}>×</button></div>
            {importWarning && !editId && <div className="campaign-import-preview-note">{importWarning}</div>}
            <div className="campaign-import-grid">
              <label><span>活动名称 *</span><input className="field" value={form.title} onChange={(e) => setForm({ ...form, title: e.target.value })} /></label>
              <label><span>平台 *</span><select className="field" value={form.platform} onChange={(e) => setForm({ ...form, platform: e.target.value as CampaignPlatform })}>{PLATFORMS.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}</select></label>
              <label><span>主办方</span><input className="field" value={form.organizer || ''} onChange={(e) => setForm({ ...form, organizer: e.target.value })} /></label>
              <label><span>活动类型</span><input className="field" value={form.activity_type || ''} onChange={(e) => setForm({ ...form, activity_type: e.target.value })} placeholder="征稿 / 激励 / 挑战…" /></label>
              <label><span>奖励类型</span><input className="field" value={form.reward_type || ''} onChange={(e) => setForm({ ...form, reward_type: e.target.value })} placeholder="现金 / 流量扶持 / 实物…" /></label>
              <label><span>奖励摘要</span><input className="field" value={form.reward_summary || ''} onChange={(e) => setForm({ ...form, reward_summary: e.target.value })} /></label>
              <label className="wide"><span>活动摘要</span><textarea className="field" value={form.summary || ''} onChange={(e) => setForm({ ...form, summary: e.target.value })} placeholder="面向用户的一段活动概览；没有可靠信息可以留空。" /></label>
              <label><span>活动开始</span><input className="field" type="date" value={dateOnly(form.starts_at)} onChange={(e) => setForm({ ...form, starts_at: e.target.value })} /></label>
              <label><span>投稿截止</span><input className="field" type="date" value={dateOnly(form.submit_deadline)} onChange={(e) => setForm({ ...form, submit_deadline: e.target.value })} /></label>
              <label><span>资格状态</span><select className="field" value={form.qualification_state || 'unknown'} onChange={(e) => setForm({ ...form, qualification_state: e.target.value as Campaign['qualification_state'] })}><option value="unknown">资格信息不足</option><option value="eligible">可参与</option><option value="ineligible">暂不符合</option></select></label>
              <label><span>参与账号</span><select className="field" value={form.account_id || ''} onChange={(e) => setForm({ ...form, account_id: e.target.value })}><option value="">未绑定账号</option>{accounts.filter((a) => a.platform === form.platform).map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}</select></label>
              <label className="wide"><span>来源链接</span><input className="field" type="url" value={form.source_url || ''} onChange={(e) => setForm({ ...form, source_url: e.target.value })} placeholder="https://…" /></label>
              <label className="wide"><span>参与条件（每行一条）</span><textarea className="field" value={eligibilityText} onChange={(e) => setEligibilityText(e.target.value)} /></label>
              <label className="wide"><span>内容要求（每行一条）</span><textarea className="field" value={requirementsText} onChange={(e) => setRequirementsText(e.target.value)} /></label>
              <div className="wide campaign-spec-editor"><strong>参赛作品规格</strong><small>没有明确规则的项目保持为空，不会解释成“不限”。</small></div>
              <div className="wide campaign-format-picker"><span>作品形式</span>{['video','short_video','long_video','image_text','text','live','audio'].map((key) => <label key={key}><input type="checkbox" checked={formSpec.formats.includes(key)} onChange={(e) => updateSubmissionSpec({ formats: e.target.checked ? Array.from(new Set([...formSpec.formats, key])) : formSpec.formats.filter((x) => x !== key) })} />{FORMAT_LABELS[key]}</label>)}</div>
              <label className="wide"><span>内容方向（每行一条）</span><textarea className="field" value={joined(formSpec.content_directions)} onChange={(e) => updateSubmissionSpec({ content_directions: splitLines(e.target.value) })} placeholder="攻略 / 实机体验 / 剧情二创…" /></label>
              <label className="wide"><span>风格要求（每行一条）</span><textarea className="field" value={joined(formSpec.style_requirements)} onChange={(e) => updateSubmissionSpec({ style_requirements: splitLines(e.target.value) })} placeholder="轻松搞笑 / 专业讲解 / 真实体验…" /></label>
              <label><span>视频最短时长（秒）</span><input className="field" type="number" min="0" value={formSpec.duration_seconds.min ?? ''} onChange={(e) => updateSubmissionSpec({ duration_seconds: { ...formSpec.duration_seconds, min: e.target.value ? Number(e.target.value) : null } })} /></label>
              <label><span>视频最长时长（秒）</span><input className="field" type="number" min="0" value={formSpec.duration_seconds.max ?? ''} onChange={(e) => updateSubmissionSpec({ duration_seconds: { ...formSpec.duration_seconds, max: e.target.value ? Number(e.target.value) : null } })} /></label>
              <label><span>画面比例</span><input className="field" value={formSpec.aspect_ratios.join('，')} onChange={(e) => updateSubmissionSpec({ aspect_ratios: splitLines(e.target.value) })} placeholder="16:9，9:16" /></label>
              <label><span>分辨率</span><input className="field" value={formSpec.resolutions.join('，')} onChange={(e) => updateSubmissionSpec({ resolutions: splitLines(e.target.value) })} placeholder="1080P，4K" /></label>
              <label><span>画面方向</span><select className="field" value={formSpec.orientation || ''} onChange={(e) => updateSubmissionSpec({ orientation: (e.target.value || null) as CampaignSubmissionSpec['orientation'] })}><option value="">未说明</option><option value="vertical">竖屏</option><option value="horizontal">横屏</option><option value="square">方形</option></select></label>
              <label><span>最少图片数</span><input className="field" type="number" min="0" value={formSpec.image_count.min ?? ''} onChange={(e) => updateSubmissionSpec({ image_count: { ...formSpec.image_count, min: e.target.value ? Number(e.target.value) : null } })} /></label>
              <label><span>最多图片数</span><input className="field" type="number" min="0" value={formSpec.image_count.max ?? ''} onChange={(e) => updateSubmissionSpec({ image_count: { ...formSpec.image_count, max: e.target.value ? Number(e.target.value) : null } })} /></label>
              <label><span>最少字数</span><input className="field" type="number" min="0" value={formSpec.text_length.min ?? ''} onChange={(e) => updateSubmissionSpec({ text_length: { ...formSpec.text_length, min: e.target.value ? Number(e.target.value) : null } })} /></label>
              <label><span>最多字数</span><input className="field" type="number" min="0" value={formSpec.text_length.max ?? ''} onChange={(e) => updateSubmissionSpec({ text_length: { ...formSpec.text_length, max: e.target.value ? Number(e.target.value) : null } })} /></label>
              <label><span>直播最短时长（秒）</span><input className="field" type="number" min="0" value={formSpec.live.min_duration_seconds ?? ''} onChange={(e) => updateSubmissionSpec({ live: { ...formSpec.live, min_duration_seconds: e.target.value ? Number(e.target.value) : null } })} /></label>
              <label><span>直播分区</span><input className="field" value={formSpec.live.required_category} onChange={(e) => updateSubmissionSpec({ live: { ...formSpec.live, required_category: e.target.value } })} /></label>
              <label className="wide"><span>直播标题关键词</span><input className="field" value={formSpec.live.title_keywords.join('，')} onChange={(e) => updateSubmissionSpec({ live: { ...formSpec.live, title_keywords: splitLines(e.target.value) } })} /></label>
              {([['original_required','原创'],['first_publish_required','首发'],['exclusive_required','独家']] as const).map(([key,label]) => <label key={key}><span>{label}要求</span><select className="field" value={formSpec[key] == null ? 'unknown' : formSpec[key] ? 'yes' : 'no'} onChange={(e) => updateSubmissionSpec({ [key]: e.target.value === 'unknown' ? null : e.target.value === 'yes' } as Partial<CampaignSubmissionSpec>)}><option value="unknown">未说明</option><option value="yes">必须</option><option value="no">明确不要求</option></select></label>)}
              <label><span>最少投稿数</span><input className="field" type="number" min="0" value={formSpec.min_entries ?? ''} onChange={(e) => updateSubmissionSpec({ min_entries: e.target.value ? Number(e.target.value) : null })} /></label>
              <label><span>最多投稿数</span><input className="field" type="number" min="0" value={formSpec.max_entries ?? ''} onChange={(e) => updateSubmissionSpec({ max_entries: e.target.value ? Number(e.target.value) : null })} /></label>
              <label className="wide"><span>投稿方式</span><input className="field" value={formSpec.submission_method} onChange={(e) => updateSubmissionSpec({ submission_method: e.target.value })} placeholder="活动页报名 / 指定分区投稿 / 话题页投稿…" /></label>
              <label className="wide"><span>指定 @账号（逗号或换行分隔）</span><input className="field" value={formSpec.required_mentions.join('，')} onChange={(e) => updateSubmissionSpec({ required_mentions: splitLines(e.target.value) })} /></label>
              <label className="wide"><span>指定音乐（逗号或换行分隔）</span><input className="field" value={formSpec.required_music.join('，')} onChange={(e) => updateSubmissionSpec({ required_music: splitLines(e.target.value) })} /></label>
              <label className="wide"><span>奖品 / 奖励（每行一条）</span><textarea className="field" value={prizesText} onChange={(e) => setPrizesText(e.target.value)} placeholder="例如：瓜分 5 万元奖金池 / 流量扶持 / 实物奖品" /></label>
              <label className="wide"><span>获奖条件（每行一条）</span><textarea className="field" value={winningConditionsText} onChange={(e) => setWinningConditionsText(e.target.value)} placeholder="例如：单稿播放量 ≥ 20 万；进入评审 TOP 10" /></label>
              <label className="wide"><span>奖励规则（每行一条）</span><textarea className="field" value={rewardRulesText} onChange={(e) => setRewardRulesText(e.target.value)} /></label>
              <label className="wide"><span>指定话题 / 标签</span><input className="field" value={topicsText} onChange={(e) => setTopicsText(e.target.value)} placeholder="#开学季，#效率工具" /></label>
              <label className="wide"><span>AI 使用要求</span><input className="field" value={form.ai_policy || ''} onChange={(e) => setForm({ ...form, ai_policy: e.target.value || 'unknown' })} placeholder="未说明 / 允许辅助 / 禁止自动生成…" /></label>
              <label className="wide"><span>来源原文 / 补充备注</span><textarea className="field" value={form.note || ''} onChange={(e) => setForm({ ...form, note: e.target.value })} /></label>
            </div>
            <div className="campaign-import-foot"><span>保存前请确认解析结果；用户确认后的规则不会被后续自动采集静默覆盖。</span><div><button className="btn btn-sm" onClick={() => setForm(null)}>取消</button><button className="btn btn-sm btn-primary" disabled={saving || !form.title.trim()} onClick={() => void submitCampaign()}>{saving ? '保存中…' : editId ? '保存新规则版本' : '确认并导入'}</button></div></div>
          </div>
        </div>
      )}

      {recommendOpen && (
        <div className="overlay">
          <div className="modal idea-rec-modal campaign-rec-modal">
            <div className="idea-rec-head"><div><h3>AI 选题灵感</h3><p>结合活动规则、账号画像与实际读取到的热点生成。</p></div><button className="icon-btn" disabled={recommending} onClick={() => setRecommendOpen(false)}>×</button></div>
            {recommendCampaign && <div className="campaign-rec-context">
              <span><small>活动</small>{recommendCampaign.title}</span>
              <span><small>画像</small>{persona || '未选择'}</span>
              <span><small>目标平台</small>{recommendCampaign.platform_label}</span>
              <span><small>规则版本</small>v{recommendCampaign.rule_version}</span>
            </div>}
            {recommending && <div className="idea-rec-loading"><span className="spinner" /> 正在结合活动规则与热点生成…</div>}
            {recommendError && <div className="notice-error">{recommendError}</div>}
            {recommendResult && <div className="idea-rec-list">
              {recommendResult.recommendations.map((rec, index) => (
                <article className="idea-rec-card campaign-rec-card" key={rec.title}>
                  <div className="idea-rec-score">{index + 1}</div>
                  <div className="idea-rec-main">
                    <h4>{rec.title}</h4>
                    <div className="idea-rec-angle"><b>内容角度：</b>{rec.angle}</div>
                    <div className="idea-rec-reason"><b>推荐理由：</b>{rec.reason}</div>
                    <div className="idea-rec-tags">
                      {rec.platforms.map((p) => <span key={p}>{platformLabel(p)}</span>)}
                      {rec.trend_refs.map((ref) => <span className="trend-ref" key={ref}>热点 · {ref}</span>)}
                    </div>
                    {!!rec.requirements?.length && <div className="campaign-rec-detail"><b>创作要求</b>{rec.requirements.map((x) => <span key={x}>✓ {x}</span>)}</div>}
                    {!!rec.pending_checks?.length && <div className="campaign-rec-detail pending"><b>待确认事项</b>{rec.pending_checks.map((x) => <span key={x}>! {x}</span>)}</div>}
                  </div>
                  <div className="idea-rec-actions">
                    <button className="btn btn-sm" disabled={added.has(rec.title)} onClick={() => void addRecommendation(rec)}>{added.has(rec.title) ? '已加入' : '加入选题库'}</button>
                    <button className="btn btn-sm btn-primary" onClick={() => startRecommendationContent(rec)}>做内容</button>
                  </div>
                </article>
              ))}
            </div>}
            {recommendResult && <div className="idea-rec-foot"><span>活动规则缺失的信息会保留为待确认项；发布前仍需再次核验资格与截止时间。</span><button className="btn btn-sm" onClick={() => setRecommendOpen(false)}>完成</button></div>}
          </div>
        </div>
      )}

      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
