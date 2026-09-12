import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Page } from '../Sidebar';
import { api, base, dateText, errorText, taskAction } from '../../lib/ripple';
import type { Account, BlogConnector, Task } from '../../lib/ripple';
import { Empty, Feedback, Header, Mark, Modal, Status } from './Common';

const ATTENTION = new Set(['accepted', 'unknown_result', 'verification_required', 'failed_retryable', 'failed_terminal']);

export default function Publisher({ onNavigate }: { onNavigate: (page: Page) => void }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [blogs, setBlogs] = useState<BlogConnector[]>([]);
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

  const refresh = useCallback(async () => {
    const [first, accountRows, blogRows] = await Promise.all([
      api<{ items: Task[]; total: number }>('/api/ripple/tasks'),
      api<Account[]>('/api/ripple/accounts'),
      api<{ items: BlogConnector[] }>('/api/ripple/blog/connectors'),
    ]);
    let all = first.items;
    for (let offset = 200; offset < first.total; offset += 200) {
      all = [...all, ...(await api<{ items: Task[] }>(`/api/ripple/tasks?offset=${offset}`)).items];
    }
    setTasks(all); setAccounts(accountRows); setBlogs(blogRows.items);
    if (!focusedOnce.current) {
      focusedOnce.current = true;
      const focus = sessionStorage.getItem('ripple_task_focus') || '';
      sessionStorage.removeItem('ripple_task_focus');
      if (focus && all.some((task) => task.id === focus)) setSelectedId(focus);
      else if (all[0]) setSelectedId(all[0].id);
    } else if (selectedId && !all.some((task) => task.id === selectedId)) {
      setSelectedId(all[0]?.id || '');
    }
    return all;
  }, [selectedId]);

  useEffect(() => {
    let stopped = false, running = false;
    const load = async () => {
      if (stopped || running) return;
      running = true;
      try { await refresh(); if (!stopped) setError(''); }
      catch (e) { if (!stopped) setError(errorText(e)); }
      finally { running = false; }
    };
    void load();
    const timer = setInterval(() => void load(), 4000);
    return () => { stopped = true; clearInterval(timer); };
  }, [refresh]);

  const run = async (fn: () => Promise<void>) => {
    if (gate.current) return;
    gate.current = true; setBusy(true); setError(''); setNotice('');
    try { await fn(); await refresh(); } catch (e) { setError(errorText(e)); } finally { gate.current = false; setBusy(false); }
  };

  const selected = tasks.find((task) => task.id === selectedId) || null;
  const account = selected ? accounts.find((row) => row.id === selected.content.account_id) : undefined;
  const blog = selected?.content.platform === 'blog' ? blogs.find((row) => row.id === selected.content.account_id) : undefined;
  const displayed = useMemo(() => tasks.filter((task) => {
    const needle = query.trim().toLowerCase();
    const targetLabel = task.content.platform === 'blog' ? blogs.find((row) => row.id === task.content.account_id)?.label : accounts.find((row) => row.id === task.content.account_id)?.label;
    const matchesText = !needle || task.content.title.toLowerCase().includes(needle) || task.content.platform.toLowerCase().includes(needle) || (targetLabel || '').toLowerCase().includes(needle);
    const matchesFilter = filter === 'all' || (filter === 'attention' ? ATTENTION.has(task.status) : task.status === filter);
    return matchesText && matchesFilter;
  }), [accounts, blogs, filter, query, tasks]);

  const openContent = (task: Task) => {
    if (!task.content.source_id) { setError('这个历史任务没有关联主稿，无法定位到内容工作台。'); return; }
    sessionStorage.setItem('ripple_content_focus', task.content.source_id);
    sessionStorage.setItem('ripple_variant_focus', task.variant_id || task.id);
    onNavigate('contents');
  };
  const safeAction = (action: 'query' | 'cancel') => {
    if (!selected) return;
    void run(async () => {
      const next = await taskAction(selected, action);
      setSelectedId(next.id); setNotice(next.events.at(-1)?.note || '任务状态已更新。');
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

  return <div className="r2-workspace r2-publisher-management">
    <Header title="发布任务" subtitle="集中管理已创建的发布任务、排期、状态和回执。内容编辑与发布执行请回到内容工作台。">
      <button className="r2-button" disabled={busy} onClick={() => void refresh().catch((e) => setError(errorText(e)))}>刷新</button>
    </Header>
    <Feedback error={error} notice={notice} />
    <div className="r2-publish-layout">
      <aside className="r2-task-list"><div className="r2-list-tools"><input aria-label="搜索发布任务" placeholder="搜索标题 / 渠道 / 账号" value={query} onChange={(e) => setQuery(e.target.value)} /><select aria-label="任务筛选" value={filter} onChange={(e) => setFilter(e.target.value)}><option value="all">全部状态</option><option value="draft">草稿</option><option value="review_ready">待审核</option><option value="approved">已审核</option><option value="scheduled">已排期</option><option value="attention">需要处理</option><option value="published">已发布</option><option value="exported">已导出</option><option value="cancelled">已取消</option></select></div><div className="r2-list-caption">发布任务 <span>{displayed.length}</span></div>
        {displayed.map((task) => <button key={task.id} className={`r2-list-item ${selectedId === task.id ? 'active' : ''}`} disabled={busy} onClick={() => setSelectedId(task.id)}><div><Mark platform={task.content.platform} /><strong>{task.content.title}</strong></div><Status status={task.status} /><small>{(task.content.platform === 'blog' ? blogs.find((row) => row.id === task.content.account_id)?.label : accounts.find((row) => row.id === task.content.account_id)?.label) || task.content.account_id || '未选目标'} · {dateText(task.updated_at)}</small></button>)}
        {displayed.length === 0 && <Empty title="暂无发布任务" description="在内容工作台创建平台版本并进入发布流程后，会在这里集中管理。" />}
      </aside>
      <main className="r2-publish-editor">
        {!selected ? <Empty title="选择一条发布任务" description="这里不创建或编辑内容，只查看和管理已有任务。" /> : <>
          <div className="r2-editor-meta"><span>发布任务 {selected.id.slice(0, 8)} · 任务 V{selected.version}{selected.variant_id ? ` · 平台版本 ${selected.variant_id.slice(0, 8)}` : ''}</span><Status status={selected.status} /></div>
          <section className="r2-section"><div className="r2-section-heading"><div><h2><Mark platform={selected.content.platform} /> {selected.content.title}</h2><p className="r2-muted">内容快照 · 只读</p></div><button className="r2-button primary" disabled={!selected.content.source_id} onClick={() => openContent(selected)}>打开内容工作台</button></div>
            <dl className="r2-review-fields"><dt>渠道</dt><dd>{selected.content.platform}</dd><dt>目标</dt><dd>{blog?.label || account?.identity?.name || account?.label || selected.content.account_id || '未选择'}</dd><dt>执行方式</dt><dd>{selected.content.mode === 'real' ? '真实账号发布' : selected.content.mode === 'blog' ? 'Blog 本地导出' : '开发模拟'}</dd><dt>排期</dt><dd>{selected.content.scheduled_at ? `${dateText(selected.content.scheduled_at)} · ${selected.content.timezone}` : '未排期 / 手动执行'}</dd><dt>来源主稿</dt><dd>{selected.content.source_id ? `${selected.content.source_id.slice(0, 8)} · ${selected.content.source_version_id ? `版本 ${selected.content.source_version_id.slice(0, 10)}` : '未知版本'}` : '历史任务未关联主稿'}</dd><dt>素材</dt><dd>{selected.content.media.length} 个</dd></dl>
            <details className="r2-details"><summary>查看发布内容快照</summary><h3>{selected.content.title}</h3><div className="r2-review-copy">{selected.content.body || '（无正文）'}</div>{selected.content.tags && <p className="r2-muted">话题：{selected.content.tags}</p>}</details>
          </section>
          <section className="r2-section"><div className="r2-section-heading"><h2>审核与执行</h2><Status status={selected.status} /></div><dl className="r2-review-fields"><dt>审核</dt><dd>{selected.approval ? `已审核 · ${dateText(selected.approval.at)}${selected.approval.real_publish_confirmed ? ' · 已确认真实发布' : ''}` : '未审核'}</dd><dt>执行尝试</dt><dd>{selected.attempts} 次</dd><dt>最近状态</dt><dd>{selected.events.at(-1)?.note || '—'}</dd></dl>
            <div className="r2-toolbar">{canQuery && <button className="r2-button" disabled={busy} onClick={() => safeAction('query')}>核对执行回执</button>}{canReceipt && <button className="r2-button" disabled={busy} onClick={openReceipt}>登记已发布链接</button>}{canCancel && <button className="r2-text-button muted" disabled={busy} onClick={() => { if (window.confirm('取消这个尚未完成的本地任务？')) safeAction('cancel'); }}>取消任务</button>}{selected.status === 'exported' && <a className="r2-button" href={`${base}/api/ripple/tasks/${selected.id}/export`}>下载导出包</a>}{selected.receipt?.public_url && <a className="r2-button" href={selected.receipt.public_url} target="_blank" rel="noreferrer">打开作品 ↗</a>}</div>
          </section>
          <section className="r2-section"><div className="r2-section-heading"><h2>回执</h2></div>{selected.receipt ? <dl className="r2-review-fields"><dt>Adapter</dt><dd>{selected.receipt.adapter || '—'}</dd><dt>结果</dt><dd>{selected.receipt.result || '—'}</dd><dt>公开链接</dt><dd>{selected.receipt.public_url || selected.receipt.candidate_url || '—'}</dd><dt>远端流程</dt><dd>{selected.receipt.flow_id || '—'}</dd></dl> : <p className="r2-muted">当前没有发布回执。</p>}</section>
          <section className="r2-section"><div className="r2-section-heading"><h2>审计记录</h2><span>{selected.events.length} 条</span></div><ol className="r2-event-list">{selected.events.map((event, index) => <li key={index}><time>{dateText(event.at)}</time><span>{event.note}</span></li>)}</ol></section>
        </>}
      </main>
    </div>
    {receiptOpen && selected && <Modal title="人工核对发布结果" busy={busy} onClose={() => setReceiptOpen(false)}><p>请先在目标平台确认此作品已经发布。记录会注明“人工核对”，不会当作接口自动验证。</p><label className="r2-field">作品链接<input aria-label="作品链接" type="url" value={publicUrl} onChange={(e) => setPublicUrl(e.target.value)} placeholder="https://…" /></label><label className="r2-checkbox"><input type="checkbox" checked={receiptConsent} onChange={(e) => setReceiptConsent(e.target.checked)} />已在目标账号核对内容与公开可见状态</label><Feedback error={error} /><footer><button className="r2-button" onClick={() => setReceiptOpen(false)} disabled={busy}>取消</button><button className="r2-button primary" disabled={busy || !receiptConsent || !publicUrl} onClick={() => void run(async () => { const next = await taskAction(selected, 'receipt', { confirmed: true, public_url: publicUrl }); setSelectedId(next.id); setReceiptOpen(false); setNotice('已登记人工核对回执。'); })}>保存核对结果</button></footer></Modal>}
  </div>;
}
