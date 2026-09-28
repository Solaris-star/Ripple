import { useEffect, useMemo, useRef, useState } from 'react';
import { api, errorText } from '../../lib/ripple';
import { newId } from '../../lib/id';
import type { Account, BlogConnector, Channel, Mother, PlatformVariant } from '../../lib/ripple';
import { channelCapabilityText } from '../../lib/channelCapability';
import type { SessionWorkScope } from '../../lib/store';
import { preferredVariantTargets } from '../../lib/creationIntent';
import { Feedback, Mark, Modal } from './Common';

type Props = {
  source: Mother;
  workScope?: SessionWorkScope;
  targetPlatforms?: string[];
  onClose: () => void;
  onDone: (count: number) => void;
};

export default function VariantBatch({ source, workScope, targetPlatforms = [], onClose, onDone }: Props) {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [blogs, setBlogs] = useState<BlogConnector[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const key = useRef(newId());
  const gate = useRef(false);
  const existing = useMemo(() => new Set((source.variants || []).map(item => `${item.platform}|${item.target_id || ''}`)), [source.variants]);
  const preferred = preferredVariantTargets(channels, accounts, workScope, targetPlatforms);
  const defaultSelection = preferred.filter(target => !existing.has(target)).join('\n');
  useEffect(() => { setSelected(defaultSelection ? defaultSelection.split('\n') : []); }, [defaultSelection]);
  useEffect(() => {
    void Promise.all([api<Channel[]>('/api/ripple/channels'), api<Account[]>('/api/ripple/accounts'), api<{ items: BlogConnector[] }>('/api/ripple/blog/connectors')])
      .then(([channelRows, accountRows, blogRows]) => { setChannels(channelRows); setAccounts(accountRows); setBlogs(blogRows.items); })
      .catch(e => setError(errorText(e)));
  }, []);
  const create = async () => {
    if (gate.current) return;
    gate.current = true; setBusy(true); setError('');
    try {
      const targets = selected.map(key => { const [platform, account_id = ''] = key.split('|'); return { platform, account_id }; });
      const result = await api<{ items: PlatformVariant[] }>(`/api/ripple/contents/${source.id}/variants`, 'POST', {
        expected_source_version: source.version_id, idempotency_key: key.current, targets,
      });
      onDone(result.items.length);
    } catch (e) { setError(errorText(e)); } finally { gate.current = false; setBusy(false); }
  };
  const targets = channels.flatMap(channel => {
      const accountRows = accounts.filter(account => account.platform === channel.id);
      const targets = channel.id === 'blog' ? [
        ...blogs.filter(blog => blog.status === 'connected').map(blog => ({ id: blog.id, label: `Blog · ${blog.label}`, detail: '已连接，将直接使用此 Blog 作为发布目标' })),
        { id: '', label: 'Blog · 本地导出 / 后续连接', detail: '可导出 Markdown 与素材' },
      ] : accountRows.length === 0
        ? [{ id: '', label: channel.id === 'blog' ? 'Blog · 仅平台规划' : `${channel.name} · 仅平台规划`, detail: channel.id === 'blog' ? '后续可连接 Blog 或导出 Markdown' : '后续再选择发布账号' }]
        : [
          ...accountRows.map(account => ({ id: account.id, label: `${channel.name} · ${account.identity?.name || account.label}`, detail: account.status === 'connected' ? '账号已连接' : '账号未连接，仍可先制作版本' })),
          { id: '', label: `${channel.name} · 仅平台规划`, detail: '不绑定具体账号，后续可再选择' },
        ];
      return targets.map(target => ({ ...target, detail: `${channelCapabilityText(channel)} · ${target.detail}`, platform: channel.id, key: `${channel.id}|${target.id}` }));
    });
  const renderTarget = (target: typeof targets[number]) => {
    const already = existing.has(target.key), checked = selected.includes(target.key);
    return <label className={`r2-remote-account ${already ? 'disabled' : ''}`} key={target.key}><input type="checkbox" aria-label={`选择账号版本 ${target.label}`} checked={checked || already} disabled={busy || already || (!checked && selected.length >= 20)} onChange={e => setSelected(current => e.target.checked ? [...current, target.key] : current.filter(id => id !== target.key))} /><Mark platform={target.platform} /><span><strong>{target.label}</strong><small>{already ? '已有账号版本，可返回内容页继续编辑' : target.detail}</small></span></label>;
  };
  const otherTargets = targets.filter(target => !preferred.includes(target.key));
  return <Modal title="创建账号版本" busy={busy} onClose={onClose}><h3>{source.content.title}</h3><p>母稿 V{source.version}。{workScope ? `默认延续当前账号「${workScope.accountLabel}」。` : preferred.length ? '默认延续选题的目标平台。' : '选择这份草稿要使用的账号或平台。'}创建后可以继续修改各平台的表达。</p><Feedback error={error} />
    {preferred.length > 0 && <div className="r2-batch-targets">{targets.filter(target => preferred.includes(target.key)).map(renderTarget)}</div>}
    {otherTargets.length > 0 && (preferred.length > 0 ? <details><summary>添加其他账号或平台{selected.filter(target => !preferred.includes(target)).length > 0 ? ` · 已选 ${selected.filter(target => !preferred.includes(target)).length} 个` : ''}</summary><div className="r2-batch-targets">{otherTargets.map(renderTarget)}</div></details> : <div className="r2-batch-targets">{otherTargets.map(renderTarget)}</div>)}
    <p className="r2-muted">本步只创建可编辑的内容版本。没有绑定账号时，也可以先选择“仅平台规划”。</p>
    <footer><button className="r2-button" disabled={busy} onClick={onClose}>取消</button><button className="r2-button primary" disabled={busy || selected.length === 0} onClick={() => void create()}>创建 {selected.length} 个账号版本</button></footer>
  </Modal>;
}
