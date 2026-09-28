import { useCallback, useEffect, useMemo, useState } from 'react';
import type { Page } from '../Sidebar';
import { api, dateText, errorText } from '../../lib/ripple';
import type { Task, Account, Mother, ContentPlan } from '../../lib/ripple';
import type { PersonaItem } from '../../lib/api';
import { Feedback } from './Common';
import { taskNeedsAttention } from './variantPublishing';

interface OverviewProps {
  onNavigate: (page: Page) => void;
  personas: PersonaItem[];
  selectedPersona: string;
  onPersonaChange: (name: string) => void;
  onNewPersona: () => void;
  onEditPersona: (name: string) => void;
}

const ACTION: Record<string, string> = { unknown_result: '核对结果', verification_required: '查看核对', accepted: '核对回执', failed_retryable: '查看原因' };

export default function Overview({ onNavigate, personas, selectedPersona, onPersonaChange, onNewPersona, onEditPersona }: OverviewProps) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [contents, setContents] = useState<Mother[]>([]);
  const [plans, setPlans] = useState<ContentPlan[]>([]);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [error, setError] = useState('');
  const refresh = useCallback(async () => {
    const [taskRows, accountRows, contentRows, planRows] = await Promise.all([
      api<{ items: Task[]; counts: Record<string, number> }>('/api/ripple/tasks'),
      api<Account[]>('/api/ripple/accounts'),
      api<Mother[]>('/api/ripple/contents'),
      api<ContentPlan[]>('/api/ripple/plans'),
    ]);
    setTasks(taskRows.items); setCounts(taskRows.counts); setAccounts(accountRows); setContents(contentRows); setPlans(planRows); setError('');
  }, []);
  useEffect(() => { void refresh().catch((cause) => setError(errorText(cause))); }, [refresh]);
  const featured = useMemo(() => [...contents].sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0], [contents]);
  const attention = tasks.filter(taskNeedsAttention);
  const upcoming = [...plans.map((plan) => ({ id: plan.id, title: plan.title, at: plan.scheduled_at, label: '计划创作', kind: 'plan' as const })),
    ...tasks.filter((task) => task.status === 'scheduled').map((task) => ({ id: task.id, title: task.content.title, at: task.content.scheduled_at || '', label: '已定时', kind: 'task' as const }))]
    .filter((item) => item.at).sort((a, b) => a.at.localeCompare(b.at)).slice(0, 4);
  const openTask = (id: string) => { sessionStorage.setItem('ripple_task_focus', id); onNavigate('publish'); };
  const openContent = (id: string) => { sessionStorage.setItem('ripple_content_focus', id); onNavigate('contents'); };
  const today = new Intl.DateTimeFormat('zh-CN', { month: 'long', day: 'numeric', weekday: 'long' }).format(new Date());

  return <div className="page-scroll r2-page focus-overview">
    <Feedback error={error} />
    <header className="focus-overview-heading"><div><span className="focus-kicker">YOUR WORKSPACE</span><h1>今天，从这里继续。</h1><p>{today}{selectedPersona ? ` · ${selectedPersona}` : ' · 通用工作区'}</p></div><button className="focus-primary" onClick={() => onNavigate('contents')}>＋ 开始创作</button></header>

    <section className="focus-overview-feature"><div><span className="focus-kicker">{featured ? `正在创作 · 主稿 V${featured.version}` : '开始创作'}</span><h2>{featured?.content.title || '把下一份想法写成主稿'}</h2><p>{featured ? '主稿和平台版本各自保存，可以接着完成上一次的内容。' : '创建一份主稿，再按需要为不同平台准备版本。'}</p><div><button className="focus-primary" onClick={() => featured ? openContent(featured.id) : onNavigate('contents')}>→ {featured ? '继续编辑' : '创建主稿'}</button>{featured?.variants?.length ? <button className="focus-text-button" onClick={() => openContent(featured.id)}>查看平台版本</button> : null}</div></div><div className="focus-feature-illustration" aria-hidden="true"><span>留一点时间，<br />给日常的观察。</span><i /><small>R / {new Date().getDate()}</small></div></section>

    <div className="focus-overview-columns"><section className="focus-overview-card"><header><h2>需要你处理</h2><span>{attention.length} 项</span></header>{attention.length ? attention.slice(0, 4).map((task) => <button className="focus-overview-row" key={task.id} onClick={() => openTask(task.id)}><span className="focus-row-icon">▤</span><span><strong>{task.content.title}</strong><small>{task.events.at(-1)?.note || task.status}</small></span><em>{ACTION[task.status] || '查看'}</em></button>) : <div className="focus-overview-empty"><strong>目前没有待处理事项</strong><span>需要核对的发布结果会出现在这里。</span></div>}</section>
      <section className="focus-overview-card"><header><h2>接下来的安排</h2><button className="focus-text-button" onClick={() => onNavigate('calendar')}>打开日历 →</button></header>{upcoming.length ? upcoming.map((item) => <button className="focus-overview-row" key={`${item.kind}:${item.id}`} onClick={() => item.kind === 'task' ? openTask(item.id) : onNavigate('calendar')}><time>{dateText(item.at)}</time><span><strong>{item.title}</strong><small>{item.label}</small></span><b>→</b></button>) : <div className="focus-overview-empty"><strong>近期还没有安排</strong><span>日历中的创作计划不会自动发布。</span><button className="focus-text-button" onClick={() => onNavigate('calendar')}>添加计划 →</button></div>}</section></div>

    <footer className="focus-overview-footer"><span>创作记录</span><strong>{contents.length}</strong><span>份主稿</span><strong>{(counts.draft || 0) + (counts.review_ready || 0)}</strong><span>个版本待审核</span><strong>{(counts.published || 0) + (counts.exported || 0)}</strong><span>次发布已核对</span><button className="focus-text-button" onClick={() => onNavigate('publish')}>查看记录 →</button></footer>
    <div className="focus-overview-persona"><span>当前账号画像：{selectedPersona || '通用模式'} · 已连接账号 {accounts.filter((item) => item.status === 'connected').length} 个</span><select aria-label="切换首页账号画像" value={selectedPersona} onChange={(event) => onPersonaChange(event.target.value)}><option value="">通用模式</option>{personas.map((persona) => <option key={persona.name} value={persona.name}>{persona.name}</option>)}</select>{selectedPersona ? <button onClick={() => onEditPersona(selectedPersona)}>编辑画像</button> : <button onClick={onNewPersona}>创建画像</button>}</div>
  </div>;
}
