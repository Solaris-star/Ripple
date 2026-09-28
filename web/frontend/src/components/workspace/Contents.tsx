import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import type { Page } from '../Sidebar';
import { IconChevron } from '../icons';
import type { AgentTurnInjection, UploadedFile } from '../../lib/api';
import { api, dateText, errorText, uploadMedia } from '../../lib/ripple';
import { newId } from '../../lib/id';
import type { Mother } from '../../lib/ripple';
import type { ChatSession, SessionWorkScope, StreamState } from '../../lib/store';
import { contentDraftChanged, mergeContentDraft } from '../../lib/contentDraftMerge';
import '../../styles/content-workflow.css';
import { Empty, Feedback, Mark, Modal } from './Common';
import ContentAssistant from './ContentAssistant';
import ContentVariantPublisher from './ContentVariantPublisher';
import VariantBatch from './VariantBatch';
import BatchPublish from './BatchPublish';
import VariantMediaPreview from './VariantMediaPreview';
import { PlatformIcon } from '../PlatformBrand';
import { platformDisplayName } from '../../lib/platforms';
import { workspaceUrl } from '../../lib/workspaceNavigation';
import type { WorkspaceFocus } from '../../lib/workspaceNavigation';

const blank = (): Mother['content'] => ({ title: '', body: '', media: [], tags: '', project_id: 'local' });

const CONTENT_LIST_WIDTH_KEY = 'ripple_content_list_width_v1';
const CONTENT_LIST_COLLAPSED_KEY = 'ripple_content_list_collapsed_v1';
const CONTENT_AI_WIDTH_KEY = 'ripple_content_ai_width_v1';
const CONTENT_LIST_DEFAULT_WIDTH = 220;
const CONTENT_LIST_MIN_WIDTH = 170;
const CONTENT_LIST_MAX_WIDTH = 360;
const CONTENT_LIST_COLLAPSED_WIDTH = 46;
const CONTENT_AI_DEFAULT_WIDTH = 360;
const CONTENT_AI_MIN_WIDTH = 300;
const CONTENT_AI_MAX_WIDTH = 520;
const CONTENT_EDITOR_MIN_WIDTH = 420;
const CONTENT_AI_DRAWER_BREAKPOINT = 1440;
const CONTENT_LIST_STACK_BREAKPOINT = 700;

function clampWidth(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}

function storedPaneWidth(key: string, fallback: number, min: number, max: number): number {
  try {
    const raw = localStorage.getItem(key);
    const value = raw === null ? fallback : Number(raw);
    return Number.isFinite(value) ? clampWidth(value, min, max) : fallback;
  } catch { return fallback; }
}

function storedBoolean(key: string, fallback = false): boolean {
  try {
    const raw = localStorage.getItem(key);
    return raw === null ? fallback : raw === '1';
  } catch { return fallback; }
}

type UndoState = {
  contentId: string;
  afterVersion: string;
  before: Mother['content'];
  fields: string[];
};

type Props = {
  workScope?: SessionWorkScope;
  onNavigate: (page: Page, focus?: WorkspaceFocus) => void;
  sessions: ChatSession[];
  session: ChatSession | null;
  stream?: StreamState;
  onContentFocus: (content: Mother | null) => void;
  onAiSend: (content: Mother | null, text: string, attachments?: UploadedFile[], injection?: AgentTurnInjection) => void;
  onAiStop: () => void;
  onAiSelectSession: (id: string) => void;
  onAiNewSession: (content: Mother | null) => void;
  onAiResend: (userIndex: number, text: string, attachments?: UploadedFile[], agentText?: string, injection?: AgentTurnInjection) => void;
  onAiDraftChange: (id: string, patch: Pick<ChatSession, 'draft' | 'draftAttachments' | 'draftSkills'>) => void;
};

function changedFields(before: Mother['content'], after: Mother['content']): string[] {
  const fields: string[] = [];
  if (before.title !== after.title) fields.push('标题');
  if (before.body !== after.body) fields.push('正文');
  if (before.tags !== after.tags) fields.push('话题');
  if (JSON.stringify(before.media || []) !== JSON.stringify(after.media || [])) fields.push('素材');
  return fields;
}

export default function Contents({
  workScope, onNavigate, sessions, session, stream, onContentFocus, onAiSend, onAiStop,
  onAiSelectSession, onAiNewSession, onAiResend, onAiDraftChange,
}: Props) {
  const [records, setRecords] = useState<Mother[]>([]);
  const [selected, setSelected] = useState<Mother | null>(null);
  const [batchSource, setBatchSource] = useState<Mother | null>(null);
  const [batchPublishOpen, setBatchPublishOpen] = useState(false);
  const [selectedVariantId, setSelectedVariantId] = useState('');
  const [variantReload, setVariantReload] = useState(0);
  const [draft, setDraftState] = useState(blank);
  const [selectedText, setSelectedText] = useState('');
  const [busy, setBusy] = useState(false);
  const dirty = contentDraftChanged(draft, selected?.content || blank());
  const [variantDirty, setVariantDirty] = useState(false);
  const [compactListOpen, setCompactListOpen] = useState(false);
  const [pendingLeave, setPendingLeave] = useState<(() => void) | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [uploadProgress, setUploadProgress] = useState('');
  const [aiOpen, setAiOpen] = useState(false);
  const [listWidth, setListWidth] = useState(() => storedPaneWidth(CONTENT_LIST_WIDTH_KEY, CONTENT_LIST_DEFAULT_WIDTH, CONTENT_LIST_MIN_WIDTH, CONTENT_LIST_MAX_WIDTH));
  const [listCollapsed, setListCollapsed] = useState(() => storedBoolean(CONTENT_LIST_COLLAPSED_KEY, true));
  const [aiWidth, setAiWidth] = useState(() => storedPaneWidth(CONTENT_AI_WIDTH_KEY, CONTENT_AI_DEFAULT_WIDTH, CONTENT_AI_MIN_WIDTH, CONTENT_AI_MAX_WIDTH));
  const [resizingPane, setResizingPane] = useState<'list' | 'ai' | null>(null);
  const [aiUpdate, setAiUpdate] = useState<{ created: boolean; fields: string[] } | null>(null);
  const [undo, setUndo] = useState<UndoState | null>(null);
  const key = useRef(newId());
  const gate = useRef(false);
  const focusHandled = useRef(false);
  const handledArtifact = useRef('');
  const aiBaseline = useRef<Mother | null>(null);
  const layoutRef = useRef<HTMLDivElement>(null);
  const resizePointerId = useRef<number | null>(null);
  const mediaInputRef = useRef<HTMLInputElement>(null);
  const latestEditor = useRef({ selected, draft });
  latestEditor.current = { selected, draft };
  const setDraft = useCallback((update: Mother['content'] | ((current: Mother['content']) => Mother['content'])) => {
    // 立即更新引用，避免同一批次内上传和 AI 返回互相覆盖。
    const next = typeof update === 'function' ? update(latestEditor.current.draft) : update;
    latestEditor.current.draft = next;
    setDraftState(next);
  }, []);

  const updateRecord = useCallback((record: Mother) => {
    setRecords((current) => {
      const exists = current.some((item) => item.id === record.id);
      const next = exists ? current.map((item) => item.id === record.id ? record : item) : [record, ...current];
      return next.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
    });
  }, []);

  const refresh = useCallback(async () => {
    const rows = await api<Mother[]>('/api/ripple/contents');
    setRecords(rows);
    if (!focusHandled.current) {
      focusHandled.current = true;
      let focus = new URLSearchParams(location.search).get('content') || '';
      let variantFocus = new URLSearchParams(location.search).get('variant') || '';
      try {
        focus ||= sessionStorage.getItem('ripple_content_focus') || '';
        variantFocus ||= sessionStorage.getItem('ripple_variant_focus') || '';
        sessionStorage.removeItem('ripple_content_focus'); sessionStorage.removeItem('ripple_variant_focus');
      } catch { /* ignore */ }
      if (focus) {
        const item = rows.find((row) => row.id === focus);
        if (item) {
          setSelected(item); setDraft({ ...item.content });
          const restoredVariant = variantFocus && item.variants?.some(variant => variant.id === variantFocus) ? variantFocus
            : !variantFocus && item.variants?.length === 1 ? item.variants[0].id : '';
          setSelectedVariantId(restoredVariant);
          history.replaceState({}, '', workspaceUrl(location.href, 'contents', { contentId: item.id, variantId: restoredVariant || undefined }));
          if (variantFocus && !restoredVariant) setError('关联的平台稿已不存在，已打开通用草稿。');
        } else {
          setError('关联稿件已不存在，无法打开。请从全部内容中选择其他稿件。');
        }
      }
    }
  }, [setDraft]);
  useEffect(() => { void refresh().catch((e) => setError(errorText(e))); }, [refresh]);

  useEffect(() => {
    onContentFocus(selected);
  }, [onContentFocus, selected]);

  useEffect(() => {
    const streamingArtifact = stream?.artifacts?.at(-1);
    let artifact = streamingArtifact;
    if (!artifact && session && (!selected || session.contentContext?.id === selected.id)) {
      for (let i = session.messages.length - 1; i >= 0 && !artifact; i -= 1) artifact = session.messages[i].artifacts?.at(-1);
    }
    if (!artifact) return;
    if (artifact.proposal_id) return;
    if (selected && artifact.id !== selected.id && !streamingArtifact) return;
    const artifactKey = `${artifact.id}:${artifact.version_id}`;
    if (handledArtifact.current === artifactKey) return;
    handledArtifact.current = artifactKey;
    void api<Mother>(`/api/ripple/contents/${encodeURIComponent(artifact.id)}`).then((record) => {
      const baseline = aiBaseline.current;
      const same = selected?.id === record.id || baseline?.id === record.id;
      const before = same && baseline ? { ...baseline.content, media: [...baseline.content.media] } : same && selected ? { ...selected.content, media: [...selected.content.media] } : blank();
      const fields = changedFields(before, record.content);
      const latest = latestEditor.current;
      if (latest.selected && latest.selected.id !== record.id && latest.selected.id !== baseline?.id) return;
      const merged = latest.selected?.id === record.id
        ? mergeContentDraft(latest.selected.content, latest.draft, record.content)
        : mergeContentDraft(blank(), latest.draft, record.content);
      const hasLocalChanges = contentDraftChanged(merged, record.content);
      updateRecord(record);
      setSelected(record); setDraft(merged);
      if (hasLocalChanges) setNotice('AI 结果已更新，等待期间的手工修改已保留。请保存当前草稿。');
      if (same && fields.length && !hasLocalChanges) setUndo({ contentId: record.id, afterVersion: record.version_id, before, fields });
      else setUndo(null);
      setAiUpdate({ created: !same, fields: fields.length ? fields : ['主稿'] });
      setError('');
    }).catch((e) => setError(errorText(e)));
  }, [selected, session, stream?.artifacts, updateRecord, setDraft]);

  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => { if (dirty || variantDirty) { e.preventDefault(); e.returnValue = ''; } };
    const beforeNavigate = (e: Event) => {
      if (!dirty && !variantDirty) return;
      const proceed = (e as CustomEvent<{ proceed?: () => void }>).detail?.proceed;
      if (proceed) { e.preventDefault(); setPendingLeave(() => proceed); }
      else if (!window.confirm(`${variantDirty ? '平台版本' : '主稿'}尚未保存，放弃修改并离开？`)) e.preventDefault();
    };
    window.addEventListener('beforeunload', handler); window.addEventListener('ripple:before-navigate', beforeNavigate);
    return () => { window.removeEventListener('beforeunload', handler); window.removeEventListener('ripple:before-navigate', beforeNavigate); };
  }, [dirty, variantDirty]);

  useEffect(() => { try { localStorage.setItem(CONTENT_LIST_WIDTH_KEY, String(listWidth)); } catch { /* ignore */ } }, [listWidth]);
  useEffect(() => { try { localStorage.setItem(CONTENT_LIST_COLLAPSED_KEY, listCollapsed ? '1' : '0'); } catch { /* ignore */ } }, [listCollapsed]);
  useEffect(() => { try { localStorage.setItem(CONTENT_AI_WIDTH_KEY, String(aiWidth)); } catch { /* ignore */ } }, [aiWidth]);
  useEffect(() => {
    if (!resizingPane) return;
    const previous = document.body.style.userSelect;
    document.body.style.userSelect = 'none';
    document.body.classList.add('r2-content-resizing');
    const move = (event: PointerEvent) => {
      if (resizePointerId.current !== null && event.pointerId !== resizePointerId.current) return;
      const rect = layoutRef.current?.getBoundingClientRect();
      if (!rect) return;
      if (resizingPane === 'list') {
        if (window.innerWidth <= CONTENT_LIST_STACK_BREAKPOINT) return;
        const reservedAiWidth = window.innerWidth > CONTENT_AI_DRAWER_BREAKPOINT ? aiWidth : 0;
        const max = Math.max(CONTENT_LIST_MIN_WIDTH, Math.min(CONTENT_LIST_MAX_WIDTH, rect.width - reservedAiWidth - CONTENT_EDITOR_MIN_WIDTH));
        setListWidth(clampWidth(event.clientX - rect.left, CONTENT_LIST_MIN_WIDTH, max));
      } else {
        if (window.innerWidth <= CONTENT_AI_DRAWER_BREAKPOINT) return;
        const max = Math.max(CONTENT_AI_MIN_WIDTH, Math.min(CONTENT_AI_MAX_WIDTH, rect.width - listWidth - CONTENT_EDITOR_MIN_WIDTH));
        setAiWidth(clampWidth(rect.right - event.clientX, CONTENT_AI_MIN_WIDTH, max));
      }
    };
    const finish = (event: PointerEvent) => {
      if (resizePointerId.current !== null && event.pointerId !== resizePointerId.current) return;
      resizePointerId.current = null;
      setResizingPane(null);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
    return () => {
      document.body.style.userSelect = previous;
      document.body.classList.remove('r2-content-resizing');
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
    };
  }, [resizingPane, listWidth, aiWidth, listCollapsed]);

  const paneMaxWidth = (pane: 'list' | 'ai'): number => {
    const rect = layoutRef.current?.getBoundingClientRect();
    const hardMax = pane === 'list' ? CONTENT_LIST_MAX_WIDTH : CONTENT_AI_MAX_WIDTH;
    const min = pane === 'list' ? CONTENT_LIST_MIN_WIDTH : CONTENT_AI_MIN_WIDTH;
    if (!rect) return hardMax;
    const effectiveListWidth = listCollapsed ? CONTENT_LIST_COLLAPSED_WIDTH : listWidth;
    const other = pane === 'list'
      ? (window.innerWidth > CONTENT_AI_DRAWER_BREAKPOINT ? aiWidth : 0)
      : effectiveListWidth;
    return Math.max(min, Math.min(hardMax, rect.width - other - CONTENT_EDITOR_MIN_WIDTH));
  };
  const adjustPaneWidth = (pane: 'list' | 'ai', delta: number) => {
    const max = paneMaxWidth(pane);
    if (pane === 'list') setListWidth((current) => clampWidth(current + delta, CONTENT_LIST_MIN_WIDTH, max));
    else setAiWidth((current) => clampWidth(current + delta, CONTENT_AI_MIN_WIDTH, max));
  };
  const resetPaneWidth = (pane: 'list' | 'ai') => {
    const max = paneMaxWidth(pane);
    if (pane === 'list') setListWidth(clampWidth(CONTENT_LIST_DEFAULT_WIDTH, CONTENT_LIST_MIN_WIDTH, max));
    else setAiWidth(clampWidth(CONTENT_AI_DEFAULT_WIDTH, CONTENT_AI_MIN_WIDTH, max));
  };

  const patch = <K extends keyof Mother['content']>(name: K, value: Mother['content'][K]) => {
    setDraft((current) => ({ ...current, [name]: value })); setAiUpdate(null); setUndo(null);
  };
  const persist = useCallback(async (): Promise<Mother | null> => {
    if (!dirty) return selected;
    if (!draft.title.trim()) throw new Error('当前手动主稿还没有标题。先填写标题再交给 AI，或新建空白内容后直接让 AI 从零创作。');
    const record = await api<Mother>(selected ? `/api/ripple/contents/${selected.id}` : '/api/ripple/contents', selected ? 'PUT' : 'POST', {
      ...draft,
      project_id: selected?.content.project_id || 'local',
      ...(selected ? { expected_version: selected.version_id } : { idempotency_key: key.current }),
    });
    updateRecord(record); setSelected(record); setDraft({ ...record.content, media: [...record.content.media] });
    return record;
  }, [dirty, draft, selected, updateRecord, setDraft]);

  const run = async (fn: () => Promise<void>) => {
    if (gate.current) return;
    gate.current = true; setBusy(true); setError(''); setNotice('');
    try { await fn(); } catch (e) { setError(errorText(e)); } finally { gate.current = false; setBusy(false); }
  };
  const pick = (item: Mother | null, updateUrl = true, afterPick?: () => void, discard = false): boolean => {
    if ((dirty || variantDirty) && !discard) {
      setPendingLeave(() => () => pick(item, true, afterPick, true));
      return false;
    }
    aiBaseline.current = null;
    setSelected(item); setDraft(item ? { ...item.content, media: [...item.content.media] } : blank()); setSelectedVariantId('');
    setSelectedText('');
    setVariantDirty(false); setCompactListOpen(false); setError(''); setNotice(''); setAiUpdate(null); setUndo(null); key.current = newId();
    if (updateUrl) history.pushState({}, '', workspaceUrl(location.href, 'contents', { contentId: item?.id }));
    afterPick?.();
    return true;
  };
  const selectVariant = (id: string, updateUrl = true) => {
    const proceed = () => {
      setVariantDirty(false); setSelectedVariantId(id);
      if (updateUrl) history.pushState({}, '', workspaceUrl(location.href, 'contents', { contentId: selected?.id, variantId: id || undefined }));
    };
    if (variantDirty) setPendingLeave(() => proceed); else proceed();
  };
  useEffect(() => {
    // 保存完成后继续先前的切换，避免仍显示过时的未保存提示。
    if (pendingLeave && !dirty && !variantDirty && !busy) { setPendingLeave(null); pendingLeave(); }
  }, [pendingLeave, dirty, variantDirty, busy]);
  useEffect(() => {
    const onBack = () => {
      if (new URLSearchParams(location.search).get('page') !== 'contents') return;
      const id = new URLSearchParams(location.search).get('content');
      const variantId = new URLSearchParams(location.search).get('variant') || '';
      if (id === selected?.id || (!id && !selected)) {
        if (variantId !== selectedVariantId) {
          if (variantDirty) {
            history.replaceState({}, '', workspaceUrl(location.href, 'contents', { contentId: selected?.id, variantId: selectedVariantId || undefined }));
            selectVariant(variantId);
          } else selectVariant(variantId, false);
        }
        return;
      }
      const record = records.find((item) => item.id === id) || null;
      if (!pick(record, false, () => setSelectedVariantId(record?.variants?.some(item => item.id === variantId) ? variantId : ''))) history.go(1);
    };
    window.addEventListener('popstate', onBack);
    return () => window.removeEventListener('popstate', onBack);
  });
  const save = () => run(async () => {
    const record = await persist();
    if (record) setNotice(`已保存 · 主稿 V${record.version}。已有平台版本不会被自动覆盖。`);
  });
  const openVariantBatch = () => run(async () => {
    const record = await persist();
    if (!record) throw new Error('请先填写标题，保存为母稿后再创建平台版本。');
    // 重新读取平台目标，避免刚改过 Blog 连接后仍按旧目标显示重复选项。
    setBatchSource(await api<Mother>(`/api/ripple/contents/${record.id}`));
  });
  const sendToAi = (text: string, attachments?: UploadedFile[], injection: AgentTurnInjection = {}) => {
    void run(async () => {
      const content = await persist();
      aiBaseline.current = content ? { ...content, content: { ...content.content, media: [...content.content.media] } } : null;
      onAiSend(content, text, attachments, injection);
      setAiOpen(true);
    });
  };
  const newAiSession = () => { onAiNewSession(selected); setAiOpen(true); };
  const createContent = () => {
    pick(null, true, () => { onAiNewSession(null); setAiOpen(true); });
  };
  const selectAiSession = (id: string) => {
    const target = sessions.find((item) => item.id === id);
    const targetContent = target?.contentContext?.id ? records.find((item) => item.id === target.contentContext?.id) : undefined;
    const proceed = () => { onAiSelectSession(id); setAiOpen(true); };
    if (targetContent && targetContent.id !== selected?.id) { pick(targetContent, true, proceed); return; }
    proceed();
  };
  const undoAi = () => run(async () => {
    if (dirty) throw new Error('请先保存当前手工修改，再处理 AI 修改。');
    if (!undo || !selected || selected.id !== undo.contentId || selected.version_id !== undo.afterVersion) throw new Error('当前主稿已经继续变化，不能再应用这次撤销。');
    const record = await api<Mother>(`/api/ripple/contents/${selected.id}`, 'PUT', { ...undo.before, expected_version: selected.version_id });
    updateRecord(record); setSelected(record); setDraft({ ...record.content, media: [...record.content.media] });
    setNotice(`已撤销上一轮 AI 对 ${undo.fields.join('、')} 的修改，并保存为主稿 V${record.version}。`); setUndo(null); setAiUpdate(null);
  });

  const onProposalApplied = (contentId: string) => {
    void api<Mother>(`/api/ripple/contents/${encodeURIComponent(contentId)}`).then((record) => {
      updateRecord(record);
      const latest = latestEditor.current;
      if (latest.selected?.id === record.id) {
        const merged = mergeContentDraft(latest.selected.content, latest.draft, record.content);
        setSelected(record); setDraft(merged);
        setAiUpdate({ created: false, fields: ['主稿'] }); setUndo(null);
      }
    }).catch((cause) => setError(errorText(cause)));
  };

  const editorLocked = busy;
  const topicContext = session?.topicContext;
  const sessionForContent = useMemo(() => session, [session]);
  const lastReply = sessionForContent?.messages.at(-1);
  const noGeneratedBody = !stream && !draft.body.trim() && lastReply?.role === 'assistant'
    && sessionForContent?.contentContext?.id === selected?.id && !lastReply.artifacts?.length;
  const layoutStyle = {
    '--r2-content-list-width': `${listCollapsed ? CONTENT_LIST_COLLAPSED_WIDTH : listWidth}px`,
    '--r2-content-ai-width': `${aiWidth}px`,
  } as CSSProperties;

  return <div className="r2-workspace r2-content-workspace">
    <Feedback error={error} notice={notice} />
    {pendingLeave && <Modal title="还有未保存的修改" onClose={() => setPendingLeave(null)}>
      <p>{variantDirty ? '平台版本' : '通用草稿'}的修改尚未保存。可以继续编辑并保存，或放弃本次修改后离开。</p>
      <footer><button className="r2-button primary" onClick={() => setPendingLeave(null)}>继续编辑</button><button className="r2-button" onClick={() => { const proceed = pendingLeave; setPendingLeave(null); proceed(); }}>放弃修改并离开</button></footer>
    </Modal>}
    {aiUpdate && <div className="r2-ai-update-banner"><span>{aiUpdate.created ? 'AI 创建内容' : `AI 更新：${aiUpdate.fields.join(' · ')}`}</span>{undo && !aiUpdate.created && <button className="r2-text-button" disabled={busy || !!stream} onClick={() => void undoAi()}>撤销这次 AI 修改</button>}</div>}
    {topicContext && <section className="r2-topic-handoff">
      <header>
        <div>
          <strong>{topicContext.campaignTitle ? `活动创作任务 · ${topicContext.campaignTitle}` : '选题创作任务'}</strong>
          <span>{topicContext.title}</span>
        </div>
        {topicContext.campaignId && <button className="r2-text-button" type="button" onClick={() => onNavigate('campaigns')}>活动广场</button>}
      </header>
      <div className="r2-topic-handoff-tags">
        {topicContext.targetPlatforms?.map((platform) => <span key={platform}><PlatformIcon platform={platform} size={12} />目标平台 · {platformDisplayName(platform)}</span>)}
        {topicContext.campaignSubmitDeadline && <span>投稿截止 · {topicContext.campaignSubmitDeadline.slice(0, 10)}</span>}
        {topicContext.campaignCurrentRuleVersion && <span>当前规则 · v{topicContext.campaignCurrentRuleVersion}</span>}
        {topicContext.trendRefs?.slice(0, 3).map((ref) => <span key={ref}>热点 · {ref}</span>)}
      </div>
      {!!topicContext.campaignRequiredTopics?.length && <div className="r2-topic-handoff-row"><b>指定话题</b><span>{topicContext.campaignRequiredTopics.join('、')}</span></div>}
      {!!topicContext.campaignRequirements?.length && <div className="r2-topic-handoff-row"><b>活动要求</b><span>{topicContext.campaignRequirements.join('；')}</span></div>}
      {!!topicContext.pendingChecks?.length && <div className="r2-topic-handoff-row pending"><b>待确认</b><span>{topicContext.pendingChecks.join('；')}</span></div>}
      {topicContext.campaignAiPolicy && topicContext.campaignAiPolicy !== 'unknown' && <div className="r2-topic-handoff-row"><b>AI 使用要求</b><span>{topicContext.campaignAiPolicy}</span></div>}
    </section>}

    <nav className="r2-content-view-tabs" aria-label="创作视图">
      <button aria-pressed={!aiOpen && !compactListOpen} onClick={() => { setAiOpen(false); setCompactListOpen(false); }}>编辑与预览</button>
      <button aria-pressed={aiOpen} onClick={() => { setAiOpen(true); setCompactListOpen(false); }}>AI 协作{stream ? ' · 处理中' : ''}</button>
      <button aria-pressed={compactListOpen} onClick={() => { setCompactListOpen(true); setListCollapsed(false); setAiOpen(false); }}>全部内容</button>
    </nav>
    <div ref={layoutRef} className={`r2-content-layout r2-content-layout-ai ${listCollapsed ? 'list-collapsed' : ''} ${resizingPane ? 'resizing' : ''} ${aiOpen ? 'workflow-show-ai' : compactListOpen ? 'workflow-show-list' : 'workflow-show-editor'}`.trim()} style={layoutStyle}>
      <div className="r2-content-resize-handle r2-content-list-resize" role="separator" aria-label="调整内容列表宽度" aria-orientation="vertical" aria-valuemin={CONTENT_LIST_MIN_WIDTH} aria-valuemax={paneMaxWidth('list')} aria-valuenow={listWidth} tabIndex={0}
        onPointerDown={(event) => { if (listCollapsed || window.innerWidth <= CONTENT_LIST_STACK_BREAKPOINT) return; resizePointerId.current = event.pointerId; setResizingPane('list'); }}
        onDoubleClick={() => resetPaneWidth('list')}
        onKeyDown={(event) => { if (event.key === 'ArrowLeft') { event.preventDefault(); adjustPaneWidth('list', -10); } else if (event.key === 'ArrowRight') { event.preventDefault(); adjustPaneWidth('list', 10); } else if (event.key === 'Home') { event.preventDefault(); resetPaneWidth('list'); } }} />
      <div className="r2-content-resize-handle r2-content-ai-resize" role="separator" aria-label="调整 AI 协作宽度" aria-orientation="vertical" aria-valuemin={CONTENT_AI_MIN_WIDTH} aria-valuemax={paneMaxWidth('ai')} aria-valuenow={aiWidth} tabIndex={0}
        onPointerDown={(event) => { if (window.innerWidth <= CONTENT_AI_DRAWER_BREAKPOINT) return; resizePointerId.current = event.pointerId; setResizingPane('ai'); }}
        onDoubleClick={() => resetPaneWidth('ai')}
        onKeyDown={(event) => { if (event.key === 'ArrowLeft') { event.preventDefault(); adjustPaneWidth('ai', 10); } else if (event.key === 'ArrowRight') { event.preventDefault(); adjustPaneWidth('ai', -10); } else if (event.key === 'Home') { event.preventDefault(); resetPaneWidth('ai'); } }} />
      <aside className={`r2-content-list ${records.length === 0 ? 'empty' : ''}`}>
        <div className="r2-list-caption r2-content-list-caption"><span>全部内容 <b>{records.length}</b></span><button type="button" className="r2-list-new" disabled={busy || !!stream} onClick={createContent}>新建内容</button><button type="button" className="r2-list-collapse" aria-label={listCollapsed ? '展开全部内容' : '收起全部内容'} title={listCollapsed ? '展开全部内容' : '收起全部内容'} onClick={() => { setResizingPane(null); setListCollapsed((value) => !value); }}><IconChevron size={14} /></button></div>
        {records.map((record) => <button key={record.id} className={`r2-list-item ${selected?.id === record.id ? 'active' : ''}`} disabled={busy || !!stream} onClick={() => pick(record)}><strong>{record.content.title}</strong><span>主稿 V{record.version} · {record.variants?.length || 0} 个平台版本</span><small>{dateText(record.updated_at)}</small></button>)}
        {records.length === 0 && <Empty title="还没有内容" description="直接在右侧告诉 AI 想创作什么，或者先手动写一份主稿。" />}
      </aside>

      <main className="r2-content-editor">
        {noGeneratedBody && <div className="campaign-snapshot-notice stale" role="status">
          <strong>正文尚未生成</strong><p>当前保存的是空草稿。可以重试正文，或直接在下方填写；已有素材会保留。</p>
          <button className="r2-button" disabled={busy} onClick={() => sendToAi('请根据已确认策划仅重试生成正文和标题、话题，保存为修改建议；保留已有图片，不调用图片生成，不编造亲测经历。')}>重试正文</button>
        </div>}
        <details className="r2-mother-editor" open={!selectedVariantId}>
        <summary>通用草稿{dirty ? ' · 有未保存修改' : selectedVariantId ? ' · 点击查看或编辑' : ''}</summary>
        <div className="r2-field r2-title-field"><div className="r2-title-field-head"><span className="r2-title-label">标题</span><div className="r2-title-actions">{(stream || dirty || selected) && <span className="r2-editor-state">{stream ? 'AI 正在处理' : dirty ? '未保存' : '已保存'}</span>}<button type="button" className="r2-button r2-title-action r2-ai-pane-toggle" onClick={() => setAiOpen(true)}>AI 协作</button><button type="button" className="r2-button primary r2-title-action" disabled={busy || !!stream || !draft.title.trim() || (!dirty && !!selected)} onClick={() => void save()}>保存</button></div></div><input className="r2-title-input" aria-label="内容标题" placeholder="给主稿起个标题" value={draft.title} maxLength={200} disabled={editorLocked} onChange={(e) => patch('title', e.target.value)} /></div>
        <label className="r2-field">正文<textarea className="r2-mother-body" aria-label="内容正文" value={draft.body} maxLength={100000} placeholder="这里是内容主稿。也可以直接在右侧告诉 AI 主题和要求，让它完成初稿。" disabled={editorLocked} onChange={(e) => patch('body', e.target.value)} onSelect={(e) => { const input = e.currentTarget; setSelectedText(input.value.slice(input.selectionStart, input.selectionEnd).trim().slice(0, 2000)); }} /></label>
        {selectedText && <div className="focus-selection-actions"><span>已选中文字：{selectedText.length} 字</span>{[['润色', '润色选中文字，保留原意'], ['缩短', '缩短选中文字，保留关键信息'], ['调整语气', '调整选中文字的语气，使其更自然']].map(([label, instruction]) => <button key={label} disabled={busy || !!stream} onClick={() => { sendToAi(`${instruction}。请基于当前主稿生成修改建议，先不要覆盖正文。选中文字：\n${selectedText}`); setSelectedText(''); }}>{label}</button>)}</div>}
        <label className="r2-field">话题标签<input aria-label="内容话题标签" value={draft.tags} maxLength={1000} placeholder="用逗号分隔；也可以让 AI 自动整理" onChange={(e) => patch('tags', e.target.value)} disabled={editorLocked} /></label>

        <div className="r2-section-heading"><h2>素材 <span>{draft.media.length}</span></h2><div className="r2-toolbar"><label className="r2-button r2-file-button">添加图片 / 视频<input ref={mediaInputRef} type="file" aria-label="上传内容素材" accept=".png,.jpg,.jpeg,.webp,.gif,.mp4,.mov,.webm" multiple disabled={editorLocked || draft.media.length >= 12} onChange={(e) => { const files = Array.from(e.target.files || []); e.target.value = ''; void run(async () => { if (files.length + draft.media.length > 12) throw new Error('最多添加 12 个素材。'); const paths: string[] = []; try { for (const file of files) { const uploaded = await uploadMedia(file, (n) => setUploadProgress(`${file.name} · ${n}%`)); paths.push(uploaded.path); } } finally { if (paths.length) { setDraft((current) => ({ ...current, media: [...current.media, ...paths] })); setAiUpdate(null); setUndo(null); } setUploadProgress(''); } }); }} /></label></div></div>
        {uploadProgress && <p className="r2-muted">{uploadProgress}</p>}
        <div className="variant-media-grid">{draft.media.map((path, index) => <figure className="variant-media-card" aria-label={`通用草稿素材 ${index + 1}`} key={path}>
          <VariantMediaPreview path={path} index={index} />
          <figcaption><strong>{index === 0 ? '封面' : `素材 ${index + 1}`}</strong><br /><small>{/^[a-f0-9]{32}\.[a-z0-9]+$/i.test(path.split('/').at(-1) || '') ? `已上传素材 ${index + 1}` : path.split('/').at(-1)}</small></figcaption>
          <div className="variant-media-actions">
            <button className="r2-button" disabled={editorLocked || index === 0} onClick={() => { const media = [...draft.media]; [media[index - 1], media[index]] = [media[index], media[index - 1]]; patch('media', media); }}>前移</button>
            <button className="r2-button" disabled={editorLocked || index === draft.media.length - 1} onClick={() => { const media = [...draft.media]; [media[index + 1], media[index]] = [media[index], media[index + 1]]; patch('media', media); }}>后移</button>
            <button className="r2-button" aria-label={`移除素材 ${index + 1}`} disabled={editorLocked} onClick={() => patch('media', draft.media.filter((_, i) => i !== index))}>移除</button>
          </div>
        </figure>)}</div>
        </details>

        <details className="r2-version-list" open={!selectedVariantId}>
        <summary>平台稿与更多平台</summary>
        {selected && <button className="r2-button" disabled={busy || dirty || variantDirty || !!stream} onClick={() => setBatchPublishOpen(true)}>多平台发布总览</button>}
        <div className="r2-section-heading r2-variants-heading"><h2>平台版本</h2><div className="r2-toolbar"><button className="r2-button" disabled={busy || !!stream || !draft.title.trim()} onClick={() => void openVariantBatch()}>创建平台版本</button></div></div>
        {selected && (records.find((record) => record.id === selected.id)?.variants || []).map((variant) => <button className={`r2-variant-row ${selectedVariantId === variant.id ? 'active' : ''}`} key={variant.id} onClick={() => { if (selectedVariantId === variant.id) return; selectVariant(variant.id); }}><Mark platform={variant.platform} /><span>{platformDisplayName(variant.platform)} · 平台版本 V{variant.version}</span><small>{variant.delivery === 'export' ? 'Markdown 导出' : variant.target_id ? '已选择发布目标' : '待选择发布目标'}</small>{variant.stale && <small className="r2-warning">基于旧母稿</small>}</button>)}
        {selected && !(records.find((record) => record.id === selected.id)?.variants || []).length && <p className="r2-muted">先选择平台建立独立版本。账号、Blog 连接、排期和发布方式都在平台版本中再选择。</p>}
        </details>
        {selected && selectedVariantId && <ContentVariantPublisher key={`${selectedVariantId}-${variantReload}`} variantId={selectedVariantId} source={selected} onNavigate={(page, taskId) => onNavigate(page, taskId ? { taskId } : undefined)} onDirtyChange={setVariantDirty} onUpdated={() => void refresh().catch((e) => setError(errorText(e)))} />}
        {selected && batchPublishOpen && <BatchPublish source={selected} onClose={() => { setBatchPublishOpen(false); setVariantReload(value => value + 1); }} onUpdated={() => void refresh().catch(e => setError(errorText(e)))} />}
      </main>

      <div className={`r2-content-ai-wrap ${aiOpen ? 'mobile-open' : ''}`}>
        <button className="r2-ai-mobile-close" aria-label="关闭 AI 协作" onClick={() => setAiOpen(false)}>×</button>
        <ContentAssistant editingVariantId={selectedVariantId || undefined} onVariantUpdated={() => { setVariantReload(value => value + 1); void refresh().catch(e => setError(errorText(e))); }} contentId={selected?.id} contentTitle={selected?.content.title || draft.title} editingPlatform={selected?.variants?.find(item => item.id === selectedVariantId)?.platform || records.find(item => item.id === selected?.id)?.variants?.find(item => item.id === selectedVariantId)?.platform} sessions={sessions} session={sessionForContent} stream={stream} onSend={sendToAi} onStop={onAiStop} onSelectSession={selectAiSession} onNewSession={newAiSession} onResend={onAiResend} onDraftChange={(patch) => { if (sessionForContent) onAiDraftChange(sessionForContent.id, patch); }} onProposalApplied={onProposalApplied} canApplyProposal={!dirty && !variantDirty && !busy} onUploadMedia={() => { setAiOpen(false); setCompactListOpen(false); mediaInputRef.current?.click(); }} />
      </div>
    </div>

    {batchSource && <VariantBatch source={batchSource} workScope={workScope || session?.workScope} targetPlatforms={topicContext?.targetPlatforms} onClose={() => setBatchSource(null)} onDone={(count) => { setBatchSource(null); setNotice(`已创建 ${count} 个平台版本。核对内容后，点击“准备发布”。`); void refresh().then(() => api<Mother>(`/api/ripple/contents/${batchSource.id}`)).then((record) => { if (record.variants?.length === 1) setSelectedVariantId(record.variants[0].id); }).catch((e) => setError(errorText(e))); }} />}
  </div>;
}
