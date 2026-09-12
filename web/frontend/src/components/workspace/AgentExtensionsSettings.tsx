import { useEffect, useState } from 'react';
import { errorText, fetchAgentExtensions, saveAgentExtensions } from '../../lib/ripple';
import type { AgentExtensions, McpServerMeta } from '../../lib/ripple';
import { Feedback } from './Common';

const emptyServer = (): McpServerMeta => ({ id: '', name: '', transport: 'streamable_http', endpoint: '', enabled: true, status: 'not_connected', tools: [] });

export default function AgentExtensionsSettings() {
  const [state, setState] = useState<AgentExtensions | null>(null);
  const [servers, setServers] = useState<McpServerMeta[]>([]);
  const [draft, setDraft] = useState<McpServerMeta>(emptyServer);
  const [toolIds, setToolIds] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const load = async () => { const value = await fetchAgentExtensions(); setState(value); setServers(value.mcp_servers || []); };
  useEffect(() => { void load().catch((e) => setError(errorText(e))); }, []);
  const add = () => {
    if (!draft.id.trim() || !draft.name.trim()) return;
    const tools = toolIds.split(',').map((x) => x.trim()).filter(Boolean).slice(0, 30).map((id) => ({ id, name: id, description: '由 MCP Server 声明的能力', risk: 'read' }));
    setServers((rows) => [...rows.filter((row) => row.id !== draft.id.trim()), { ...draft, id: draft.id.trim(), name: draft.name.trim(), endpoint: draft.endpoint.trim(), tools }]);
    setDraft(emptyServer()); setToolIds('');
  };
  const save = async () => {
    setBusy(true); setError(''); setNotice('');
    try { const result = await saveAgentExtensions(servers); setState(result); setServers(result.mcp_servers || []); setNotice('Agent 扩展已保存。'); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  return <section className="r2-settings-section" data-section="agent-extensions">
    <div><h2>Agent 扩展</h2></div>
    <div className="r2-settings-form"><Feedback error={error} notice={notice} />
      <div className="r2-extension-list">{servers.map((server) => <div key={server.id} className="r2-extension-card"><div><strong>{server.name}</strong><span>{server.id} · {server.transport} · {server.status === 'ready' ? '已就绪' : '未连接'}</span><small>{server.tools.length} 个 MCP 能力{server.endpoint ? ` · ${server.endpoint}` : ''}</small></div><label className="r2-checkbox"><input type="checkbox" checked={server.enabled} onChange={(e) => setServers((rows) => rows.map((row) => row.id === server.id ? { ...row, enabled: e.target.checked } : row))} />启用</label><button className="r2-text-button" onClick={() => setServers((rows) => rows.filter((row) => row.id !== server.id))}>移除</button></div>)}</div>
      <div className="r2-extension-add"><h3>注册 MCP Server</h3><div className="r2-field-grid"><label className="r2-field">ID<input value={draft.id} onChange={(e) => setDraft((row) => ({ ...row, id: e.target.value }))} placeholder="notion" /></label><label className="r2-field">名称<input value={draft.name} onChange={(e) => setDraft((row) => ({ ...row, name: e.target.value }))} placeholder="Notion" /></label></div><div className="r2-field-grid"><label className="r2-field">Transport<select value={draft.transport} onChange={(e) => setDraft((row) => ({ ...row, transport: e.target.value as McpServerMeta['transport'] }))}><option value="streamable_http">Streamable HTTP</option><option value="sse">SSE</option><option value="http">HTTP</option><option value="mock">Mock（本地确定性）</option></select></label><label className="r2-field">Endpoint<input value={draft.endpoint} disabled={draft.transport === 'mock'} onChange={(e) => setDraft((row) => ({ ...row, endpoint: e.target.value }))} placeholder="https://mcp.example.com" /></label></div><label className="r2-field">声明的能力 ID（逗号分隔）<input value={toolIds} onChange={(e) => setToolIds(e.target.value)} placeholder="search, read_page" /></label><button className="r2-button" disabled={!draft.id.trim() || !draft.name.trim()} onClick={add}>加入配置</button></div>
      <div className="r2-section-heading"><strong>内置插件</strong></div><div className="r2-extension-list">{(state?.plugins || []).map((plugin) => <div key={plugin.id} className="r2-extension-card"><div><strong>{plugin.name}</strong><span>{plugin.description}</span><small>{plugin.skills.length} 个技能{plugin.mcp_tools.length ? ` · ${plugin.mcp_tools.length} 个 MCP 能力` : ''}</small></div></div>)}</div>
      <button className="r2-button primary" disabled={busy} onClick={() => void save()}>{busy ? '保存中…' : '保存 Agent 扩展'}</button>
    </div>
  </section>;
}
