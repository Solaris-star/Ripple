import { useCallback, useEffect, useState } from 'react';
import { api, errorText } from '../../lib/ripple';
import type { ExecutionNode } from '../../lib/ripple';
import { Feedback } from './Common';

interface PairingCode { pairing_code: string; expires_in: number; }

const browserName = (value: string) => value === 'msedge' ? 'Microsoft Edge' : value === 'chrome' ? 'Google Chrome' : value === 'chromium' ? 'Chromium' : value;

export default function ExecutionNodeSettings() {
  const [nodes, setNodes] = useState<ExecutionNode[]>([]);
  const [pairing, setPairing] = useState<PairingCode | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const refresh = useCallback(async () => {
    const result = await api<{ items: ExecutionNode[] }>('/api/ripple/execution-nodes');
    setNodes(result.items);
  }, []);

  useEffect(() => {
    let stopped = false;
    const run = async () => { try { if (!stopped) await refresh(); } catch (value) { if (!stopped) setError(errorText(value)); } };
    void run();
    const timer = setInterval(() => void run(), 5000);
    return () => { stopped = true; clearInterval(timer); };
  }, [refresh]);

  const createPairing = async () => {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await api<PairingCode>('/api/ripple/execution-nodes/pairing', 'POST', { confirmed: true });
      setPairing(result);
      setNotice('配对码只用于注册执行节点，5 分钟后失效。');
    } catch (value) { setError(errorText(value)); } finally { setBusy(false); }
  };

  return <div className="r2-execution-node-list">
    <Feedback error={error} notice={notice} />
    <div className="r2-runtime-grid">{nodes.map(node => <article className="r2-runtime-card" key={node.id} data-execution-node={node.id}>
      <div><strong>{node.name}</strong><span>{node.kind === 'local' ? 'Ripple 服务所在设备' : '远程 Browser Node'}</span></div>
      <em className={node.online ? 'ready' : 'limited'}>{node.online ? '在线' : '离线'}</em>
      <p>{node.interactive_browsers.length ? `人工登录：${node.interactive_browsers.map(browserName).join(' / ')}` : '无人工登录浏览器'}<br />{node.browsers.length ? `自动化：${node.browsers.map(browserName).join(' / ')}` : '未检测到浏览器'}</p>
    </article>)}</div>
    <div className="r2-node-pairing">
      <button className="r2-button" disabled={busy} onClick={() => void createPairing()}>{busy ? '正在生成…' : '配对远程设备'}</button>
      {pairing && <code className="r2-pairing-code">{pairing.pairing_code}</code>}
      <p className="r2-muted">远程设备协议采用出站连接，浏览器 Profile、Cookie 和登录态保留在执行设备，不上传 Ripple Server。当前版本已完成配对 / 心跳 / 登录与状态检查的服务端协议骨架；独立 Browser Node 客户端和远程发布传输尚未随本版本交付。</p>
    </div>
  </div>;
}
