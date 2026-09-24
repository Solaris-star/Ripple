import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  cancelIdeaRun, confirmIdeaBrief, createIdea, createIdeaRun, deleteIdea, developIdea, feedbackIdea,
  fetchIdeaBrief, fetchIdeaDiscovery, fetchIdeaDiscoveryAccounts, fetchIdeaRun, fetchIdeaRuns, fetchIdeas,
  markIdeaSeen, planIdea, saveIdeaDiscoveryPolicy, startIdeaContent, updateIdea, updateIdeaBrief,
} from '../lib/api';
import type {
  Idea, IdeaBrief, IdeaBriefData, IdeaDiscoveryAccount, IdeaDiscoveryState, IdeaInput, IdeaRun,
  IdeaSourceSnapshot, PersonaItem, TopicUseContext,
} from '../lib/api';
import { IconCalendar, IconCheck, IconEdit, IconIdea, IconSkills, IconTrash } from './icons';
import { loadTrendSelection, TREND_PLATFORMS } from '../lib/trendPrefs';
import { PlatformIcon } from './PlatformBrand';
import { platformDisplayName } from '../lib/platforms';
import { newId } from '../lib/id';
import { executeStructuredOperation, fetchStructuredOperations } from '../lib/ripple';
import type { OperationResult, TopicEvaluationOutput } from '../lib/ripple';
import IdeaBriefEditor from './IdeaBriefEditor';
import '../styles/ideas-workbench.css';

interface IdeasPageProps {
  onUseTopic: (input: string | TopicUseContext) => void;
  onOpenContent?: (id: string) => void;
  persona: string;
  aiReady: boolean;
  accountIds?: string[];
  personas: PersonaItem[];
  onPersonaChange: (name: string) => void;
  onNewPersona: () => void;
}

const TARGET_PLATFORMS = ['xiaohongshu', 'douyin', 'bilibili', 'wechat', 'weixin-channels', 'zhihu', 'x'] as const;
const ACTIVE_RUNS = new Set(['queued', 'running', 'partial', 'waiting_user']);
const STAGE_LABELS: Record<string, string> = {
  candidate: '候选', saved: '待深化', developing: '策划中', review: '待确认',
  ready: '可制作', production: '制作中', done: '已完成', rejected: '不感兴趣', archived: '已归档',
};
const BOARD_COLUMNS = [
  { key: 'saved', label: '待深化', stages: ['saved'] },
  { key: 'review', label: '策划中', stages: ['developing', 'review'] },
  { key: 'ready', label: '可制作', stages: ['ready'] },
  { key: 'production', label: '制作中', stages: ['production'] },
  { key: 'done', label: '已完成', stages: ['done'] },
];

const blankIdea = (persona = ''): IdeaInput => ({ title: '', note: '', source: '手动灵感', status: 'pending', stage: 'saved', persona });
const keyFor = (prefix: string) => (prefix + '-' + newId().replaceAll('-', '')).slice(0, 120);

function recommendationLevel(score = 0): string {
  if (score >= 82) return '高匹配';
  if (score >= 68) return '较匹配';
  return '可探索';
}

function stageOf(idea: Idea): string {
  return idea.stage || (idea.status === 'doing' ? 'production' : idea.status === 'done' ? 'done' : 'saved');
}

function sourceLabel(source: IdeaSourceSnapshot): string {
  if (source.kind === 'campaign') return '活动';
  if (source.kind === 'trend') return '热点';
  return '来源';
}

export default function IdeasPage({
  onUseTopic, onOpenContent, persona, aiReady, accountIds = [], personas, onPersonaChange, onNewPersona,
}: IdeasPageProps) {
  const [ideas, setIdeas] = useState<Idea[]>([]);
  const [runs, setRuns] = useState<IdeaRun[]>([]);
  const [tab, setTab] = useState<'candidates' | 'mine'>('candidates');
  const [mineView, setMineView] = useState<'list' | 'board'>('list');
  const [mineFilter, setMineFilter] = useState('all');
  const [query, setQuery] = useState('');
  const [targetPlatforms, setTargetPlatforms] = useState<string[]>([]);
  const [includeTrends, setIncludeTrends] = useState(true);
  const [includeCampaigns, setIncludeCampaigns] = useState(true);
  const [goal, setGoal] = useState('');
  const [effort, setEffort] = useState(120);
  const [instruction, setInstruction] = useState('');
  const [workbenchMode, setWorkbenchMode] = useState<'proactive' | 'manual'>('manual');
  const [discovery, setDiscovery] = useState<IdeaDiscoveryState | null>(null);
  const [discoveryAccounts, setDiscoveryAccounts] = useState<IdeaDiscoveryAccount[]>([]);
  const [policyEditing, setPolicyEditing] = useState(false);
  const [policyTargets, setPolicyTargets] = useState<string[]>([]);
  const [policyFocus, setPolicyFocus] = useState('');
  const [policyMode, setPolicyMode] = useState<'balanced' | 'combo_only'>('balanced');
  const [policyEffort, setPolicyEffort] = useState(120);
  const [policyRuns, setPolicyRuns] = useState(6);
  const [policyCandidates, setPolicyCandidates] = useState(6);
  const [policyNotices, setPolicyNotices] = useState(2);
  const [policyAccountIds, setPolicyAccountIds] = useState<string[]>([]);
  const [policyTimezone, setPolicyTimezone] = useState(() => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; } });
  const [policyQuietStart, setPolicyQuietStart] = useState('22:00');
  const [policyQuietEnd, setPolicyQuietEnd] = useState('08:00');
  const [policySaving, setPolicySaving] = useState(false);
  const [fixedTrendTitles, setFixedTrendTitles] = useState<string[]>([]);
  const [fixedTrendPlatform, setFixedTrendPlatform] = useState('');
  const [activeRun, setActiveRun] = useState<IdeaRun | null>(null);
  const [error, setError] = useState('');
  const [toast, setToast] = useState('');
  const [historyOpen, setHistoryOpen] = useState(false);
  const [evidenceIdea, setEvidenceIdea] = useState<Idea | null>(null);
  const [evidenceSources, setEvidenceSources] = useState<IdeaSourceSnapshot[]>([]);
  const [evidenceLoading, setEvidenceLoading] = useState(false);
  const [form, setForm] = useState<IdeaInput | null>(null);
  const [editId, setEditId] = useState('');
  const [briefIdea, setBriefIdea] = useState<Idea | null>(null);
  const [brief, setBrief] = useState<IdeaBrief | null>(null);
  const [briefBusy, setBriefBusy] = useState(false);
  const [pendingBriefIdeaId, setPendingBriefIdeaId] = useState('');
  const [scheduleIdea, setScheduleIdea] = useState<Idea | null>(null);
  const [scheduleDate, setScheduleDate] = useState('');
  const [scheduleTime, setScheduleTime] = useState('');
  const [evaluation, setEvaluation] = useState<OperationResult<TopicEvaluationOutput> | null>(null);
  const [evaluationIdea, setEvaluationIdea] = useState<Idea | null>(null);
  const [evaluating, setEvaluating] = useState(false);
  const [evaluationReady, setEvaluationReady] = useState(false);
  const focusHandled = useRef(false);
  const openBriefRef = useRef<(id: string) => Promise<void>>(async () => {});
  const discoveryHydratedRevision = useRef(-1);
  const discoveryModeInitialized = useRef(false);

  const trendSelection = loadTrendSelection();
  const trendLabels = TREND_PLATFORMS.filter((value) => trendSelection.includes(value.key)).map((value) => value.label);

  const showToast = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(''), 2400);
  };

  const load = useCallback(async () => {
    const [ideaResult, runResult, discoveryResult, accountsResult] = await Promise.allSettled([
      fetchIdeas(persona, true), fetchIdeaRuns(20, persona, 'manual'),
      persona ? fetchIdeaDiscovery(persona) : Promise.resolve(null), fetchIdeaDiscoveryAccounts(),
    ]);
    if (ideaResult.status === 'fulfilled') setIdeas(ideaResult.value);
    if (runResult.status === 'fulfilled') {
      setRuns(runResult.value.items);
      setActiveRun((current) => {
        if (current && ACTIVE_RUNS.has(current.status)) return current;
        return runResult.value.items.find((value) => ACTIVE_RUNS.has(value.status)) || null;
      });
    }
    if (accountsResult.status === 'fulfilled') setDiscoveryAccounts(accountsResult.value);
    if (discoveryResult.status === 'fulfilled') {
      const state = discoveryResult.value;
      setDiscovery(state);
      const policy = state?.policy;
      if (policy && discoveryHydratedRevision.current !== policy.revision) {
        discoveryHydratedRevision.current = policy.revision;
        setPolicyTargets(policy.target_platforms || []);
        setPolicyFocus((policy.focus_keywords || []).join('，'));
        setPolicyMode(policy.mode || 'balanced');
        setPolicyEffort(policy.effort_minutes || 120);
        setPolicyRuns(policy.max_daily_runs || 6);
        setPolicyCandidates(policy.max_daily_candidates || 6);
        setPolicyNotices(policy.max_daily_notices ?? 2);
        setPolicyAccountIds(policy.account_ids || []);
        setPolicyTimezone(policy.timezone || 'UTC');
        setPolicyQuietStart(policy.quiet_start || '22:00');
        setPolicyQuietEnd(policy.quiet_end || '08:00');
      }
      if (!discoveryModeInitialized.current) {
        discoveryModeInitialized.current = true;
        if (policy?.enabled) setWorkbenchMode('proactive');
      }
    }
    const failed = [ideaResult, runResult, discoveryResult].find((value) => value.status === 'rejected');
    if (failed?.status === 'rejected') setError(failed.reason instanceof Error ? failed.reason.message : '选题数据读取失败');
  }, [persona]);

  useEffect(() => {
    discoveryHydratedRevision.current = -1;
    discoveryModeInitialized.current = false;
    setDiscovery(null);
    setPolicyEditing(false);
  }, [persona]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (!discovery?.policy) setPolicyAccountIds(accountIds);
  }, [accountIds, discovery?.policy]);
  useEffect(() => {
    if (!persona || !discovery?.policy?.enabled) return;
    let stopped = false;
    const refresh = async () => {
      try {
        const [state, currentIdeas] = await Promise.all([fetchIdeaDiscovery(persona), fetchIdeas(persona, true)]);
        if (stopped) return;
        setDiscovery(state); setIdeas(currentIdeas);
      } catch { /* keep the last visible inbox while background status is unavailable */ }
    };
    const timer = window.setInterval(() => void refresh(), 10000);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [persona, discovery?.policy?.enabled]);
  useEffect(() => {
    try {
      const rawSeed = sessionStorage.getItem('ripple_idea_seed') || '';
      if (!rawSeed) return;
      sessionStorage.removeItem('ripple_idea_seed');
      const seed = JSON.parse(rawSeed) as { trend_title?: unknown; trend_platform?: unknown };
      const title = typeof seed.trend_title === 'string' ? seed.trend_title.trim() : '';
      const platform = typeof seed.trend_platform === 'string' ? seed.trend_platform.trim() : '';
      if (!title) return;
      setFixedTrendTitles([title.slice(0, 500)]);
      setFixedTrendPlatform(platform.slice(0, 40));
      setIncludeTrends(true);
      setInstruction((current) => current || `优先围绕指定热点「${title.slice(0, 200)}」寻找与账号赛道自然相关的选题；不相关时说明，不强行蹭热点。`);
    } catch { /* invalid cross-page seed is ignored */ }
  }, []);
  useEffect(() => {
    void fetchStructuredOperations()
      .then(({ items }) => setEvaluationReady(!!items.find((value) => value.id === 'topic_evaluate')?.ready))
      .catch(() => setEvaluationReady(false));
  }, []);

  useEffect(() => {
    if (!activeRun || !ACTIVE_RUNS.has(activeRun.status)) return;
    let stopped = false;
    const read = async () => {
      try {
        const next = await fetchIdeaRun(activeRun.id);
        if (stopped) return;
        setActiveRun(next);
        setRuns((current) => [next, ...current.filter((value) => value.id !== next.id)].slice(0, 20));
        if (!ACTIVE_RUNS.has(next.status)) {
          await load();
          if (next.status === 'succeeded') {
            if (next.kind === 'recommend') {
              setTab('candidates');
              showToast('候选选题已完成来源与约束校验');
            } else {
              const ideaId = String(next.result?.idea_id || pendingBriefIdeaId || '');
              if (ideaId) await openBrief(ideaId);
            }
          } else if (next.error) {
            setError(next.error);
          }
        }
      } catch (cause) {
        if (!stopped) setError(cause instanceof Error ? cause.message : '选题任务状态读取失败');
      }
    };
    void read();
    const timer = window.setInterval(() => void read(), 1400);
    return () => { stopped = true; window.clearInterval(timer); };
    // openBrief is intentionally resolved from the latest component closure through state refresh.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRun?.id, activeRun?.status, load, pendingBriefIdeaId]);

  const candidates = useMemo(() => ideas.filter((idea) => stageOf(idea) === 'candidate' && (!persona || !idea.persona || idea.persona === persona)), [ideas, persona]);
  const visibleCandidates = useMemo(() => candidates.filter((idea) => workbenchMode === 'proactive' ? idea.origin === 'automatic' : idea.origin !== 'automatic'), [candidates, workbenchMode]);
  const proactiveUnread = useMemo(() => candidates.filter((idea) => idea.origin === 'automatic' && idea.unread && idea.validity !== 'expired').length, [candidates]);
  const policyAvailableAccounts = useMemo(() => discoveryAccounts.filter((account) => account.status === 'connected' && policyTargets.includes(account.platform)), [discoveryAccounts, policyTargets]);
  const discoveryPolicy = discovery?.policy || null;
  const discoveryTrendStatuses = ((discovery?.source_health?.trend_statuses || {}) as Record<string, string>);
  const discoverySourceText = Object.keys(discoveryTrendStatuses).length
    ? Object.entries(discoveryTrendStatuses).map(([platform, status]) => `${platformDisplayName(platform)} ${status === 'fresh' ? '正常' : status === 'stale' ? '缓存' : '异常'}`).join(' · ')
    : '等待已有热点缓存';
  const mine = useMemo(() => ideas.filter((idea) => {
    const stage = stageOf(idea);
    return stage !== 'candidate' && stage !== 'rejected' && stage !== 'archived' && (!persona || !idea.persona || idea.persona === persona);
  }), [ideas, persona]);
  const rejected = useMemo(() => ideas.filter((idea) => stageOf(idea) === 'rejected'), [ideas]);

  const filteredMine = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return mine.filter((idea) => {
      const stage = stageOf(idea);
      const stageMatch = mineFilter === 'all' || (
        mineFilter === 'planning' ? ['saved', 'developing', 'review'].includes(stage)
          : mineFilter === 'ready' ? stage === 'ready'
            : mineFilter === 'production' ? stage === 'production'
              : mineFilter === 'done' ? stage === 'done' : true
      );
      const textMatch = !needle || [idea.title, idea.note, idea.angle, idea.reason].some((value) => String(value || '').toLowerCase().includes(needle));
      return stageMatch && textMatch;
    });
  }, [mine, mineFilter, query]);

  const saveDiscovery = async (enabled: boolean) => {
    if (!persona) { setError('先选择账号画像，再设置主动发现。'); return; }
    if (enabled && !aiReady) { setError('Agent 推荐服务未配置，不能开启主动发现。'); return; }
    if (enabled && policyTargets.length === 0) { setError('开启主动发现前，至少确认一个目标平台。'); return; }
    setPolicySaving(true); setError('');
    try {
      const focus = policyFocus.split(/[，,\n]/).map((value) => value.trim()).filter(Boolean).slice(0, 30);
      const state = await saveIdeaDiscoveryPolicy({
        persona, enabled, target_platforms: policyTargets,
        trend_sources: trendSelection.length ? trendSelection : TREND_PLATFORMS.map((value) => value.key),
        account_ids: policyAccountIds.filter((id) => policyAvailableAccounts.some((account) => account.id === id)),
        mode: policyMode, focus_keywords: focus, effort_minutes: policyEffort,
        max_daily_runs: policyRuns, max_daily_candidates: policyCandidates, max_daily_notices: policyNotices,
        min_candidate_score: 68, timezone: policyTimezone || 'UTC', quiet_start: policyQuietStart,
        quiet_end: policyQuietEnd, important_notifications: false,
      });
      setDiscovery(state); setPolicyEditing(false); discoveryHydratedRevision.current = state.policy?.revision ?? -1;
      if (enabled) { setWorkbenchMode('proactive'); showToast('主动发现已开启，只会在有有效变化且预算允许时分析'); }
      else showToast('主动发现已暂停，不会再启动新的自动分析');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '主动发现策略保存失败'); }
    finally { setPolicySaving(false); }
  };

  const startRun = async () => {
    if (!persona) { setError('先选择一个账号画像，再开始推荐。'); return; }
    if (!aiReady) { setError('Agent 推荐服务未配置或不可用。'); return; }
    setError('');
    try {
      const run = await createIdeaRun({
        persona,
        target_platforms: targetPlatforms,
        account_ids: accountIds,
        trend_sources: includeTrends ? Array.from(new Set([...trendSelection, fixedTrendPlatform].filter(Boolean))) : [],
        trend_titles: includeTrends ? fixedTrendTitles : [],
        include_trends: includeTrends,
        include_campaigns: includeCampaigns,
        instruction: instruction.trim(),
        goal: goal.trim(),
        effort_minutes: effort,
        limit: 6,
        idempotency_key: keyFor('ideas'),
      });
      setActiveRun(run);
      setRuns((current) => [run, ...current.filter((value) => value.id !== run.id)].slice(0, 20));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : 'Agent 选题任务创建失败');
    }
  };

  const cancelRun = async () => {
    if (!activeRun) return;
    try { setActiveRun(await cancelIdeaRun(activeRun.id)); } catch (cause) { setError(cause instanceof Error ? cause.message : '取消失败'); }
  };

  const feedback = async (idea: Idea, action: 'stash' | 'reject' | 'reopen') => {
    try {
      await feedbackIdea(idea.id, action);
      await load();
      showToast(action === 'stash' ? '已暂存到我的选题' : action === 'reject' ? '已移出当前候选' : '已恢复候选');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '操作失败'); }
  };

  const develop = async (idea: Idea, developInstruction = '') => {
    if (!aiReady) { setError('Agent 模型尚未配置。'); return; }
    setError('');
    setPendingBriefIdeaId(idea.id);
    setBriefBusy(true);
    try {
      const run = await developIdea(idea.id, {
        scope: developInstruction ? 'partial' : 'full',
        instruction: developInstruction,
        idempotency_key: keyFor('develop-' + idea.id),
      });
      setActiveRun(run);
      setRuns((current) => [run, ...current.filter((value) => value.id !== run.id)].slice(0, 20));
      if (!brief) setBriefIdea(idea);
    } catch (cause) {
      setBriefBusy(false);
      setError(cause instanceof Error ? cause.message : '策划深化失败');
    }
  };

  async function openBrief(id: string) {
    setBriefBusy(true);
    try {
      const result = await fetchIdeaBrief(id);
      setBriefIdea(result.idea);
      setBrief(result.brief);
      if (!result.brief) {
        setBriefBusy(false);
        await develop(result.idea);
        return;
      }
      setBriefBusy(false);
    } catch (cause) {
      setBriefBusy(false);
      setError(cause instanceof Error ? cause.message : '策划单读取失败');
    }
  }
  openBriefRef.current = openBrief;

  useEffect(() => {
    if (focusHandled.current || ideas.length === 0) return;
    let focus = '';
    try {
      focus = sessionStorage.getItem('ripple_idea_focus') || '';
      if (focus) sessionStorage.removeItem('ripple_idea_focus');
    } catch { /* ignore */ }
    if (!focus) { focusHandled.current = true; return; }
    const idea = ideas.find((value) => value.id === focus);
    focusHandled.current = true;
    if (!idea) return;
    setTab('mine');
    void openBriefRef.current(idea.id);
  }, [ideas]);


  const saveBrief = async (data: IdeaBriefData, locked: string[]) => {
    if (!briefIdea || !brief) return;
    setBriefBusy(true);
    try {
      const saved = await updateIdeaBrief(briefIdea.id, { expected_revision: brief.revision, data, locked_fields: locked });
      setBrief(saved);
      await load();
      showToast('策划单已保存为新版本');
    } finally { setBriefBusy(false); }
  };

  const confirmBrief = async () => {
    if (!briefIdea || !brief) return;
    setBriefBusy(true);
    try {
      const confirmed = await confirmIdeaBrief(briefIdea.id, brief.revision);
      setBrief(confirmed);
      await load();
      showToast('策划已确认，可以进入内容制作');
    } finally { setBriefBusy(false); }
  };

  const startContent = async () => {
    if (!briefIdea || !brief || brief.status !== 'confirmed') return;
    setBriefBusy(true);
    try {
      const result = await startIdeaContent(briefIdea.id, brief.revision, 'idea-' + briefIdea.id + '-brief-' + brief.revision + '-content');
      const context: TopicUseContext = {
        title: result.idea.title,
        ideaId: result.idea.id,
        contentId: result.content.id,
        brief: brief.data,
        autoStart: false,
        angle: result.idea.angle,
        reason: result.idea.reason,
        campaignId: result.idea.campaign_id,
        campaignRuleVersion: result.idea.campaign_rule_version,
        trendRefs: result.idea.trend_refs,
        targetPlatforms: result.idea.target_platforms,
        requirements: result.idea.requirements,
        pendingChecks: result.idea.pending_checks,
        source: result.idea.source,
      };
      setBriefIdea(null);
      setBrief(null);
      onUseTopic(context);
    } finally { setBriefBusy(false); }
  };

  const saveForm = async () => {
    if (!form?.title.trim()) return;
    try {
      if (editId) await updateIdea(editId, { ...form, persona: form.persona || persona });
      else await createIdea({ ...form, stage: 'saved', persona });
      setForm(null); setEditId('');
      await load();
      setTab('mine');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '保存失败'); }
  };

  const remove = async (idea: Idea) => {
    try { await deleteIdea(idea.id); await load(); } catch (cause) { setError(cause instanceof Error ? cause.message : '删除失败'); }
  };

  const openEdit = (idea: Idea) => {
    setEditId(idea.id);
    setForm({
      title: idea.title, note: idea.note, source: idea.source, status: idea.status,
      stage: stageOf(idea) as IdeaInput['stage'], persona: idea.persona, angle: idea.angle, reason: idea.reason,
      campaign_id: idea.campaign_id, campaign_rule_version: idea.campaign_rule_version,
      trend_refs: idea.trend_refs, target_platforms: idea.target_platforms, requirements: idea.requirements,
      pending_checks: idea.pending_checks, platform_plans: idea.platform_plans, source_refs: idea.source_refs, score: idea.score,
    });
  };

  const showEvidence = async (idea: Idea) => {
    setEvidenceIdea(idea);
    setEvidenceSources([]);
    if (idea.unread) {
      try {
        const updated = await markIdeaSeen(idea.id);
        setIdeas((current) => current.map((value) => value.id === idea.id ? updated : value));
        if (persona) setDiscovery(await fetchIdeaDiscovery(persona));
      } catch { /* evidence can still be inspected if read state update fails */ }
    }
    if (!idea.run_id) return;
    setEvidenceLoading(true);
    try {
      const run = await fetchIdeaRun(idea.run_id);
      const refs = new Set(idea.source_refs || []);
      setEvidenceSources((run.sources || []).filter((source) => refs.has(source.id)));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '来源依据读取失败');
    } finally { setEvidenceLoading(false); }
  };

  const saveSchedule = async () => {
    if (!scheduleIdea || !scheduleDate || !scheduleTime) return;
    try {
      const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
      await planIdea(scheduleIdea.id, {
        scheduled_local: scheduleDate + 'T' + scheduleTime,
        timezone,
        idempotency_key: keyFor('idea-plan-' + scheduleIdea.id),
      });
      setScheduleIdea(null); setScheduleDate(''); setScheduleTime('');
      await load();
      showToast('已加入创作计划');
    } catch (cause) { setError(cause instanceof Error ? cause.message : '排期失败'); }
  };

  const evaluate = async (idea: Idea) => {
    if (!evaluationReady || evaluating) return;
    setEvaluationIdea(idea); setEvaluation(null); setEvaluating(true);
    try {
      setEvaluation(await executeStructuredOperation<TopicEvaluationOutput>('topic_evaluate', {
        title: idea.title, note: idea.note || idea.angle || '',
        platform: idea.target_platforms?.[0] || '', persona: persona || idea.persona || '',
      }, { kind: 'idea', ref: idea.id, version: String(idea.updated || idea.created || ''), snapshot: { title: idea.title, source: idea.source, stage: stageOf(idea) } }));
    } catch (cause) { setError(cause instanceof Error ? cause.message : '选题评估失败'); }
    finally { setEvaluating(false); }
  };

  const candidateCard = (idea: Idea) => {
    const automatic = idea.origin === 'automatic';
    const expired = idea.validity === 'expired';
    return <article className={`idea-candidate-card card${automatic ? ' automatic' : ''}${idea.unread ? ' unread' : ''}${expired ? ' expired' : ''}`} key={idea.id}>
      <div className="idea-candidate-top">
        <div><div className="idea-candidate-badges"><span className="idea-match">{recommendationLevel(idea.score)}</span>{automatic && <span className="idea-auto-badge">主动发现{idea.unread ? ' · 新' : ''}</span>}{expired && <span className="idea-expired-badge">已失效</span>}</div><h3>{idea.title}</h3></div>
        <button className="idea-evidence-button" onClick={() => void showEvidence(idea)}>查看依据</button>
      </div>
      {automatic && idea.trigger_summary && <p className="idea-trigger-summary"><b>为什么现在出现：</b>{idea.trigger_summary}</p>}
      <p className="idea-candidate-angle">{idea.angle || idea.note || '等待补充内容角度'}</p>
      {idea.reason && <p className="idea-candidate-reason">{idea.reason}</p>}
      <div className="idea-platform-list">{(idea.target_platforms || []).map((platform) => <span key={platform}><PlatformIcon platform={platform} size={13} />{platformDisplayName(platform)}</span>)}</div>
      <div className="idea-source-summary">
        {idea.campaign_id && <span>活动约束</span>}
        {(idea.trend_refs || []).slice(0, 2).map((ref) => <span key={ref}>热点 · {ref}</span>)}
        {!idea.campaign_id && !(idea.trend_refs || []).length && <span>赛道常青方向</span>}
      </div>
      {(idea.pending_checks?.length || idea.requirements?.length) ? <div className="idea-cost-line">
        {idea.requirements?.length ? <span>已知约束 {idea.requirements.length}</span> : null}
        {idea.pending_checks?.length ? <span className="warn">待核实 {idea.pending_checks.length}</span> : null}
      </div> : null}
      <div className="idea-candidate-actions">
        <button className="btn btn-sm btn-primary" disabled={expired || (!!activeRun && ACTIVE_RUNS.has(activeRun.status))} onClick={() => void develop(idea)}><IconSkills size={13} /> {expired ? '机会已失效' : '选定并深化'}</button>
        {!expired && <button className="btn btn-sm" onClick={() => void feedback(idea, 'stash')}>暂存</button>}
        <button className="r2-text-button" onClick={() => void feedback(idea, 'reject')}>不感兴趣</button>
      </div>
    </article>;
  };

  const mineCard = (idea: Idea) => {
    const stage = stageOf(idea);
    return <article className="idea-owned-card card" key={idea.id}>
      <header><div><span className={'idea-stage stage-' + stage}>{STAGE_LABELS[stage] || stage}</span><h3>{idea.title}</h3></div>
        <div className="idea-owned-menu">
          <button title="编辑" onClick={() => openEdit(idea)}><IconEdit size={13} /></button>
          <button title="删除" onClick={() => void remove(idea)}><IconTrash size={13} /></button>
        </div>
      </header>
      {idea.angle && <p>{idea.angle}</p>}
      <div className="idea-platform-list">{(idea.target_platforms || []).map((platform) => <span key={platform}><PlatformIcon platform={platform} size={12} />{platformDisplayName(platform)}</span>)}</div>
      <footer>
        {stage === 'saved' && <button className="btn btn-sm btn-primary" onClick={() => void develop(idea)} disabled={!aiReady}>深化策划</button>}
        {stage === 'developing' && <span className="idea-progress-label">Agent 正在深化…</span>}
        {['review', 'ready'].includes(stage) && <button className="btn btn-sm btn-primary" onClick={() => void openBrief(idea.id)}>{stage === 'ready' ? '查看已确认策划' : '继续策划'}</button>}
        {stage === 'production' && idea.content_id && <button className="btn btn-sm btn-primary" onClick={() => onOpenContent ? onOpenContent(idea.content_id!) : onUseTopic({ title: idea.title, ideaId: idea.id, contentId: idea.content_id })}>打开内容</button>}
        <button className="btn btn-sm" onClick={() => { setScheduleIdea(idea); setScheduleDate(''); setScheduleTime(''); }}><IconCalendar size={13} /> {idea.plan_id ? '调整排期' : '排期'}</button>
        {evaluationReady && <button className="btn btn-sm" disabled={evaluating} onClick={() => void evaluate(idea)}>七维评估</button>}
      </footer>
    </article>;
  };

  return <div className="page-scroll ideas-page ideas-workbench">
    <div className="page-head ideas-workbench-head">
      <div><h1 className="page-title"><IconIdea size={21} /> 选题库</h1><p className="page-subtitle">让 Agent 结合账号定位、每日热点和活动机会，先给候选，再把你选中的方向深化成可制作策划。</p></div>
      <div className="idea-head-actions"><div className="idea-mode-switch"><button className={workbenchMode === 'proactive' ? 'active' : ''} onClick={() => setWorkbenchMode('proactive')}>主动发现{proactiveUnread > 0 && <b>{proactiveUnread}</b>}</button><button className={workbenchMode === 'manual' ? 'active' : ''} onClick={() => setWorkbenchMode('manual')}>按需找选题</button></div><button className="btn btn-sm" onClick={() => setHistoryOpen(true)}>历史批次</button><button className="btn btn-sm" onClick={() => { setEditId(''); setForm(blankIdea(persona)); }}>+ 记灵感</button></div>
    </div>

    {workbenchMode === 'proactive' ? <section className="idea-discovery-panel card">
      <header className="idea-discovery-head"><div><div className="idea-discovery-title"><span className={'idea-discovery-dot ' + (discoveryPolicy?.enabled && !discoveryPolicy.requires_review ? 'on' : 'off')} /><strong>主动发现</strong><span>{discoveryPolicy?.requires_review ? '待复核' : discoveryPolicy?.enabled ? '已开启' : '未开启'}</span></div><p>不用每天先想主题。Ripple 只在已同步热点/活动出现有效变化、且预算允许时让 Agent 形成候选。</p></div><button className="btn btn-sm" onClick={() => setWorkbenchMode('manual')}>按需找选题</button></header>
      {discoveryPolicy?.requires_review && <div className="campaign-snapshot-notice stale">{discoveryPolicy.review_reason || '画像或账号范围已变化，请确认策略后再继续主动发现。'} 当前不会启动新的模型分析。</div>}
      {!persona ? <div className="idea-discovery-empty"><strong>先选择账号画像</strong><p>主动发现需要一个长期账号策略；首次只需确认方向和目标平台。</p><button className="btn btn-sm" onClick={onNewPersona}>选择 / 创建画像</button></div>
        : (!discoveryPolicy?.enabled || policyEditing || discoveryPolicy?.requires_review) ? <div className="idea-policy-form">
          <div className="idea-policy-row"><span>目标平台 *</span><div className="idea-target-platforms">{TARGET_PLATFORMS.map((platform) => <button key={platform} className={policyTargets.includes(platform) ? 'chip active' : 'chip'} onClick={() => setPolicyTargets((current) => current.includes(platform) ? current.filter((value) => value !== platform) : [...current, platform])}><PlatformIcon platform={platform} size={12} />{platformDisplayName(platform)}</button>)}</div></div>
          <div className="idea-policy-grid">
            <label className="wide">关注方向<input value={policyFocus} maxLength={800} onChange={(e) => setPolicyFocus(e.target.value)} placeholder="留空时沿用画像；也可填：AI工具实测，办公效率，真实体验" /></label>
            <label>机会范围<select value={policyMode} onChange={(e) => setPolicyMode(e.target.value as 'balanced' | 'combo_only')}><option value="balanced">组合优先，也允许独立热点 / 活动</option><option value="combo_only">只看热点 + 活动组合</option></select></label>
            <label>默认制作投入<select value={policyEffort} onChange={(e) => setPolicyEffort(Number(e.target.value))}><option value={60}>约 1 小时</option><option value={120}>约 2 小时</option><option value={240}>半天内</option><option value={480}>一天内</option></select></label>
            <label>每日最多分析<input type="number" min="1" max="24" value={policyRuns} onChange={(e) => setPolicyRuns(Math.max(1, Math.min(24, Number(e.target.value) || 1)))} /></label>
            <label>每日最多新候选<input type="number" min="1" max="40" value={policyCandidates} onChange={(e) => setPolicyCandidates(Math.max(1, Math.min(40, Number(e.target.value) || 1)))} /></label>
            <label>每日最多新机会提醒<input type="number" min="0" max="12" value={policyNotices} onChange={(e) => setPolicyNotices(Math.max(0, Math.min(12, Number(e.target.value) || 0)))} /></label>
            <label>通知时区<input value={policyTimezone} onChange={(e) => setPolicyTimezone(e.target.value)} /></label>
            <label>安静时段<div className="idea-policy-times"><input type="time" value={policyQuietStart} onChange={(e) => setPolicyQuietStart(e.target.value)} /><span>→</span><input type="time" value={policyQuietEnd} onChange={(e) => setPolicyQuietEnd(e.target.value)} /></div></label>
          </div>
          {policyAvailableAccounts.length > 0 && <div className="idea-policy-accounts"><span>活动账号范围</span><div>{policyAvailableAccounts.map((account) => <label key={account.id}><input type="checkbox" checked={policyAccountIds.includes(account.id)} onChange={(e) => setPolicyAccountIds((current) => e.target.checked ? Array.from(new Set([...current, account.id])) : current.filter((value) => value !== account.id))} />{account.label} · {platformDisplayName(account.platform)}</label>)}</div><small>未勾选时允许使用所选平台的公开活动；勾选后额外限制账号私有活动。</small></div>}
          <div className="idea-policy-note">热点来源沿用当前热点设置：{trendLabels.length ? trendLabels.join('、') : '全部可用热点源'}。当前只投递站内机会，不自动写稿、发布或报名，也不会提高平台采集频率。</div>
          <div className="idea-policy-actions">{discoveryPolicy?.enabled && !discoveryPolicy.requires_review && <button className="btn btn-sm" disabled={policySaving} onClick={() => setPolicyEditing(false)}>取消</button>}<button className="btn btn-sm btn-primary" disabled={policySaving || !persona || !aiReady || policyTargets.length === 0} onClick={() => void saveDiscovery(true)}>{policySaving ? '保存中…' : discoveryPolicy?.requires_review ? '确认新画像版本并继续' : discoveryPolicy?.enabled ? '保存策略' : '开启主动发现'}</button></div>
        </div> : <div className="idea-discovery-summary">
          <div className="idea-discovery-metrics"><span><b>{discovery?.unread || 0}</b> 新机会</span><span><b>{discovery?.waiting || 0}</b> 等待 / 分析中</span><span><b>{discovery?.today.runs || 0}/{discoveryPolicy.max_daily_runs}</b> 今日分析</span><span><b>{discovery?.today.candidates || 0}/{discoveryPolicy.max_daily_candidates}</b> 候选预算</span><span><b>{discovery?.today.notices || 0}/{discoveryPolicy.max_daily_notices}</b> 今日新机会提醒</span></div>
          <div className="idea-discovery-strategy"><span>方向：{discoveryPolicy.focus_keywords.length ? discoveryPolicy.focus_keywords.join('、') : '沿用账号画像'}</span><span>平台：{discoveryPolicy.target_platforms.map(platformDisplayName).join('、')}</span><span>模式：{discoveryPolicy.mode === 'combo_only' ? '仅热点 + 活动组合' : '组合优先'}</span><span>投入：约 {Math.max(1, Math.round(discoveryPolicy.effort_minutes / 60))} 小时</span></div>
          <div className="idea-discovery-health"><span>来源：{discoverySourceText}</span><span>上次检查：{discoveryPolicy.last_scan_at ? new Date(discoveryPolicy.last_scan_at * 1000).toLocaleString('zh-CN') : '等待首次检查'}</span></div>
          <div className="idea-policy-actions"><button className="btn btn-sm" onClick={() => setPolicyEditing(true)}>策略设置</button><button className="btn btn-sm" disabled={policySaving} onClick={() => void saveDiscovery(false)}>暂停主动发现</button></div>
        </div>}
    </section> : <section className="idea-command card">
      <div className="idea-command-primary">
        <label>账号画像<select value={persona} aria-label="选题账号画像" onChange={(e) => onPersonaChange(e.target.value)}><option value="">选择账号画像</option>{personas.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}</select></label>
        <button className="r2-text-button" onClick={onNewPersona}>编辑 / 创建画像</button>
        <div className="idea-target-platforms"><span>目标平台</span><button className={targetPlatforms.length === 0 ? 'chip active' : 'chip'} onClick={() => setTargetPlatforms([])}>智能分配</button>{TARGET_PLATFORMS.map((platform) => <button key={platform} className={targetPlatforms.includes(platform) ? 'chip active' : 'chip'} onClick={() => setTargetPlatforms((current) => current.includes(platform) ? current.filter((x) => x !== platform) : [...current, platform])}><PlatformIcon platform={platform} size={12} />{platformDisplayName(platform)}</button>)}</div>
      </div>
      <div className="idea-command-secondary"><label>本次目标<input value={goal} maxLength={200} onChange={(e) => setGoal(e.target.value)} placeholder="例如：实用教程 / 活动投稿 / 新功能解读" /></label><label>制作投入<select value={effort} onChange={(e) => setEffort(Number(e.target.value))}><option value={0}>不限制</option><option value={60}>约 1 小时</option><option value={120}>约 2 小时</option><option value={240}>半天内</option><option value={480}>一天内</option></select></label><label className="idea-source-toggle"><input type="checkbox" checked={includeTrends} onChange={(e) => setIncludeTrends(e.target.checked)} />自动匹配热点</label><label className="idea-source-toggle"><input type="checkbox" checked={includeCampaigns} onChange={(e) => setIncludeCampaigns(e.target.checked)} />自动匹配活动</label></div>
      {includeTrends && <small className="idea-trend-scope">热点来源：{trendLabels.length ? trendLabels.join('、') : '使用可用热点源'}</small>}
      {fixedTrendTitles.length > 0 && <div className="idea-seed-note"><span>已带入热点：{fixedTrendTitles.join('、')}{fixedTrendPlatform ? ` · 来源 ${platformDisplayName(fixedTrendPlatform)}` : ''}</span><button className="r2-text-button" type="button" onClick={() => { setFixedTrendTitles([]); setFixedTrendPlatform(''); }}>移除固定热点</button></div>}
      <div className="idea-command-bottom"><textarea value={instruction} maxLength={2000} onChange={(e) => setInstruction(e.target.value)} placeholder="补充要求，例如：只做有实测支撑的内容；不参与和科技工具无关的粉丝活动。" /><button className="btn btn-primary idea-run-button" disabled={!persona || !aiReady || (!!activeRun && ACTIVE_RUNS.has(activeRun.status))} onClick={() => void startRun()}><IconSkills size={14} /> 帮我找选题</button></div>
      {!persona && <p className="idea-command-hint">先选择账号画像，Agent 才能按赛道、受众和平台偏好筛选。</p>}{persona && !aiReady && <p className="idea-command-hint warn">Agent 推荐服务未配置。仍可以记录和管理已有选题。</p>}
    </section>}

    {activeRun && ACTIVE_RUNS.has(activeRun.status) && <section className="idea-run-bar" role="status">
      <div><span className="spinner" /><strong>{activeRun.kind === 'recommend' ? '正在生成候选' : '正在深化策划'}</strong><span>{activeRun.stage || '任务已提交'}</span></div>
      <button className="btn btn-sm" onClick={() => void cancelRun()}>取消后续生成</button>
    </section>}
    {activeRun?.status === 'interrupted' && <div className="campaign-snapshot-notice stale">上次任务在服务重启期间中断，没有自动重复调用模型。你可以重新发起。</div>}
    {error && <div className="notice-error">{error}</div>}

    <div className="idea-tabs">
      <button className={tab === 'candidates' ? 'active' : ''} onClick={() => setTab('candidates')}>{workbenchMode === 'proactive' ? '推荐机会' : '推荐候选'} <b>{visibleCandidates.length}</b></button>
      <button className={tab === 'mine' ? 'active' : ''} onClick={() => setTab('mine')}>我的选题 <b>{mine.length}</b></button>
    </div>

    {tab === 'candidates' ? <section className="idea-candidate-section">
      {visibleCandidates.length === 0 ? <div className="idea-workbench-empty"><IconIdea size={26} /><strong>{workbenchMode === 'proactive' ? '还没有新的主动机会' : '还没有候选'}</strong><p>{workbenchMode === 'proactive' ? (discoveryPolicy?.enabled ? 'Agent 会在已同步热点或活动出现有效变化、且通过本地筛选后再分析；没有值得推荐的机会时不会为了凑数生成。' : '确认一次长期策略并开启主动发现；也可以切到“按需找选题”立即探索。') : '选择画像与目标平台后点击“帮我找选题”。已有热点和活动会作为线索，找不到自然关联时也可以给赛道常青题。'}</p></div>
        : <div className="idea-candidate-grid">{visibleCandidates.map(candidateCard)}</div>}
      {rejected.length > 0 && <details className="idea-rejected"><summary>不感兴趣 / 已隐藏 {rejected.length}</summary><div>{rejected.slice(0, 20).map((idea) => <span key={idea.id}>{idea.title}<button className="r2-text-button" onClick={() => void feedback(idea, 'reopen')}>恢复</button></span>)}</div></details>}
    </section> : <section className="idea-mine-section">
      <div className="idea-mine-toolbar">
        <input type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索我的选题" />
        <div>{[['all', '全部'], ['planning', '待策划'], ['ready', '可制作'], ['production', '制作中'], ['done', '已完成']].map(([value, label]) => <button key={value} className={mineFilter === value ? 'chip active' : 'chip'} onClick={() => setMineFilter(value)}>{label}</button>)}</div>
        <div className="idea-view-toggle"><button className={mineView === 'list' ? 'active' : ''} onClick={() => setMineView('list')}>列表</button><button className={mineView === 'board' ? 'active' : ''} onClick={() => setMineView('board')}>看板</button></div>
      </div>
      {mineView === 'list' ? <div className="idea-owned-grid">{filteredMine.map(mineCard)}{filteredMine.length === 0 && <div className="idea-workbench-empty"><strong>当前筛选没有选题</strong><p>可以从推荐候选中暂存或选定，也可以手动记录灵感。</p></div>}</div>
        : <div className="idea-board">{BOARD_COLUMNS.map((column) => <div className="idea-board-col" key={column.key}><header>{column.label}<b>{mine.filter((idea) => column.stages.includes(stageOf(idea))).length}</b></header><div>{mine.filter((idea) => column.stages.includes(stageOf(idea))).map(mineCard)}</div></div>)}</div>}
    </section>}

    {evidenceIdea && <div className="overlay" onClick={() => setEvidenceIdea(null)}><div className="modal idea-evidence-modal" onClick={(e) => e.stopPropagation()}>
      <header><div><h3>来源依据</h3><p>{evidenceIdea.title}</p></div><button className="icon-btn" onClick={() => setEvidenceIdea(null)}>×</button></header>
      {evidenceLoading && <div className="idea-rec-loading"><span className="spinner" />正在读取本次任务的来源快照…</div>}
      {!evidenceLoading && evidenceSources.length === 0 && <p className="r2-muted">这是一条常青题或旧记录，本次没有绑定可追溯来源。</p>}
      <div className="idea-evidence-list">{evidenceSources.map((source) => <article key={source.id}><div><span>{sourceLabel(source)}</span><PlatformIcon platform={source.platform} size={13} /><strong>{source.title}</strong></div><small>{source.fetched_at ? new Date(source.fetched_at * 1000).toLocaleString('zh-CN') : '时间未记录'}{source.version ? ' · 版本 ' + source.version : ''}</small>{source.url && <a href={source.url} target="_blank" rel="noreferrer">打开来源</a>}<code>{source.id}</code></article>)}</div>
    </div></div>}

    {historyOpen && <div className="overlay" onClick={() => setHistoryOpen(false)}><div className="modal idea-history-modal" onClick={(e) => e.stopPropagation()}>
      <header><h3>Agent 选题批次</h3><button className="icon-btn" onClick={() => setHistoryOpen(false)}>×</button></header>
      <div className="idea-run-history">{runs.length === 0 && <p className="r2-muted">还没有 Agent 选题批次。</p>}{runs.map((run) => <div key={run.id}><div><strong>{run.kind === 'recommend' ? '候选推荐' : '策划深化'}</strong><span className={'run-status status-' + run.status}>{run.status}</span></div><p>{run.stage || run.error || '任务已提交'}</p><small>{new Date(run.created_at).toLocaleString('zh-CN')} · {run.target_platforms.length ? run.target_platforms.map(platformDisplayName).join(' / ') : '智能平台'}</small>{run.error && <em>{run.error}</em>}</div>)}</div>
    </div></div>}

    {form && <div className="overlay" onClick={() => setForm(null)}><div className="modal idea-manual-modal" onClick={(e) => e.stopPropagation()}>
      <header><h3>{editId ? '编辑选题' : '记录灵感'}</h3><button className="icon-btn" onClick={() => setForm(null)}>×</button></header>
      <label className="field-label">选题 *</label><input className="field" value={form.title} autoFocus maxLength={240} onChange={(e) => setForm({ ...form, title: e.target.value })} />
      <label className="field-label">备注 / 想法</label><textarea className="field" value={form.note || ''} onChange={(e) => setForm({ ...form, note: e.target.value })} />
      <label className="field-label">来源</label><input className="field" value={form.source || ''} onChange={(e) => setForm({ ...form, source: e.target.value })} />
      <footer><button className="btn btn-sm" onClick={() => setForm(null)}>取消</button><button className="btn btn-sm btn-primary" disabled={!form.title.trim()} onClick={() => void saveForm()}>保存到我的选题</button></footer>
    </div></div>}

    {briefIdea && brief && <IdeaBriefEditor idea={briefIdea} brief={brief} busy={briefBusy}
      onSave={saveBrief} onConfirm={confirmBrief} onDevelop={async (text) => { await develop(briefIdea, text); }}
      onStart={startContent} onClose={() => { if (!briefBusy) { setBriefIdea(null); setBrief(null); } }} />}

    {briefIdea && !brief && briefBusy && <div className="idea-floating-status"><span className="spinner" />正在为「{briefIdea.title}」形成内容策划单，可继续浏览其他页面。</div>}

    {scheduleIdea && <div className="overlay" onClick={() => setScheduleIdea(null)}><div className="modal idea-plan-modal" onClick={(e) => e.stopPropagation()}>
      <header><div><h3>{scheduleIdea.plan_id ? '调整创作排期' : '加入创作计划'}</h3><p>{scheduleIdea.title}</p></div><button className="icon-btn" onClick={() => setScheduleIdea(null)}>×</button></header>
      <label>日期<input type="date" value={scheduleDate} onChange={(e) => setScheduleDate(e.target.value)} /></label>
      <label>时间<input type="time" value={scheduleTime} onChange={(e) => setScheduleTime(e.target.value)} /></label>
      <small>使用当前浏览器时区：{Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai'}。排期只创建创作计划，不自动发布。</small>
      <footer><button className="btn btn-sm" onClick={() => setScheduleIdea(null)}>取消</button><button className="btn btn-sm btn-primary" disabled={!scheduleDate || !scheduleTime} onClick={() => void saveSchedule()}><IconCalendar size={13} /> 保存排期</button></footer>
    </div></div>}

    {evaluationIdea && <div className="overlay" onClick={() => !evaluating && setEvaluationIdea(null)}><div className="modal r2-topic-eval-modal" onClick={(e) => e.stopPropagation()}>
      <div className="idea-rec-head"><div><h3>七维选题评估</h3><p>{evaluationIdea.title}</p></div><button className="icon-btn" disabled={evaluating} onClick={() => setEvaluationIdea(null)}>×</button></div>
      {evaluating && <div className="idea-rec-loading"><span className="spinner" />正在评估…</div>}
      {evaluation && <div className="r2-topic-eval-body"><div className="r2-topic-eval-summary"><div><strong>{evaluation.output.score}</strong><span>/100</span></div><div><b>{evaluation.output.decision}</b><p>{evaluation.output.summary}</p></div></div><div className="r2-topic-dimensions">{evaluation.output.dimensions.map((dimension) => <div key={dimension.name}><header><strong>{dimension.name}</strong><span>{dimension.score}/10</span></header><div className="r2-topic-score-track"><i style={{ width: String(dimension.score * 10) + '%' }} /></div><p>{dimension.reason}</p></div>)}</div>{evaluation.output.assumptions.length > 0 && <section><h4>信息边界</h4><ul>{evaluation.output.assumptions.map((value) => <li key={value}>{value}</li>)}</ul></section>}</div>}
    </div></div>}

    {toast && <div className="toast ok"><span className="toast-icon"><IconCheck size={14} /></span>{toast}</div>}
  </div>;
}
