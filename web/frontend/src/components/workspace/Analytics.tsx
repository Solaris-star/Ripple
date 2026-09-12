import { useEffect, useMemo, useState } from 'react';
import { api, errorText, xhsNotes } from '../../lib/ripple';
import type { Account, Task, XhsNoteSummary } from '../../lib/ripple';
import { Header, Feedback, Empty, Mark } from './Common';

function noteIdFromUrl(value?: string | null): string {
  const match = String(value || '').match(/\/(?:explore|discovery\/item|item)\/([0-9A-Za-z]+)/);
  return match?.[1] || '';
}

function metric(value: number | null | undefined): string {
  return typeof value === 'number' ? value.toLocaleString() : '—';
}

export default function Analytics() {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [xhsAccountId, setXhsAccountId] = useState('');
  const [xhsNotesData, setXhsNotesData] = useState<XhsNoteSummary[]>([]);
  const [xhsFetchedAt, setXhsFetchedAt] = useState('');
  const [xhsLoading, setXhsLoading] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    Promise.all([
      api<{ items: Task[]; real_counts: Record<string, number> }>('/api/ripple/tasks'),
      api<Account[]>('/api/ripple/accounts'),
    ]).then(([taskData, accountData]) => {
      setTasks(taskData.items); setCounts(taskData.real_counts);
      const xhs = accountData.filter((row) => row.platform === 'xiaohongshu' && row.status === 'connected' && row.adapter !== 'aitoearn-rest');
      setAccounts(xhs); setXhsAccountId(xhs[0]?.id || '');
    }).catch((e) => setError(errorText(e)));
  }, []);

  const platforms = [...new Set(tasks.filter((task) => task.content.mode === 'real').map((task) => task.content.platform))];
  const publishedXhs = useMemo(() => tasks.filter((task) => task.content.mode === 'real' && task.content.platform === 'xiaohongshu' && task.status === 'published'), [tasks]);

  const refreshXhs = async () => {
    if (!xhsAccountId || xhsLoading) return;
    setXhsLoading(true); setError('');
    try {
      const data = await xhsNotes(xhsAccountId, 30);
      setXhsNotesData(data.items || []); setXhsFetchedAt(data.fetched_at || new Date().toISOString());
    } catch (e) { setError(errorText(e)); }
    finally { setXhsLoading(false); }
  };

  const linkedTask = (note: XhsNoteSummary): { task: Task; strength: 'id' | 'title' } | undefined => {
    const byId = publishedXhs.find((task) => noteIdFromUrl(task.receipt?.public_url) === note.note_id);
    if (byId) return { task: byId, strength: 'id' };
    const byTitle = publishedXhs.find((task) => task.content.account_id === xhsAccountId && task.content.title.trim() === note.title.trim());
    return byTitle ? { task: byTitle, strength: 'title' } : undefined;
  };

  return <div className="page-scroll r2-page">
    <Header title="发布记录" subtitle="真实发布回执与平台读取到的作品指标分开记录；未读取到的指标显示为 —，不会补零。" />
    <Feedback error={error} />
    <div className="r2-metrics"><div><span>已人工核对发布</span><strong>{counts.published || 0}</strong></div><div><span>提交后待核对</span><strong>{counts.accepted || 0}</strong></div><div><span>结果未知</span><strong>{counts.unknown_result || 0}</strong></div></div>
    <section className="r2-section"><div className="r2-section-heading"><h2>渠道执行记录</h2></div>{platforms.length === 0 ? <Empty title="暂无真实账号发布任务" description="创建真实发布任务后，会在这里汇总。" /> : <table className="r2-table"><thead><tr><th>渠道</th><th>本页任务数</th><th>已核对发布</th><th>待核对</th></tr></thead><tbody>{platforms.map((platform) => { const rows = tasks.filter((task) => task.content.platform === platform && task.content.mode === 'real'); return <tr key={platform}><td><Mark platform={platform} /> {platform}</td><td>{rows.length}</td><td>{rows.filter((task) => task.status === 'published').length}</td><td>{rows.filter((task) => ['accepted', 'unknown_result'].includes(task.status)).length}</td></tr>; })}</tbody></table>}</section>

    <section className="r2-section"><div className="r2-section-heading"><div><h2>小红书作品表现</h2><p className="r2-muted">从所选已连接账号的创作者作品页按需读取。关联发布任务优先使用作品 ID，标题只作兜底。</p></div><div className="r2-analytics-xhs-tools"><select value={xhsAccountId} onChange={(e) => { setXhsAccountId(e.target.value); setXhsNotesData([]); setXhsFetchedAt(''); }}><option value="">选择小红书账号</option>{accounts.map((row) => <option key={row.id} value={row.id}>{row.label}{row.identity?.name ? ` · @${row.identity.name}` : ''}</option>)}</select><button className="r2-button" disabled={!xhsAccountId || xhsLoading} onClick={() => void refreshXhs()}>{xhsLoading ? '读取中…' : '刷新作品数据'}</button></div></div>
      {!xhsAccountId && <Empty title="暂无已连接小红书账号" description="先在「账号与平台」完成小红书账号连接。" />}
      {xhsAccountId && xhsNotesData.length === 0 && !xhsLoading && <Empty title="尚未读取平台指标" description="点击“刷新作品数据”后，Ripple 会读取当前账号近期作品的可见指标。" />}
      {xhsNotesData.length > 0 && <><div className="r2-analytics-fetch-time">本次采样：{xhsFetchedAt ? new Date(xhsFetchedAt).toLocaleString('zh-CN') : '刚刚'} · {xhsNotesData.length} 篇</div><div className="r2-table-scroll"><table className="r2-table"><thead><tr><th>作品</th><th>浏览</th><th>点赞</th><th>收藏</th><th>评论</th><th>分享</th><th>Ripple 发布关联</th></tr></thead><tbody>{xhsNotesData.map((note) => { const link = linkedTask(note); return <tr key={note.note_id || note.title}><td><div className="r2-table-title">{note.title}</div>{note.url && <a className="r2-text-button" href={note.url} target="_blank" rel="noreferrer">打开作品 ↗</a>}</td><td>{metric(note.metrics.views)}</td><td>{metric(note.metrics.likes)}</td><td>{metric(note.metrics.collects)}</td><td>{metric(note.metrics.comments)}</td><td>{metric(note.metrics.shares)}</td><td>{link ? <span title={link.task.id}>{link.strength === 'id' ? '已关联' : '标题可能关联'} · {link.task.id.slice(0, 8)}</span> : <span className="r2-muted">未关联</span>}</td></tr>; })}</tbody></table></div></>}
    </section>
  </div>;
}
