import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties } from 'react';
import type { Page } from '../Sidebar';
import { IconChevron } from '../icons';
import type { AgentTurnInjection, UploadedFile } from '../../lib/api';
import { api, dateText, errorText, uploadMedia } from '../../lib/ripple';
import { newId } from '../../lib/id';
import type { Mother } from '../../lib/ripple';
import type { ChatSession, StreamState } from '../../lib/store';
import { Empty, Feedback, Mark } from './Common';
import ContentAssistant from './ContentAssistant';
import ContentVariantPublisher from './ContentVariantPublisher';
import VariantBatch from './VariantBatch';

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
const CONTENT_AI_DRAWER_BREAKPOINT = 1200;
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
  onNavigate: (page: Page) => void;
  sessions: ChatSession[];
  session: ChatSession | null;
  stream?: StreamState;
  onContentFocus: (content: Mother | null) => void;
  onAiSend: (content: Mother | null, text: string, attachments?: UploadedFile[], injection?: AgentTurnInjection) => void;
  onAiStop: () => void;
  onAiSelectSession: (id: string) => void;
  onAiNewSession: (content: Mother | null) => void;
  onAiResend: (userIndex: number, text: string, attachments?: UploadedFile[], agentText?: string, injection?: AgentTurnInjection) => void;
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
  onNavigate, sessions, session, stream, onContentFocus, onAiSend, onAiStop,
  onAiSelectSession, onAiNewSession, onAiResend,
}: Props) {
  const [records, setRecords] = useState<Mother[]>([]);
  const [selected, setSelected] = useState<Mother | null>(null);
  const [batchSource, setBatchSource] = useState<Mother | null>(null);
  const [selectedVariantId, setSelectedVariantId] = useState('');
  const [draft, setDraft] = useState(blank);
  const [busy, setBusy] = useState(false);
  const [dirty, setDirty] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [uploadProgress, setUploadProgress] = useState('');
  const [aiOpen, setAiOpen] = useState(false);
  const [listWidth, setListWidth] = useState(() => storedPaneWidth(CONTENT_LIST_WIDTH_KEY, CONTENT_LIST_DEFAULT_WIDTH, CONTENT_LIST_MIN_WIDTH, CONTENT_LIST_MAX_WIDTH));
  const [listCollapsed, setListCollapsed] = useState(() => storedBoolean(CONTENT_LIST_COLLAPSED_KEY));
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
      let focus = '', variantFocus = '';
      try {
        focus = sessionStorage.getItem('ripple_content_focus') || '';
        variantFocus = sessionStorage.getItem('ripple_variant_focus') || '';
        sessionStorage.removeItem('ripple_content_focus'); sessionStorage.removeItem('ripple_variant_focus');
      } catch { /* ignore */ }
      if (focus) {
        const item = rows.find((row) => row.id === focus);
        if (item) {
          setSelected(item); setDraft({ ...item.content }); setDirty(false);
          if (variantFocus && item.variants?.some((variant) => variant.id === variantFocus)) setSelectedVariantId(variantFocus);
        }
      }
    }
  }, []);
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
    if (selected && artifact.id !== selected.id && !streamingArtifact) return;
    const artifactKey = `${artifact.id}:${artifact.version_id}`;
    if (handledArtifact.current === artifactKey) return;
    handledArtifact.current = artifactKey;
    void api<Mother>(`/api/ripple/contents/${encodeURIComponent(artifact.id)}`).then((record) => {
      const baseline = aiBaseline.current;
      const same = selected?.id === record.id || baseline?.id === record.id;
      const before = same && baseline ? { ...baseline.content, media: [...baseline.content.media] } : same && selected ? { ...selected.content, media: [...selected.content.media] } : blank();
      const fields = changedFields(before, record.content);
      updateRecord(record);
      setSelected(record); setDraft({ ...record.content, media: [...record.content.media] }); setDirty(false);
      if (same && fields.length) setUndo({ contentId: record.id, afterVersion: record.version_id, before, fields });
      else setUndo(null);
      setAiUpdate({ created: !same, fields: fields.length ? fields : ['主稿'] });
      setError('');
    }).catch((e) => setError(errorText(e)));
  }, [selected, session, stream?.artifacts, updateRecord]);

  useEffect(() => {
    const handler = (e: BeforeUnloadEvent) => { if (dirty) { e.preventDefault(); e.returnValue = ''; } };
    const beforeNavigate = (e: Event) => { if (dirty && !window.confirm('主稿尚未保存，放弃修改并离开？')) e.preventDefault(); };
    window.addEventListener('beforeunload', handler); window.addEventListener('ripple:before-navigate', beforeNavigate);
    return () => { window.removeEventListener('beforeunload', handler); window.removeEventListener('ripple:before-navigate', beforeNavigate); };
  }, [dirty]);

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
    setDraft((current) => ({ ...current, [name]: value })); setDirty(true); setAiUpdate(null);
  };
  const persist = useCallback(async (): Promise<Mother | null> => {
    if (!dirty) return selected;
    if (!draft.title.trim()) throw new Error('当前手动主稿还没有标题。先填写标题再交给 AI，或新建空白内容后直接让 AI 从零创作。');
    const record = await api<Mother>(selected ? `/api/ripple/contents/${selected.id}` : '/api/ripple/contents', selected ? 'PUT' : 'POST', {
      ...draft,
      project_id: selected?.content.project_id || 'local',
      ...(selected ? { expected_version: selected.version_id } : { idempotency_key: key.current }),
    });
    updateRecord(record); setSelected(record); setDraft({ ...record.content, media: [...record.content.media] }); setDirty(false);
    return record;
  }, [dirty, draft, selected, updateRecord]);

  const run = async (fn: () => Promise<void>) => {
    if (gate.current) return;
    gate.current = true; setBusy(true); setError(''); setNotice('');
    try { await fn(); } catch (e) { setError(errorText(e)); } finally { gate.current = false; setBusy(false); }
  };
  const pick = (item: Mother | null): boolean => {
    if (dirty && !window.confirm('当前主稿尚未保存，放弃修改？')) return false;
    aiBaseline.current = null;
    setSelected(item); setDraft(item ? { ...item.content, media: [...item.content.media] } : blank()); setSelectedVariantId('');
    setDirty(false); setError(''); setNotice(''); setAiUpdate(null); setUndo(null); key.current = newId();
    return true;
  };
  const save = () => run(async () => {
    const record = await persist();
    if (record) setNotice(`已保存 · 主稿 V${record.version}。已有平台版本不会被自动覆盖。`);
  });
  const openVariantBatch = () => run(async () => {
    const record = await persist();
    if (!record) throw new Error('请先填写标题，保存为母稿后再创建平台版本。');
    setBatchSource(record);
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
    if (!pick(null)) return;
    onAiNewSession(null);
    setAiOpen(true);
  };
  const selectAiSession = (id: string) => {
    const target = sessions.find((item) => item.id === id);
    const targetContent = target?.contentContext?.id ? records.find((item) => item.id === target.contentContext?.id) : undefined;
    if (targetContent && targetContent.id !== selected?.id && !pick(targetContent)) return;
    onAiSelectSession(id); setAiOpen(true);
  };
  const undoAi = () => run(async () => {
    if (!undo || !selected || selected.id !== undo.contentId || selected.version_id !== undo.afterVersion) throw new Error('当前主稿已经继续变化，不能再应用这次撤销。');
    const record = await api<Mother>(`/api/ripple/contents/${selected.id}`, 'PUT', { ...undo.before, expected_version: selected.version_id });
    updateRecord(record); setSelected(record); setDraft({ ...record.content, media: [...record.content.media] }); setDirty(false);
    setNotice(`已撤销上一轮 AI 对 ${undo.fields.join('、')} 的修改，并保存为主稿 V${record.version}。`); setUndo(null); setAiUpdate(null);
  });

  const editorLocked = busy || !!stream;
  const sessionForContent = useMemo(() => session, [session]);
  const layoutStyle = {
    '--r2-content-list-width': `${listCollapsed ? CONTENT_LIST_COLLAPSED_WIDTH : listWidth}px`,
    '--r2-content-ai-width': `${aiWidth}px`,
  } as CSSProperties;

  return <div className="r2-workspace r2-content-workspace">
    <Feedback error={error} notice={notice} />
    {aiUpdate && <div className="r2-ai-update-banner"><span>{aiUpdate.created ? 'AI 创建内容' : `AI 更新：${aiUpdate.fields.join(' · ')}`}</span>{undo && !aiUpdate.created && <button className="r2-text-button" disabled={busy || !!stream} onClick={() => void undoAi()}>撤销这次 AI 修改</button>}</div>}

    <div ref={layoutRef} className={`r2-content-layout r2-content-layout-ai ${listCollapsed ? 'list-collapsed' : ''} ${resizingPane ? 'resizing' : ''}`.trim()} style={layoutStyle}>
      <div className="r2-content-resize-handle r2-content-list-resize" role="separator" aria-label="调整内容列表宽度" aria-orientation="vertical" aria-valuemin={CONTENT_LIST_MIN_WIDTH} aria-valuemax={paneMaxWidth('list')} aria-valuenow={listWidth} tabIndex={0}
        onPointerDown={(event) => { if (listCollapsed || window.innerWidth <= CONTENT_LIST_STACK_BREAKPOINT) return; resizePointerId.current = event.pointerId; setResizingPane('list'); }}
        onDoubleClick={() => resetPaneWidth('list')}
        onKeyDown={(event) => { if (event.key === 'ArrowLeft') { event.preventDefault(); adjustPaneWidth('list', -10); } else if (event.key === 'ArrowRight') { event.preventDefault(); adjustPaneWidth('list', 10); } else if (event.key === 'Home') { event.preventDefault(); resetPaneWidth('list'); } }} />
      <div className="r2-content-resize-handle r2-content-ai-resize" role="separator" aria-label="调整 AI 协作宽度" aria-orientation="vertical" aria-valuemin={CONTENT_AI_MIN_WIDTH} aria-valuemax={paneMaxWidth('ai')} aria-valuenow={aiWidth} tabIndex={0}
        onPointerDown={(event) => { if (window.innerWidth <= CONTENT_AI_DRAWER_BREAKPOINT) return; resizePointerId.current = event.pointerId; setResizingPane('ai'); }}
        onDoubleClick={() => resetPaneWidth('ai')}
        onKeyDown={(event) => { if (event.key === 'ArrowLeft') { event.preventDefault(); adjustPaneWidth('ai', 10); } else if (event.key === 'ArrowRight') { event.preventDefault(); adjustPaneWidth('ai', -10); } else if (event.key === 'Home') { event.preventDefault(); resetPaneWidth('ai'); } }} />
      <aside className="r2-content-list">
        <div className="r2-list-caption r2-content-list-caption"><span>全部内容 <b>{records.length}</b></span><button type="button" className="r2-list-new" disabled={busy || !!stream} onClick={createContent}>新建内容</button><button type="button" className="r2-list-collapse" aria-label={listCollapsed ? '展开全部内容' : '收起全部内容'} title={listCollapsed ? '展开全部内容' : '收起全部内容'} onClick={() => { setResizingPane(null); setListCollapsed((value) => !value); }}><IconChevron size={14} /></button></div>
        {records.map((record) => <button key={record.id} className={`r2-list-item ${selected?.id === record.id ? 'active' : ''}`} disabled={busy || !!stream} onClick={() => pick(record)}><strong>{record.content.title}</strong><span>主稿 V{record.version} · {record.variants?.length || 0} 个平台版本</span><small>{dateText(record.updated_at)}</small></button>)}
        {records.length === 0 && <Empty title="还没有内容" description="直接在右侧告诉 AI 想创作什么，或者先手动写一份主稿。" />}
      </aside>

      <main className="r2-content-editor">
        <div className="r2-field r2-title-field"><div className="r2-title-field-head"><span className="r2-title-label">标题</span><div className="r2-title-actions">{(stream || dirty || selected) && <span className="r2-editor-state">{stream ? 'AI 正在处理' : dirty ? '未保存' : '已保存'}</span>}<button type="button" className="r2-button r2-title-action r2-ai-pane-toggle" onClick={() => setAiOpen(true)}>AI 协作</button><button type="button" className="r2-button primary r2-title-action" disabled={busy || !!stream || !draft.title.trim() || (!dirty && !!selected)} onClick={() => void save()}>保存</button></div></div><input className="r2-title-input" aria-label="内容标题" value={draft.title} maxLength={200} disabled={editorLocked} onChange={(e) => patch('title', e.target.value)} /></div>
        <label className="r2-field">正文<textarea className="r2-mother-body" aria-label="内容正文" value={draft.body} maxLength={100000} placeholder="这里是内容主稿。也可以直接在右侧告诉 AI 主题和要求，让它完成初稿。" disabled={editorLocked} onChange={(e) => patch('body', e.target.value)} /></label>
        <label className="r2-field">话题标签<input aria-label="内容话题标签" value={draft.tags} maxLength={1000} placeholder="用逗号分隔；也可以让 AI 自动整理" onChange={(e) => patch('tags', e.target.value)} disabled={editorLocked} /></label>

        <div className="r2-section-heading"><h2>素材 <span>{draft.media.length}</span></h2><div className="r2-toolbar"><label className="r2-button r2-file-button">添加图片 / 视频<input type="file" aria-label="上传内容素材" accept=".png,.jpg,.jpeg,.webp,.gif,.mp4,.mov,.webm" multiple disabled={editorLocked || draft.media.length >= 12} onChange={(e) => { const files = Array.from(e.target.files || []); e.target.value = ''; void run(async () => { if (files.length + draft.media.length > 12) throw new Error('最多添加 12 个素材。'); const paths: string[] = []; for (const file of files) { const uploaded = await uploadMedia(file, (n) => setUploadProgress(`${file.name} · ${n}%`)); paths.push(uploaded.path); } patch('media', [...draft.media, ...paths]); setUploadProgress(''); }); }} /></label></div></div>
        {uploadProgress && <p className="r2-muted">{uploadProgress}</p>}
        <div className="r2-media-chips">{draft.media.map((path) => <span key={path}>{path.split('/').at(-1)}<button aria-label={`移除 ${path}`} disabled={editorLocked} onClick={() => patch('media', draft.media.filter((item) => item !== path))}>×</button></span>)}</div>

        <div className="r2-section-heading r2-variants-heading"><h2>平台版本</h2><div className="r2-toolbar"><button className="r2-button" disabled={busy || !!stream || !draft.title.trim()} onClick={() => void openVariantBatch()}>创建平台版本</button></div></div>
        {selected && (records.find((record) => record.id === selected.id)?.variants || []).map((variant) => <button className={`r2-variant-row ${selectedVariantId === variant.id ? 'active' : ''}`} key={variant.id} onClick={() => setSelectedVariantId(variant.id)}><Mark platform={variant.platform} /><span>{variant.platform} · 平台版本 V{variant.version}</span><small>{variant.delivery === 'export' ? 'Markdown 导出' : variant.target_id ? '已选择发布目标' : '待选择发布目标'}</small>{variant.stale && <small className="r2-warning">基于旧母稿</small>}</button>)}
        {selected && !(records.find((record) => record.id === selected.id)?.variants || []).length && <p className="r2-muted">先选择平台建立独立版本。账号、Blog 连接、排期和发布方式都在平台版本中再选择。</p>}
        {selected && selectedVariantId && <ContentVariantPublisher key={selectedVariantId} variantId={selectedVariantId} source={selected} onNavigate={onNavigate} onUpdated={() => void refresh().catch((e) => setError(errorText(e)))} />}
        <p className="r2-muted">母稿负责通用内容；平台版本负责平台改写和目标配置；发布任务只保存一次审核与执行快照。更新母稿不会自动覆盖已有平台版本。</p>
      </main>

      <div className={`r2-content-ai-wrap ${aiOpen ? 'mobile-open' : ''}`}>
        <button className="r2-ai-mobile-close" aria-label="关闭 AI 协作" onClick={() => setAiOpen(false)}>×</button>
        <ContentAssistant contentId={selected?.id} contentTitle={selected?.content.title || draft.title} sessions={sessions} session={sessionForContent} stream={stream} onSend={sendToAi} onStop={onAiStop} onSelectSession={selectAiSession} onNewSession={newAiSession} onResend={onAiResend} />
      </div>
    </div>

    {batchSource && <VariantBatch source={batchSource} onClose={() => setBatchSource(null)} onDone={(count) => { setBatchSource(null); setNotice(`已创建 ${count} 个平台版本。接下来可分别选择账号或 Blog 目标，再创建发布任务。`); void refresh().catch((e) => setError(errorText(e))); }} />}
  </div>;
}
