import { useEffect, useState } from 'react';
import { errorText, fetchAgentExtensions, saveAgentExtensions } from '../../lib/ripple';
import type { McpServerMeta } from '../../lib/ripple';
import { Feedback } from './Common';

const emptyServer = (): McpServerMeta => ({ id: '', name: '', transport: 'streamable_http', endpoint: '', enabled: true, status: 'not_connected', tools: [] });

export default function AgentExtensionsSettings() {
  const [servers, setServers] = useState<McpServerMeta[]>([]);
  const [draft, setDraft] = useState<McpServerMeta>(emptyServer);
  const [toolIds, setToolIds] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const load = async () => { const value = await fetchAgentExtensions(); setServers(value.mcp_servers || []); };
  useEffect(() => { void load().catch((e) => setError(errorText(e))); }, []);
  const add = () => {
    if (!draft.id.trim() || !draft.name.trim()) return;
    const tools = toolIds.split(',').map((x) => x.trim()).filter(Boolean).slice(0, 30).map((id) => ({ id, name: id, description: '由 MCP Server 声明的能力', risk: 'read' }));
    setServers((rows) => [...rows.filter((row) => row.id !== draft.id.trim()), { ...draft, id: draft.id.trim(), name: draft.name.trim(), endpoint: draft.endpoint.trim(), tools }]);
    setDraft(emptyServer()); setToolIds('');
  };
  const save = async () => {
    setBusy(true); setError(''); setNotice('');
    try { const result = await saveAgentExtensions(servers); setServers(result.mcp_servers || []); setNotice('MCP 配置已保存。连接状态和真实工具能力仍以服务端探测结果为准。'); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  return <section className="r2-settings-section" data-section="agent-extensions">
    <div><h2>MCP</h2><p className="r2-muted">管理外部 MCP Server。手工填写的能力 ID 只是声明，不代表服务端已经发现、授权或可以执行。</p></div>
    <div className="r2-settings-form"><Feedback error={error} notice={notice} />
      <div className="r2-extension-list">{servers.map((server) => <div key={server.id} className="r2-extension-card"><div><strong>{server.name}</strong><span>{server.id} · {server.transport} · {server.status === 'ready' ? '已就绪' : '未连接'}</span><small>{server.tools.length} 个 MCP 能力{server.endpoint ? ` · ${server.endpoint}` : ''}</small></div><label className="r2-checkbox"><input type="checkbox" checked={server.enabled} onChange={(e) => setServers((rows) => rows.map((row) => row.id === server.id ? { ...row, enabled: e.target.checked } : row))} />启用</label><button className="r2-text-button" onClick={() => setServers((rows) => rows.filter((row) => row.id !== server.id))}>移除</button></div>)}</div>
      <div className="r2-extension-add"><h3>注册 MCP Server</h3><div className="r2-field-grid"><label className="r2-field">ID<input value={draft.id} onChange={(e) => setDraft((row) => ({ ...row, id: e.target.value }))} placeholder="notion" /></label><label className="r2-field">名称<input value={draft.name} onChange={(e) => setDraft((row) => ({ ...row, name: e.target.value }))} placeholder="Notion" /></label></div><div className="r2-field-grid"><label className="r2-field">Transport<select value={draft.transport} onChange={(e) => setDraft((row) => ({ ...row, transport: e.target.value as McpServerMeta['transport'] }))}><option value="streamable_http">Streamable HTTP</option><option value="sse">SSE</option><option value="http">HTTP</option><option value="mock">Mock（本地确定性）</option></select></label><label className="r2-field">Endpoint<input value={draft.endpoint} disabled={draft.transport === 'mock'} onChange={(e) => setDraft((row) => ({ ...row, endpoint: e.target.value }))} placeholder="https://mcp.example.com" /></label></div><label className="r2-field">预期能力 ID（仅声明，逗号分隔）<input value={toolIds} onChange={(e) => setToolIds(e.target.value)} placeholder="search, read_page" /></label><button className="r2-button" disabled={!draft.id.trim() || !draft.name.trim()} onClick={add}>加入配置</button></div>
      <button className="r2-button primary" disabled={busy} onClick={() => void save()}>{busy ? '保存中…' : '保存 MCP 配置'}</button>
    </div>
  </section>;
}
