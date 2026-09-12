import { useState } from 'react';
import { api } from '../../lib/ripple';

export default function EnvironmentInstallButton({ component, label, disabled, onStart, onDone, onError }: { component: 'browser' | 'bilibili'; label: string; disabled: boolean; onStart: () => void; onDone: () => void; onError: (error: unknown) => void }) {
  const [pending, setPending] = useState(false);
  const run = async () => {
    if (pending || !window.confirm(`修复安装「${label}」所需的 Ripple 本地组件？`)) return;
    setPending(true);
    onStart();
    const endpoint = component === 'browser' ? '/api/ripple/environment/browser/install' : '/api/ripple/environment/bilibili/install';
    try { await api(endpoint, 'POST', { confirmed: true }); onDone(); }
    catch (error) { onError(error); }
    finally { setPending(false); }
  };
  return <button className="r2-button" disabled={disabled || pending} onClick={() => void run()}>{pending ? '修复中…' : '修复安装'}</button>;
}
