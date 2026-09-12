import { useEffect, useMemo, useRef, useState } from 'react';
import { api, errorText } from '../../lib/ripple';
import type { Channel, Mother, PlatformVariant } from '../../lib/ripple';
import { Feedback, Mark, Modal } from './Common';

export default function VariantBatch({ source, onClose, onDone }: { source: Mother; onClose: () => void; onDone: (count: number) => void }) {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const key = useRef(crypto.randomUUID());
  const gate = useRef(false);
  const existing = useMemo(() => new Set((source.variants || []).map(item => item.platform)), [source.variants]);
  useEffect(() => { api<Channel[]>('/api/ripple/channels').then(setChannels).catch(e => setError(errorText(e))); }, []);
  const create = async () => {
    if (gate.current) return;
    gate.current = true; setBusy(true); setError('');
    try {
      const targets = selected.map(platform => ({ platform }));
      const result = await api<{ items: PlatformVariant[] }>(`/api/ripple/contents/${source.id}/variants`, 'POST', {
        expected_source_version: source.version_id, idempotency_key: key.current, targets,
      });
      onDone(result.items.length);
    } catch (e) { setError(errorText(e)); } finally { gate.current = false; setBusy(false); }
  };
  return <Modal title="创建平台版本" busy={busy} onClose={onClose}><h3>{source.content.title}</h3><p>母稿 V{source.version}。先选择要适配的平台；账号、Blog 连接、排期和发布方式都在版本创建后单独配置。</p><Feedback error={error} />
    <div className="r2-batch-targets">{channels.map(channel => {
      const already = existing.has(channel.id), checked = selected.includes(channel.id);
      return <label className={`r2-remote-account ${already ? 'disabled' : ''}`} key={channel.id}><input type="checkbox" aria-label={`选择平台 ${channel.name}`} checked={checked || already} disabled={busy || already || (!checked && selected.length >= 20)} onChange={e => setSelected(current => e.target.checked ? [...current, channel.id] : current.filter(id => id !== channel.id))} /><Mark platform={channel.id} /><span><strong>{channel.name}</strong><small>{already ? '已有平台版本' : channel.id === 'blog' ? '可连接 Blog OpenAPI，也可只导出 Markdown' : channel.direct_publish ? '发布能力已就绪' : '可先创作版本，发布能力稍后配置'}</small></span></label>;
    })}</div>
    <p className="r2-muted">创建平台版本不会登录账号、审核、排期或执行发布。</p>
    <footer><button className="r2-button" disabled={busy} onClick={onClose}>取消</button><button className="r2-button primary" disabled={busy || selected.length === 0} onClick={() => void create()}>创建 {selected.length} 个平台版本</button></footer>
  </Modal>;
}
