import { useEffect, useMemo, useRef, useState } from 'react';
import { api, dateText, errorText, executeStructuredOperation, taskAction, uploadMedia } from '../../lib/ripple';
import { newId } from '../../lib/id';
import type { Account, BlogConnector, Channel, Mother, PlatformVariant, PreflightResult, PublishChecklistOutput, Task, VariantContent } from '../../lib/ripple';
import { Feedback, Mark, Modal, Status } from './Common';

type Props = { variantId: string; source: Mother; onNavigate: (page: 'publish' | 'accounts', taskId?: string) => void; onUpdated: () => void };

const terminal = new Set(['published', 'exported', 'simulated', 'cancelled', 'failed_terminal']);
const cloneContent = (content: VariantContent): VariantContent => ({ ...content, media: [...content.media], options: { ...(content.options || {}) } });

export default function ContentVariantPublisher({ variantId, source, onNavigate, onUpdated }: Props) {
  const [variant, setVariant] = useState<PlatformVariant | null>(null);
  const [form, setForm] = useState<VariantContent | null>(null);
  const [sourceVersionId, setSourceVersionId] = useState('');
  const [task, setTask] = useState<Task | null>(null);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [blogs, setBlogs] = useState<BlogConnector[]>([]);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [review, setReview] = useState<PreflightResult | null>(null);
  const [receipt, setReceipt] = useState(false);
  const [publicUrl, setPublicUrl] = useState('');
  const [checklist, setChecklist] = useState<PublishChecklistOutput | null>(null);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);

  const load = async () => {
    const [next, accountRows, channelRows, blogRows, taskRows] = await Promise.all([
      api<PlatformVariant>(`/api/ripple/variants/${variantId}`),
      api<Account[]>('/api/ripple/accounts'),
      api<Channel[]>('/api/ripple/channels'),
      api<{ items: BlogConnector[] }>('/api/ripple/blog/connectors'),
      api<{ items: Task[] }>(`/api/ripple/variants/${variantId}/tasks`),
    ]);
    setVariant(next); setForm(cloneContent(next.content)); setSourceVersionId(next.source_version_id);
    setAccounts(accountRows); setChannels(channelRows); setBlogs(blogRows.items);
    setTask(taskRows.items.find(item => item.variant_version_id === next.version_id && !terminal.has(item.status)) || taskRows.items.find(item => item.variant_version_id === next.version_id) || null);
    setDirty(false);
  };

  useEffect(() => { void load().catch(e => setError(errorText(e))); }, [variantId]);

  const channel = useMemo(() => channels.find(item => item.id === variant?.platform), [channels, variant?.platform]);
  const platformAccounts = useMemo(() => accounts.filter(item => item.platform === variant?.platform), [accounts, variant?.platform]);
  const isBlog = variant?.platform === 'blog';
  const isWechat = variant?.platform === 'wechat';
  const wechatAction = form?.options?.wechat_action === 'publish' ? 'publish' : 'draft';
  const contentType = form?.options?.content_type === 'thought' ? 'thought' : 'article';
  const selectedAccount = platformAccounts.find(item => item.id === form?.target_id);
  const wechatCanPublish = !!selectedAccount?.capabilities?.includes('freepublish');
  const selectedBlog = blogs.find(item => item.id === form?.target_id);
  const stale = !!variant && variant.source_version_id !== source.version_id;
  const blogCapabilities = [`${contentType}.create`, `${contentType}.read`, `${contentType}.update`, `${contentType}.publish`];
  const blogTypeReady = !isBlog || form?.delivery === 'export' || (!!selectedBlog && blogCapabilities.every(capability => selectedBlog.capabilities.includes(capability)));
  const blogMediaReady = !isBlog || form?.delivery === 'export' || (!!selectedBlog && (!form?.media.length || (contentType === 'article' && selectedBlog.capabilities.includes('media.upload'))));
  const targetReady = !!form && (form.delivery === 'export' || (isBlog ? !!selectedBlog : !!form.target_id));
  const canCreateTask = !!form && targetReady && blogTypeReady && blogMediaReady && !stale;

  const patch = (next: Partial<VariantContent>) => { setForm(current => current ? { ...current, ...next } : current); setDirty(true); setNotice(''); setChecklist(null); };
  const patchOption = (key: string, value: unknown) => { setForm(current => current ? { ...current, options: { ...(current.options || {}), [key]: value } } : current); setDirty(true); setNotice(''); setChecklist(null); };

  const saveVariant = async (): Promise<PlatformVariant> => {
    if (!variant || !form) throw new Error('平台版本尚未加载。');
    if (!dirty) return variant;
    const updated = await api<PlatformVariant>(`/api/ripple/variants/${variant.id}`, 'PUT', {
      ...form, expected_version: variant.version_id, source_version_id: sourceVersionId,
    });
    setVariant(updated); setForm(cloneContent(updated.content)); setSourceVersionId(updated.source_version_id); setDirty(false); setTask(null);
    setNotice(`平台版本已保存 · V${updated.version}。旧发布任务保留为历史快照。`); onUpdated();
    return updated;
  };

  const save = async () => { setBusy(true); setError(''); try { await saveVariant(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const syncMother = () => {
    if (!form) return;
    setForm({ ...form, project_id: source.content.project_id, title: source.content.title, body: source.content.body, media: [...source.content.media], tags: source.content.tags });
    setSourceVersionId(source.version_id); setDirty(true); setTask(null); setNotice('已载入最新母稿内容；保存平台版本后生效。'); setChecklist(null);
  };

  const ensureTask = async (): Promise<Task> => {
    let current = variant;
    if (!current) throw new Error('平台版本尚未加载。');
    if (dirty) current = await saveVariant();
    if (current.source_version_id !== source.version_id) throw new Error('当前平台版本基于旧母稿。请先“同步最新母稿”并保存。');
    if (task && task.variant_version_id === current.version_id && !terminal.has(task.status)) return task;
    const created = await api<Task>(`/api/ripple/variants/${current.id}/tasks`, 'POST', {
      expected_version: current.version_id, idempotency_key: newId(),
    });
    setTask(created); setNotice(`已创建发布任务快照 · ${created.id.slice(0, 8)}。平台版本仍可继续编辑。`); return created;
  };

  const createTask = async () => { setBusy(true); setError(''); try { await ensureTask(); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const runChecklist = async () => { setBusy(true); setError(''); try { const current = await ensureTask(); const result = await executeStructuredOperation<PublishChecklistOutput>('publish_checklist', {}, { kind: 'publish_task', ref: current.id, version: current.version_id }); setChecklist(result.output); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const preflight = async () => { setBusy(true); setError(''); try { const current = await ensureTask(); const result = await api<PreflightResult>(`/api/ripple/tasks/${current.id}/preflight`, 'POST', { expected_version: current.version_id }); setTask(result.task); if (!result.ok) throw new Error(result.problems.join('；')); setReview(result); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const approve = async (realConfirmed: boolean, externalConfirmed: boolean) => {
    if (!review) return; setBusy(true); setError('');
    try {
      const current = review.task;
      const result = await taskAction(current, 'approve', {
        confirmed: true, real_publish_confirmed: realConfirmed,
        expected_account_revision: current.review_context?.auth_revision,
        external_service_confirmed: externalConfirmed,
      });
      setTask(result); setReview(null); setNotice(result.status === 'scheduled' ? '当前发布任务已审核并进入排期。' : '当前发布任务已审核，可以执行发布。');
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };

  const dispatch = async () => { if (!task) return; setBusy(true); setError(''); try { const result = await taskAction(task, 'dispatch'); setTask(result); setNotice(result.status === 'dispatching' ? '发布已开始执行。' : `任务状态：${result.status}`); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };
  const query = async () => { if (!task) return; setBusy(true); setError(''); try { const result = await taskAction(task, 'query'); setTask(result); setNotice(`已核对任务状态：${result.status}`); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const confirmReceipt = async () => { if (!task) return; setBusy(true); setError(''); try { const result = await api<Task>(`/api/ripple/tasks/${task.id}/receipt`, 'POST', { expected_version: task.version_id, confirmed: true, public_url: publicUrl, note: '' }); setTask(result); setReceipt(false); setNotice('作品链接已记录。'); } catch (e) { setError(errorText(e)); } finally { setBusy(false); } };

  const addMedia = async (files: FileList | null) => {
    if (!files || !form) return; setUploading(true); setProgress(0); setError('');
    try { const paths = [...form.media]; for (const file of Array.from(files)) { if (paths.length >= 12) throw new Error('每个版本最多 12 个素材。'); const result = await uploadMedia(file, setProgress); paths.push(result.path); } patch({ media: paths }); } catch (e) { setError(errorText(e)); } finally { setUploading(false); setProgress(0); if (fileRef.current) fileRef.current.value = ''; }
  };

  if (!variant || !form) return <div className="r2-loading">正在读取平台版本…</div>;
  const title = channel?.name || variant.platform;
  const realTask = task?.content.mode === 'real';
  const canQuery = !!task && ['accepted', 'unknown_result', 'verification_required', 'dispatching'].includes(task.status);
  const canDispatch = !!task && ['approved', 'failed_retryable'].includes(task.status);

  return <section className="r2-variant-publisher">
    <Feedback error={error} notice={notice} />
    <div className="r2-section-heading"><div><h2><Mark platform={variant.platform} /> {title} · 平台版本 V{variant.version}</h2><p>平台版本可独立改写。发布任务只在检查 / 审核 / 执行时生成不可变快照。</p></div>{task && <Status status={task.status} />}</div>
    {stale && <div className="r2-alert"><strong>母稿已有更新</strong><span>当前版本仍保留原文案。需要采用最新母稿时，先同步再保存。</span><button className="r2-button" disabled={busy} onClick={syncMother}>同步最新母稿</button></div>}

    <div className="r2-form-grid">
      <label className="r2-field"><span>平台版本标题</span><input value={form.title} maxLength={200} onChange={e => patch({ title: e.target.value })} /></label>
      <label className="r2-field"><span>平台版本正文</span><textarea rows={10} value={form.body} onChange={e => patch({ body: e.target.value })} /></label>
      <label className="r2-field"><span>话题标签</span><input value={form.tags} onChange={e => patch({ tags: e.target.value })} placeholder="逗号分隔" /></label>
    </div>

    <div className="r2-version-target">
      <h3>发布目标</h3>
      {isBlog ? <>
        <label className="r2-field"><span>发布方式</span><select value={form.delivery} onChange={e => patch({ delivery: e.target.value as 'remote' | 'export', target_id: e.target.value === 'export' ? '' : form.target_id })}><option value="remote">连接 Blog 发布</option><option value="export">仅导出 Markdown / 素材</option></select></label>
        {form.delivery === 'remote' && <label className="r2-field"><span>Blog</span><select value={form.target_id} onChange={e => patch({ target_id: e.target.value })}><option value="">选择已连接的 Blog</option>{blogs.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>}
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
        <label className="r2-field"><span>平台版本目标账号</span><select value={form.target_id} onChange={e => patch({ target_id: e.target.value, delivery: 'remote' })}><option value="">选择发布账号</option>{platformAccounts.map(item => <option key={item.id} value={item.id}>{item.label} · {item.status === 'connected' ? '已连接' : '未连接'}</option>)}</select></label>
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
      <label className="r2-field"><span>平台版本计划时间（可选）</span><input type="datetime-local" value={form.scheduled_local || ''} onChange={e => patch({ scheduled_local: e.target.value || null })} /></label>
      {form.scheduled_local && <small className="r2-muted">时区：{form.timezone}</small>}
    </div>

    <div className="r2-media-editor"><div className="r2-section-heading"><h3>素材 {form.media.length}</h3><button className="r2-button" disabled={uploading || form.media.length >= 12} onClick={() => fileRef.current?.click()}>{uploading ? `上传 ${progress}%` : isWechat ? '添加图片' : '添加图片 / 视频'}</button></div><input ref={fileRef} hidden type="file" multiple accept={isWechat ? 'image/png,image/jpeg,image/webp,image/gif' : 'image/png,image/jpeg,image/webp,image/gif,video/mp4,video/quicktime,video/webm'} onChange={e => void addMedia(e.target.files)} />{form.media.map((path, index) => <div className="r2-media-row" key={`${path}-${index}`}><code>{path}</code>{isWechat && (String(form.options.wechat_cover || form.media[0] || '') === path) && <span className="r2-muted">封面</span>}<button className="r2-button" onClick={() => patch({ media: form.media.filter((_, i) => i !== index) })}>移除</button></div>)}</div>

    <div className="r2-toolbar r2-variant-actions">
      <button className="r2-button primary" disabled={busy || uploading || !dirty || !form.title.trim()} onClick={() => void save()}>保存平台版本</button>
      <button className="r2-button" disabled={busy || stale || !canCreateTask} onClick={() => void createTask()}>{task && !terminal.has(task.status) ? '发布任务已建立' : '创建发布任务'}</button>
      <button className="r2-button" disabled={busy || stale || !canCreateTask} onClick={() => void runChecklist()}>发布检查</button>
      <button className="r2-button primary" disabled={busy || stale || !canCreateTask} onClick={() => void preflight()}>预检并审核</button>
      {canDispatch && <button className="r2-button danger" disabled={busy} onClick={() => void dispatch()}>{isWechat && wechatAction === 'draft' ? '写入公众号草稿箱' : '执行发布'}</button>}
      {canQuery && <button className="r2-button" disabled={busy} onClick={() => void query()}>核对结果</button>}
      {task && <button className="r2-button" onClick={() => onNavigate('publish', task.id)}>发布管理</button>}
      {task?.status === 'exported' && <a className="r2-button" href={`/api/ripple/tasks/${task.id}/export`}>下载 Blog 导出包</a>}
      {task && realTask && !(task.content.platform === 'wechat' && task.content.options?.wechat_action !== 'publish') && ['accepted', 'unknown_result', 'verification_required'].includes(task.status) && <button className="r2-button" onClick={() => setReceipt(true)}>人工记录作品链接</button>}
    </div>

    {task && <div className="r2-task-summary"><strong>发布任务 {task.id.slice(0, 8)}</strong><span>快照来自平台版本 V{variant.version} · {dateText(task.created_at)}</span>{task.receipt?.public_url && <a href={task.receipt.public_url} target="_blank" rel="noreferrer">打开已记录作品</a>}</div>}
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
  return <Modal title="审核发布任务快照" busy={busy} onClose={onClose}><div className="r2-review-summary"><strong>{task.content.title}</strong><span>{task.content.platform} · {task.review_context?.label || task.content.account_id}</span><span>素材 {task.content.media.length} · {task.content.scheduled_at ? `排期 ${dateText(task.content.scheduled_at)}` : '立即执行'}</span></div>
    <p>当前任务保存的是平台版本的不可变快照。之后再编辑平台版本，会创建新的发布任务。</p>
    {real && <label className="r2-check"><input type="checkbox" checked={realConfirmed} onChange={e => setRealConfirmed(e.target.checked)} /><span>{blog ? '我确认这份内容将发布到所选 Blog。' : wechat && wechatAction === 'draft' ? '我确认把这份内容写入所选微信公众号的草稿箱；此操作不会公开发布。' : wechat ? '我确认先创建公众号草稿，并提交到微信发布接口。' : '我确认这份内容将发布到所选账号。'}</span></label>}
    {external && <label className="r2-check"><input type="checkbox" checked={externalConfirmed} onChange={e => setExternalConfirmed(e.target.checked)} /><span>我确认使用外部服务执行本次传输，并了解可能存在独立额度或费用。</span></label>}
    <footer><button className="r2-button" disabled={busy} onClick={onClose}>取消</button><button className="r2-button primary" disabled={busy || (real && !realConfirmed) || (external && !externalConfirmed)} onClick={() => onConfirm(realConfirmed, externalConfirmed)}>{real ? '确认真实发布授权' : '确认审核'}</button></footer>
  </Modal>;
}
