import { useState, useEffect, useCallback, useRef } from 'react';
import { fetchTrends, createIdea } from '../lib/api';
import type { TrendGroup } from '../lib/api';
import { IconFire, IconRefresh, IconBookmark, IconCheck, IconSkills } from './icons';
import { api as rippleApi, xhsFeed, xhsSearch } from '../lib/ripple';
import type { Account, XhsNoteSummary } from '../lib/ripple';
import type { SessionWorkScope } from '../lib/store';
import { sampleMetricSummary } from '../lib/creationIntent';
import { platformDisplayName } from '../lib/platforms';
import { TREND_PLATFORMS, ALL_TREND_KEYS, loadTrendSelection, saveTrendSelection } from '../lib/trendPrefs';
import { PlatformIcon } from './PlatformBrand';

interface TrendsPageProps {
  workScope?: SessionWorkScope;
  onOpenIdeas: (seed: { trend_title: string; trend_platform: string }) => void;
  onBreakdown: (seed: string) => void;
}

export default function TrendsPage({ workScope, onOpenIdeas, onBreakdown }: TrendsPageProps) {
  const [selected, setSelected] = useState<string[]>(() => workScope?.platform && ALL_TREND_KEYS.includes(workScope.platform as never) ? [workScope.platform] : loadTrendSelection());
  const [groups, setGroups] = useState<TrendGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [updated, setUpdated] = useState(0);
  const [saved, setSaved] = useState<Set<string>>(new Set());
  const [xhsAccounts, setXhsAccounts] = useState<Account[]>([]);
  const [xhsCandidate, setXhsCandidate] = useState('');
  const [xhsLoading, setXhsLoading] = useState(false);
  const [xhsOpsLoading, setXhsOpsLoading] = useState(false);
  const [xhsQuery, setXhsQuery] = useState('');
  const [xhsOpsItems, setXhsOpsItems] = useState<XhsNoteSummary[]>([]);
  const [xhsOpsScope, setXhsOpsScope] = useState('');
  const loadSequence = useRef(0);
  const sampleSequence = useRef(0);

  const save = async (title: string, source: string) => {
    if (saved.has(title)) return;
    try {
      await createIdea({ title, source, status: 'pending', target_platforms: workScope?.platform ? [workScope.platform] : [] });
      setSaved((prev) => new Set(prev).add(title));
    } catch { /* keep the trend view usable if the idea store is temporarily busy */ }
  };

  const load = useCallback((pfs: string[], force = false) => {
    const sequence = ++loadSequence.current;
    if (pfs.length === 0) { setGroups([]); setLoading(false); setError(''); return; }
    setLoading(true); setError('');
    fetchTrends(pfs.join(','), 15, { refresh: force })
      .then((d) => { if (sequence === loadSequence.current) { setGroups(d.trends); setUpdated(d.updated); } })
      .catch((e) => { if (sequence === loadSequence.current) setError(e instanceof Error ? e.message : '热点服务请求失败。'); })
      .finally(() => { if (sequence === loadSequence.current) setLoading(false); });
  }, []);

  useEffect(() => { load(selected); }, [load, selected]);
  useEffect(() => { saveTrendSelection(selected); }, [selected]);
  useEffect(() => {
    if (workScope?.platform && ALL_TREND_KEYS.includes(workScope.platform as never)) {
      const platform = workScope.platform;
      setSelected((current) => current.length === 1 && current[0] === platform ? current : [platform]);
    }
  }, [workScope?.platform, workScope?.accountId]);
  useEffect(() => {
    rippleApi<Account[]>('/api/ripple/accounts')
      .then((rows) => {
        const connected = rows.filter((a) => a.platform === 'xiaohongshu' && a.status === 'connected' && a.adapter !== 'aitoearn-rest');
        setXhsAccounts(connected);
        setXhsCandidate((current) => connected.some((row) => row.id === current) ? current : (connected[0]?.id || ''));
      })
      .catch(() => {});
  }, []);
  useEffect(() => {
    if (workScope?.platform !== 'xiaohongshu' || workScope.targetKind !== 'account') return;
    setXhsCandidate(xhsAccounts.some((account) => account.id === workScope.accountId) ? workScope.accountId : '');
  }, [workScope?.platform, workScope?.targetKind, workScope?.accountId, xhsAccounts]);
  useEffect(() => {
    // 切换账号后丢弃旧请求的结果，避免把上个账号的样本用于当前账号。
    sampleSequence.current += 1;
    setXhsOpsItems([]); setXhsOpsScope(''); setXhsOpsLoading(false);
  }, [xhsCandidate]);

  const readXiaohongshu = async (accountId?: string) => {
    if (xhsLoading) return;
    setXhsLoading(true); setError('');
    try {
      const data = await fetchTrends('xiaohongshu', 15, {
        refresh: true, xiaohongshuProbe: !accountId, xiaohongshuAccountId: accountId,
      });
      const group = data.trends[0];
      if (group) {
        setGroups((prev) => prev.some((g) => g.platform === 'xiaohongshu')
          ? prev.map((g) => g.platform === 'xiaohongshu' ? group : g)
          : [...prev, group]);
        if (data.updated) setUpdated((prev) => Math.max(prev, data.updated));
      }
    } catch (e) { setError(e instanceof Error ? e.message : '小红书热点读取失败。'); }
    finally { setXhsLoading(false); }
  };

  const runXhsOps = async (mode: 'feed' | 'search') => {
    if (xhsOpsLoading) return;
    if (!xhsCandidate) { setError('请先在「账号与平台」连接一个小红书账号。'); return; }
    if (mode === 'search' && !xhsQuery.trim()) { setError('请输入要搜索的小红书关键词。'); return; }
    const sequence = ++sampleSequence.current;
    setXhsOpsLoading(true); setError('');
    try {
      const data = mode === 'feed'
        ? await xhsFeed(xhsCandidate, 16)
        : await xhsSearch(xhsCandidate, xhsQuery.trim(), 16);
      if (sequence !== sampleSequence.current) return;
      setXhsOpsItems(data.items || []);
      setXhsOpsScope(mode === 'feed' ? '当前账号的个性化推荐样本' : `关键词「${xhsQuery.trim()}」搜索样本`);
    } catch (e) { if (sequence === sampleSequence.current) setError(e instanceof Error ? e.message : '小红书内容读取失败。'); }
    finally { if (sequence === sampleSequence.current) setXhsOpsLoading(false); }
  };

  const breakdown = (item: XhsNoteSummary) => {
    onBreakdown(JSON.stringify({
      type: 'xhs-note', accountId: xhsCandidate, noteId: item.note_id,
      url: item.url, title: item.title, author: item.author || '', scope: xhsOpsScope,
    }));
  };

  const toggle = (key: string) => setSelected((prev) => {
    const next = prev.includes(key) ? prev.filter((x) => x !== key) : [...prev, key];
    saveTrendSelection(next); return next;
  });

  return (
    <div className="page-scroll trends-page">
      <div className="page-head">
        <div>
          <h1 className="page-title"><IconFire size={22} /> 热点雷达</h1>
          <p className="page-subtitle">
            多平台热榜 + 小红书运营采样。小红书热点优先读取公开聚合榜；账号推荐流和关键词搜索单独展示，不把个性化推荐冒充全平台趋势。
            {updated > 0 && <span style={{ color: 'var(--text-tertiary)' }}> · {new Date(updated * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })} 更新</span>}
          </p>
        </div>
        <button className="btn btn-sm" onClick={() => load(selected, true)} disabled={loading}>
          <IconRefresh size={14} /> {loading ? '刷新中…' : '刷新热榜'}
        </button>
      </div>

      <div className="trend-platforms">
        {workScope && <span className="r2-muted">当前账号：{workScope.accountLabel} · {platformDisplayName(workScope.platform)}</span>}
        <button className={`chip ${selected.length === ALL_TREND_KEYS.length ? 'active' : ''}`}
          onClick={() => setSelected((prev) => {
            const next = prev.length === ALL_TREND_KEYS.length ? [] : [...ALL_TREND_KEYS];
            saveTrendSelection(next); return next;
          })}>全部</button>
        {TREND_PLATFORMS.map((p) => <button key={p.key} className={`chip trend-platform-chip ${selected.includes(p.key) ? 'active' : ''}`} onClick={() => toggle(p.key)}><PlatformIcon platform={p.key} size={13} />{p.label}</button>)}
      </div>

      {error && <div className="notice-error">{error}</div>}

      <section className="xhs-radar-panel card">
        <div className="xhs-radar-head">
          <div className="xhs-radar-brand"><PlatformIcon platform="xiaohongshu" size={18} /><div><strong>小红书运营采样</strong><small>使用所选已连接账号的独立登录 Profile，只读推荐流或关键词结果；不会点赞、收藏、评论或发布。</small></div></div>
          <select aria-label="小红书运营采样账号" value={xhsCandidate} onChange={(e) => { setXhsCandidate(e.target.value); setXhsOpsItems([]); setXhsOpsScope(''); }}>
            <option value="">选择已连接小红书账号</option>
            {xhsAccounts.map((a) => <option value={a.id} key={a.id}>{a.label}{a.identity?.name ? ` · @${a.identity.name}` : ''}</option>)}
          </select>
        </div>
        <div className="xhs-radar-tools">
          <button className="btn btn-sm" disabled={xhsOpsLoading || !xhsCandidate} onClick={() => void runXhsOps('feed')}>{xhsOpsLoading ? '读取中…' : '读取推荐流样本'}</button>
          <div className="xhs-search-box"><input value={xhsQuery} onChange={(e) => setXhsQuery(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') void runXhsOps('search'); }} placeholder="搜索小红书关键词，例如：AI 旅行图" /><button className="btn btn-sm" disabled={xhsOpsLoading || !xhsCandidate || !xhsQuery.trim()} onClick={() => void runXhsOps('search')}>搜索笔记</button></div>
        </div>
        {xhsOpsScope && <div className="xhs-sample-scope">样本范围：{xhsOpsScope} · {xhsOpsItems.length} 条。这里只代表本次可见样本。</div>}
        {xhsOpsItems.length > 0 && <div className="xhs-sample-list">
          {xhsOpsItems.map((item, index) => {
            const metrics = sampleMetricSummary(item.metrics || {});
            return <div className="xhs-sample-row" key={`${item.note_id}-${index}`}>
            <span className="trend-rank">{index + 1}</span>
            <div className="xhs-sample-main"><a href={item.url || undefined} target="_blank" rel="noreferrer">{item.title}</a><small>{item.author ? `@${item.author}` : '作者信息未读取'} · {metrics.text}</small><small>指标完整度 {metrics.known}/{metrics.total} · 未读取的数据保持未知</small></div>
            <button className="trend-save" title="存入选题库" onClick={() => void save(item.title, xhsOpsScope)}>{saved.has(item.title) ? <IconCheck size={14} /> : <IconBookmark size={14} />}</button>
            <button className="trend-use" onClick={() => breakdown(item)}><IconSkills size={12} /> 分析内容</button>
            <button className="trend-use" title="围绕此热点找选题" onClick={() => onOpenIdeas({ trend_title: item.title, trend_platform: 'xiaohongshu' })}>找选题</button>
          </div>; })}
        </div>}
        {!xhsCandidate && <div className="trend-empty-hint">连接小红书账号后可读取推荐流、关键词搜索、自己的近期作品和评论。Ripple 不复用日常浏览器登录态。</div>}
      </section>

      <div className="trend-grid">
        {groups.map((g) => (
          <div key={g.platform} className="card trend-col">
            <div className="trend-col-head"><span className="trend-platform-brand"><PlatformIcon platform={g.platform} size={16} />{g.label}<span className="trend-count">{g.items.length}</span></span><span className={`trend-source ${g.status === 'error' && g.attempts.some((a) => a.status === 'on_demand') ? 'trend-source-stale' : `trend-source-${g.status}`}`} title={g.attempts.map((a) => `${a.provider}: ${a.status}`).join('\n')}>{g.status === 'fresh' ? g.source : g.status === 'stale' ? `缓存 · ${g.source}` : g.attempts.some((a) => a.status === 'on_demand') ? '按需读取' : '数据源异常'}</span></div>
            {g.status === 'stale' && <div className="trend-state-note">当前源刷新失败，显示 {new Date(g.fetched_at * 1000).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })} 的缓存。</div>}
            <div className="trend-list">
              {g.items.length === 0 && !loading && <div className="trend-empty"><div>{g.error || '当前没有可用数据。'}</div>{g.platform === 'xiaohongshu' && <div className="trend-xhs-auth"><button className="btn btn-sm" disabled={xhsLoading} onClick={() => void readXiaohongshu()}>{xhsLoading ? '读取中…' : '重新获取公开热点'}</button>{xhsAccounts.length > 0 && <button className="btn btn-sm" disabled={xhsLoading || !xhsCandidate} onClick={() => void readXiaohongshu(xhsCandidate)}>使用所选账号只读获取</button>}</div>}{g.platform === 'xiaohongshu' && <div className="trend-empty-hint">公开聚合源失败时会保留最近成功缓存；已连接账号只用于你明确发起的只读读取。</div>}</div>}
              {g.items.map((it, i) => <div key={i} className="trend-item"><span className={`trend-rank ${i < 3 ? 'top' : ''}`}>{i + 1}</span><div className="trend-main"><a className="trend-title" href={it.url || undefined} target="_blank" rel="noreferrer" title={it.title}>{it.title}</a>{it.hot && <span className="trend-hot">{it.hot}</span>}</div><button className="trend-save" title={saved.has(it.title) ? '已收藏到选题库' : '收藏到选题库'} onClick={() => void save(it.title, `${g.label}热搜`)}>{saved.has(it.title) ? <IconCheck size={14} /> : <IconBookmark size={14} />}</button><button className="trend-use" title="围绕此热点找选题" onClick={() => onOpenIdeas({ trend_title: it.title, trend_platform: g.platform })}>找选题</button></div>)}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
