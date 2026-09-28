import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, dateText, errorText, executeStructuredOperation, taskAction, uploadMedia } from '../../lib/ripple';
import { newId } from '../../lib/id';
import type { Account, BlogConnector, Channel, ContentPlan, Mother, PlatformVariant, PreflightResult, PublishChecklistOutput, Task, VariantContent } from '../../lib/ripple';
import { Feedback, Mark, Modal, Status } from './Common';
import PublishPreview from './PublishPreview';
import VariantMediaPreview from './VariantMediaPreview';
import { bodyLength, canDispatchVariantTask, needsResultCheck, publishResultText, reorderMedia, selectVariantTask, variantDraftChanged, xhsContentProblems, xhsTitleLength, XHS_BODY_LIMIT, XHS_TITLE_LIMIT } from './variantPublishing';
import './ContentVariantPublisher.css';
import { MOTHER_SYNC_FIELDS, syncMotherFields } from './variantPublishing';
import type { MotherSyncField } from './variantPublishing';
import { platformContentProblems, taskStatusText } from './variantPublishing';
import { countXReply } from '../../lib/xText';
import { channelCapabilityText, channelPublishAvailable } from '../../lib/channelCapability';
import SharedMediaPicker from './SharedMediaPicker';

type Props = { variantId: string; source: Mother; onNavigate: (page: 'publish' | 'accounts', taskId?: string) => void; onUpdated: () => void; onDirtyChange?: (dirty: boolean) => void };

const replaceableTasks = new Set(['cancelled', 'failed_terminal']);
const cloneContent = (content: VariantContent): VariantContent => ({ ...content, media: [...content.media], options: { ...(content.options || {}) } });

export default function ContentVariantPublisher({ variantId, source, onNavigate, onUpdated, onDirtyChange }: Props) {
  const [variant, setVariant] = useState<PlatformVariant | null>(null);
  const [form, setForm] = useState<VariantContent | null>(null);
  const [sourceVersionId, setSourceVersionId] = useState('');
  const [task, setTask] = useState<Task | null>(null);
  const [plan, setPlan] = useState<ContentPlan | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [blogs, setBlogs] = useState<BlogConnector[]>([]);
  const dirty = !!variant && !!form && variantDraftChanged(form, variant.content, sourceVersionId, variant.source_version_id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [review, setReview] = useState<PreflightResult | null>(null);
  const [reviewProblems, setReviewProblems] = useState<string[]>([]);
  const [receipt, setReceipt] = useState(false);
  const [publicUrl, setPublicUrl] = useState('');
  const [checklist, setChecklist] = useState<PublishChecklistOutput | null>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const taskKeyRef = useRef<{ version: string; key: string } | null>(null);
  const actionGate = useRef(false);
  const [lastChecked, setLastChecked] = useState('');
  const [autoCheckId, setAutoCheckId] = useState('');
  const [syncOpen, setSyncOpen] = useState(false);
  const [mediaPickerOpen, setMediaPickerOpen] = useState(false);
  const feedbackRef = useRef<HTMLDivElement>(null);
  const [syncFields, setSyncFields] = useState<MotherSyncField[]>([]);
  const [syncUndo, setSyncUndo] = useState<{ content: VariantContent; sourceVersion: string; fields: MotherSyncField[] } | null>(null);

  const load = useCallback(async () => {
    const [next, accountRows, channelRows, blogRows, taskRows, planRows] = await Promise.all([
      api<PlatformVariant>(`/api/ripple/variants/${variantId}`),
      api<Account[]>('/api/ripple/accounts'),
      api<Channel[]>('/api/ripple/channels'),
      api<{ items: BlogConnector[] }>('/api/ripple/blog/connectors'),
      api<{ items: Task[] }>(`/api/ripple/variants/${variantId}/tasks`),
      api<ContentPlan[]>('/api/ripple/plans'),
    ]);
    setVariant(next); setForm(cloneContent(next.content)); setSourceVersionId(next.source_version_id);
    setSyncUndo(null); setSyncOpen(false);
    setAccounts(accountRows); setChannels(channelRows); setBlogs(blogRows.items);
    setTask(selectVariantTask(taskRows.items, next.version_id));
    setPlan(planRows.filter(item => item.variant_id === variantId && item.status === 'planned').at(-1) || null);
  }, [variantId]);

  useEffect(() => { void load().catch(e => setError(errorText(e))); }, [load]);
  useEffect(() => { if (error || reviewProblems.length) feedbackRef.current?.scrollIntoView({ block: 'center' }); }, [error, reviewProblems]);

  useEffect(() => {
    if (!autoCheckId) return;
    let stopped = false, attempts = 0;
    let timer: ReturnType<typeof setTimeout>;
    const check = async () => {
      if (actionGate.current) { timer = setTimeout(check, 1500); return; }
      actionGate.current = true;
      try {
        const current = await api<Task>(`/api/ripple/tasks/${autoCheckId}`);
        const result = needsResultCheck(current) ? await taskAction(current, 'query') : current;
        if (stopped) return;
        setTask(result); setLastChecked(new Date().toISOString()); setNotice(publishResultText(result));
        if (needsResultCheck(result) && ++attempts < 3) timer = setTimeout(check, 3000);
        else setAutoCheckId('');
      } catch (cause) { if (!stopped) { setAutoCheckId(''); setError(`自动检查未完成：${errorText(cause)}。可点击“检查结果”重试。`); } }
      finally { actionGate.current = false; }
    };
    timer = setTimeout(check, 2000);
    return () => { stopped = true; clearTimeout(timer); };
  }, [autoCheckId]);

  useEffect(() => { onDirtyChange?.(dirty || uploading); }, [dirty, uploading, onDirtyChange]);
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange]);
  useEffect(() => {
    // 父级接管时只上报编辑状态，避免离开页面时重复询问。
    if (onDirtyChange) return;
    const beforeUnload = (event: BeforeUnloadEvent) => { if (dirty || uploading) { event.preventDefault(); event.returnValue = ''; } };
    const beforeNavigate = (event: Event) => { if ((dirty || uploading) && !window.confirm('平台草稿尚未保存，放弃修改并离开？')) event.preventDefault(); };
    window.addEventListener('beforeunload', beforeUnload);
    window.addEventListener('ripple:before-navigate', beforeNavigate);
    return () => { window.removeEventListener('beforeunload', beforeUnload); window.removeEventListener('ripple:before-navigate', beforeNavigate); };
  }, [dirty, uploading, onDirtyChange]);

  const channel = useMemo(() => channels.find(item => item.id === variant?.platform), [channels, variant?.platform]);
  const platformAccounts = useMemo(() => accounts.filter(item => item.platform === variant?.platform), [accounts, variant?.platform]);
  const isBlog = variant?.platform === 'blog';
  const isWechat = variant?.platform === 'wechat';
  const isXhs = variant?.platform === 'xiaohongshu';
  const isX = variant?.platform === 'x';
  const isBilibili = variant?.platform === 'bilibili';
  const isExport = form?.delivery === 'export';
  const unavailable = !!channel && !channelPublishAvailable(channel) && !isExport;
  const wechatAction = form?.options?.wechat_action === 'publish' ? 'publish' : 'draft';
  const contentType = form?.options?.content_type === 'thought' ? 'thought' : 'article';
  const selectedAccount = platformAccounts.find(item => item.id === form?.target_id);
  const wechatCanPublish = !!selectedAccount?.capabilities?.includes('freepublish');
  const selectedBlog = blogs.find(item => item.id === form?.target_id);
  const stale = !!variant && sourceVersionId !== source.version_id;
  const blogCapabilities = [`${contentType}.create`, `${contentType}.read`, `${contentType}.update`, `${contentType}.publish`];
  const blogTypeReady = !isBlog || form?.delivery === 'export' || (!!selectedBlog && blogCapabilities.every(capability => selectedBlog.capabilities.includes(capability)));
  const blogMediaReady = !isBlog || form?.delivery === 'export' || (!!selectedBlog && (!form?.media.length || (contentType === 'article' && selectedBlog.capabilities.includes('media.upload'))));
  const targetReady = !!form && (form.delivery === 'export' || (isBlog ? !!selectedBlog : !!form.target_id));

  const validateForPublish = () => {
    if (!form) throw new Error('平台版本尚未加载。');
    const problems: string[] = [];
    if (unavailable) problems.push(`${channel?.name}暂时只能创作，发布尚未接入。`);
    problems.push(...platformContentProblems(variant?.platform || '', form));
    if (!form.title.trim()) problems.push('请填写平台版本标题。');
    if (!targetReady) problems.push('请先选择发布目标账号或 Blog。');
    if (selectedAccount && selectedAccount.status !== 'connected') problems.push('目标账号尚未连接，请先前往账号与平台完成连接。');
    if (!blogTypeReady) problems.push('目标 Blog 不支持当前内容类型。');
    if (!blogMediaReady) problems.push('目标 Blog 不支持当前素材，请移除素材或调整内容类型。');
    if (isXhs && selectedAccount?.adapter !== 'aitoearn-rest') problems.push(...xhsContentProblems(form));
    if (isWechat && !form.media.length) problems.push('微信公众号图文需要封面图片，请先添加素材。');
    if (problems.length) { setReviewProblems(problems); throw new Error('请先处理以下问题，再准备发布。'); }
  };

  const patch = (next: Partial<VariantContent>) => { setForm(current => current ? { ...current, ...next } : current); setNotice(''); setChecklist(null); setReviewProblems([]); };
  const patchOption = (key: string, value: unknown) => { setForm(current => current ? { ...current, options: { ...(current.options || {}), [key]: value } } : current); setNotice(''); setChecklist(null); setReviewProblems([]); };

  const saveVariant = async (): Promise<PlatformVariant> => {
    if (!variant || !form) throw new Error('平台版本尚未加载。');
    if (!dirty) return variant;
    const updated = await api<PlatformVariant>(`/api/ripple/variants/${variant.id}`, 'PUT', {
      ...form, expected_version: variant.version_id, source_version_id: sourceVersionId,
    });
    setVariant(updated); setForm(cloneContent(updated.content)); setSourceVersionId(updated.source_version_id);
    setSyncUndo(null);
    setNotice(`平台版本已保存 · V${updated.version}。旧发布任务保留为历史快照。`); onUpdated();
    return updated;
  };

  const save = async () => { setBusy(true); setError(''); try { await saveVariant(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const ensurePlan = async (current: PlatformVariant): Promise<ContentPlan> => {
    if (!current.content.scheduled_local) throw new Error('请先选择计划时间。');
    const input = { title: current.content.title, scheduled_local: current.content.scheduled_local,
      timezone: current.content.timezone, fold: current.content.fold,
      source_id: source.id, variant_id: current.id };
    if (plan && plan.variant_version_id === current.version_id && plan.scheduled_local === input.scheduled_local && plan.title === input.title) return plan;
    const next = plan
      ? await api<ContentPlan>(`/api/ripple/plans/${plan.id}`, 'PUT', { ...input, expected_version: plan.version })
      : await api<ContentPlan>('/api/ripple/plans', 'POST', { ...input, idempotency_key: newId() });
    setPlan(next);
    return next;
  };

  const savePlan = async () => {
    setBusy(true); setError('');
    try { const current = await saveVariant(); await ensurePlan(current); setNotice('创作计划已保存。保存计划不会创建发布任务。'); }
    catch (cause) { setError(errorText(cause)); } finally { setBusy(false); }
  };

  const syncMother = () => {
    if (!form) return;
    setSyncFields([]); setSyncOpen(true);
  };

  const applyMotherSync = () => {
    if (!form || !syncFields.length) return;
    setSyncUndo({ content: cloneContent(form), sourceVersion: sourceVersionId, fields: [...syncFields] });
    setForm(syncMotherFields(form, source.content, syncFields));
    setSourceVersionId(source.version_id); setSyncOpen(false);
    setNotice('已应用所选字段，可撤销本次同步；保存平台版本后生效。'); setChecklist(null); setReviewProblems([]);
  };

  const undoMotherSync = () => {
    if (!syncUndo) return;
    setForm(current => current ? syncMotherFields(current, syncUndo.content, syncUndo.fields) : current);
    setSourceVersionId(syncUndo.sourceVersion); setSyncUndo(null);
    setNotice('已撤销本次同步，其他字段的编辑仍保留。'); setChecklist(null); setReviewProblems([]);
  };

  const ensureTask = async (): Promise<Task> => {
    validateForPublish();
    let current = variant;
    if (!current) throw new Error('平台版本尚未加载。');
    if (dirty) current = await saveVariant();
    const rows = await api<{ items: Task[] }>(`/api/ripple/variants/${current.id}/tasks`);
    const unresolved = rows.items.find(needsResultCheck);
    if (unresolved) { setTask(unresolved); throw new Error('这份平台版本已有待核对的发布结果，请先核对原任务，避免重复发布。'); }
    const scheduled = rows.items.find(item => item.status === 'scheduled');
    if (scheduled) { setTask(scheduled); throw new Error('这份平台版本已有定时发布，请先取消原任务的定时，再准备新的发布任务。'); }
    const existing = rows.items.find(item => item.variant_version_id === current.version_id && !replaceableTasks.has(item.status));
    if (existing) { setTask(existing); return existing; }
    const currentPlan = current.content.scheduled_local ? await ensurePlan(current) : null;
    if (taskKeyRef.current?.version !== current.version_id) taskKeyRef.current = { version: current.version_id, key: newId() };
    const created = await api<Task>(`/api/ripple/variants/${current.id}/tasks`, 'POST', {
      expected_version: current.version_id, idempotency_key: taskKeyRef.current.key, plan_id: currentPlan?.id || '',
    });
    taskKeyRef.current = null;
    setTask(created); setNotice(`已创建发布任务快照 · ${created.id.slice(0, 8)}。平台版本仍可继续编辑。`); return created;
  };

  const createTask = async () => { setBusy(true); setError(''); try { await ensureTask(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const runChecklist = async () => { setBusy(true); setError(''); try { const current = await ensureTask(); const result = await executeStructuredOperation<PublishChecklistOutput>('publish_checklist', {}, { kind: 'publish_task', ref: current.id, version: current.version_id }); setChecklist(result.output); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const preflight = async () => { setBusy(true); setError(''); setReviewProblems([]); try {
    const current = await ensureTask();
    if (!['draft', 'review_ready'].includes(current.status)) { setNotice('这份草稿已有发布任务，请查看下方任务状态并继续处理。'); return; }
    const result = await api<PreflightResult>(`/api/ripple/tasks/${current.id}/preflight`, 'POST', { expected_version: current.version_id });
    setTask(result.task);
    if (!result.ok) { setReviewProblems(result.problems); throw new Error('预检未通过，请查看下方问题并修改对应字段。'); }
    setReview(result);
  } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const approve = async (realConfirmed: boolean, externalConfirmed: boolean) => {
    if (!review || actionGate.current || !variant) return;
    if (dirty || uploading || review.task.variant_version_id !== variant.version_id) { setError('草稿已变化，请重新准备发布。'); setReview(null); return; }
    actionGate.current = true; setBusy(true); setError('');
    try {
      const current = review.task;
      const result = await taskAction(current, 'approve', {
        confirmed: true, real_publish_confirmed: realConfirmed,
        expected_account_revision: current.review_context?.auth_revision,
        external_service_confirmed: externalConfirmed,
      });
      setTask(result); setReview(null);
      const submitted = result.status === 'approved' ? await taskAction(result, 'dispatch') : result;
      setTask(submitted); setNotice(publishResultText(submitted));
      if (needsResultCheck(submitted)) setAutoCheckId(submitted.id);
    } catch (e) { setError(errorText(e)); } finally { actionGate.current = false; setBusy(false); }
  };

  const dispatch = async () => {
    if (!task || !variant || busy || actionGate.current) return;
    if (!canDispatchVariantTask(task, variant.version_id, dirty, uploading)) { setError('草稿与审核任务不一致，或任务当前不能执行。请重新准备发布并审核。'); return; }
    actionGate.current = true; setBusy(true); setError('');
    try { const result = await taskAction(task, 'dispatch'); setTask(result); setNotice(publishResultText(result)); if (needsResultCheck(result)) setAutoCheckId(result.id); }
    catch (e) { setError(errorText(e)); } finally { actionGate.current = false; setBusy(false); }
  };
  const query = async () => { if (!task || actionGate.current) return; actionGate.current = true; setBusy(true); setError(''); try { const current = await api<Task>(`/api/ripple/tasks/${task.id}`); const result = needsResultCheck(current) ? await taskAction(current, 'query') : current; setTask(result); setLastChecked(new Date().toISOString()); setNotice(publishResultText(result)); } catch (e) { setError(errorText(e)); } finally { actionGate.current = false; setBusy(false); } };
  const unschedule = async () => { if (!task) return; setBusy(true); setError(''); try {
    const result = await api<{ task: Task; plan: ContentPlan }>(`/api/ripple/tasks/${task.id}/return-to-plan`, 'POST', { expected_version: task.version_id });
    setTask(result.task); setPlan(result.plan); setNotice('定时已取消，创作计划和原任务记录均已保留。');
  } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const confirmReceipt = async () => { if (!task) return; setBusy(true); setError(''); try { const result = await api<Task>(`/api/ripple/tasks/${task.id}/receipt`, 'POST', { expected_version: task.version_id, confirmed: true, public_url: publicUrl, note: '' }); setTask(result); setReceipt(false); setNotice('作品链接已记录。'); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const addMedia = async (files: FileList | null) => {
    if (!files || !form) return; setUploading(true); setProgress(0); setError('');
    const paths = [...form.media];
    try {
      if (paths.length + files.length > 12) throw new Error('每个版本最多 12 个素材，请减少本次选择的文件。');
      for (const file of Array.from(files)) { const result = await uploadMedia(file, setProgress); paths.push(result.path); patch({ media: [...paths] }); }
    } catch (e) { setError(errorText(e)); } finally { setUploading(false); setProgress(0); if (fileRef.current) fileRef.current.value = ''; }
  };

  if (!variant || !form) return <div className="r2-loading">正在读取平台版本…</div>;
  const title = channel?.name || variant.platform;
  const realTask = task?.content.mode === 'real';
  const canQuery = !!task && ['accepted', 'unknown_result', 'verification_required', 'dispatching'].includes(task.status);
  const canDispatch = canDispatchVariantTask(task, variant.version_id, dirty, uploading);
  const hasDispatchAction = !!task && ['approved', 'failed_retryable'].includes(task.status);
  const taskNeedsRefresh = !!task && (dirty || task.variant_version_id !== variant.version_id);
  const scheduled = task?.status === 'scheduled';
  const unresolved = !!task && needsResultCheck(task);
  const titleCount = isXhs ? xhsTitleLength(form.title) : bodyLength(form.title);
  const titleLimit = isXhs ? XHS_TITLE_LIMIT : isWechat ? 32 : undefined;
  const bodyCount = isX ? countXReply(form.body).weightedLength : bodyLength(form.body);
  const bodyLimit = isX ? 280 : isXhs ? XHS_BODY_LIMIT : undefined;
  const problemField = (problem: string) => /媒体|素材|图片|视频|封面/.test(problem) ? 'media' : /标题/.test(problem) ? 'title' : /正文|内容|文本/.test(problem) ? 'body' : /账号|目标|Blog|连接/.test(problem) ? 'account' : /时间|排期|时区/.test(problem) ? 'time' : '';
  const focusProblem = (problem: string) => { const field = problemField(problem); if (field) { const target = document.getElementById(`variant-${variantId}-${field}`); target?.scrollIntoView({ block: 'center' }); target?.focus(); } };

  return <section className="r2-variant-publisher">
    <div ref={feedbackRef}><Feedback error={error} notice={notice} /></div>
    {!!reviewProblems.length && <div className="focus-preflight-problems"><strong>需要处理</strong>{reviewProblems.map((problem) => <button key={problem} onClick={() => focusProblem(problem)}>{problemField(problem) ? `${problem} →` : problem}</button>)}</div>}
    <div className="r2-section-heading"><div><h2><Mark platform={variant.platform} /> {title}稿</h2><p>{isExport ? '检查并导出会自动保存，并展示导出内容。' : unavailable ? '当前平台支持保存和编辑稿件。' : '准备发布会自动保存，并展示本次发布的完整内容。'}</p></div><span className="variant-editor-state" role="status">{uploading ? '正在上传素材' : busy ? '正在处理' : dirty ? '草稿未保存' : '草稿已保存'}</span></div>
    {unavailable && <p className="r2-inline-warning" role="status">{channelCapabilityText(channel)}。可以保存稿件，接入前无法执行发布。</p>}
    {stale && <div className="r2-alert"><strong>通用草稿已有更新</strong><span>平台改写仍保留，可直接用于发布。也可以查看差异后选择要同步的字段。</span><button className="r2-button" disabled={busy || uploading} onClick={syncMother}>查看同步差异</button><button className="r2-button" disabled={busy || uploading} onClick={preflight}>保留平台稿继续发布</button></div>}
    {syncUndo && <button className="r2-button" disabled={busy || uploading} onClick={undoMotherSync}>撤销本次同步</button>}
    {syncOpen && <Modal title="选择同步字段" busy={busy} onClose={() => setSyncOpen(false)}>
      <p>默认保留平台稿，只覆盖勾选字段。</p>
      {MOTHER_SYNC_FIELDS.map(({ key, label }) => <section key={key}>
        <label className="r2-check"><input type="checkbox" checked={syncFields.includes(key)} onChange={e => setSyncFields(current => e.target.checked ? [...current, key] : current.filter(field => field !== key))} />同步{label}</label>
        <div className="variant-sync-diff"><div><strong>当前平台稿</strong><pre>{key === 'media' ? form.media.join('\n') || '无素材' : form[key] || '空'}</pre></div><div><strong>最新通用草稿</strong><pre>{key === 'media' ? source.content.media.join('\n') || '无素材' : source.content[key] || '空'}</pre></div></div>
      </section>)}
      <footer><button className="r2-button" onClick={() => setSyncOpen(false)}>保留平台稿</button><button className="r2-button primary" disabled={!syncFields.length} onClick={applyMotherSync}>应用所选字段</button></footer>
    </Modal>}
    {unresolved && <div className="r2-inline-warning variant-result-warning"><span>{publishResultText(task)}请先核对本次结果，避免重复发布。</span></div>}
    {scheduled && <div className="r2-inline-warning variant-result-warning"><span>已有定时任务将于 {dateText(task.content.scheduled_at || '')} 执行。修改草稿不会取消定时；请先取消原任务定时，再准备新的发布任务。</span></div>}

    <fieldset className="variant-form-fields" disabled={busy || uploading}>
    <div className="r2-form-grid">
      <label className="r2-field"><span className="variant-field-heading">{isX ? '本地稿件名称（不发送到 X）' : '平台版本标题'}{titleLimit && <small className={titleCount > titleLimit ? 'over-limit' : ''}>{titleCount} / {titleLimit} 字{titleCount > titleLimit ? ` · 超出 ${titleCount - titleLimit} 字` : ''}</small>}</span><input id={`variant-${variantId}-title`} value={form.title} maxLength={200} aria-invalid={!!titleLimit && titleCount > titleLimit} onChange={e => patch({ title: e.target.value })} />{isXhs && <small className="r2-muted">按发布检查计数：两个英文字符或数字折算一个字，Emoji 按实际占用计算。</small>}</label>
      <label className="r2-field"><span className="variant-field-heading">平台版本正文{bodyLimit && <small className={bodyCount > bodyLimit ? 'over-limit' : ''}>{bodyCount} / {bodyLimit} {isX ? '加权字符' : '字'}{bodyCount > bodyLimit ? ` · 超出 ${bodyCount - bodyLimit}` : ''}</small>}</span><textarea id={`variant-${variantId}-body`} rows={10} value={form.body} maxLength={100000} aria-invalid={!!bodyLimit && bodyCount > bodyLimit} onChange={e => patch({ body: e.target.value })} />{isX && <small>中文通常计为 2，链接按 23 加权字符计算。话题标签请直接写入正文。</small>}</label>
      {!isX && <label className="r2-field"><span>话题标签</span><input value={form.tags} maxLength={1000} onChange={e => patch({ tags: e.target.value })} placeholder="逗号分隔" /></label>}
      {isX && form.tags && <div role="alert">已有独立话题：{form.tags}<button className="r2-button" onClick={() => patch({ body: `${form.body}\n\n${form.tags.split(/[,，\s]+/).filter(Boolean).map(tag => tag.startsWith('#') ? tag : `#${tag}`).join(' ')}`, tags: '' })}>移入正文</button><button className="r2-button" onClick={() => patch({ tags: '' })}>清除话题</button></div>}
      {isBilibili && <div className="r2-form-grid compact">
        <label className="r2-field"><span>投稿分区 ID</span><input type="number" min={1} max={99999} value={Number(form.options.bilibili_tid) || ''} onChange={e => patchOption('bilibili_tid', Number(e.target.value))} placeholder="填写目标投稿分区 ID" /><small>请使用 B 站当前投稿页对应的分区 ID。</small></label>
        <label className="r2-field"><span>版权类型</span><select value={String(form.options.bilibili_copyright || '')} onChange={e => patchOption('bilibili_copyright', Number(e.target.value))}><option value="">请选择</option><option value="1">原创</option><option value="2">转载</option></select></label>
        {form.options.bilibili_copyright === 2 && <label className="r2-field"><span>转载来源</span><input maxLength={2000} value={String(form.options.bilibili_source || '')} onChange={e => patchOption('bilibili_source', e.target.value)} /></label>}
      </div>}
    </div>

    <div className="r2-version-target">
      <h3>发布目标</h3>
      {isBlog ? <>
        <label className="r2-field"><span>发布方式</span><select value={form.delivery} onChange={e => patch({ delivery: e.target.value as 'remote' | 'export', target_id: e.target.value === 'export' ? '' : form.target_id, scheduled_local: e.target.value === 'export' ? null : form.scheduled_local })}><option value="remote">连接 Blog 发布</option><option value="export">仅导出 Markdown / 素材</option></select></label>
        {form.delivery === 'remote' && <label className="r2-field"><span>Blog</span><select id={`variant-${variantId}-account`} value={form.target_id} onChange={e => patch({ target_id: e.target.value })}><option value="">选择已连接的 Blog</option>{blogs.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>}
        {form.delivery === 'remote' && !blogs.length && <p className="r2-muted">还没有 Blog 连接。<button className="r2-link-button" onClick={() => onNavigate('accounts')}>前往账号与平台连接</button></p>}
        {form.delivery === 'remote' && selectedBlog && <p className="r2-muted">能力：{selectedBlog.capabilities.join(' · ')}</p>}
        {form.delivery === 'remote' && selectedBlog && contentType === 'thought' && !selectedBlog.content_types.includes('thought') && <p className="r2-warning">这个 Blog 没有声明想法 / 短记发布能力。</p>}
        {form.delivery === 'remote' && selectedBlog && form.media.length > 0 && !selectedBlog.capabilities.includes('media.upload') && <p className="r2-warning">这个 Blog 没有声明媒体上传能力，请移除素材或改用 Markdown 导出。</p>}
        {form.delivery === 'remote' && contentType === 'thought' && form.media.length > 0 && <p className="r2-warning">想法 / 短记当前只支持文字，请移除素材或改用文章。</p>}
        {form.delivery === 'remote' && <label className="r2-field"><span>内容类型</span><select value={contentType} onChange={e => patchOption('content_type', e.target.value)}><option value="article">文章</option><option value="thought">想法 / 短记</option></select></label>}
        {form.delivery === 'remote' && contentType === 'article' && <div className="r2-form-grid compact">
          <label className="r2-field"><span>Slug（可选）</span><input value={String(form.options.slug || '')} onChange={e => patchOption('slug', e.target.value)} placeholder="留空则自动生成" /></label>
          <label className="r2-field"><span>摘要（可选）</span><input value={String(form.options.excerpt || '')} onChange={e => patchOption('excerpt', e.target.value)} /></label>
          <label className="r2-field"><span>发布日期（可选）</span><input type="date" value={String(form.options.date || '')} onChange={e => patchOption('date', e.target.value)} /></label>
        </div>}
        {form.delivery === 'remote' && contentType === 'thought' && <label className="r2-field"><span>想法标签（可选）</span><input value={String(form.options.thought_tag || '')} onChange={e => patchOption('thought_tag', e.target.value)} /></label>}
      </> : <>
        <label className="r2-field"><span>平台版本目标账号</span><select id={`variant-${variantId}-account`} value={form.target_id} onChange={e => patch({ target_id: e.target.value, delivery: 'remote' })}><option value="">选择发布账号</option>{platformAccounts.map(item => <option key={item.id} value={item.id}>{item.label} · {item.status === 'connected' ? '已连接' : '未连接'}</option>)}</select></label>
        {!platformAccounts.length && <p className="r2-muted">该平台还没有账号。<button className="r2-link-button" onClick={() => onNavigate('accounts')}>前往账号与平台</button></p>}
        {selectedAccount && selectedAccount.status !== 'connected' && <p className="r2-warning">该账号尚未连接，发布预检会被阻止。</p>}
        {isWechat && <div className="r2-form-grid compact">
          <label className="r2-field"><span>执行到</span><select value={wechatAction} onChange={e => patchOption('wechat_action', e.target.value)}><option value="draft">保存到公众号草稿箱</option><option value="publish" disabled={!!selectedAccount && !wechatCanPublish}>创建草稿并提交发布</option></select></label>
          <label className="r2-field"><span>作者（可选）</span><input value={String(form.options.wechat_author || '')} maxLength={16} onChange={e => patchOption('wechat_author', e.target.value)} /></label>
          <label className="r2-field"><span>摘要（可选）</span><input value={String(form.options.wechat_digest || '')} maxLength={128} onChange={e => patchOption('wechat_digest', e.target.value)} /></label>
          <label className="r2-field"><span>阅读原文 URL（可选）</span><input value={String(form.options.wechat_source_url || '')} maxLength={1024} placeholder="https://…" onChange={e => patchOption('wechat_source_url', e.target.value)} /></label>
          <label className="r2-field"><span>封面图片</span><select value={String(form.options.wechat_cover || form.media[0] || '')} onChange={e => patchOption('wechat_cover', e.target.value)}><option value="">请先添加封面图片</option>{form.media.map(path => <option value={path} key={path}>{path}</option>)}</select></label>
        </div>}
        {isWechat && !form.media.length && <p className="r2-warning">微信公众号图文至少需要一张本地图片作为封面；没有封面时预检会阻止。</p>}
        {isWechat && selectedAccount && !selectedAccount.capabilities?.includes('draft') && <p className="r2-warning">该账号凭据有效，但没有草稿箱 API 权限。请检查公众号类型、认证状态和接口权限。</p>}
        {isWechat && wechatAction === 'publish' && selectedAccount && !wechatCanPublish && <p className="r2-warning">该账号没有 freepublish 权限。请改为“保存到公众号草稿箱”。</p>}
        {isWechat && <p className="r2-muted">封面会转换后上传为微信永久缩略图素材；正文图片会上传到微信图床并自动回写正文 URL。服务器出口 IP 需在公众号开发配置的 IP 白名单中。</p>}
      </>}
      {!isExport && <label className="r2-field"><span>平台版本计划时间（可选）</span><input id={`variant-${variantId}-time`} type="datetime-local" value={form.scheduled_local || ''} onChange={e => patch({ scheduled_local: e.target.value || null })} /></label>}
      {!isExport && form.scheduled_local && <small className="r2-muted">时区：{form.timezone}</small>}
    </div>

    <div className="r2-media-editor"><div className="r2-section-heading"><h3>素材 {form.media.length}</h3><button id={`variant-${variantId}-media`} className="r2-button" disabled={uploading || form.media.length >= 12} onClick={() => fileRef.current?.click()}>{uploading ? `上传 ${progress}%` : isWechat ? '添加图片' : '添加图片 / 视频'}</button></div>
      <button className="r2-button" disabled={uploading || form.media.length >= 12} onClick={() => setMediaPickerOpen(true)}>使用已有素材</button>
      {mediaPickerOpen && <SharedMediaPicker source={source} current={form.media} onClose={() => setMediaPickerOpen(false)} onApply={media => { patch({ media }); setMediaPickerOpen(false); }} />}
      <input ref={fileRef} hidden type="file" multiple accept={isWechat ? 'image/png,image/jpeg,image/webp,image/gif' : 'image/png,image/jpeg,image/webp,image/gif,video/mp4,video/quicktime,video/webm'} onChange={e => void addMedia(e.target.files)} />
      {isXhs && <p className="r2-muted">最多 9 张图片，或 1 个视频。使用下方按钮调整发布顺序。</p>}
      {!form.media.length && <p className="r2-muted">尚未添加素材。<button className="r2-link-button" onClick={() => fileRef.current?.click()}>选择文件上传</button></p>}
      <div className="variant-media-grid">{form.media.map((path, index) => <figure className="variant-media-card" key={`${path}-${index}`}>
        <VariantMediaPreview path={path} index={index} />
        <figcaption>素材 {index + 1}{isWechat && (String(form.options.wechat_cover || form.media[0] || '') === path) && <span> · 封面</span>}</figcaption>
        <div className="variant-media-actions"><button className="r2-button" disabled={index === 0} aria-label={`前移素材 ${index + 1}`} onClick={() => patch({ media: reorderMedia(form.media, index, -1) })}>前移</button><button className="r2-button" disabled={index === form.media.length - 1} aria-label={`后移素材 ${index + 1}`} onClick={() => patch({ media: reorderMedia(form.media, index, 1) })}>后移</button><button className="r2-button" aria-label={`移除素材 ${index + 1}`} onClick={() => patch({ media: form.media.filter((_, i) => i !== index) })}>移除</button></div>
      </figure>)}</div>
    </div>
    </fieldset>

    <details className="variant-extra-actions"><summary>查看完整内容预览</summary><PublishPreview content={form} heading="当前草稿预览" /></details>

    <div className="r2-toolbar r2-variant-actions focus-variant-actions">
      <div className="focus-variant-action-context"><strong>{selectedAccount?.label || selectedBlog?.label || (form.delivery === 'export' ? '本地导出' : '未选择账号')}</strong><span>{form.scheduled_local ? `${form.scheduled_local.replace('T', ' ')} · ${form.timezone}` : '尚未安排时间'}{plan ? ' · 计划已保存' : ''}</span></div>
      <button className="r2-button" disabled={busy || uploading || !dirty || !form.title.trim()} onClick={() => void save()}>保存草稿</button>
      <button className="r2-button primary" disabled={busy || uploading || unresolved || scheduled || unavailable} onClick={() => void preflight()}>{busy ? '正在处理…' : isExport ? '检查并导出' : unavailable ? '发布尚未接入' : '准备发布'}</button>
      {hasDispatchAction && <button className="r2-button danger" disabled={busy || !canDispatch} title={taskNeedsRefresh ? '草稿已修改，请重新准备发布并审核' : undefined} onClick={() => void dispatch()}>{isWechat && wechatAction === 'draft' ? '写入公众号草稿箱' : '执行发布'}</button>}
      {hasDispatchAction && taskNeedsRefresh && <span className="r2-muted variant-action-note" role="status">{task.variant_version_id !== variant.version_id ? '当前显示的是历史任务。' : '当前审核任务不包含未保存的修改。'}请重新准备发布并审核后再执行。</span>}
      {canQuery && <button className="r2-button" disabled={busy} onClick={() => void query()}>检查结果</button>}
      {task?.status === 'scheduled' && <button className="r2-button" disabled={busy} onClick={() => void unschedule()}>取消定时，保留计划</button>}

      {task?.status === 'exported' && <a className="r2-button" href={`/api/ripple/tasks/${task.id}/export`}>下载 Blog 导出包</a>}
      {task && realTask && !(task.content.platform === 'wechat' && task.content.options?.wechat_action !== 'publish') && ['accepted', 'unknown_result', 'verification_required'].includes(task.status) && <button className="r2-button" onClick={() => setReceipt(true)}>我已找到作品链接</button>}
      <details className="variant-extra-actions"><summary>更多操作</summary><div className="r2-toolbar">
        {task && <button className="r2-button" onClick={() => onNavigate('publish', task.id)}>查看发布详情</button>}
        <button className="r2-button" disabled={busy || uploading || !form.title.trim() || !form.scheduled_local} onClick={() => void savePlan()}>仅保存计划</button>
        <button className="r2-button" disabled={busy || uploading || unresolved || scheduled} onClick={() => void runChecklist()}>发布检查</button>
        <button className="r2-button" disabled={busy || uploading || unresolved || scheduled} onClick={() => void createTask()}>仅创建发布任务</button>
      </div></details>
    </div>

    {task && <div className="r2-task-summary"><strong>发布进度</strong><Status status={task.status} label={taskStatusText(task)} /><span>{task.variant_version_id === variant.version_id ? '当前已保存版本' : '历史版本快照'} · {dateText(task.created_at)}</span>{lastChecked && <span>最近检查：{dateText(lastChecked)}{autoCheckId ? ' · 正在自动检查' : ''}</span>}{task.receipt?.public_url && <a href={task.receipt.public_url} target="_blank" rel="noreferrer">打开已记录作品</a>}</div>}
    {task?.content.platform === 'wechat' && task.receipt?.draft_media_id && <div className="r2-inline-warning"><strong>{task.receipt.draft_only ? '公众号草稿已创建' : '公众号发布任务已提交'}</strong><br />草稿 media_id 已绑定到当前发布任务。{task.receipt.publish_id ? ` publish_id：${task.receipt.publish_id}` : ' 当前没有执行公开发布。'}</div>}
    {checklist && <div className={`r2-checklist ${checklist.ready ? 'ready' : ''}`}><strong>{checklist.ready ? '发布检查通过' : '发布检查发现阻塞项'}</strong>{checklist.blocking_issues.map(item => <p key={item}>{item}</p>)}{checklist.advisories?.map(item => <p className="r2-muted" key={item.item}>{item.item}：{item.detail}</p>)}</div>}

    {review && <ReviewModal result={review} busy={busy} onClose={() => setReview(null)} onConfirm={(real, external) => void approve(real, external)} />}
    {receipt && <Modal title="人工记录作品链接" busy={busy} onClose={() => setReceipt(false)}><p>仅在你已经在目标平台核对到具体作品时填写。此操作不会再次执行发布。</p><label className="r2-field"><span>作品 URL</span><input value={publicUrl} onChange={e => setPublicUrl(e.target.value)} placeholder="https://…" /></label><footer><button className="r2-button" onClick={() => setReceipt(false)}>取消</button><button className="r2-button primary" disabled={!publicUrl.trim() || busy} onClick={() => void confirmReceipt()}>确认记录</button></footer></Modal>}
  </section>;
}

function ReviewModal({ result, busy, onClose, onConfirm }: { result: PreflightResult; busy: boolean; onClose: () => void; onConfirm: (real: boolean, external: boolean) => void }) {
  const task = result.task;
  const real = task.content.mode === 'real';
  const external = task.review_context?.adapter === 'aitoearn-rest';
  const blog = task.review_context?.adapter === 'blog-openapi';
  const wechat = task.content.platform === 'wechat';
  const wechatAction = task.content.options?.wechat_action === 'publish' ? 'publish' : 'draft';
  const [realConfirmed, setRealConfirmed] = useState(false);
  const [externalConfirmed, setExternalConfirmed] = useState(false);
  return <Modal title={task.content.mode === 'blog' ? '确认导出内容' : '确认发布内容'} className="variant-review-dialog" busy={busy} onClose={onClose}>
    <dl className="variant-review-meta">{task.content.mode !== 'blog' && <><dt>发布账号</dt><dd><Mark platform={task.content.platform} /> {task.review_context?.label || task.content.account_id}{task.review_context?.identity?.name ? ` · @${task.review_context.identity.name}` : ''}</dd></>}
      <dt>执行方式</dt><dd>{real ? wechat && wechatAction === 'draft' ? '写入公众号草稿箱' : '真实发布' : task.content.mode === 'blog' ? '导出到本地' : '本地模拟'}</dd>
      {task.content.mode !== 'blog' && <><dt>发布时间</dt><dd>{task.content.scheduled_at ? `${task.content.scheduled_local?.replace('T', ' ')} · ${task.content.timezone}` : '确认后立即执行'}</dd></>}

      {wechat && <><dt>封面图片</dt><dd>{String(task.content.options?.wechat_cover || task.content.media[0] || '').split('/').at(-1) || '未设置'}</dd><dt>作者</dt><dd>{String(task.content.options?.wechat_author || '未设置')}</dd><dt>摘要</dt><dd>{String(task.content.options?.wechat_digest || '未设置')}</dd></>}
      {blog && <><dt>内容类型</dt><dd>{task.content.options?.content_type === 'thought' ? '想法 / 短记' : '文章'}</dd>{task.content.options?.excerpt && <><dt>摘要</dt><dd>{String(task.content.options.excerpt)}</dd></>}</>}
    </dl>
    <PublishPreview content={task.content} heading="本次发布内容" />
    <p>请核对完整内容与素材顺序。本次审核只授权当前快照；修改草稿后需要重新检查。</p>
    {real && <label className="r2-check"><input type="checkbox" checked={realConfirmed} onChange={e => setRealConfirmed(e.target.checked)} /><span>{blog ? '我确认这份内容将发布到所选 Blog。' : wechat && wechatAction === 'draft' ? '我确认把这份内容写入所选微信公众号的草稿箱；此操作不会公开发布。' : wechat ? '我确认先创建公众号草稿，并提交到微信发布接口。' : '我确认这份内容将发布到所选账号。'}</span></label>}
    {external && <label className="r2-check"><input type="checkbox" checked={externalConfirmed} onChange={e => setExternalConfirmed(e.target.checked)} /><span>我确认使用外部服务执行本次传输，并了解可能存在独立额度或费用。</span></label>}
    <footer><button className="r2-button" disabled={busy} onClick={onClose}>取消</button><button className="r2-button primary" disabled={busy || (real && !realConfirmed) || (external && !externalConfirmed)} onClick={() => onConfirm(realConfirmed, externalConfirmed)}>{task.content.scheduled_at ? '确认定时发布' : wechat && wechatAction === 'draft' ? '确认写入草稿箱' : task.content.mode === 'blog' ? '确认并导出' : real ? '确认并发布' : '确认并模拟发布'}</button></footer>
  </Modal>;
}
