import { useEffect, useMemo, useState } from 'react';
import { api, errorText } from '../../lib/ripple';
import { Feedback, Modal } from './Common';

type RouteField = { key: string; label: string; required: boolean; kind: 'text' | 'select'; choices: string[]; placeholder: string };
type ProtocolSpec = { id: string; label: string; discoverable: boolean; auth: string[]; fields: RouteField[]; default_base: string };
type ModelSpec = { id: string; name: string; enabled: boolean; source: string; capabilities: Record<string, boolean | string | number> };
type ProviderSpec = {
  id: string; name: string; base_url: string; protocol: string; protocol_label: string;
  configured: boolean; is_default: boolean; default_model: string | null;
  network: { mode: 'system' | 'direct' | 'custom'; proxy_url: string };
  secrets: Record<string, boolean>; route_settings: Record<string, string>; models: ModelSpec[];
};
type CatalogProvider = {
  id: string; name: string; base_url: string; groups: string[];
  network: { mode: 'system' | 'direct' | 'custom'; proxy_url: string };
  secrets: Record<string, boolean>;
};
type GroupSpec = {
  group: string; label: string; configured: boolean;
  default: { provider_id: string; model_id: string } | null;
  protocols: ProtocolSpec[]; providers: ProviderSpec[];
};
type ConfigPayload = {
  schema: number; revision: number; groups: GroupSpec[]; providers: CatalogProvider[];
  system_proxy: { available: boolean; http: string; https: string };
};
type DiscoveredModel = { id: string; name: string };
type EditorState = {
  provider_id?: string; name: string; protocol: string; base_url: string;
  api_key: string; access_key: string; secret_key: string;
  network_mode: 'system' | 'direct' | 'custom'; proxy_url: string;
  route_settings: Record<string, string>; models: string[]; default_model: string;
  set_default: boolean; secrets: Record<string, boolean>;
};

const LABELS: Record<string, string> = { image: '图片生成', video: '视频生成', music: '音乐生成', voice: '语音 / 声音克隆' };
const MEDIA_TAB_KEY = 'ripple_media_model_tab_v2';
const NETWORK_LABELS = { system: '系统代理', direct: '直连', custom: '自定义代理' } as const;

export default function MediaModelSettings() {
  const [config, setConfig] = useState<ConfigPayload | null>(null);
  const [activeGroup, setActiveGroup] = useState(() => {
    try { return window.localStorage.getItem(MEDIA_TAB_KEY) || ''; } catch { return ''; }
  });
  const [editor, setEditor] = useState<EditorState | null>(null);
  const [editorInitial, setEditorInitial] = useState('');
  const [modalTab, setModalTab] = useState<'connection' | 'models'>('connection');
  const [discovered, setDiscovered] = useState<DiscoveredModel[]>([]);
  const [modelSearch, setModelSearch] = useState('');
  const [modelFilter, setModelFilter] = useState<'all' | 'selected'>('all');
  const [manualModel, setManualModel] = useState('');
  const [busy, setBusy] = useState(false);
  const [discovering, setDiscovering] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [modalError, setModalError] = useState('');
  const [modalNotice, setModalNotice] = useState('');

  const apply = (next: ConfigPayload) => setConfig(next);
  const load = async () => apply(await api<ConfigPayload>('/api/media-model-config'));
  useEffect(() => { void load().catch((e) => setError(errorText(e))); }, []);

  const groups = useMemo(() => (config?.groups || []).filter((g) => ['image', 'video', 'music', 'voice'].includes(g.group)), [config]);
  const active = groups.find((group) => group.group === activeGroup) || null;

  useEffect(() => {
    if (!groups.length || active) return;
    const next = groups.find((group) => !group.configured)?.group || groups[0].group;
    setActiveGroup(next);
    try { window.localStorage.setItem(MEDIA_TAB_KEY, next); } catch { /* ignore */ }
  }, [groups, active]);

  const selectGroup = (group: string) => {
    setActiveGroup(group); setError(''); setNotice('');
    try { window.localStorage.setItem(MEDIA_TAB_KEY, group); } catch { /* ignore */ }
  };

  const protocolFor = (id: string) => active?.protocols.find((item) => item.id === id) || null;
  const reusable = (config?.providers || []).filter((item) => active && !item.groups.includes(active.group));

  const snapshot = (value: EditorState) => JSON.stringify({ ...value, api_key: value.api_key ? 'changed' : '', access_key: value.access_key ? 'changed' : '', secret_key: value.secret_key ? 'changed' : '' });
  const openEditor = (value: EditorState) => {
    setEditor(value); setEditorInitial(snapshot(value)); setModalTab('connection');
    setDiscovered([]); setModelSearch(''); setModelFilter('all'); setManualModel(''); setModalError(''); setModalNotice('');
  };

  const beginAdd = () => {
    if (!active?.protocols.length) return;
    const protocol = active.protocols[0];
    openEditor({
      name: '', protocol: protocol.id, base_url: protocol.default_base || '', api_key: '', access_key: '', secret_key: '',
      network_mode: 'system', proxy_url: '', route_settings: {}, models: [], default_model: '', set_default: !active.default,
      secrets: {},
    });
  };

  const beginEdit = (provider: ProviderSpec) => openEditor({
    provider_id: provider.id, name: provider.name, protocol: provider.protocol, base_url: provider.base_url,
    api_key: '', access_key: '', secret_key: '', network_mode: provider.network.mode, proxy_url: provider.network.proxy_url,
    route_settings: { ...provider.route_settings }, models: provider.models.map((model) => model.id),
    default_model: provider.default_model || provider.models[0]?.id || '', set_default: provider.is_default,
    secrets: { ...provider.secrets },
  });

  const attachExistingProvider = (providerId: string) => {
    if (!editor || !active) return;
    if (!providerId) {
      const protocol = active.protocols[0];
      setEditor({ ...editor, provider_id: undefined, name: '', base_url: protocol.default_base || '', network_mode: 'system', proxy_url: '', secrets: {}, models: [], default_model: '' });
      return;
    }
    const provider = config?.providers.find((item) => item.id === providerId);
    if (!provider) return;
    setEditor({
      ...editor, provider_id: provider.id, name: provider.name, base_url: provider.base_url,
      network_mode: provider.network.mode, proxy_url: provider.network.proxy_url, secrets: { ...provider.secrets },
      api_key: '', access_key: '', secret_key: '', models: [], default_model: '',
    });
  };

  const closeEditor = () => {
    if (!editor || busy || discovering) return;
    if (snapshot(editor) !== editorInitial && !window.confirm('放弃尚未保存的 Provider 修改？')) return;
    setEditor(null);
  };

  const updateEditor = (patch: Partial<EditorState>) => editor && setEditor({ ...editor, ...patch });

  const discoverModels = async () => {
    if (!active || !editor || discovering) return;
    setDiscovering(true); setModalError(''); setModalNotice('');
    try {
      const result = await api<{ supported: boolean; models: DiscoveredModel[]; count?: number; message?: string }>(`/api/media-model-config/${active.group}/discover-models`, 'POST', {
        provider_id: editor.provider_id, protocol: editor.protocol, base_url: editor.base_url,
        api_key: editor.api_key, access_key: editor.access_key, secret_key: editor.secret_key,
        network_mode: editor.network_mode, proxy_url: editor.proxy_url,
      });
      setDiscovered(result.models || []); setModalTab('models');
      setModalNotice(result.supported ? `已获取 ${result.models.length} 个模型。勾选你要在 Ripple 中启用的模型。` : (result.message || '当前协议需要手动添加模型。'));
    } catch (e) { setModalError(errorText(e)); } finally { setDiscovering(false); }
  };

  const toggleModel = (modelId: string, checked: boolean) => {
    if (!editor) return;
    const models = checked ? Array.from(new Set([...editor.models, modelId])) : editor.models.filter((id) => id !== modelId);
    const defaultModel = models.includes(editor.default_model) ? editor.default_model : (models[0] || '');
    setEditor({ ...editor, models, default_model: defaultModel });
  };

  const addManualModel = () => {
    const model = manualModel.trim();
    if (!editor || !model) return;
    if (model.length > 200) { setModalError('模型 ID 不能超过 200 字符。'); return; }
    toggleModel(model, true); setManualModel(''); setModalError('');
  };

  const saveProvider = async () => {
    if (!active || !editor || !config || busy) return;
    if (!editor.name.trim()) { setModalError('请填写 Provider 名称。'); setModalTab('connection'); return; }
    if (!editor.base_url.trim()) { setModalError('请填写 API 根地址。'); setModalTab('connection'); return; }
    setBusy(true); setModalError(''); setModalNotice('');
    try {
      const next = await api<ConfigPayload>(`/api/media-model-config/${active.group}/providers`, 'POST', {
        provider_id: editor.provider_id, expected_revision: config.revision,
        name: editor.name.trim(), protocol: editor.protocol, base_url: editor.base_url.trim(),
        api_key: editor.api_key, access_key: editor.access_key, secret_key: editor.secret_key,
        network_mode: editor.network_mode, proxy_url: editor.proxy_url.trim(), route_settings: editor.route_settings,
        models: editor.models, default_model: editor.default_model || null, set_default: editor.set_default,
      });
      apply(next); setEditor(null); setNotice(`${LABELS[active.group] || active.label} Provider 已保存。`);
    } catch (e) { setModalError(errorText(e)); } finally { setBusy(false); }
  };

  const removeProvider = async (provider: ProviderSpec) => {
    if (!active || !config || busy) return;
    const catalog = config.providers.find((item) => item.id === provider.id);
    const suffix = (catalog?.groups.length || 0) > 1 ? `\n这个 Provider 还被 ${catalog?.groups.filter((group) => group !== active.group).map((group) => LABELS[group] || group).join('、')} 使用，本次只会从 ${LABELS[active.group]} 移除。` : '';
    if (!window.confirm(`从 ${LABELS[active.group]} 移除「${provider.name}」？${suffix}`)) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const next = await api<ConfigPayload>(`/api/media-model-config/${active.group}/providers/${provider.id}`, 'DELETE', { expected_revision: config.revision });
      apply(next); setNotice(`已从 ${LABELS[active.group]} 移除「${provider.name}」。`);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };

  const setDefault = async (value: string) => {
    if (!active || !config || !value || busy) return;
    const [providerId, modelId] = JSON.parse(value) as [string, string];
    setBusy(true); setError(''); setNotice('');
    try {
      const next = await api<ConfigPayload>(`/api/media-model-config/${active.group}/default`, 'POST', {
        provider_id: providerId, model_id: modelId, expected_revision: config.revision,
      });
      apply(next); setNotice(`默认${LABELS[active.group]}模型已更新。`);
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };

  const testImage = async () => {
    if (testing) return;
    setTesting(true); setError(''); setNotice('');
    try {
      const result = await api<{ path: string; width: number; height: number }>('/api/ripple/media/generate/image', 'POST', {
        prompt: 'A minimal abstract ripple on a clean white background, editorial product illustration, no text',
        size: '1024x1024', resolution: '1k',
      });
      setNotice(`测试生图成功：${result.path} · ${result.width}×${result.height}`);
    } catch (e) { setError(errorText(e)); } finally { setTesting(false); }
  };

  const editingProtocol = editor ? protocolFor(editor.protocol) : null;
  const candidates = useMemo(() => {
    if (!editor) return [];
    const ids = new Map<string, DiscoveredModel>();
    for (const item of discovered) ids.set(item.id, item);
    for (const id of editor.models) if (!ids.has(id)) ids.set(id, { id, name: id });
    const query = modelSearch.trim().toLowerCase();
    return Array.from(ids.values())
      .filter((item) => (!query || item.id.toLowerCase().includes(query)) && (modelFilter === 'all' || editor.models.includes(item.id)))
      .sort((a, b) => Number(editor.models.includes(b.id)) - Number(editor.models.includes(a.id)) || a.id.localeCompare(b.id))
      .slice(0, 200);
  }, [editor, discovered, modelSearch, modelFilter]);

  const defaultValue = active?.default ? JSON.stringify([active.default.provider_id, active.default.model_id]) : '';
  const defaultOptions = active?.providers.filter((provider) => provider.configured).flatMap((provider) => provider.models.map((model) => ({ value: JSON.stringify([provider.id, model.id]), label: `${provider.name} · ${model.name || model.id}` }))) || [];

  return <section className="r2-settings-section r2-media-model-settings">
    <div><h2>媒体生成模型</h2></div>
    <div className="r2-settings-form r2-media-model-config"><Feedback error={error} notice={notice} />
      {groups.length > 0 && <>
        <div className="r2-model-tabs" role="tablist" aria-label="媒体生成模型">
          {groups.map((group) => <button type="button" role="tab" aria-selected={group.group === activeGroup}
            className={`r2-model-tab ${group.group === activeGroup ? 'active' : ''}`} key={group.group} onClick={() => selectGroup(group.group)}>
            <span>{LABELS[group.group] || group.label}</span><small className={group.configured ? 'ready' : ''}>{group.providers.length} 个 API</small>
          </button>)}
        </div>

        {active && <div className="r2-model-panel" role="tabpanel" aria-label={LABELS[active.group] || active.label}>
          <div className="r2-default-model-row">
            <label><span>默认{LABELS[active.group]}模型</span><select value={defaultValue} disabled={busy || !defaultOptions.length} onChange={(e) => void setDefault(e.target.value)}>
              {!defaultOptions.length && <option value="">尚无可用模型</option>}
              {defaultOptions.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
            </select></label>
            <button className="r2-button" disabled={busy} onClick={beginAdd}>+ 添加 API</button>
          </div>

          <div className="r2-api-list">
            {active.providers.length === 0 && <div className="r2-api-empty">还没有绑定 Provider。添加新 Provider，或复用已经配置过的 Provider。</div>}
            {active.providers.map((provider) => <div className={`r2-api-row ${provider.is_default ? 'default' : ''}`} key={provider.id}>
              <div className="r2-api-main">
                <div className="r2-api-title"><strong>{provider.name}</strong>{provider.is_default && <span className="r2-api-badge">默认</span>}<span className={`r2-api-state ${provider.configured ? 'ready' : ''}`}>{provider.configured ? '已配置' : '待完善'}</span></div>
                <small>{NETWORK_LABELS[provider.network.mode]} · {provider.protocol_label} · {provider.models.length} 个模型{provider.default_model ? ` · 默认 ${provider.default_model}` : ''}</small>
                {provider.models.length > 0 && <div className="r2-model-chips">{provider.models.slice(0, 4).map((model) => <span key={model.id}>{model.name || model.id}</span>)}{provider.models.length > 4 && <span>+{provider.models.length - 4}</span>}</div>}
              </div>
              <div className="r2-api-actions"><button disabled={busy} onClick={() => beginEdit(provider)}>管理</button><button className="danger" disabled={busy} onClick={() => void removeProvider(provider)}>移除</button></div>
            </div>)}
          </div>

          {active.group === 'image' && active.configured && <div className="r2-toolbar r2-api-test"><button className="r2-button" disabled={testing || busy} onClick={() => void testImage()}>{testing ? '生成中…' : '测试默认生图 API'}</button></div>}
        </div>}
      </>}
    </div>

    {editor && active && editingProtocol && <Modal title={editor.provider_id ? '编辑 API Provider' : '添加 API Provider'} onClose={closeEditor} busy={busy || discovering} className="r2-provider-dialog">
      <div className="r2-provider-tabs"><button className={modalTab === 'connection' ? 'active' : ''} onClick={() => setModalTab('connection')}>连接</button><button className={modalTab === 'models' ? 'active' : ''} onClick={() => setModalTab('models')}>模型 <span>{editor.models.length}</span></button></div>
      <div className="r2-provider-dialog-body">
        <Feedback error={modalError} notice={modalNotice} />
        {modalTab === 'connection' ? <>
          {!editor.provider_id && reusable.length > 0 && <label className="r2-field compact-model-field"><span>Provider</span><select value="" onChange={(e) => attachExistingProvider(e.target.value)}><option value="">创建新的 Provider</option>{reusable.map((item) => <option key={item.id} value={item.id}>复用：{item.name} · {item.base_url}</option>)}</select></label>}
          {editor.provider_id && (config?.providers.find((item) => item.id === editor.provider_id)?.groups.length || 0) > 1 && <div className="r2-inline-warning">这个 Provider 同时用于其他媒体类型。修改地址、密钥或网络路径会影响所有引用它的模型。</div>}
          <label className="r2-field compact-model-field"><span>名称</span><input value={editor.name} onChange={(e) => updateEditor({ name: e.target.value })} placeholder="例如：m365 / 主力生成服务 / 内网模型" /></label>
          {active.protocols.length > 1 && <label className="r2-field compact-model-field"><span>接口协议</span><select value={editor.protocol} onChange={(e) => {
            const protocol = active.protocols.find((item) => item.id === e.target.value); updateEditor({ protocol: e.target.value, route_settings: {}, base_url: editor.base_url || protocol?.default_base || '' });
          }}>{active.protocols.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select></label>}
          <label className="r2-field compact-model-field"><span>API 根地址</span><input value={editor.base_url} onChange={(e) => updateEditor({ base_url: e.target.value })} placeholder="https://api.example.com/v1" /></label>
          {editingProtocol.auth.includes('api_key') && <SecretField label="API Key" value={editor.api_key} saved={!!editor.secrets.api_key} onChange={(value) => updateEditor({ api_key: value })} />}
          {editingProtocol.auth.includes('access_key') && <SecretField label="Access Key" value={editor.access_key} saved={!!editor.secrets.access_key} onChange={(value) => updateEditor({ access_key: value })} />}
          {editingProtocol.auth.includes('secret_key') && <SecretField label="Secret Key" value={editor.secret_key} saved={!!editor.secrets.secret_key} onChange={(value) => updateEditor({ secret_key: value })} />}

          <fieldset className="r2-network-path"><legend>网络路径</legend>{(['system', 'direct', 'custom'] as const).map((mode) => <label className={editor.network_mode === mode ? 'active' : ''} key={mode}><input type="radio" checked={editor.network_mode === mode} onChange={() => updateEditor({ network_mode: mode, proxy_url: mode === 'custom' ? editor.proxy_url : '' })} /><span><strong>{NETWORK_LABELS[mode]}</strong><small>{mode === 'system' ? '继承 Ripple 当前系统/进程代理' : mode === 'direct' ? '显式绕过应用层代理' : '仅此 Provider 使用指定 HTTP(S) / SOCKS5 代理'}</small></span></label>)}</fieldset>
          {editor.network_mode === 'system' && <p className="r2-provider-network-note">当前检测：{config?.system_proxy.available ? [config.system_proxy.https, config.system_proxy.http].filter(Boolean).join(' · ') : '未检测到显式系统代理'}</p>}
          {editor.network_mode === 'custom' && <label className="r2-field compact-model-field"><span>自定义代理</span><input value={editor.proxy_url} onChange={(e) => updateEditor({ proxy_url: e.target.value })} placeholder="http://127.0.0.1:7890 或 socks5://127.0.0.1:1080" /></label>}

          {editingProtocol.fields.map((field) => <RouteFieldInput key={field.key} field={field} value={editor.route_settings[field.key] || ''} onChange={(value) => updateEditor({ route_settings: { ...editor.route_settings, [field.key]: value } })} />)}
          <div className="r2-provider-discovery-row"><button className="r2-button" disabled={discovering} onClick={() => void discoverModels()}>{discovering ? '正在获取…' : editingProtocol.discoverable ? '测试连接并拉取模型' : '进入模型管理'}</button><small>{editingProtocol.discoverable ? '只获取候选模型，不会产生生成费用。' : '当前协议没有标准模型列表接口，可手动添加模型 ID。'}</small></div>
        </> : <>
          <div className="r2-model-manager-head"><input value={modelSearch} onChange={(e) => setModelSearch(e.target.value)} placeholder="搜索模型…" /><div><button className={modelFilter === 'all' ? 'active' : ''} onClick={() => setModelFilter('all')}>全部</button><button className={modelFilter === 'selected' ? 'active' : ''} onClick={() => setModelFilter('selected')}>已选 {editor.models.length}</button></div><button className="r2-button" disabled={discovering} onClick={() => void discoverModels()}>{discovering ? '刷新中…' : '刷新列表'}</button></div>
          <div className="r2-model-discovery-list">
            {candidates.length === 0 && <div className="r2-api-empty">暂无候选模型。可以刷新模型列表，或在下方手动添加模型 ID。</div>}
            {candidates.map((model) => <label key={model.id}><input type="checkbox" checked={editor.models.includes(model.id)} onChange={(e) => toggleModel(model.id, e.target.checked)} /><span>{model.name || model.id}</span>{editor.default_model === model.id && <small>默认</small>}</label>)}
          </div>
          {(discovered.length > 200 || candidates.length >= 200) && <p className="r2-provider-network-note">列表较长，当前最多展示 200 条；使用搜索可定位其他模型。</p>}
          <div className="r2-manual-model"><input value={manualModel} onChange={(e) => setManualModel(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); addManualModel(); } }} placeholder="手动输入模型 ID" /><button className="r2-button" onClick={addManualModel}>添加模型</button></div>
          <label className="r2-field compact-model-field"><span>此 Provider 的默认模型</span><select value={editor.default_model} disabled={!editor.models.length} onChange={(e) => updateEditor({ default_model: e.target.value })}><option value="">请选择</option>{editor.models.map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
          <label className="r2-checkbox r2-provider-default-check"><input type="checkbox" checked={editor.set_default} disabled={!editor.models.length} onChange={(e) => updateEditor({ set_default: e.target.checked })} />保存后设为 {LABELS[active.group]} 的默认 Provider + 模型</label>
        </>}
      </div>
      <footer className="r2-provider-dialog-footer"><button className="r2-button" disabled={busy || discovering} onClick={closeEditor}>取消</button>{modalTab === 'connection' && <button className="r2-button" disabled={busy || discovering} onClick={() => setModalTab('models')}>下一步：模型</button>}<button className="r2-button primary" disabled={busy || discovering} onClick={() => void saveProvider()}>{busy ? '保存中…' : '保存 Provider'}</button></footer>
    </Modal>}
  </section>;
}

function SecretField({ label, value, saved, onChange }: { label: string; value: string; saved: boolean; onChange: (value: string) => void }) {
  return <label className="r2-field compact-model-field"><span>{label}</span><input type="password" autoComplete="new-password" value={value} onChange={(e) => onChange(e.target.value)} placeholder={saved ? '已保存；留空保持不变' : `填写 ${label}`} /></label>;
}

function RouteFieldInput({ field, value, onChange }: { field: RouteField; value: string; onChange: (value: string) => void }) {
  if (field.kind === 'select') return <label className="r2-field compact-model-field"><span>{field.label}{field.required ? '' : ' · 可选'}</span><select value={value} onChange={(e) => onChange(e.target.value)}><option value="">请选择</option>{field.choices.map((choice) => <option key={choice} value={choice}>{choice}</option>)}</select></label>;
  return <label className="r2-field compact-model-field"><span>{field.label}{field.required ? '' : ' · 可选'}</span><input value={value} onChange={(e) => onChange(e.target.value)} placeholder={field.placeholder || `填写 ${field.label}`} /></label>;
}
