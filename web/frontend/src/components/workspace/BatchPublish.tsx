import { useEffect, useRef, useState } from 'react';
import { api, errorText, taskAction } from '../../lib/ripple';
import type { Account, BlogConnector, Channel, Mother, PlatformVariant, PreflightResult, Task } from '../../lib/ripple';
import { newId } from '../../lib/id';
import { platformDisplayName } from '../../lib/platforms';
import { channelCapabilityText, channelPublishAvailable } from '../../lib/channelCapability';
import { platformContentProblems, publishResultText, taskStatusText } from './variantPublishing';
import { Feedback, Modal, Status } from './Common';
import PublishPreview from './PublishPreview';

type Row = { variant: PlatformVariant; checked?: PreflightResult; task?: Task; error?: string; attempted?: boolean };

export default function BatchPublish({ source, onClose, onUpdated }: { source: Mother; onClose: () => void; onUpdated: () => void }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [blogs, setBlogs] = useState<BlogConnector[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [reviewed, setReviewed] = useState<string[]>([]);
  const [externalConsent, setExternalConsent] = useState(false);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState('');
  const gate = useRef(false);
  const dispatching = rows.filter(row => row.task?.status === 'dispatching').map(row => row.task!.id).join(',');
  useEffect(() => {
    if (!dispatching) return;
    let stopped = false, running = false;
    const timer = setInterval(async () => {
      if (running) return;
      running = true;
      try {
        const results = await Promise.allSettled(dispatching.split(',').map(id => api<Task>(`/api/ripple/tasks/${id}`)));
        if (!stopped) setRows(values => values.map(row => {
          const fresh = results.find(result => result.status === 'fulfilled' && result.value.id === row.task?.id);
          return fresh?.status === 'fulfilled' ? { ...row, task: fresh.value } : row;
        }));
      } finally { running = false; }
    }, 1500);
    return () => { stopped = true; clearInterval(timer); };
  }, [dispatching]);
  useEffect(() => {
    let stopped = false;
    void Promise.all([api<{ items: PlatformVariant[] }>(`/api/ripple/contents/${source.id}/variants`), api<Account[]>('/api/ripple/accounts'), api<{ items: BlogConnector[] }>('/api/ripple/blog/connectors'), api<Channel[]>('/api/ripple/channels')])
      .then(([variants, accountRows, blogRows, channelRows]) => { if (!stopped) { setRows(variants.items.map(variant => ({ variant }))); setAccounts(accountRows); setBlogs(blogRows.items); setChannels(channelRows); } })
      .catch(cause => { if (!stopped) setError(errorText(cause)); }).finally(() => { if (!stopped) setBusy(false); });
    return () => { stopped = true; };
  }, [source.id]);
  const update = (id: string, patch: Partial<Row>) => setRows(values => values.map(row => row.variant.id === id ? { ...row, ...patch } : row));
  const check = async () => {
    if (gate.current) return;
    gate.current = true; setBusy(true); setError(''); setReviewed([]); setExternalConsent(false);
    try {
      for (const row of rows.filter(item => selected.includes(item.variant.id))) {
        update(row.variant.id, { checked: undefined, error: '', attempted: false, task: undefined });
        try {
          const variant = await api<PlatformVariant>(`/api/ripple/variants/${row.variant.id}`);
          update(variant.id, { variant });
          const problems = platformContentProblems(variant.platform, variant.content);
          if (problems.length) throw new Error(problems.join('；'));
          const task = await api<Task>(`/api/ripple/variants/${variant.id}/tasks`, 'POST', { expected_version: variant.version_id, idempotency_key: newId() });
          update(variant.id, { task });
          if (!['draft', 'review_ready'].includes(task.status)) throw new Error(`已有任务：${publishResultText(task)}请在发布管理中继续处理。`);
          const checked = await api<PreflightResult>(`/api/ripple/tasks/${task.id}/preflight`, 'POST', { expected_version: task.version_id });
          update(variant.id, { checked, task: checked.task, error: checked.ok ? '' : checked.problems.join('；') });
        } catch (cause) { update(row.variant.id, { error: errorText(cause) }); }
      }
    } finally { gate.current = false; setBusy(false); onUpdated(); }
  };
  const eligible = rows.filter(row => selected.includes(row.variant.id) && reviewed.includes(row.variant.id) && row.checked?.ok && !row.attempted);
  const hasExternal = eligible.some(row => row.checked?.task.review_context?.adapter === 'aitoearn-rest');
  const execute = async () => {
    if (gate.current || !eligible.length || (hasExternal && !externalConsent)) return;
    gate.current = true; setBusy(true); setError('');
    try {
      for (const row of eligible) {
        update(row.variant.id, { attempted: true, error: '' });
        try {
          const task = row.checked!.task;
          const approved = await taskAction(task, 'approve', { confirmed: true, real_publish_confirmed: task.content.mode === 'real', expected_account_revision: task.review_context?.auth_revision, external_service_confirmed: externalConsent });
          update(row.variant.id, { task: approved });
          const result = approved.status === 'approved' ? await taskAction(approved, 'dispatch') : approved;
          update(row.variant.id, { task: result });
        } catch (cause) { update(row.variant.id, { error: `${errorText(cause)} 请在发布管理中核对任务结果后继续。` }); }
      }
    } finally { gate.current = false; setBusy(false); onUpdated(); }
  };
  const label = (row: Row) => row.variant.content.delivery === 'export' ? '本地导出' : (row.variant.platform === 'blog' ? blogs.find(item => item.id === row.variant.content.target_id)?.label : accounts.find(item => item.id === row.variant.content.target_id)?.label) || '未选择账号';
  return <Modal title="多平台发布总览" className="variant-review-dialog" busy={busy} onClose={onClose}>
    <p>{source.content.title}。选择平台后统一检查，再逐个平台核对内容并勾选授权。</p><Feedback error={error} />
    {rows.map(row => {
      const channel = channels.find(item => item.id === row.variant.platform);
      const unavailable = row.variant.content.delivery !== 'export' && !channelPublishAvailable(channel);
      return <section className="r2-section" key={row.variant.id}>
        <label className="r2-check"><input type="checkbox" checked={selected.includes(row.variant.id)} disabled={busy || unavailable || row.attempted} onChange={e => { setSelected(values => e.target.checked ? [...values, row.variant.id] : values.filter(id => id !== row.variant.id)); setReviewed(values => values.filter(id => id !== row.variant.id)); }} /><strong>{platformDisplayName(row.variant.platform)} · {label(row)}</strong></label>
        <p>{row.variant.content.delivery === 'export' ? '导出 Markdown 与素材' : channelCapabilityText(channel)}</p>
        {row.error && <p role="alert" className="r2-warning">{row.error}</p>}
        {row.task && <><Status status={row.task.status} label={taskStatusText(row.task)} /><p>{publishResultText(row.task)}</p></>}
        {row.checked?.ok && selected.includes(row.variant.id) && !row.attempted && <>
          <PublishPreview content={row.checked.task.content} heading={`${platformDisplayName(row.variant.platform)} · ${label(row)}`} />
          <p>{row.checked.task.content.scheduled_at ? `定时执行：${row.checked.task.content.scheduled_local} · ${row.checked.task.content.timezone}` : '确认后立即执行'}</p>
          <label className="r2-check"><input type="checkbox" disabled={busy} checked={reviewed.includes(row.variant.id)} onChange={e => setReviewed(values => e.target.checked ? [...values, row.variant.id] : values.filter(id => id !== row.variant.id))} />我已核对完整内容，确认{row.variant.content.delivery === 'export' ? '导出到本地' : row.variant.platform === 'wechat' && row.variant.content.options.wechat_action !== 'publish' ? `写入「${label(row)}」公众号草稿箱，不公开发布` : `发布到「${label(row)}」`}。</label>
        </>}
      </section>;
    })}
    {hasExternal && <label className="r2-check"><input type="checkbox" checked={externalConsent} disabled={busy} onChange={e => setExternalConsent(e.target.checked)} />确认使用外部服务传输，并了解可能存在独立额度或费用。</label>}
    <footer><button className="r2-button" disabled={busy} onClick={onClose}>关闭</button><button className="r2-button" disabled={busy || !selected.length} onClick={() => void check()}>检查所选平台</button><button className="r2-button primary" disabled={busy || !eligible.length || (hasExternal && !externalConsent)} onClick={() => void execute()}>确认执行已核对的 {eligible.length} 个平台</button></footer>
  </Modal>;
}
