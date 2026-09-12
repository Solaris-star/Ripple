import { useEffect, useState } from 'react';
import { api, errorText } from '../../lib/ripple';
import type { AgentModelMeta } from '../../lib/ripple';
import { Feedback } from './Common';

interface ModelState { enabled: boolean; base_url: string; model: string; default_model: string; models: AgentModelMeta[]; api_key_set: boolean; }

export default function ModelSettings() {
  const [state, setState] = useState<ModelState | null>(null);
  const [baseUrl, setBaseUrl] = useState('');
  const [models, setModels] = useState<AgentModelMeta[]>([]);
  const [defaultModel, setDefaultModel] = useState('');
  const [manualModel, setManualModel] = useState('');
  const [key, setKey] = useState('');
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const load = async () => {
    const value = await api<ModelState>('/api/model-config');
    setState(value); setBaseUrl(value.base_url); setModels(value.models || []); setDefaultModel(value.default_model || value.model); setEnabled(value.enabled);
  };
  useEffect(() => { void load().catch((e) => setError(errorText(e))); }, []);
  const addManual = () => {
    const id = manualModel.trim(); if (!id || models.some((row) => row.id === id)) return;
    setModels((rows) => [...rows, { id, name: id, effort_levels: ['auto'], supports_reasoning: false, source: 'manual' }]);
    if (!defaultModel) setDefaultModel(id); setManualModel('');
  };
  const discover = async () => {
    if (busy) return; setBusy(true); setError(''); setNotice('');
    try {
      const result = await api<{ items: AgentModelMeta[] }>('/api/model-config/discover', 'POST', { base_url: baseUrl, api_key: key });
      setModels((current) => {
        const byId = new Map(current.map((row) => [row.id, row]));
        for (const row of result.items) if (!byId.has(row.id)) byId.set(row.id, row);
        return [...byId.values()];
      });
      if (!defaultModel && result.items[0]) setDefaultModel(result.items[0].id);
      setNotice(`发现 ${result.items.length} 个候选模型；请确认保留的模型和默认项后保存。`);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const save = async () => {
    if (busy) return; setBusy(true); setError(''); setNotice('');
    try {
      const next = await api<ModelState>('/api/model-config', 'POST', { base_url: baseUrl, api_key: key, models, default_model: defaultModel, enabled });
      setState(next); setModels(next.models || []); setDefaultModel(next.default_model); setKey(''); setNotice('模型配置已保存。');
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  const test = async () => {
    setBusy(true); setError(''); setNotice('');
    try { const result = await api<{ ok: boolean; agent: boolean; model: string }>('/api/model-config/test', 'POST', {}); setNotice(result.ok && result.agent ? `默认模型 ${result.model} 与 Ripple Agent 均已就绪。` : '模型响应成功，Agent Runtime 仍在启动。'); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };
  return <section className="r2-settings-section" data-section="model-settings">
    <div><h2>模型配置</h2><span className="r2-setting-tag">OpenCode</span></div>
    <div className="r2-settings-form"><Feedback error={error} notice={notice} />
      <div className="r2-section-heading"><strong>{state?.enabled && state?.api_key_set ? '已配置' : '未配置'}</strong><span>默认：{defaultModel || '未选择模型'}</span></div>
      <label className="r2-field">Base URL<input aria-label="AI 模型 Base URL" type="url" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://api.example.com/v1" autoComplete="off" /></label>
      <label className="r2-field">API Key<input aria-label="AI 模型 API Key" type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder={state?.api_key_set ? '已保存；留空保留原 Key' : '填写 API Key'} autoComplete="new-password" /></label>
      <div className="r2-toolbar"><button className="r2-button" disabled={busy || !baseUrl || (!key && !state?.api_key_set)} onClick={() => void discover()}>发现 /models</button><span className="r2-muted">发现失败时可以直接手工添加模型 ID。</span></div>
      <div className="r2-agent-model-add"><input aria-label="手工添加 Agent 模型" value={manualModel} onChange={(e) => setManualModel(e.target.value)} placeholder="模型 ID，例如 gpt-5.6" onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addManual(); } }} /><button className="r2-button" onClick={addManual} disabled={!manualModel.trim()}>添加</button></div>
      <div className="r2-agent-model-list">{models.map((row) => <div key={row.id}><label className="r2-radio-line"><input type="radio" name="agent-default-model" checked={defaultModel === row.id} onChange={() => setDefaultModel(row.id)} /><span><strong>{row.name}</strong><small>{row.id} · {row.source || 'manual'} · Provider reasoning：{row.supports_reasoning ? '已声明' : '未声明 / 未知'}</small></span></label><button className="r2-text-button" disabled={models.length <= 1 || defaultModel === row.id} onClick={() => setModels((rows) => rows.filter((x) => x.id !== row.id))}>移除</button></div>)}</div>
      <label className="r2-checkbox"><input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} />启用 Ripple AI / Agent</label>
      <div className="r2-toolbar"><button className="r2-button" disabled={busy || !baseUrl || !defaultModel || models.length === 0 || (!key && !state?.api_key_set)} onClick={() => void save()}>保存模型配置</button><button className="r2-button primary" disabled={busy || !state?.api_key_set} onClick={() => void test()}>{busy ? '测试中…' : '测试默认模型与 Agent'}</button></div>
    </div>
  </section>;
}
