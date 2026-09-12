import { useCallback, useEffect, useState } from 'react';
import type { Page } from '../Sidebar';
import { api, dateText, errorText } from '../../lib/ripple';
import type { CalendarItem } from '../../lib/ripple';
import { Header, Feedback, Mark, Status, Empty } from './Common';

export default function Calendar({ onNavigate }: { onNavigate: (page: Page) => void }) {
  const [rows, setRows] = useState<CalendarItem[]>([]);
  const [error, setError] = useState('');
  const [month, setMonth] = useState(() => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`; });
  const load = useCallback(async () => { setRows(await api<CalendarItem[]>('/api/ripple/calendar')); setError(''); }, []);
  useEffect(() => { void load().catch(e => setError(errorText(e))); }, [load]);
  const [year, m] = month.split('-').map(Number);
  const first = new Date(year, m - 1, 1), days = new Date(year, m, 0).getDate();
  const offset = (first.getDay() + 6) % 7;
  const dateKey = (date: Date) => `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  const shown = rows.filter(r => dateKey(new Date(r.scheduled_at)).startsWith(month)).sort((a, b) => a.scheduled_at.localeCompare(b.scheduled_at));
  const open = (id: string) => { sessionStorage.setItem('ripple_task_focus', id); onNavigate('publish'); };
  return <div className="page-scroll r2-page"><Header title="发布日历" subtitle="同一份任务数据，按浏览器本地时区展示"><button className="r2-button" onClick={() => onNavigate('planning')}>选题日历</button><label className="r2-field compact">月份<input aria-label="日历月份" type="month" value={month} onChange={e => { if (e.target.value) setMonth(e.target.value); }} /></label><button className="r2-button" onClick={() => void load().catch(e => setError(errorText(e)))}>刷新</button></Header><Feedback error={error} />
    <div className="r2-calendar"><div className="r2-week-header">{['一', '二', '三', '四', '五', '六', '日'].map(d => <span key={d}>{d}</span>)}</div><div className="r2-days">{Array.from({ length: Math.ceil((offset + days) / 7) * 7 }, (_, i) => { const day = i - offset + 1; if (day < 1 || day > days) return <div className="r2-day outside" key={i} />; const key = `${month}-${String(day).padStart(2, '0')}`; return <div className={`r2-day ${key === dateKey(new Date()) ? 'today' : ''}`} key={i}><span>{day}</span>{shown.filter(r => dateKey(new Date(r.scheduled_at)) === key).map(r => <button title={`${r.title} · ${r.account}`} onClick={() => open(r.id)} key={r.id}><Mark platform={r.platform} /><span>{r.title}</span></button>)}</div>; })}</div></div>
    <section className="r2-section"><div className="r2-section-heading"><h2>本月排期</h2><span>{shown.length} 条</span></div>{shown.length === 0 ? <Empty title="本月暂无排期" description="在内容工作台的平台版本中设置时间并审核后，任务会自动出现在这里。" /> : <table className="r2-table"><thead><tr><th>内容</th><th>计划时间</th><th>账号</th><th>状态</th></tr></thead><tbody>{shown.map(r => <tr key={r.id}><td><button className="r2-text-button" onClick={() => open(r.id)}>{r.title}</button></td><td>{dateText(r.scheduled_at)}<small>原时区 {r.timezone}</small></td><td>{r.account}</td><td><Status status={r.status} /></td></tr>)}</tbody></table>}</section><p className="r2-muted">服务停止期间不会执行任务。重启后的过期排期会暂停，需重新审核；不会自动补发。</p>
  </div>;
}
