import { useCallback, useEffect, useState } from 'react';
import { api, dateText, errorText } from '../../lib/ripple';
import type { Account, RemotePost, RemotePostAction } from '../../lib/ripple';
import { Empty, Feedback, Mark, Modal, Status } from './Common';
import { countXReply } from '../../lib/xText';
import { newId } from '../../lib/id';

type SyncInfo = { checked_at?: string; complete?: boolean; next_cursor?: string | null; last_count?: number };

const stateText = (post: RemotePost) => post.remote_status === 'reviewing' ? '审核中'
  : post.remote_status === 'published' && post.visibility === 'private' ? '仅自己可见'
  : post.remote_status === 'published' ? '平台已发布 · 公开范围未核实'
  : post.remote_status === 'deleted' ? '已删除' : '状态待核对';

export default function RemotePostsPanel({ accounts }: { accounts: Account[] }) {
  const choices = accounts.filter(account => ['x', 'xiaohongshu'].includes(account.platform) && account.adapter !== 'x-api');
  const [accountId, setAccountId] = useState('');
  const [rows, setRows] = useState<RemotePost[]>([]);
  const [post, setPost] = useState<RemotePost | null>(null);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [sync, setSync] = useState<SyncInfo>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [dialog, setDialog] = useState<'edit' | 'delete' | null>(null);
  const [action, setAction] = useState<RemotePostAction | null>(null);
  const [title, setTitle] = useState('');
  const [body, setBody] = useState('');
  const [topics, setTopics] = useState('');
  const currentAccount = choices.find(account => account.id === accountId);

  useEffect(() => {
    if (!choices.some(account => account.id === accountId)) setAccountId(choices[0]?.id || '');
  }, [accountId, choices]);

  const load = useCallback(async (id: string, pageOffset: number) => {
    if (!id) { setRows([]); setPost(null); return; }
    const result = await api<{ items: RemotePost[]; total: number; sync: SyncInfo }>(`/api/ripple/remote-posts?account_id=${encodeURIComponent(id)}&limit=30&offset=${pageOffset}`);
    setRows(result.items); setTotal(result.total); setSync(result.sync || {});
    setPost(current => result.items.find(item => item.id === current?.id) || result.items[0] || null);
  }, []);

  useEffect(() => {
    setOffset(0); setPost(null); setAction(null);
    void load(accountId, 0).catch(e => {
      const message = errorText(e);
      setError(message === 'Not Found' ? '当前后端尚未加载平台作品接口，请重启 Ripple 本地服务后刷新。' : message);
    });
  }, [accountId, load]);

  const run = async (task: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { await task(); } catch (e) { setError(errorText(e)); }
    finally { setBusy(false); }
  };

  const select = (item: RemotePost) => void run(async () => {
    const fresh = await api<RemotePost>(`/api/ripple/remote-posts/${encodeURIComponent(item.id)}`);
    setPost(fresh); setAction(null);
  });

  const refreshPost = () => { if (!post) return; void run(async () => {
    const fresh = await api<RemotePost>(`/api/ripple/remote-posts/${encodeURIComponent(post.id)}/refresh`, 'POST');
    setPost(fresh); await load(accountId, offset); setPost(fresh);
    setNotice('已从平台重新读取目标作品。');
  }); };

  const synchronize = (more = false) => void run(async () => {
    if (!accountId) return;
    const result = await api<{ items: RemotePost[]; sync: SyncInfo }>('/api/ripple/remote-posts/sync', 'POST',
      { account_id: accountId, cursor: more ? sync.next_cursor || '' : '', limit: 30 });
    await load(accountId, 0);
    setOffset(0);
    setSync(result.sync);
    setNotice(result.sync.complete ? `已读完当前账号作品，本次新增或更新 ${result.items.length} 条。`
      : `已同步 ${result.items.length} 条，仍有下一页；当前结果不是全部作品。`);
  });

  const open = (kind: 'edit' | 'delete') => { if (!post) return; void run(async () => {
    const fresh = await api<RemotePost>(`/api/ripple/remote-posts/${encodeURIComponent(post.id)}/refresh`, 'POST');
    setPost(fresh);
    if (!fresh.capabilities[kind]) throw new Error(fresh.capabilities[`${kind}_reason`] || '当前作品不能执行此操作。');
    setTitle(fresh.title); setBody(fresh.body); setTopics(fresh.topics.join('、'));
    setAction(null); setDialog(kind);
  }); };

  const preview = () => { if (!post || !dialog) return; void run(async () => {
    const changes = dialog === 'edit' ? post.platform === 'x'
      ? { body } : { title, body, topics: topics.split(/[、,，]/).map(value => value.trim().replace(/^#/, '')).filter(Boolean) }
      : {};
    const next = await api<RemotePostAction>(`/api/ripple/remote-posts/${encodeURIComponent(post.id)}/actions`, 'POST',
      { kind: dialog, expected_version: post.version, idempotency_key: newId(), ...changes });
    setAction(next);
  }); };

  const execute = () => { if (!action) return; void run(async () => {
    const next = await api<RemotePostAction>(`/api/ripple/remote-post-actions/${action.id}/execute`, 'POST',
      { confirmed: true, operation_id: action.operation_id, expected_version: action.confirmed_version,
        auth_revision: action.auth_revision });
    setAction(next);
    if (next.status === 'verified') {
      setDialog(null); await load(accountId, offset); setNotice(next.kind === 'delete' ? '已取得目标删除证据并重新核对。' : '修改内容和媒体已重新核对。');
    } else setNotice(next.status === 'partial' ? '平台只保存了部分文字，请刷新作品后查看差异。'
      : next.status === 'reviewing' ? '修改已提交，审核中；请等待平台回读。'
      : next.status === 'unknown_result' ? '平台结果待核对，不能再次提交；请使用只读核对。' : next.reason || '操作状态已更新。');
  }); };

  const query = () => { if (!action) return; void run(async () => {
    const next = await api<RemotePostAction>(`/api/ripple/remote-post-actions/${action.id}/query`, 'POST');
    setAction(next);
    if (next.status === 'verified') { setDialog(null); await load(accountId, offset); }
    setNotice(next.status === 'verified' ? '平台结果已核对。'
      : next.status === 'partial' ? '已确认只有部分文字生效，请重新预览未完成的修改。'
      : next.reason || '仍缺少目标绑定证据，保留待核对状态。');
  }); };

  return <>
    <Feedback error={error} notice={notice} />
    <section className="r2-section"><div className="r2-toolbar">
      <label className="r2-field"><span>平台账号</span><select aria-label="平台作品账号" value={accountId} onChange={e => setAccountId(e.target.value)}>
        {choices.map(account => <option key={account.id} value={account.id}>{account.label} · {account.platform === 'x' ? 'X' : '小红书'} · {account.status === 'connected' ? '已连接' : '未连接'}</option>)}
      </select></label>
      <button className="r2-button primary" disabled={busy || !accountId || currentAccount?.status !== 'connected'} onClick={() => synchronize()}>从平台同步</button>
      {sync.next_cursor && <button className="r2-button" disabled={busy} onClick={() => synchronize(true)}>继续加载平台作品</button>}
    </div>
      <p className="r2-muted">{sync.checked_at ? `上次同步：${dateText(sync.checked_at)} · ${sync.complete ? '已读完' : '部分结果，未读完'}` : '尚未同步当前账号。'}平台作品与本地稿件、发布任务历史分开保存。</p>
    </section>
    <div className="r2-publish-layout"><aside className="r2-task-list"><div className="r2-list-caption">平台作品 <span>{total}</span></div>
      {rows.map(item => <button key={item.id} className={`r2-list-item ${post?.id === item.id ? 'active' : ''}`} onClick={() => select(item)} disabled={busy}>
        <div><Mark platform={item.platform} /><strong>{item.title || item.body.slice(0, 50) || item.remote_id}</strong></div>
        <Status status={item.remote_status} label={stateText(item)} /><small>ID：{item.remote_id} · {dateText(item.checked_at)}</small>
      </button>)}
      {rows.length === 0 && <Empty title="暂无已同步作品" description={sync.complete ? '平台当前返回空列表。' : '选择账号并从平台同步一页作品。'} />}
      {total > 30 && <div className="r2-toolbar"><button className="r2-button" disabled={busy || offset === 0} onClick={() => { const next = Math.max(0, offset - 30); setOffset(next); void load(accountId, next); }}>上一页</button><span>{Math.floor(offset / 30) + 1} / {Math.ceil(total / 30)}</span><button className="r2-button" disabled={busy || offset + 30 >= total} onClick={() => { const next = offset + 30; setOffset(next); void load(accountId, next); }}>下一页</button></div>}
    </aside><main className="r2-publish-editor">
      {!post ? <Empty title="选择平台作品" description="同步后查看作品状态、历史任务和当前可执行操作。" /> : <>
        <section className="r2-section"><div className="r2-section-heading"><h2><Mark platform={post.platform} /> {post.title || post.body.slice(0, 60) || post.remote_id}</h2><Status status={post.remote_status} label={stateText(post)} /></div>
          <dl className="r2-review-fields"><dt>账号</dt><dd>{currentAccount?.label || post.account_id}</dd><dt>远端 ID</dt><dd>{post.remote_id}</dd>
            <dt>类型</dt><dd>{post.kind}</dd><dt>平台状态</dt><dd>{post.remote_status}</dd><dt>可见范围</dt><dd>{post.visibility === 'private' ? '仅自己可见' : post.visibility === 'public' ? '公开' : '尚未核实'}</dd>
            <dt>最近读取</dt><dd>{dateText(post.checked_at)}</dd><dt>本地发布任务</dt><dd>{post.task_ids?.length ? post.task_ids.map(id => id.slice(0, 8)).join('、') : '无；可能在平台直接发布'}</dd>
            <dt>版本关联</dt><dd>{post.version_ids?.join('、') || post.remote_id}</dd><dt>媒体</dt><dd>{post.media.length} 个；编辑时保留原媒体</dd></dl>
          {!!post.action_ids?.length && <div><p>平台操作记录</p><div className="r2-toolbar">{post.action_ids.map(id => <button key={id} className="r2-button" disabled={busy} onClick={() => void run(async () => {
            const record = await api<RemotePostAction>(`/api/ripple/remote-post-actions/${id}`);
            setTitle(record.changes.title ?? record.snapshot.title);
            setBody(record.changes.body ?? record.snapshot.body);
            setTopics((record.changes.topics ?? record.snapshot.topics).join('、'));
            setAction(record); setDialog(record.kind);
          })}>{id.slice(0, 8)}</button>)}</div></div>}
          {post.body && <p style={{ whiteSpace: 'pre-wrap' }}>{post.body}</p>}
          {!!post.topics.length && <p>话题：{post.topics.map(topic => `#${topic}`).join(' ')}</p>}
          <div className="r2-toolbar"><button className="r2-button" disabled={busy} onClick={refreshPost}>刷新平台状态</button>
            {post.url && <a className="r2-button" href={post.url} target="_blank" rel="noreferrer">打开作品 ↗</a>}
            <button className="r2-button" disabled={busy || !post.capabilities.edit} title={post.capabilities.edit_reason} onClick={() => open('edit')}>编辑平台作品</button>
            <button className="r2-button danger" disabled={busy || !post.capabilities.delete} title={post.capabilities.delete_reason} onClick={() => open('delete')}>删除平台作品</button>
          </div>
          {!post.capabilities.edit && <p className="r2-muted">编辑限制：{post.capabilities.edit_reason}</p>}
          {!post.capabilities.delete && <p className="r2-muted">删除限制：{post.capabilities.delete_reason}</p>}
        </section>
      </>}
    </main></div>
    {dialog && post && <Modal title={dialog === 'delete' ? '删除平台作品' : '编辑平台作品'} busy={busy} onClose={() => { setDialog(null); setAction(null); }}>
      <p>账号：{currentAccount?.label || post.account_id}<br />作品 ID：{post.remote_id}<br />当前版本：{post.version.slice(0, 12)}<br />摘要：{post.title || post.body.slice(0, 100)}{post.url && <><br /><a href={post.url} target="_blank" rel="noreferrer">打开作品</a></>}</p>
      {dialog === 'edit' && <><label className="r2-field"><span>{post.platform === 'x' ? '原正文' : '原标题'}</span><p>{post.platform === 'x' ? post.body : post.title}</p></label>
        {post.platform === 'xiaohongshu' && <label className="r2-field"><span>新标题</span><input value={title} onChange={e => setTitle(e.target.value)} disabled={!!action} /></label>}
        <label className="r2-field"><span>新正文</span><textarea value={body} onChange={e => setBody(e.target.value)} disabled={!!action} rows={7} /></label>
        {post.platform === 'x' && <p>{countXReply(body).weightedLength} / 280 加权字符</p>}
        {post.platform === 'xiaohongshu' && <label className="r2-field"><span>话题，以顿号分隔</span><input value={topics} onChange={e => setTopics(e.target.value)} disabled={!!action} /></label>}</>}
      {dialog === 'delete' && <p>删除后仍保留本地稿件、发布历史和操作记录。</p>}
      {action && <div className="r2-warning"><strong>{action.kind === 'delete' ? '删除预览' : '修改差异'}</strong>
        {action.kind === 'delete' ? <p>删除账号 {currentAccount?.label || action.account_id} 下的作品 {action.remote_id}</p>
          : <dl className="r2-review-fields">{Object.entries(action.changes).map(([key, value]) => <div key={key}>
              <dt>{key === 'title' ? '标题' : key === 'body' ? '正文' : '话题'}</dt>
              <dd><span>原：{Array.isArray(action.snapshot[key as 'topics']) ? action.snapshot.topics.join('、') : String(action.snapshot[key as 'title' | 'body'] || '')}</span><br />
                <span>改：{Array.isArray(value) ? value.join('、') : value}</span></dd>
            </div>)}</dl>}
        <p>操作状态：{action.status}。操作 ID：{action.operation_id}</p></div>}
      <Feedback error={error} notice={notice} />
      <footer><button className="r2-button" disabled={busy} onClick={() => { setDialog(null); setAction(null); }}>关闭</button>
        {!action && <button className="r2-button primary" disabled={busy || dialog === 'edit' && post.platform === 'x' && !countXReply(body).valid} onClick={preview}>查看操作预览</button>}
        {action?.status === 'preview' && <button className="r2-button primary" disabled={busy} onClick={execute}>确认{action.kind === 'delete' ? '删除' : '保存'}</button>}
        {(action?.status === 'unknown_result' || action?.status === 'reviewing') && <button className="r2-button" disabled={busy} onClick={query}>只读核对结果</button>}
      </footer>
    </Modal>}
  </>;
}
