import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  createCampaign, createIdea, createSchedule, fetchCampaigns, fetchCampaignSources, fetchTrends,
  recommendIdeas, saveCampaign, updateCampaign,
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

interface CampaignsPageProps {
  onUseTopic: (input: string | TopicUseContext) => void;
  persona: string;
  aiReady: boolean;
  personas: PersonaItem[];
  onPersonaChange: (name: string) => void;
  onNewPersona: () => void;
}

const PLATFORMS: { key: CampaignPlatform; label: string }[] = [
  { key: 'x', label: 'X' },
  { key: 'xiaohongshu', label: '小红书' },
  { key: 'douyin', label: '抖音' },
  { key: 'bilibili', label: 'B站' },
  { key: 'wechat', label: '微信公众号' },
  { key: 'weixin-channels', label: '微信视频号' },
];

const QUALIFICATION: Record<string, { label: string; cls: string }> = {
  eligible: { label: '已知符合', cls: 'ok' },
  ineligible: { label: '已知不符合', cls: 'bad' },
  unknown: { label: '待确认', cls: 'warn' },
};

const STATUS: Record<string, string> = {
  active: '进行中',
  upcoming: '即将开始',
  ended: '已结束',
  cancelled: '已取消',
  unknown: '状态未知',
};

const SOURCE_STATUS: Record<string, string> = {
  imported: '用户导入',
  verified: '官方已核验',
  stale: '缓存/待复核',
  unavailable: '来源不可用',
};

const emptyCampaign = (): CampaignInput => ({
  title: '',
  platform: 'xiaohongshu',
  organizer: '',
  organizer_type: 'unknown',
  activity_type: '征稿/活动',
  reward_type: '',
  reward_summary: '',
  starts_at: '',
  signup_deadline: '',
  submit_deadline: '',
  stats_deadline: '',
  timezone: '',
  eligibility: [],
  qualification_state: 'unknown',
  content_requirements: [],
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

function platformLabel(key: string): string {
  return PLATFORMS.find((p) => p.key === key)?.label || key;
}

export default function CampaignsPage({
  onUseTopic, persona, aiReady, personas, onPersonaChange, onNewPersona,
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
      const [campaignRows, sourceState] = await Promise.all([fetchCampaigns(), fetchCampaignSources()]);
      setCampaigns(campaignRows);
      setSources(sourceState.items || []);
      setAutomaticCount(sourceState.automatic_count || 0);
      setSelected((current) => current ? campaignRows.find((x) => x.id === current.id) || null : null);
      setError('');
    } catch (e) {
      setError(e instanceof Error ? e.message : '活动数据读取失败');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  useEffect(() => {
    const selectedTrends = loadTrendSelection();
    if (!selectedTrends.length) return;
    fetchTrends(selectedTrends.join(','), 6)
      .then((value) => setTrendGroups(value.trends || []))
      .catch(() => setTrendGroups([]));
    rippleApi<Account[]>('/api/ripple/accounts').then(setAccounts).catch(() => setAccounts([]));
  }, []);

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
    group.items.slice(0, 4).map((item) => ({ ...item, platform: group.label, status: group.status }))).slice(0, 10), [trendGroups]);

  const openNew = () => {
    setEditId(null);
    setForm(emptyCampaign());
    setEligibilityText('');
    setRequirementsText('');
    setRewardRulesText('');
    setTopicsText('');
  };

  const openEdit = (campaign: Campaign) => {
    setEditId(campaign.id);
    setForm({
      title: campaign.title, platform: campaign.platform, organizer: campaign.organizer,
      organizer_type: campaign.organizer_type, activity_type: campaign.activity_type,
      reward_type: campaign.reward_type, reward_summary: campaign.reward_summary,
      starts_at: campaign.starts_at, signup_deadline: campaign.signup_deadline,
      submit_deadline: campaign.submit_deadline, stats_deadline: campaign.stats_deadline,
      timezone: campaign.timezone, qualification_state: campaign.qualification_state,
      ai_policy: campaign.ai_policy, source_url: campaign.source_url, note: campaign.note,
      status: campaign.status, account_id: campaign.account_id,
      eligibility: campaign.eligibility, content_requirements: campaign.content_requirements,
      reward_rules: campaign.reward_rules, required_topics: campaign.required_topics,
    });
    setEligibilityText(joined(campaign.eligibility));
    setRequirementsText(joined(campaign.content_requirements));
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
        <button className="btn btn-sm btn-primary" onClick={openNew}>+ 导入活动</button>
      </div>

      <div className="campaign-stats">
        <div className="campaign-stat"><span className="campaign-stat-dot ok" /><span>进行中活动</span><strong>{stats.active}</strong></div>
        <div className="campaign-stat"><span className="campaign-stat-dot warn" /><span>即将截止</span><strong>{stats.soon}</strong></div>
        <div className="campaign-stat"><span className="campaign-stat-dot saved" /><span>已收藏</span><strong>{stats.saved}</strong></div>
      </div>

      <div className="campaign-source-note">
        <IconRefresh size={15} />
        <div>
          <strong>{automaticCount > 0 ? `已启用 ${automaticCount} 个自动活动源` : '自动活动源尚未通过 Ripple 验收'}</strong>
          <span>{automaticCount > 0
            ? '自动来源会标明最近核验时间；失败时不会伪装为最新数据。'
            : '当前只展示你导入或确认过的真实活动，不填充演示活动。各平台自动采集会按来源逐个验收后启用。'}</span>
        </div>
      </div>

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
          <option value="unknown">待确认</option>
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
              <p>{campaigns.length ? '调整筛选条件继续查看。' : '可以先导入平台活动链接和规则。Ripple 不会用虚构活动填充这里。'}</p>
              {!campaigns.length && <button className="btn btn-primary btn-sm" onClick={openNew}>导入第一个活动</button>}
            </div>
          )}
          <div className="campaign-grid">
            {visible.map((campaign) => {
              const qualification = QUALIFICATION[campaign.qualification_state] || QUALIFICATION.unknown;
              const deadlineDays = daysUntil(campaign.submit_deadline);
              return (
                <article className="card campaign-card" key={campaign.id}>
                  <div className="campaign-card-top">
                    <div className={`campaign-platform platform-${campaign.platform}`}>{campaign.platform_label}</div>
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
                    <span className={qualification.cls}>{qualification.label}</span>
                  </div>
                  {campaign.reward_summary && <div className="campaign-reward">{campaign.reward_summary}</div>}
                  <div className="campaign-meta">
                    <span><small>投稿截止</small>{dateOnly(campaign.submit_deadline) || '未说明'}{deadlineDays !== null && deadlineDays >= 0 && deadlineDays <= 7 ? ` · 剩 ${deadlineDays} 天` : ''}</span>
                    <span><small>活动状态</small>{STATUS[campaign.status] || campaign.status}</span>
                    <span><small>来源状态</small>{sourceLabel(campaign)}</span>
                    <span><small>规则版本</small>v{campaign.rule_version}</span>
                  </div>
                  <div className="campaign-actions">
                    <button className="btn btn-sm" onClick={() => setSelected(campaign)}>查看规则</button>
                    <button className="btn btn-sm btn-primary" disabled={campaign.status === 'ended' || campaign.status === 'cancelled'}
                      onClick={() => void generateIdeas(campaign)}><IconSkills size={13} /> 生成选题</button>
                  </div>
                </article>
              );
            })}
          </div>

          {sources.length > 0 && campaigns.length === 0 && (
            <section className="campaign-source-grid">
              <h3>六平台来源接入状态</h3>
              <div>
                {sources.map((source) => (
                  <article key={source.platform}>
                    <strong>{source.label}</strong>
                    <span>{source.automatic ? '自动采集已启用' : '当前支持导入'}</span>
                    <p>{source.detail}</p>
                  </article>
                ))}
              </div>
            </section>
          )}
        </main>

        <aside className="campaign-trends">
          <div className="campaign-trends-head"><IconIdea size={17} /><strong>相关热点</strong><span>来自热点雷达</span></div>
          {trends.length === 0 && <p className="campaign-trends-empty">当前没有可用热点。活动仍可单独生成选题。</p>}
          {trends.map((item, index) => (
            <a key={`${item.platform}-${item.title}-${index}`} href={item.url || undefined} target="_blank" rel="noreferrer">
              <b>{index + 1}</b><span><strong>{item.title}</strong><small>{item.platform}{item.hot ? ` · ${item.hot}` : ''}</small></span>
            </a>
          ))}
          <p className="campaign-trends-note">AI 只会引用实际读取到的热点；没有自然关联时允许不绑定热点。</p>
        </aside>
      </div>

      {selected && (
        <div className="campaign-drawer-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) setSelected(null); }}>
          <aside className="campaign-drawer">
            <header><div><span>{selected.platform_label}</span><h2>活动详情</h2></div><button onClick={() => setSelected(null)}>×</button></header>
            <div className="campaign-drawer-title">
              <h3>{selected.title}</h3>
              <p>主办方 · {selected.organizer || '未说明'} · {STATUS[selected.status] || selected.status}</p>
              <div className="campaign-tags"><span className={(QUALIFICATION[selected.qualification_state] || QUALIFICATION.unknown).cls}>{(QUALIFICATION[selected.qualification_state] || QUALIFICATION.unknown).label}</span><span>{sourceLabel(selected)}</span><span>规则 v{selected.rule_version}</span></div>
            </div>

            {selected.note && <section><h4>活动简介</h4><p className="campaign-copy">{selected.note}</p></section>}
            <section><h4>参与条件</h4>{selected.eligibility.length ? <ul>{selected.eligibility.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">未说明，参与前需要确认。</p>}</section>
            <section><h4>内容要求</h4>{selected.content_requirements.length ? <ul>{selected.content_requirements.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">未说明。</p>}</section>
            <section><h4>奖励规则</h4>{selected.reward_rules.length ? <ul>{selected.reward_rules.map((x) => <li key={x}>{x}</li>)}</ul> : <p className="campaign-unknown">{selected.reward_summary || '未说明。'}</p>}</section>
            <section><h4>指定话题 / 标签</h4>{selected.required_topics.length ? <div className="campaign-topic-tags">{selected.required_topics.map((x) => <span key={x}>{x.startsWith('#') ? x : `# ${x}`}</span>)}</div> : <p className="campaign-unknown">未说明。</p>}</section>
            <section><h4>AI 使用要求</h4><p className="campaign-copy">{selected.ai_policy === 'unknown' || !selected.ai_policy ? '未说明；AI 辅助创作前需要确认活动规则。' : selected.ai_policy}</p></section>
            <section>
              <h4>时间节点</h4>
              <div className="campaign-timeline">
                <span><small>活动开始</small>{dateOnly(selected.starts_at) || '未说明'}</span>
                <span><small>报名截止</small>{dateOnly(selected.signup_deadline) || '未说明'}</span>
                <span><small>投稿截止</small>{dateOnly(selected.submit_deadline) || '未说明'}</span>
                <span><small>统计截止</small>{dateOnly(selected.stats_deadline) || '未说明'}</span>
              </div>
            </section>
            <section><h4>来源与核验</h4>
              {selected.source_url ? <a className="campaign-source-link" href={selected.source_url} target="_blank" rel="noreferrer">{selected.source_url}</a> : <p className="campaign-unknown">没有保存来源链接。</p>}
              <p className="campaign-verify">来源状态：{sourceLabel(selected)} · 最近核验：{selected.last_verified_at ? new Date(selected.last_verified_at * 1000).toLocaleString('zh-CN') : '未由 Ripple 核验'}</p>
            </section>
            <footer>
              <button className="btn btn-primary campaign-generate" disabled={selected.status === 'ended' || selected.status === 'cancelled'}
                onClick={() => void generateIdeas(selected)}><IconSkills size={15} /> 活动 + 热点生成选题</button>
              <div>
                <button className="btn btn-sm" onClick={() => void addCalendar(selected)}><IconCalendar size={13} /> 加入选题日历</button>
                {selected.source_url && <a className="btn btn-sm" href={selected.source_url} target="_blank" rel="noreferrer">打开原活动页</a>}
                {selected.source_type === 'user_import' && <button className="btn btn-sm" onClick={() => openEdit(selected)}>编辑规则</button>}
              </div>
            </footer>
          </aside>
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
              <label><span>活动开始</span><input className="field" type="date" value={dateOnly(form.starts_at)} onChange={(e) => setForm({ ...form, starts_at: e.target.value })} /></label>
              <label><span>投稿截止</span><input className="field" type="date" value={dateOnly(form.submit_deadline)} onChange={(e) => setForm({ ...form, submit_deadline: e.target.value })} /></label>
              <label><span>资格状态</span><select className="field" value={form.qualification_state || 'unknown'} onChange={(e) => setForm({ ...form, qualification_state: e.target.value as Campaign['qualification_state'] })}><option value="unknown">待确认</option><option value="eligible">已知符合</option><option value="ineligible">已知不符合</option></select></label>
              <label><span>参与账号</span><select className="field" value={form.account_id || ''} onChange={(e) => setForm({ ...form, account_id: e.target.value })}><option value="">未绑定账号</option>{accounts.filter((a) => a.platform === form.platform).map((a) => <option key={a.id} value={a.id}>{a.label}</option>)}</select></label>
              <label className="wide"><span>来源链接</span><input className="field" type="url" value={form.source_url || ''} onChange={(e) => setForm({ ...form, source_url: e.target.value })} placeholder="https://…" /></label>
              <label className="wide"><span>参与条件（每行一条）</span><textarea className="field" value={eligibilityText} onChange={(e) => setEligibilityText(e.target.value)} /></label>
              <label className="wide"><span>内容要求（每行一条）</span><textarea className="field" value={requirementsText} onChange={(e) => setRequirementsText(e.target.value)} /></label>
              <label className="wide"><span>奖励规则（每行一条）</span><textarea className="field" value={rewardRulesText} onChange={(e) => setRewardRulesText(e.target.value)} /></label>
              <label className="wide"><span>指定话题 / 标签</span><input className="field" value={topicsText} onChange={(e) => setTopicsText(e.target.value)} placeholder="#开学季，#效率工具" /></label>
              <label className="wide"><span>AI 使用要求</span><input className="field" value={form.ai_policy || ''} onChange={(e) => setForm({ ...form, ai_policy: e.target.value || 'unknown' })} placeholder="未说明 / 允许辅助 / 禁止自动生成…" /></label>
              <label className="wide"><span>活动简介 / 备注</span><textarea className="field" value={form.note || ''} onChange={(e) => setForm({ ...form, note: e.target.value })} /></label>
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
