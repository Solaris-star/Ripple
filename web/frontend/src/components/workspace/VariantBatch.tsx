import { useEffect, useMemo, useRef, useState } from 'react';
import { api, errorText } from '../../lib/ripple';
import { newId } from '../../lib/id';
import type { Account, Channel, Mother, PlatformVariant } from '../../lib/ripple';
import { Feedback, Mark, Modal } from './Common';

export default function VariantBatch({ source, onClose, onDone }: { source: Mother; onClose: () => void; onDone: (count: number) => void }) {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const key = useRef(newId());
  const gate = useRef(false);
  const existing = useMemo(() => new Set((source.variants || []).map(item => `${item.platform}|${item.target_id || ''}`)), [source.variants]);
  useEffect(() => {
    void Promise.all([api<Channel[]>('/api/ripple/channels'), api<Account[]>('/api/ripple/accounts')])
      .then(([channelRows, accountRows]) => { setChannels(channelRows); setAccounts(accountRows); })
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
  return <Modal title="创建账号版本" busy={busy} onClose={onClose}><h3>{source.content.title}</h3><p>母稿 V{source.version}。可为同一平台的不同账号分别创建版本；没有绑定账号时也可以先创建“仅平台规划”版本。</p><Feedback error={error} />
    <div className="r2-batch-targets">{channels.flatMap(channel => {
      const accountRows = accounts.filter(account => account.platform === channel.id);
      const targets = channel.id === 'blog' || accountRows.length === 0
        ? [{ id: '', label: channel.id === 'blog' ? 'Blog · 仅平台规划' : `${channel.name} · 仅平台规划`, detail: channel.id === 'blog' ? '后续可连接 Blog 或导出 Markdown' : '后续再选择发布账号' }]
        : [
          ...accountRows.map(account => ({ id: account.id, label: `${channel.name} · ${account.identity?.name || account.label}`, detail: account.status === 'connected' ? '账号已连接' : '账号未连接，仍可先制作版本' })),
          { id: '', label: `${channel.name} · 仅平台规划`, detail: '不绑定具体账号，后续可再选择' },
        ];
      return targets.map(target => {
        const key = `${channel.id}|${target.id}`;
        const already = existing.has(key), checked = selected.includes(key);
        return <label className={`r2-remote-account ${already ? 'disabled' : ''}`} key={key}><input type="checkbox" aria-label={`选择账号版本 ${target.label}`} checked={checked || already} disabled={busy || already || (!checked && selected.length >= 20)} onChange={e => setSelected(current => e.target.checked ? [...current, key] : current.filter(id => id !== key))} /><Mark platform={channel.id} /><span><strong>{target.label}</strong><small>{already ? '已有账号版本' : target.detail}</small></span></label>;
      });
    })}</div>
    <p className="r2-muted">创建账号版本不会登录账号、审核、排期或执行发布。发布前仍会再次核验账号身份与权限。</p>
    <footer><button className="r2-button" disabled={busy} onClick={onClose}>取消</button><button className="r2-button primary" disabled={busy || selected.length === 0} onClick={() => void create()}>创建 {selected.length} 个账号版本</button></footer>
  </Modal>;
}
