import { useCallback, useEffect, useMemo, useState } from 'react';
import type { Page } from '../Sidebar';
import { fetchSchedule } from '../../lib/api';
import type { ScheduleItem } from '../../lib/api';
import { api, dateText, errorText } from '../../lib/ripple';
import type { CalendarEntry, ContentPlan, Mother, Task } from '../../lib/ripple';
import { newId } from '../../lib/id';
import { Feedback, Header } from './Common';

type Row = { kind: 'plan' | 'publish_task' | 'event'; id: string; title: string; date: string; platform: string; group: string; zone: string; plan?: ContentPlan };
const LABELS: Record<string, string> = { planned: '计划创作', review: '待审核', scheduled: '已定时', published: '已核对发布', attention: '需处理', event: '活动' };
const dayKey = (day: Date) => `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`;
const localDate = (value: string) => { const date = new Date(value); return Number.isNaN(date.valueOf()) ? '' : `${dayKey(date)}T${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`; };
function statusGroup(value: string): string {
  if (value === 'draft') return 'planned';
  if (value === 'review_ready') return 'review';
  if (value === 'scheduled' || value === 'approved') return 'scheduled';
  if (['verified', 'published', 'exported', 'simulated'].includes(value)) return 'published';
  return 'attention';
}

export default function Calendar({ onNavigate }: { onNavigate: (page: Page) => void }) {
  const [entries, setEntries] = useState<CalendarEntry[]>([]);
  const [events, setEvents] = useState<ScheduleItem[]>([]);
  const [contents, setContents] = useState<Mother[]>([]);
  const [error, setError] = useState('');
  const [month, setMonth] = useState(() => dayKey(new Date()).slice(0, 7));
  const [view, setView] = useState<'month' | 'list'>('month');
  const [filter, setFilter] = useState('all');
  const [platform, setPlatform] = useState('all');
  const [dialog, setDialog] = useState<'new' | 'detail' | null>(null);
  const [selected, setSelected] = useState<Row | null>(null);
  const [title, setTitle] = useState('');
  const [when, setWhen] = useState('');
  const [sourceId, setSourceId] = useState('');
  const [busy, setBusy] = useState(false);
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
  const load = useCallback(async () => {
    const [calendar, legacy, mothers] = await Promise.allSettled([api<CalendarEntry[]>('/api/ripple/calendar/entries'), fetchSchedule(), api<Mother[]>('/api/ripple/contents')]);
    if (calendar.status === 'fulfilled') setEntries(calendar.value);
    if (legacy.status === 'fulfilled') setEvents(legacy.value);
    if (mothers.status === 'fulfilled') setContents(mothers.value);
    const failure = [calendar, legacy, mothers].find((row) => row.status === 'rejected');
    setError(failure?.status === 'rejected' ? errorText(failure.reason) : '');
  }, []);
  useEffect(() => { void load(); }, [load]);

  const rows = useMemo<Row[]>(() => {
    const plans = entries.flatMap((entry): Row[] => entry.kind === 'plan'
      ? [{ kind: 'plan', id: entry.id, title: entry.title, date: entry.scheduled_at, platform: '', group: 'planned', zone: entry.timezone, plan: entry }]
      : entry.status === 'cancelled' ? [] : [{ kind: 'publish_task', id: entry.id, title: entry.title, date: entry.scheduled_at, platform: entry.platform, group: statusGroup(entry.status), zone: entry.timezone }]);
    const milestones = events.filter((row) => row.kind === 'event').map((row): Row => ({ kind: 'event', id: row.id, title: row.title, date: `${row.date}T${row.time || '00:00'}:00`, platform: row.platform, group: 'event', zone: timezone }));
    return [...plans, ...milestones].filter((row) => row.date && localDate(row.date).startsWith(month) && (filter === 'all' || row.group === filter) && (platform === 'all' || row.platform === platform)).sort((a, b) => a.date.localeCompare(b.date));
  }, [entries, events, month, filter, platform, timezone]);
  const [year, number] = month.split('-').map(Number);
  const first = new Date(year, number - 1, 1), days = new Date(year, number, 0).getDate(), offset = (first.getDay() + 6) % 7;
  const openNew = (date?: string) => { setSelected(null); setTitle(''); setSourceId(''); setWhen(`${date || dayKey(new Date(Date.now() + 86400000))}T18:30`); setDialog('new'); setError(''); };
  const openRow = (row: Row) => { setSelected(row); setTitle(row.title); setSourceId(row.plan?.source_id || ''); setWhen(row.plan?.scheduled_local || localDate(row.date)); setDialog('detail'); setError(''); };
  const close = () => { setDialog(null); setSelected(null); };
  const save = async () => {
    if (!title.trim() || !when) { setError('请填写计划名称和时间。'); return; }
    setBusy(true); setError('');
    try {
      const current = selected?.plan;
      const input = { title: title.trim(), scheduled_local: when, timezone: current?.timezone || timezone, fold: current?.fold ?? null, source_id: sourceId, variant_id: current?.variant_id || '' };
      if (current) await api<ContentPlan>(`/api/ripple/plans/${encodeURIComponent(current.id)}`, 'PUT', { ...input, expected_version: current.version });
      else await api<ContentPlan>('/api/ripple/plans', 'POST', { ...input, idempotency_key: newId() });
      close(); await load();
    } catch (cause) { setError(errorText(cause)); } finally { setBusy(false); }
  };
  const unschedule = async () => {
    if (!selected || selected.kind !== 'publish_task') return;
    setBusy(true); setError('');
    try {
      const task = await api<Task>(`/api/ripple/tasks/${encodeURIComponent(selected.id)}`);
      await api(`/api/ripple/tasks/${encodeURIComponent(task.id)}/return-to-plan`, 'POST', { expected_version: task.version_id });
      close(); await load();
    } catch (cause) { setError(errorText(cause)); } finally { setBusy(false); }
  };
  const focusTask = (id: string) => { sessionStorage.setItem('ripple_task_focus', id); onNavigate('publish'); };
  const platforms = ['all', ...new Set(entries.filter((entry) => entry.kind === 'publish_task').map((entry) => entry.platform))];
  const rowButton = (row: Row) => <button key={`${row.kind}:${row.id}`} className={`focus-calendar-event ${row.group}`} onClick={() => openRow(row)}><strong>{row.title}</strong><span>{LABELS[row.group] || row.group}</span></button>;

  return <div className="page-scroll r2-page focus-calendar-page">
    <Header title="创作与发布安排" subtitle="计划只保留时间；确认定时后才进入发布任务。"><button className="r2-button primary" onClick={() => openNew()}>＋ 添加计划</button><button className="r2-button" onClick={() => onNavigate('planning')}>选题日历</button></Header>
    <Feedback error={error} />
    <div className="focus-calendar-toolbar"><div><button aria-label="上个月" onClick={() => setMonth(dayKey(new Date(year, number - 2, 1)).slice(0, 7))}>‹</button><label>月份<input aria-label="日历月份" type="month" value={month} onChange={(event) => event.target.value && setMonth(event.target.value)} /></label><button aria-label="下个月" onClick={() => setMonth(dayKey(new Date(year, number, 1)).slice(0, 7))}>›</button></div><div><select aria-label="筛选状态" value={filter} onChange={(event) => setFilter(event.target.value)}>{[['all', '全部状态'], ...Object.entries(LABELS)].map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><select aria-label="筛选平台" value={platform} onChange={(event) => setPlatform(event.target.value)}>{platforms.map((value) => <option key={value} value={value}>{value === 'all' ? '全部平台' : value}</option>)}</select><button onClick={() => setView(view === 'month' ? 'list' : 'month')}>{view === 'month' ? '列表视图' : '月视图'}</button></div></div>
    <div className="focus-calendar-legend">{Object.entries(LABELS).map(([value, label]) => <span key={value} className={value}><i />{label}</span>)}<span>按浏览器本地时区显示；详情保留原时区。</span></div>
    <div className={`focus-calendar-grid ${view === 'list' ? 'list-hidden' : ''}`}><div className="focus-calendar-weekdays">{['一', '二', '三', '四', '五', '六', '日'].map((day) => <span key={day}>{day}</span>)}</div><div className="focus-calendar-days">{Array.from({ length: Math.ceil((offset + days) / 7) * 7 }, (_, index) => { const day = index - offset + 1; if (day < 1 || day > days) return <div className="focus-calendar-day outside" key={index} />; const key = `${month}-${String(day).padStart(2, '0')}`; return <div key={key} className={`focus-calendar-day ${key === dayKey(new Date()) ? 'today' : ''}`}><button className="focus-day-number" onClick={() => openNew(key)} aria-label={`在 ${key} 添加计划`}>{day}</button>{rows.filter((row) => localDate(row.date).slice(0, 10) === key).map(rowButton)}</div>; })}</div></div>
    <div className={`focus-calendar-list ${view === 'list' ? 'show' : ''}`}>{rows.length ? rows.map((row) => <button key={`${row.kind}:${row.id}`} className="focus-calendar-list-row" onClick={() => openRow(row)}><time>{dateText(row.date)}</time><strong>{row.title}</strong><span>{row.platform || '创作计划'}</span><em className={row.group}>{LABELS[row.group] || row.group}</em></button>) : <p className="focus-history-empty">本月没有符合条件的安排。</p>}</div>
    {dialog && <div className="focus-session-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) close(); }}><section className="focus-session-dialog focus-plan-dialog" role="dialog" aria-modal="true" aria-label={selected?.kind === 'plan' || dialog === 'new' ? '编辑创作计划' : '查看安排'}><h2>{dialog === 'new' ? '添加创作计划' : selected?.kind === 'plan' ? '编辑创作计划' : '查看安排'}</h2>
      {dialog === 'new' || selected?.kind === 'plan' ? <><p>保存计划不会触发自动发布。</p><label>计划名称<input aria-label="计划名称" value={title} maxLength={200} onChange={(event) => setTitle(event.target.value)} /></label><label>计划时间<input aria-label="计划时间" type="datetime-local" value={when} onChange={(event) => setWhen(event.target.value)} /></label><label>关联主稿（可选）<select aria-label="关联主稿" value={sourceId} onChange={(event) => setSourceId(event.target.value)}><option value="">暂不关联</option>{contents.map((item) => <option value={item.id} key={item.id}>{item.content.title}</option>)}</select></label><p>时区：{selected?.plan?.timezone || timezone}</p></>
        : <><strong>{selected?.title}</strong><p>{selected?.group === 'scheduled' ? '已确认定时。取消后将保留创作计划和原任务记录。' : selected?.group === 'attention' ? '先核对任务结果，不能直接重新发送。' : `状态：${selected ? LABELS[selected.group] || selected.group : ''}`}</p><p>{when.replace('T', ' ')} · {selected?.zone}</p></>}
      {error && <p className="focus-session-error" role="alert">{error}</p>}
      <div className="focus-dialog-actions"><button disabled={busy} onClick={close}>关闭</button>{dialog === 'new' || selected?.kind === 'plan' ? <button className="primary" disabled={busy} onClick={() => void save()}>保存计划</button> : selected?.kind === 'publish_task' ? <><button disabled={busy || selected.group !== 'scheduled'} onClick={() => void unschedule()}>取消定时，保留计划</button><button className="primary" onClick={() => focusTask(selected.id)}>查看发布任务</button></> : null}</div>
    </section></div>}
  </div>;
}
