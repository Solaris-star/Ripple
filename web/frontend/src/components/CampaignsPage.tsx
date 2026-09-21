import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  configureCampaignSource, createCampaign, createIdea, createSchedule, fetchCampaigns, fetchCampaignSources,
  fetchStatus, fetchTrends, recommendIdeas, refreshCampaigns, saveCampaign, updateCampaign, verifyCampaign,
} from '../lib/api';
import type {
  Campaign, CampaignInput, CampaignPlatform, CampaignSourceCapability, IdeaRecommendation,
  IdeaRecommendResponse, PersonaItem, TopicUseContext, TrendGroup,
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

function sourceName(campaign: Campaign): string {
  if (campaign.source_type === 'user_import') return '用户导入';
  if (campaign.platform === 'bilibili' && campaign.source_type === 'platform_public') return 'B站官方活动页';
  if (campaign.source_type === 'x_search') return 'xAI · X Search';
  if (campaign.source_type === 'x_developer_api') return 'X Developer API';
  if (campaign.platform === 'xiaohongshu' && campaign.source_type.includes('creator')) return '小红书创作服务平台';
  if (campaign.platform === 'douyin' && campaign.source_type.includes('creator')) return '抖音创作者中心';
  if (campaign.source_type === 'third_party_api') return 'TikHub';
  return (campaign.platform_label || platformDisplayName(campaign.platform)) + '活动来源';
}

function qualificationBadge(campaign: Campaign): { label: string; cls: string } | null {
  if (campaign.qualification_state === 'eligible') return { label: '可参与', cls: 'ok' };
  if (campaign.qualification_state === 'ineligible') return { label: '暂不符合', cls: 'bad' };
  if (campaign.eligibility?.length) return { label: '资格待核验', cls: 'warn' };
  return null;
}

function concise(values: string[] | undefined, fallback: string, limit = 2): string {
  const rows = (values || []).map((x) => x.trim()).filter(Boolean);
  return rows.length ? rows.slice(0, limit).join('；') + (rows.length > limit ? ' 等 ' + rows.length + ' 条' : '') : fallback;
}

function campaignTime(campaign: Campaign): string {
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

function platformLabel(key: string): string {
  return platformDisplayName(key);
}

export default function CampaignsPage({
  onUseTopic, persona, aiReady, personas, onPersonaChange, onNewPersona, onOpenSettings,
}: CampaignsPageProps) {
  const [campaigns, setCampaigns] = useState<Campaign[]>([]);
  const [sources, setSources] = useState<CampaignSourceCapability[]>([]);
  const [automaticCount, setAutomaticCount] = useState(0);
  const [trendGroups, setTrendGroups] = useState<TrendGroup[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [toast, setToast] = useState('');
  const [selected, setSelected] = useState<Campaign | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const [verifying, setVerifying] = useState(false);
  const [sourceEditor, setSourceEditor] = useState<CampaignSourceCapability | null>(null);
  const [sourceDraft, setSourceDraft] = useState<SourceDraft>({
    method: '', x_api_account_id: '', fallback_method: '', fallback_enabled: false,
    account_id: '', tikhub_enabled: false, tikhub_api_key: '',
  });
  const [sourceSaving, setSourceSaving] = useState(false);

  const [platformFilter, setPlatformFilter] = useState('all');
  const [typeFilter, setTypeFilter] = useState('all');
  const [rewardFilter, setRewardFilter] = useState('all');
  const [deadlineFilter, setDeadlineFilter] = useState('all');
  const [qualificationFilter, setQualificationFilter] = useState('all');
  const [accountFilter, setAccountFilter] = useState('all');
  const [sortMode, setSortMode] = useState<'recommend' | 'new' | 'deadline' | 'saved'>('recommend');

  const [form, setForm] = useState<CampaignInput | null>(null);
  const [editId, setEditId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [eligibilityText, setEligibilityText] = useState('');
  const [requirementsText, setRequirementsText] = useState('');
  const [prizesText, setPrizesText] = useState('');
  const [winningConditionsText, setWinningConditionsText] = useState('');
  const [rewardRulesText, setRewardRulesText] = useState('');
  const [topicsText, setTopicsText] = useState('');

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

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const runtime = await fetchStatus();
      if (!runtime.features?.campaigns || !runtime.features?.campaign_sources_v2) {
        setCampaigns([]); setSources([]); setAutomaticCount(0);
        setError('当前运行中的 Ripple 后端版本较旧，尚未加载活动中心 API。请重启 Ripple 服务后再试。');
        return;
      }
      const [campaignRows, sourceState, accountRows] = await Promise.all([
        fetchCampaigns(), fetchCampaignSources(), rippleApi<Account[]>('/api/ripple/accounts'),
      ]);
      setAccounts(accountRows);
      setCampaigns(campaignRows);
      setSources(sourceState.items || []);
      setAutomaticCount(sourceState.automatic_count || 0);
      setSelected((current) => current ? campaignRows.find((x) => x.id === current.id) || null : null);
      setError('');
    } catch (e) {
      const message = e instanceof Error ? e.message : '活动数据读取失败';
      setError(message === 'Not Found'
        ? '当前运行中的 Ripple 后端版本较旧，尚未加载活动中心 API。请重启 Ripple 服务后再试。'
        : message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => { void load(); }, 60 * 1000);
    return () => window.clearInterval(timer);
  }, [load]);

  useEffect(() => {
    const selectedTrends = loadTrendSelection();
    if (!selectedTrends.length) return;
    fetchTrends(selectedTrends.join(','), 6)
      .then((value) => setTrendGroups(value.trends || []))
      .catch(() => setTrendGroups([]));
  }, []);

  const refreshNow = async () => {
    if (refreshing) return;
    const platforms = Array.from(new Set(sources.filter((source) => source.automatic).map((source) => source.platform)));
    if (!platforms.length) { showToast('当前没有已就绪的自动活动源'); return; }
    setRefreshing(true); setError('');
    try {
      const result = await refreshCampaigns(platforms, true);
      setCampaigns(result.campaigns);
      setSelected((current) => current ? result.campaigns.find((row) => row.id === current.id) || current : null);
      setSources(result.sources.items || []);
      setAutomaticCount(result.sources.automatic_count || 0);
      const fresh = result.results.filter((row) => row.status === 'fresh');
      const stale = result.results.filter((row) => row.status !== 'fresh' && row.status !== 'cached');
      showToast(`已刷新 ${fresh.length} 个来源${stale.length ? `，${stale.length} 个来源需要处理` : ''}`);
    } catch (e) { setError(e instanceof Error ? e.message : '活动刷新失败'); }
    finally { setRefreshing(false); }
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

  const activityTypes = useMemo(() => Array.from(new Set(campaigns.map((x) => x.activity_type).filter(Boolean))), [campaigns]);
  const rewardTypes = useMemo(() => Array.from(new Set(campaigns.map((x) => x.reward_type).filter(Boolean))), [campaigns]);

  const visible = useMemo(() => {
    let rows = campaigns.filter((campaign) => {
      if (platformFilter !== 'all' && campaign.platform !== platformFilter) return false;
      if (typeFilter !== 'all' && campaign.activity_type !== typeFilter) return false;
      if (rewardFilter !== 'all' && campaign.reward_type !== rewardFilter) return false;
      if (qualificationFilter !== 'all' && campaign.qualification_state !== qualificationFilter) return false;
      if (accountFilter !== 'all' && campaign.account_id !== accountFilter) return false;
      if (deadlineFilter !== 'all') {
        const days = daysUntil(campaign.submit_deadline);
        if (deadlineFilter === 'none' && days !== null) return false;
        if (deadlineFilter === '7' && (days === null || days < 0 || days > 7)) return false;
        if (deadlineFilter === '30' && (days === null || days < 0 || days > 30)) return false;
      }
      if (sortMode === 'saved' && !campaign.saved) return false;
      return true;
    });
    rows = [...rows].sort((a, b) => {
      if (sortMode === 'new') return b.updated_at - a.updated_at;
      if (sortMode === 'deadline') {
        const da = dateOnly(a.submit_deadline) || '9999-99-99';
        const db = dateOnly(b.submit_deadline) || '9999-99-99';
        return da.localeCompare(db);
      }
      const priority = (item: Campaign) => (
        (item.status === 'active' ? 40 : item.status === 'upcoming' ? 20 : 0)
        + (item.qualification_state === 'eligible' ? 20 : item.qualification_state === 'unknown' ? 8 : -30)
        + (item.saved ? 6 : 0)
        + (item.submit_deadline && (daysUntil(item.submit_deadline) ?? 999) >= 0 ? 4 : 0)
      );
      return priority(b) - priority(a) || b.updated_at - a.updated_at;
    });
    return rows;
  }, [campaigns, platformFilter, typeFilter, rewardFilter, deadlineFilter, qualificationFilter, accountFilter, sortMode]);

  const stats = useMemo(() => ({
    active: campaigns.filter((x) => x.status === 'active').length,
    soon: campaigns.filter((x) => {
      const days = daysUntil(x.submit_deadline);
      return days !== null && days >= 0 && days <= 7;
    }).length,
    saved: campaigns.filter((x) => x.saved).length,
  }), [campaigns]);

  const trends = useMemo(() => trendGroups.flatMap((group) =>
    group.items.slice(0, 4).map((item) => ({ ...item, platform: group.label, platformKey: group.platform, status: group.status }))).slice(0, 10), [trendGroups]);

  const openNew = () => {
    setEditId(null);
    setForm(emptyCampaign());
    setEligibilityText('');
    setRequirementsText('');
    setPrizesText('');
    setWinningConditionsText('');
    setRewardRulesText('');
    setTopicsText('');
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

  return (
    <div className="page-scroll campaign-page">
      <div className="page-head campaign-head">
        <div>
          <h1 className="page-title"><IconCompass size={22} /> 活动广场</h1>
          <p className="page-subtitle">聚合 X、小红书、抖音、B站、微信公众号、微信视频号的创作活动与激励活动，结合热点生成选题灵感。</p>
        </div>
        <div className="campaign-head-actions">
          <button className="btn btn-sm btn-primary" title="立即重新检查已启用的活动源；单个活动规则可在详情中单独重新核验" disabled={refreshing || loading} onClick={() => void refreshNow()}>
            <IconRefresh size={13} /> {refreshing ? '刷新中…' : '刷新活动'}
          </button>
          <button className="btn btn-sm" onClick={openNew}>+ 补充导入</button>
        </div>
      </div>

      <div className="campaign-stats">
        <div className="campaign-stat"><span className="campaign-stat-dot ok" /><span>进行中活动</span><strong>{stats.active}</strong></div>
        <div className="campaign-stat"><span className="campaign-stat-dot warn" /><span>即将截止</span><strong>{stats.soon}</strong></div>
        <div className="campaign-stat"><span className="campaign-stat-dot saved" /><span>已收藏</span><strong>{stats.saved}</strong></div>
      </div>

      <div className="campaign-source-note">
        <IconRefresh size={15} />
        <div>
          <strong>已就绪 {automaticCount} 个自动活动源</strong>
          <span>活动源由 Ripple 后端定时同步，页面无需保持打开；右上角“刷新活动”可随时手动重新检查。收费备用源未经显式启用不会调用。</span>
        </div>
      </div>
      <section className="campaign-source-status-grid">
        {sources.map((source) => {
          const sync = source.last_sync;
          const configurable = ['x', 'xiaohongshu', 'douyin'].includes(source.platform);
          const selectedSource = platformFilter === source.platform;
          const toggleSource = () => setPlatformFilter(selectedSource ? 'all' : source.platform);
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
            {source.automatic && source.sync_interval_seconds ? <small>自动频率：{syncIntervalLabel(source.sync_interval_seconds)}</small> : null}
            {source.cost_note && <small>{source.cost_note}</small>}
            {sync?.at && <small>最近同步：{new Date(sync.at * 1000).toLocaleString('zh-CN')} · {sync.status}{sync.count != null ? ` · ${sync.count} 条` : ''}{sync.fallback_used ? ' · 使用备用源' : ''}</small>}
            {source.automatic && source.next_sync_at ? <small>下次自动检查约：{new Date(source.next_sync_at * 1000).toLocaleString('zh-CN')}</small> : null}
            {sync?.error && <small className="error">{sync.error}</small>}
            <div className="campaign-source-status-actions">
              {configurable && <button className="r2-text-button" onClick={() => openSourceEditor(source)}>配置</button>}
            </div>
          </article>;
        })}
      </section>

      <div className="campaign-filters">
        <select className="field" value={platformFilter} onChange={(e) => setPlatformFilter(e.target.value)}>
          <option value="all">全部平台</option>
          {PLATFORMS.map((p) => <option key={p.key} value={p.key}>{p.label}</option>)}
        </select>
        <select className="field" value={typeFilter} onChange={(e) => setTypeFilter(e.target.value)}>
          <option value="all">全部活动类型</option>
          {activityTypes.map((x) => <option key={x}>{x}</option>)}
        </select>
        <select className="field" value={rewardFilter} onChange={(e) => setRewardFilter(e.target.value)}>
          <option value="all">全部奖励类型</option>
          {rewardTypes.map((x) => <option key={x}>{x}</option>)}
        </select>
        <select className="field" value={deadlineFilter} onChange={(e) => setDeadlineFilter(e.target.value)}>
          <option value="all">全部截止时间</option>
          <option value="7">7 天内截止</option>
          <option value="30">30 天内截止</option>
          <option value="none">未说明截止时间</option>
        </select>
        <select className="field" value={qualificationFilter} onChange={(e) => setQualificationFilter(e.target.value)}>
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
        <select className="field" value={accountFilter} onChange={(e) => setAccountFilter(e.target.value)}>
          <option value="all">全部参与账号</option>
          {accounts.map((a) => <option key={a.id} value={a.id}>{a.label} · {platformLabel(a.platform)}</option>)}
        </select>
      </div>

      <div className="campaign-sort">
        {([
          ['recommend', '推荐排序'], ['new', '最近更新'], ['deadline', '即将截止'], ['saved', '已收藏'],
        ] as const).map(([key, label]) => (
          <button key={key} className={sortMode === key ? 'active' : ''} onClick={() => setSortMode(key)}>{label}</button>
        ))}
        <span>共 {visible.length} 个活动</span>
      </div>

      {error && <div className="notice-error">{error}</div>}
      <div className="campaign-layout">
        <main className="campaign-list">
          {loading && <div className="campaign-empty">正在读取活动…</div>}
          {!loading && visible.length === 0 && (
            <div className="campaign-empty">
              <IconCompass size={28} />
              <strong>{campaigns.length ? '当前筛选条件没有匹配活动' : '还没有已确认的创作活动'}</strong>
              <p>{campaigns.length ? '调整筛选条件继续查看。' : '可点击“刷新活动”从已就绪的数据源自动获取；手动导入用于补充遗漏活动。Ripple 不会用虚构活动填充这里。'}</p>
              {!campaigns.length && <div style={{ display: 'flex', gap: 8 }}><button className="btn btn-primary btn-sm" disabled={refreshing} onClick={() => void refreshNow()}>刷新活动</button><button className="btn btn-sm" onClick={openNew}>补充导入</button></div>}
            </div>
          )}
          <div className="campaign-grid">
            {visible.map((campaign) => {
              const qualification = qualificationBadge(campaign);
              const sourceText = sourceName(campaign) + ' · ' + sourceLabel(campaign);
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
                    <div><small>参与条件</small><span>{concise(campaign.eligibility, '原活动页暂未解析到明确前置条件')}</span></div>
                    <div><small>奖品 / 奖励</small><span>{concise(campaign.prizes, campaign.reward_summary || '原活动页暂未解析到明确奖品')}</span></div>
                    <div><small>获奖条件</small><span>{concise(campaign.winning_conditions, '原活动页暂未解析到明确获奖门槛')}</span></div>
                  </div>
                  <div className="campaign-actions">
                    <button className="btn btn-sm" onClick={() => setSelected(campaign)}>查看详情</button>
                    <button className="btn btn-sm btn-primary" disabled={campaign.status === 'ended' || campaign.status === 'cancelled'}
                      onClick={() => void generateIdeas(campaign)}><IconSkills size={13} /> 生成选题</button>
                  </div>
                </article>
              );
            })}
          </div>

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
            <section><h4>参与条件</h4>{selected.eligibility?.length ? <ul>{selected.eligibility.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">原活动页暂未解析到明确参与条件。</p>}</section>
            <section><h4>内容要求</h4>{selected.content_requirements?.length ? <ul>{selected.content_requirements.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">原活动页暂未解析到明确内容要求。</p>}</section>
            <section><h4>奖品 / 奖励</h4>{selected.prizes?.length ? <ul>{selected.prizes.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">{selected.reward_summary || '原活动页暂未解析到明确奖品。'}</p>}</section>
            <section><h4>获奖条件</h4>{selected.winning_conditions?.length ? <ul>{selected.winning_conditions.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">原活动页暂未解析到明确获奖门槛。</p>}</section>
            <section><h4>奖励规则</h4>{selected.reward_rules?.length ? <ul>{selected.reward_rules.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">未解析到额外的奖励计算或发放规则。</p>}</section>
            <section><h4>指定话题 / 标签</h4>{selected.required_topics?.length ? <div className="campaign-topic-tags">{selected.required_topics.map((x) => <span key={x}>{x.startsWith('#') ? x : `# ${x}`}</span>)}</div> : <p className="campaign-unknown">未解析到指定话题。</p>}</section>
            <section><h4>AI 使用要求</h4><p className="campaign-copy">{selected.ai_policy === 'unknown' || !selected.ai_policy ? '原活动规则未说明 AI 使用要求。' : selected.ai_policy}</p></section>
            <section><h4>来源与核验</h4>
              <p className="campaign-source-name"><strong>{sourceName(selected)}</strong> · {sourceLabel(selected)}</p>
              {selected.source_url ? <a className="campaign-source-link" href={selected.source_url} target="_blank" rel="noreferrer">{selected.source_url}</a> : <p className="campaign-unknown">没有保存来源链接。</p>}
              <p className="campaign-verify">首次发现：{selected.discovered_at ? new Date(selected.discovered_at * 1000).toLocaleString('zh-CN') : '未知'} · 最后看到：{selected.last_seen_at ? new Date(selected.last_seen_at * 1000).toLocaleString('zh-CN') : '未知'}</p>
              <p className="campaign-verify">规则最近核验：{selected.last_verified_at ? new Date(selected.last_verified_at * 1000).toLocaleString('zh-CN') : '尚未完整核验'} · 规则版本 v{selected.rule_version}</p>
            </section>
            <footer>
              <button className="btn btn-primary campaign-generate" disabled={selected.status === 'ended' || selected.status === 'cancelled'}
                onClick={() => void generateIdeas(selected)}><IconSkills size={15} /> 活动 + 热点生成选题</button>
              <div>
                <button className="btn btn-sm" onClick={() => void addCalendar(selected)}><IconCalendar size={13} /> 加入选题日历</button>
                {selected.source_url && <a className="btn btn-sm" href={selected.source_url} target="_blank" rel="noreferrer">打开原活动页</a>}
                {selected.platform === 'bilibili' && <button className="btn btn-sm" disabled={verifying} onClick={() => void verifySelected()}><IconRefresh size={13} /> {verifying ? '核验中…' : '重新核验规则'}</button>}
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
                <option value="xai">Grok / xAI · X Search</option>
                <option value="x_api">X Developer API</option>
              </select>
              {sourceDraft.method === 'xai' && <div className="campaign-source-config-note">
                xAI 方式使用“设置 → AI / Agent Provider”里的「X 活动发现」路由，并要求 X Search 能力测试通过。X Search 可能产生调用费用。
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
                fallback_method: e.target.checked ? (sourceDraft.method === 'xai' ? 'x_api' : 'xai') : '',
              })} />主来源失败时允许使用 {sourceDraft.method === 'xai' ? 'X Developer API' : 'xAI X Search'} 备用源</label>}
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

      {form && (
        <div className="overlay">
          <div className="modal campaign-import-modal">
            <div className="campaign-modal-head"><div><h3>{editId ? '编辑活动规则' : '导入创作活动'}</h3><p>保存真实来源和已知规则；未说明的信息可以留空。</p></div><button onClick={() => setForm(null)}>×</button></div>
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
              <label className="wide"><span>奖品 / 奖励（每行一条）</span><textarea className="field" value={prizesText} onChange={(e) => setPrizesText(e.target.value)} placeholder="例如：瓜分 5 万元奖金池 / 流量扶持 / 实物奖品" /></label>
              <label className="wide"><span>获奖条件（每行一条）</span><textarea className="field" value={winningConditionsText} onChange={(e) => setWinningConditionsText(e.target.value)} placeholder="例如：单稿播放量 ≥ 20 万；进入评审 TOP 10" /></label>
              <label className="wide"><span>奖励规则（每行一条）</span><textarea className="field" value={rewardRulesText} onChange={(e) => setRewardRulesText(e.target.value)} /></label>
              <label className="wide"><span>指定话题 / 标签</span><input className="field" value={topicsText} onChange={(e) => setTopicsText(e.target.value)} placeholder="#开学季，#效率工具" /></label>
              <label className="wide"><span>AI 使用要求</span><input className="field" value={form.ai_policy || ''} onChange={(e) => setForm({ ...form, ai_policy: e.target.value || 'unknown' })} placeholder="未说明 / 允许辅助 / 禁止自动生成…" /></label>
              <label className="wide"><span>来源原文 / 补充备注</span><textarea className="field" value={form.note || ''} onChange={(e) => setForm({ ...form, note: e.target.value })} /></label>
            </div>
            <div className="campaign-import-foot"><span>导入内容默认标记为“用户导入”，不会冒充官方核验。</span><div><button className="btn btn-sm" onClick={() => setForm(null)}>取消</button><button className="btn btn-sm btn-primary" disabled={saving || !form.title.trim()} onClick={() => void submitCampaign()}>{saving ? '保存中…' : editId ? '保存新规则版本' : '导入活动'}</button></div></div>
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
