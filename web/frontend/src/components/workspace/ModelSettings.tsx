import { useEffect, useMemo, useState } from 'react';
import {
  deleteAIProvider, discoverAIProviderModels, fetchAIProviders, probeAIProvider,
  saveAIProvider, setAIProviderRoute,
} from '../../lib/api';
import type { AIProvider, AIProviderModel, AIProviderState, AIRoutePurpose } from '../../lib/api';
import { Feedback } from './Common';

type Editor = {
  provider_id?: string;
  name: string;
  kind: 'openai-compatible' | 'xai';
  base_url: string;
  api_key: string;
  models: AIProviderModel[];
  default_model: string;
  enabled: boolean;
  api_key_set?: boolean;
};

const blank = (): Editor => ({
  name: '', kind: 'openai-compatible', base_url: '', api_key: '',
  models: [], default_model: '', enabled: true,
});

export default function ModelSettings() {
  const [state, setState] = useState<AIProviderState | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [manualModel, setManualModel] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = async () => setState(await fetchAIProviders());
  useEffect(() => { void load().catch((e) => setError(e instanceof Error ? e.message : '模型配置读取失败')); }, []);

  const routeOptions = useMemo(() => {
    const rows: { value: string; label: string }[] = [];
    for (const provider of state?.providers || []) {
      if (!provider.enabled) continue;
      for (const model of provider.models) rows.push({
        value: `${provider.id}::${model.id}`,
        label: `${provider.name} / ${model.name || model.id}`,
      });
    }
    return rows;
  }, [state]);

  const editProvider = (provider: AIProvider) => {
    setEditor({
      provider_id: provider.id, name: provider.name, kind: provider.kind,
      base_url: provider.base_url, api_key: '', models: [...provider.models],
      default_model: provider.default_model, enabled: provider.enabled,
      api_key_set: provider.api_key_set,
    });
    setManualModel(''); setError(''); setNotice('');
  };

  const addManual = () => {
    const id = manualModel.trim();
    if (!editor || !id || editor.models.some((model) => model.id === id)) return;
    const models = [...editor.models, { id, name: id }];
    setEditor({ ...editor, models, default_model: editor.default_model || id });
    setManualModel('');
  };

  const discover = async () => {
    if (!editor || busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await discoverAIProviderModels({
        provider_id: editor.provider_id, kind: editor.kind, base_url: editor.base_url, api_key: editor.api_key,
      });
      const map = new Map(editor.models.map((row) => [row.id, row]));
      for (const row of result.items) map.set(row.id, row);
      const models = [...map.values()];
      setEditor({ ...editor, models, default_model: editor.default_model || models[0]?.id || '' });
      setNotice(`发现 ${result.items.length} 个模型。`);
    } catch (e) { setError(e instanceof Error ? e.message : '模型发现失败'); }
    finally { setBusy(false); }
  };

  const save = async () => {
    if (!editor || busy || !editor.base_url.trim() || !editor.default_model || !editor.models.length) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const next = await saveAIProvider({
        provider_id: editor.provider_id, name: editor.name, kind: editor.kind,
        base_url: editor.base_url, api_key: editor.api_key, models: editor.models,
        default_model: editor.default_model, enabled: editor.enabled,
      });
      setState(next); setEditor(null); setNotice('AI Provider 已保存。');
    } catch (e) { setError(e instanceof Error ? e.message : '保存失败'); }
    finally { setBusy(false); }
  };

  const remove = async (provider: AIProvider) => {
    if (!window.confirm(`移除 AI Provider「${provider.name}」？引用它的用途路由也会解除。`)) return;
    setBusy(true); setError('');
    try { setState(await deleteAIProvider(provider.id)); setNotice('AI Provider 已移除。'); }
    catch (e) { setError(e instanceof Error ? e.message : '移除失败'); }
    finally { setBusy(false); }
  };

  const setRoute = async (purpose: AIRoutePurpose, value: string) => {
    const [providerId = '', modelId = ''] = value.split('::', 2);
    setBusy(true); setError('');
    try {
      setState(await setAIProviderRoute(purpose, providerId, modelId));
      setNotice('用途路由已更新。默认 Agent 路由会同步给现有 Agent Runtime。');
    } catch (e) { setError(e instanceof Error ? e.message : '用途路由更新失败'); }
    finally { setBusy(false); }
  };

  const probe = async (provider: AIProvider, capability: 'chat' | 'x_search' | 'web_search') => {
    if (capability !== 'chat' && !window.confirm(
      `${capability === 'x_search' ? 'X Search' : 'Web Search'} 测试会发起一次真实 xAI 检索，可能产生费用。继续？`,
    )) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const result = await probeAIProvider(provider.id, provider.default_model, capability);
      setState(result.state);
      setNotice(`${provider.name} · ${capability} 测试通过。`);
    } catch (e) { setError(e instanceof Error ? e.message : '能力测试失败'); }
    finally { setBusy(false); }
  };

  return <section className="r2-settings-section" data-section="model-settings">
    <div><h2>AI / Agent Provider</h2><span className="r2-setting-tag">多 Provider</span></div>
    <div className="r2-settings-form r2-ai-provider-settings">
      <Feedback error={error} notice={notice} />
      <div className="r2-section-heading">
        <strong>{state?.providers.length || 0} 个 Provider</strong>
        <span>默认 Agent、选题生成与 X 活动发现可以分别绑定模型。</span>
      </div>

      <div className="r2-api-list">
        {(state?.providers || []).map((provider) => <div className="r2-api-row" key={provider.id}>
          <div className="r2-api-main">
            <div className="r2-api-title">
              <strong>{provider.name}</strong>
              <span className="r2-api-badge">{provider.kind === 'xai' ? 'xAI' : 'OpenAI-compatible'}</span>
              <span className={`r2-api-state ${provider.api_key_set && provider.enabled ? 'ready' : ''}`}>
                {provider.api_key_set && provider.enabled ? '已配置' : provider.enabled ? '缺少凭据' : '已停用'}
              </span>
            </div>
            <small>{provider.base_url} · 默认 {provider.default_model}</small>
            <div className="r2-model-chips">
              {provider.models.slice(0, 6).map((model) => <span key={model.id}>{model.name || model.id}</span>)}
              {provider.models.length > 6 && <span>+{provider.models.length - 6}</span>}
            </div>
            <div className="r2-provider-capabilities">
              {Object.entries(provider.capabilities || {}).map(([key, value]) => <span key={key} className={value === 'verified' ? 'ready' : ''}>{key} · {value}</span>)}
            </div>
          </div>
          <div className="r2-api-actions">
            <button disabled={busy} onClick={() => void probe(provider, 'chat')}>测试 Chat</button>
            {provider.kind === 'xai' && <button disabled={busy} onClick={() => void probe(provider, 'x_search')}>测试 X Search</button>}
            <button disabled={busy} onClick={() => editProvider(provider)}>管理</button>
            <button className="danger" disabled={busy} onClick={() => void remove(provider)}>移除</button>
          </div>
        </div>)}
        {state && !state.providers.length && <div className="r2-api-empty">还没有 AI Provider。旧版单 Provider 配置会自动迁移到这里。</div>}
      </div>
      <button className="r2-button" disabled={busy || !!editor} onClick={() => { setEditor(blank()); setManualModel(''); }}>+ 添加 Provider</button>

      <div className="r2-ai-routes">
        <h3>用途路由</h3>
        {(state?.purposes || []).filter((purpose) => purpose.id !== 'research').map((purpose) => {
          const route = state?.routes[purpose.id];
          const value = route ? `${route.provider_id}::${route.model_id}` : '';
          return <label className="r2-field" key={purpose.id}><span>{purpose.label}</span>
            <select value={value} disabled={busy} onChange={(e) => void setRoute(purpose.id, e.target.value)}>
              <option value="">未单独配置{purpose.id !== 'default_agent' ? '（回退默认 Agent）' : ''}</option>
              {routeOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
            </select>
            {purpose.id === 'x_campaign_discovery' && <small className="r2-muted">使用 Grok/xAI 时需绑定 xAI Provider，并单独通过 X Search 能力测试。X Developer API 在活动数据源中配置。</small>}
            {purpose.id === 'default_agent' && <small className="r2-muted">B站活动规则补全默认跟随这里的模型；脚本能解析完整时不会调用模型。</small>}
          </label>;
        })}
      </div>

      {editor && <div className="r2-ai-provider-editor">
        <div className="r2-section-heading"><strong>{editor.provider_id ? '管理 Provider' : '添加 Provider'}</strong><button className="r2-text-button" disabled={busy} onClick={() => setEditor(null)}>关闭</button></div>
        <div className="r2-ai-provider-editor-grid">
          <label className="r2-field"><span>名称</span><input value={editor.name} onChange={(e) => setEditor({ ...editor, name: e.target.value })} placeholder="例如 xAI / 公司网关" /></label>
          <label className="r2-field"><span>类型</span><select value={editor.kind} onChange={(e) => setEditor({ ...editor, kind: e.target.value as Editor['kind'] })}><option value="openai-compatible">OpenAI-compatible</option><option value="xai">xAI（支持能力探测）</option></select></label>
          <label className="r2-field wide"><span>Base URL</span><input type="url" value={editor.base_url} onChange={(e) => setEditor({ ...editor, base_url: e.target.value })} placeholder={editor.kind === 'xai' ? 'https://api.x.ai/v1' : 'https://api.example.com/v1'} /></label>
          <label className="r2-field wide"><span>API Key</span><input type="password" autoComplete="new-password" value={editor.api_key} onChange={(e) => setEditor({ ...editor, api_key: e.target.value })} placeholder={editor.api_key_set ? '已加密保存；留空保持原 Key' : '首次保存必须填写'} /></label>
        </div>
        <div className="r2-toolbar"><button className="r2-button" disabled={busy || !editor.base_url || (!editor.api_key && !editor.api_key_set)} onClick={() => void discover()}>发现 /models</button><span className="r2-muted">发现失败仍可手工添加模型 ID。</span></div>
        <div className="r2-agent-model-add"><input value={manualModel} onChange={(e) => setManualModel(e.target.value)} placeholder="模型 ID，例如 grok-4.6" onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addManual(); } }} /><button className="r2-button" disabled={!manualModel.trim()} onClick={addManual}>添加</button></div>
        <div className="r2-agent-model-list">{editor.models.map((model) => <div key={model.id}>
          <label className="r2-radio-line"><input type="radio" name="provider-default-model" checked={editor.default_model === model.id} onChange={() => setEditor({ ...editor, default_model: model.id })} /><span><strong>{model.name || model.id}</strong><small>{model.id}</small></span></label>
          <button className="r2-text-button" disabled={editor.models.length <= 1 || editor.default_model === model.id} onClick={() => setEditor({ ...editor, models: editor.models.filter((row) => row.id !== model.id) })}>移除</button>
        </div>)}</div>
        <label className="r2-checkbox"><input type="checkbox" checked={editor.enabled} onChange={(e) => setEditor({ ...editor, enabled: e.target.checked })} />启用此 Provider</label>
        <div className="r2-toolbar"><button className="r2-button primary" disabled={busy || !editor.base_url || !editor.default_model || !editor.models.length || (!editor.api_key && !editor.api_key_set)} onClick={() => void save()}>{busy ? '保存中…' : '保存 Provider'}</button></div>
      </div>}
    </div>
  </section>;
}
