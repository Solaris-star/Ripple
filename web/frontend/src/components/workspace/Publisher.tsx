import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Page } from '../Sidebar';
import { api, base, dateText, errorText, taskAction } from '../../lib/ripple';
import type { Account, BlogConnector, Mother, Task } from '../../lib/ripple';
import PublishPreview from './PublishPreview';
import RemotePostsPanel from './RemotePostsPanel';
import '../../styles/content-workflow.css';
import { Empty, Feedback, Header, Mark, Modal, Status } from './Common';
import { PlatformBadge } from '../PlatformBrand';
import type { WorkspaceFocus } from '../../lib/workspaceNavigation';
import { publishResultText, taskNeedsAttention, taskStatusText } from './variantPublishing';



export default function Publisher({ onNavigate }: { onNavigate: (page: Page, focus?: WorkspaceFocus) => void }) {
  const [view, setView] = useState<'tasks' | 'remote'>('tasks');
  const [tasks, setTasks] = useState<Task[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [blogs, setBlogs] = useState<BlogConnector[]>([]);
  const [contents, setContents] = useState<Mother[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [selectedId, setSelectedId] = useState('');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState('all');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [receiptOpen, setReceiptOpen] = useState(false);
  const [publicUrl, setPublicUrl] = useState('');
  const [receiptConsent, setReceiptConsent] = useState(false);
  const focusedOnce = useRef(false);
  const gate = useRef(false);

  const refreshMetadata = useCallback(async () => {
    const results = await Promise.allSettled([
      api<Account[]>('/api/ripple/accounts').then(setAccounts),
      api<{ items: BlogConnector[] }>('/api/ripple/blog/connectors').then((rows) => setBlogs(rows.items)),
      api<Mother[]>('/api/ripple/contents').then(setContents),
    ]);
    const failure = results.find((row) => row.status === 'rejected');
    if (failure?.status === 'rejected') throw failure.reason;
  }, []);

  const refreshTasks = useCallback(async (full = false) => {
    const first = await api<{ items: Task[]; total: number }>('/api/ripple/tasks');
    // Render the newest page immediately. Background polling updates only this page;
    // older immutable history is loaded once or on explicit refresh.
    setTasks((current) => first.total <= 200 ? first.items : [
      ...first.items,
      ...current.filter((task) => !first.items.some((fresh) => fresh.id === task.id)),
    ]);
    let all = first.items;
    if (full && first.total > 200) {
      const offsets = Array.from({ length: Math.ceil((first.total - 200) / 200) }, (_, index) => 200 + index * 200);
      const tail = await Promise.all(offsets.map((offset) => api<{ items: Task[] }>(`/api/ripple/tasks?offset=${offset}`).then((rows) => rows.items)));
      all = [...first.items, ...tail.flat()];
      setTasks(all);
    }
    setSelectedId((current) => {
      if (!focusedOnce.current && (full || first.total <= 200)) {
        focusedOnce.current = true;
        let focus = new URLSearchParams(location.search).get('task') || '';
        try { focus ||= sessionStorage.getItem('ripple_task_focus') || ''; sessionStorage.removeItem('ripple_task_focus'); } catch { /* 浏览器存储不可用时使用 URL。 */ }
        return focus && all.some((task) => task.id === focus) ? focus : all[0]?.id || '';
      }
      return current && (all.some((task) => task.id === current) || (!full && first.total > 200)) ? current : all[0]?.id || '';
    });
    return all;
  }, []);

  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([refreshTasks(true), refreshMetadata()]);
    const failure = results.find((row) => row.status === 'rejected');
    if (failure?.status === 'rejected') throw failure.reason;
    return results[0].status === 'fulfilled' ? results[0].value : [];
  }, [refreshMetadata, refreshTasks]);

  useEffect(() => {
    let stopped = false, running = false;
    const loadTasks = async (full = false) => {
      if (stopped || running) return;
      running = true;
      try { await refreshTasks(full); if (!stopped) setError(''); }
      catch (e) { if (!stopped) setError(errorText(e)); }
      finally { running = false; }
    };
    void loadTasks(true);
    void refreshMetadata().catch((e) => { if (!stopped) setError(errorText(e)); });
    const taskTimer = setInterval(() => void loadTasks(false), 4000);
    const metadataTimer = setInterval(() => void refreshMetadata().catch(() => {}), 30000);
    return () => { stopped = true; clearInterval(taskTimer); clearInterval(metadataTimer); };
  }, [refreshMetadata, refreshTasks]);

  const run = async (fn: () => Promise<void>) => {
    if (gate.current) return;
    gate.current = true; setBusy(true); setError(''); setNotice('');
    try { await fn(); await refresh(); } catch (e) { setError(errorText(e)); } finally { gate.current = false; setBusy(false); }
  };

  const selected = tasks.find((task) => task.id === selectedId) || null;
  const account = selected ? accounts.find((row) => row.id === selected.content.account_id) : undefined;
  const blog = selected?.content.platform === 'blog' ? blogs.find((row) => row.id === selected.content.account_id) : undefined;
  const superseded = useMemo(() => new Set(tasks.filter((task) => task.variant_id && task.attempts === 0 && ['draft', 'review_ready'].includes(task.status)
    && tasks.some((other) => other.id !== task.id && other.variant_id === task.variant_id && other.created_at > task.created_at && other.status !== 'cancelled')).map((task) => task.id)), [tasks]);
  const continueDrafts = contents.filter((content) => !tasks.some((task) => task.content.source_id === content.id && !['cancelled', 'failed_terminal'].includes(task.status)))
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 3);
  const displayed = useMemo(() => tasks.filter((task) => {
    const needle = query.trim().toLowerCase();
    const targetLabel = task.content.platform === 'blog' ? blogs.find((row) => row.id === task.content.account_id)?.label : accounts.find((row) => row.id === task.content.account_id)?.label;
    const matchesText = !needle || task.content.title.toLowerCase().includes(needle) || task.content.platform.toLowerCase().includes(needle) || (targetLabel || '').toLowerCase().includes(needle);
    const matchesFilter = filter === 'all' || (filter === 'attention' ? taskNeedsAttention(task) : task.status === filter);
    return matchesText && matchesFilter && (showHistory || !superseded.has(task.id) || task.id === selectedId);
  }), [accounts, blogs, filter, query, tasks, showHistory, superseded, selectedId]);
  useEffect(() => {
    if (!displayed.some(task => task.id === selectedId)) setSelectedId(displayed[0]?.id || '');
  }, [displayed, selectedId]);

  const continueContent = (content: Mother) => {
    onNavigate('contents', { contentId: content.id, variantId: content.variants?.length === 1 ? content.variants[0].id : undefined });
  };

  const openContent = (task: Task) => {
    if (!task.content.source_id) { setError('这个历史任务没有关联主稿，无法定位到内容工作台。'); return; }
    const url = new URL(location.href); url.searchParams.set('task', task.id); history.replaceState({}, '', url);
    onNavigate('contents', { contentId: task.content.source_id, variantId: task.variant_id || undefined });
  };
  const safeAction = (action: 'query' | 'cancel') => {
    if (!selected) return;
    void run(async () => {
      const next = action === 'cancel' && selected.status === 'scheduled'
        ? (await api<{ task: Task }>(`/api/ripple/tasks/${selected.id}/return-to-plan`, 'POST', { expected_version: selected.version_id })).task
        : await taskAction(selected, action);
      setSelectedId(next.id); setNotice(publishResultText(next));
    });
  };
  const openReceipt = () => {
    if (!selected) return;
    setPublicUrl(selected.receipt?.candidate_url || selected.receipt?.public_url || ''); setReceiptConsent(false); setReceiptOpen(true);
  };
  const canQuery = !!selected && (['accepted', 'unknown_result'].includes(selected.status) || (account?.adapter === 'aitoearn-rest' && selected.status === 'verification_required' && !!selected.receipt?.flow_id));
  const canReceipt = !!selected && selected.content.mode === 'real' && ['accepted', 'unknown_result', 'verification_required'].includes(selected.status);
  const canCancel = !!selected && ['draft', 'review_ready', 'approved', 'scheduled', 'failed_retryable', 'verification_required'].includes(selected.status)
    && !(selected.content.mode === 'real' && selected.attempts > 0 && !selected.receipt?.not_submitted);
  const viewTabs = <nav className="r2-content-view-tabs r2-publisher-tabs" role="tablist" aria-label="发布管理">
    <button type="button" role="tab" aria-selected={view === 'tasks'} onClick={() => setView('tasks')}>发布任务历史</button>
    <button type="button" role="tab" aria-selected={view === 'remote'} onClick={() => setView('remote')}>平台作品</button>
  </nav>;

  if (view === 'remote') return <div className="r2-workspace r2-publisher-management">
    <Header title="平台作品" subtitle="管理当前账号在平台上的本人作品，包括从平台 App 或网页直接发布的内容。" />
    {viewTabs}
    <RemotePostsPanel accounts={accounts} />
  </div>;

  return <div className="r2-workspace r2-publisher-management">
    <Header title="发布任务" subtitle="集中管理已创建的发布任务、排期、状态和回执。内容编辑与发布执行请回到内容工作台。">
      <button className="r2-button" disabled={busy} onClick={() => void refresh().catch((e) => setError(errorText(e)))}>刷新</button>
    </Header>
    {viewTabs}
    <Feedback error={error} notice={notice} />
    {!!continueDrafts.length && <section className="r2-continue-drafts"><h2>继续准备发布</h2><p>这些稿件还没有发布任务，可以继续编辑和检查。</p><ul>{continueDrafts.map((content) => <li key={content.id}><strong>{content.content.title}</strong><button className="r2-button" onClick={() => continueContent(content)}>继续准备</button></li>)}</ul></section>}
    {superseded.size > 0 && <label className="r2-check"><input type="checkbox" checked={showHistory} onChange={(event) => setShowHistory(event.target.checked)} />显示较早的检查记录（{superseded.size}）</label>}
    <div className="r2-publish-layout">
      <aside className="r2-task-list"><div className="r2-list-tools"><input aria-label="搜索发布任务" placeholder="搜索标题 / 渠道 / 账号" value={query} onChange={(e) => setQuery(e.target.value)} /><select aria-label="任务筛选" value={filter} onChange={(e) => setFilter(e.target.value)}><option value="all">全部状态</option><option value="draft">草稿</option><option value="review_ready">待审核</option><option value="approved">已审核</option><option value="scheduled">已排期</option><option value="attention">需要处理</option><option value="published">已发布</option><option value="exported">已导出</option><option value="cancelled">已取消</option></select></div><div className="r2-list-caption">发布任务 <span>{displayed.length}</span></div>
        {displayed.map((task) => <button key={task.id} className={`r2-list-item ${selectedId === task.id ? 'active' : ''}`} disabled={busy} onClick={() => setSelectedId(task.id)}><div><Mark platform={task.content.platform} /><strong>{task.content.title}</strong></div><Status status={task.status} label={taskStatusText(task)} /><small>{(task.content.platform === 'blog' ? blogs.find((row) => row.id === task.content.account_id)?.label : accounts.find((row) => row.id === task.content.account_id)?.label) || task.content.account_id || '未选目标'} · {dateText(task.updated_at)}</small></button>)}
        {displayed.length === 0 && <Empty title={tasks.length ? '没有符合筛选条件的任务' : '暂无发布任务'} description="编辑稿件后点击“准备发布”，检查通过后在这里查看进度。"><button className="r2-button" onClick={() => onNavigate('contents')}>继续创作</button></Empty>}
      </aside>
      <main className="r2-publish-editor">
        {!selected ? <Empty title="准备好内容后再发布" description="从上方选择稿件继续准备，或进入内容工作台开始创作。"><button className="r2-button primary" onClick={() => onNavigate('contents')}>打开内容工作台</button></Empty> : <>
          <div className="r2-editor-meta"><span>发布任务 {selected.id.slice(0, 8)} · 任务 V{selected.version}{selected.variant_id ? ` · 平台版本 ${selected.variant_id.slice(0, 8)}` : ''}</span><Status status={selected.status} label={taskStatusText(selected)} /></div>
          <section className="r2-section"><div className="r2-section-heading"><div><h2><Mark platform={selected.content.platform} /> {selected.content.title}</h2><p className="r2-muted">内容快照 · 只读</p></div><button className="r2-button primary" onClick={() => openContent(selected)}>{!selected.approval && selected.attempts === 0 ? '继续审核' : '查看或编辑稿件'}</button></div>
            <dl className="r2-review-fields"><dt>渠道</dt><dd><PlatformBadge platform={selected.content.platform} size="xs" /></dd><dt>目标</dt><dd>{blog?.label || account?.identity?.name || account?.label || selected.content.account_id || '未选择'}</dd><dt>执行方式</dt><dd>{selected.content.mode === 'real' ? '真实账号发布' : selected.content.mode === 'blog' ? 'Blog 本地导出' : '开发模拟'}</dd><dt>排期</dt><dd>{selected.content.scheduled_at ? `${dateText(selected.content.scheduled_at)} · ${selected.content.timezone}` : '未排期 / 手动执行'}</dd><dt>来源主稿</dt><dd>{selected.content.source_id ? `${selected.content.source_id.slice(0, 8)} · ${selected.content.source_version_id ? `版本 ${selected.content.source_version_id.slice(0, 10)}` : '未知版本'}` : '历史任务未关联主稿'}</dd><dt>素材</dt><dd>{selected.content.media.length} 个</dd></dl>
            <PublishPreview content={selected.content} heading="本次发布内容" />{selected.preflight_problems?.map(problem => <p className="r2-warning" key={problem}>{problem}</p>)}
          </section>
          <section className="r2-section"><div className="r2-section-heading"><h2>审核与执行</h2><Status status={selected.status} label={taskStatusText(selected)} /></div><dl className="r2-review-fields"><dt>审核</dt><dd>{selected.approval ? `已审核 · ${dateText(selected.approval.at)}${selected.approval.real_publish_confirmed ? ' · 已确认真实发布' : ''}` : '未审核'}</dd><dt>执行尝试</dt><dd>{selected.attempts} 次</dd><dt>最近状态</dt><dd>{publishResultText(selected)}</dd><dt>最近更新</dt><dd>{dateText(selected.updated_at)}</dd></dl>
            <div className="r2-toolbar">{canQuery && <button className="r2-button" disabled={busy} onClick={() => safeAction('query')}>检查结果</button>}{canReceipt && <button className="r2-button" disabled={busy} onClick={openReceipt}>我已找到作品链接</button>}{canCancel && <button className="r2-text-button muted" disabled={busy} onClick={() => { if (window.confirm('取消这个尚未完成的本地任务？')) safeAction('cancel'); }}>取消任务</button>}{selected.status === 'exported' && <a className="r2-button" href={`${base}/api/ripple/tasks/${selected.id}/export`}>下载导出包</a>}{selected.receipt?.public_url && <a className="r2-button" href={selected.receipt.public_url} target="_blank" rel="noreferrer">打开作品 ↗</a>}</div>
          </section>
          <details className="r2-section"><summary>诊断信息与回执</summary>{selected.receipt ? <dl className="r2-review-fields"><dt>Adapter</dt><dd>{selected.receipt.adapter || '—'}</dd><dt>结果</dt><dd>{selected.receipt.result || '—'}</dd><dt>公开链接</dt><dd>{selected.receipt.public_url || selected.receipt.candidate_url || '—'}</dd><dt>远端流程</dt><dd>{selected.receipt.flow_id || '—'}</dd></dl> : <p className="r2-muted">当前没有发布回执。</p>}</details>
          <section className="r2-section"><div className="r2-section-heading"><h2>审计记录</h2><span>{selected.events.length} 条</span></div><ol className="r2-event-list">{selected.events.map((event, index) => <li key={index}><time>{dateText(event.at)}</time><span>{event.note}</span></li>)}</ol></section>
        </>}
      </main>
    </div>
    {receiptOpen && selected && <Modal title="人工核对发布结果" busy={busy} onClose={() => setReceiptOpen(false)}><p>请先在目标平台确认此作品已经发布。记录会注明“人工核对”，不会当作接口自动验证。</p><label className="r2-field">作品链接<input aria-label="作品链接" type="url" value={publicUrl} onChange={(e) => setPublicUrl(e.target.value)} placeholder="https://…" /></label><label className="r2-checkbox"><input type="checkbox" checked={receiptConsent} onChange={(e) => setReceiptConsent(e.target.checked)} />已在目标账号核对内容与公开可见状态</label><Feedback error={error} /><footer><button className="r2-button" onClick={() => setReceiptOpen(false)} disabled={busy}>取消</button><button className="r2-button primary" disabled={busy || !receiptConsent || !publicUrl} onClick={() => void run(async () => { const next = await taskAction(selected, 'receipt', { confirmed: true, public_url: publicUrl }); setSelectedId(next.id); setReceiptOpen(false); setNotice('已登记人工核对回执。'); })}>保存核对结果</button></footer></Modal>}
  </div>;
}
