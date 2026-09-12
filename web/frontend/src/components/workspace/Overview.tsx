import { useCallback, useEffect, useState } from 'react';
import type { Page } from '../Sidebar';
import { api, dateText, errorText, ACCOUNT_LABELS } from '../../lib/ripple';
import type { Task, Account, Mother } from '../../lib/ripple';
import type { PersonaItem } from '../../lib/api';
import { Header, Feedback, Empty, Mark, Status } from './Common';

interface OverviewProps {
  onNavigate: (page: Page) => void;
  personas: PersonaItem[];
  selectedPersona: string;
  onPersonaChange: (name: string) => void;
  onNewPersona: () => void;
  onEditPersona: (name: string) => void;
}

export default function Overview({ onNavigate, personas, selectedPersona, onPersonaChange, onNewPersona, onEditPersona }: OverviewProps) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [contents, setContents] = useState<Mother[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    const [t, a, c] = await Promise.all([api<{ items: Task[]; counts: Record<string, number> }>('/api/ripple/tasks'), api<Account[]>('/api/ripple/accounts'), api<Mother[]>('/api/ripple/contents')]);
    setTasks(t.items); setCounts(t.counts); setAccounts(a); setContents(c); setLoaded(true); setError('');
  }, []);
  useEffect(() => { void refresh().catch(e => setError(errorText(e))); }, [refresh]);
  const openTask = (id: string) => { sessionStorage.setItem('ripple_task_focus', id); onNavigate('publish'); };
  const attention = tasks.filter(t => ['unknown_result', 'verification_required', 'accepted', 'failed_retryable'].includes(t.status));
  return <div className="page-scroll r2-page"><Header title="工作概览" subtitle="内容、排期和账号状态"><button className="r2-button" onClick={() => void refresh().catch(e => setError(errorText(e)))}>刷新</button><button className="r2-button primary" onClick={() => onNavigate('contents')}>新建内容</button></Header>
    <Feedback error={error} />
    <section className="r2-persona-card" aria-label="当前账号画像">
      <div>
        <h2>当前账号画像</h2>
        <p>{selectedPersona ? `正在使用「${selectedPersona}」。热点推荐、AI 选题和对话会继承这个画像。` : '还没有选择画像。创建后，热点推荐、AI 选题和对话会结合账号定位。'}</p>
      </div>
      <div className="r2-persona-controls">
        <select aria-label="首页账号画像" value={selectedPersona} onChange={(e) => onPersonaChange(e.target.value)}>
          <option value="">通用模式</option>
          {personas.map((persona) => <option key={persona.name} value={persona.name}>{persona.name}</option>)}
        </select>
        {selectedPersona && <button className="r2-button" onClick={() => onEditPersona(selectedPersona)}>编辑画像</button>}
        <button className="r2-button primary" onClick={onNewPersona}>+ 创建账号画像</button>
      </div>
    </section>
    <div className="r2-metrics">{[
      ['内容', contents.length], ['待审核', (counts.draft || 0) + (counts.review_ready || 0)], ['待执行', (counts.approved || 0) + (counts.scheduled || 0)], ['待核对', (counts.accepted || 0) + (counts.unknown_result || 0) + (counts.verification_required || 0)],
    ].map(([label, value]) => <div key={label}><span>{label}</span><strong>{loaded ? value : '—'}</strong></div>)}</div>
    {attention.length > 0 && <section className="r2-section"><div className="r2-section-heading"><h2>需要处理</h2><span>{attention.length} 条</span></div>{attention.slice(0, 5).map(t => <button className="r2-attention-row" key={t.id} onClick={() => openTask(t.id)}><Mark platform={t.content.platform} /><strong>{t.content.title}</strong><span>{t.events.at(-1)?.note}</span><Status status={t.status} /></button>)}</section>}
    <section className="r2-section"><div className="r2-section-heading"><h2>最近内容版本</h2><button className="r2-text-button" onClick={() => onNavigate('publish')}>打开发布工作台 →</button></div>
      <div className="r2-table-wrap"><table className="r2-table"><thead><tr><th>内容</th><th>目标账号</th><th>状态</th><th>更新时间</th></tr></thead><tbody>{tasks.slice(0, 8).map(t => <tr key={t.id}><td><button className="r2-table-title" onClick={() => openTask(t.id)}><Mark platform={t.content.platform} /><span>{t.content.title}<small>平台版本 V{t.version}{t.content.mode !== 'real' ? ` · ${t.content.mode === 'blog' ? '本地导出' : '模拟'}` : ''}</small></span></button></td><td>{accounts.find(a => a.id === t.content.account_id)?.label || (t.content.mode === 'real' ? '尚未选择' : '本地')}</td><td><Status status={t.status} /></td><td className="r2-muted">{dateText(t.updated_at)}</td></tr>)}</tbody></table></div>
      {loaded && tasks.length === 0 && <Empty title="还没有内容版本" description="先建立一份主稿，再为不同平台创建版本。"><button className="r2-button" onClick={() => onNavigate('contents')}>创建内容</button></Empty>}
    </section>
    <div className="r2-two-column"><section className="r2-section"><div className="r2-section-heading"><h2>账号</h2><button className="r2-text-button" onClick={() => onNavigate('channels')}>管理账号 →</button></div>{accounts.length === 0 ? <Empty title="尚未连接账号" description="支持独立扫码登录；每个账号使用自己的浏览器配置。"><button className="r2-button" onClick={() => onNavigate('channels')}>连接账号</button></Empty> : accounts.slice(0, 5).map(a => <div className="r2-compact-row" key={a.id}><Mark platform={a.platform} /><strong>{a.label}</strong><span>{ACCOUNT_LABELS[a.status] || a.status}</span></div>)}</section>
    <section className="r2-section"><div className="r2-section-heading"><h2>近期排期</h2><button className="r2-text-button" onClick={() => onNavigate('calendar')}>打开日历 →</button></div>{tasks.filter(t => t.status === 'scheduled').length === 0 ? <Empty title="暂无待执行排期" description="在内容版本中设置时间，审核后进入发布队列。" /> : tasks.filter(t => t.status === 'scheduled').slice(0, 5).map(t => <button key={t.id} className="r2-attention-row" onClick={() => openTask(t.id)}><strong>{t.content.title}</strong><span>{dateText(t.content.scheduled_at)}</span></button>)}</section></div>
  </div>;
}
