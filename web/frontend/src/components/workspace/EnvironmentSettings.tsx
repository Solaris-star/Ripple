import { useEffect, useState } from 'react';
import { api, errorText } from '../../lib/ripple';
import { Feedback } from './Common';
import EnvironmentInstallButton from './EnvironmentInstallButton';

interface EnvironmentCapability { id: 'browser' | 'bilibili'; label: string; ready: boolean; summary: string; installable: boolean; detail: string; browsers?: string[]; provider?: string; provider_version?: string; }
interface EnvironmentState { items: EnvironmentCapability[]; }

export default function EnvironmentSettings() {
  const [state, setState] = useState<EnvironmentState | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<string[]>([]);
  useEffect(() => { void api<EnvironmentState>('/api/ripple/environment').then(setState).catch(e => setError(errorText(e))); }, []);
  const refresh = async (label?: string) => {
    setState(await api<EnvironmentState>('/api/ripple/environment'));
    if (label) setNotice(`${label}已安装并验证。`);
  };
  const toggle = (id: string) => setExpanded(current => current.includes(id) ? current.filter(value => value !== id) : [...current, id]);
  return <div className="r2-settings-form"><Feedback error={error} notice={notice} />
    <div className="r2-agent-runtime-list">{(state?.items || []).map(item => {
      const open = expanded.includes(item.id);
      return <article className={`r2-runtime-card`} key={item.id} data-environment={item.id}>
        <div className="r2-environment-card-main"><strong>{item.label}</strong><span>{item.summary}</span></div>
        <div className="r2-row-actions">
          {!item.ready && item.installable && <EnvironmentInstallButton component={item.id} label={item.label} disabled={!!busy} onStart={() => { setBusy(item.id); setError(''); setNotice(''); }} onDone={() => { setBusy(null); void refresh(item.label).catch(value => setError(errorText(value))); }} onError={(value) => { setBusy(null); setError(errorText(value)); }} />}
          <button className="r2-text-button" onClick={() => toggle(item.id)}>{open ? '收起' : '详情'}</button>
        </div>
        {open && <div style={{ gridColumn: '1 / -1' }}><p>{item.detail}</p>{item.browsers?.length ? <p>可用：{item.browsers.join(' · ')}</p> : null}{item.id === 'bilibili' && item.provider ? <p>{item.provider}{item.provider_version ? ` ${item.provider_version}` : ''} · 第三方开源</p> : null}</div>}
      </article>;
    })}</div>
  </div>;
}
