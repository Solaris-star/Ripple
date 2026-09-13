import { useEffect, useMemo, useState } from 'react';
import {
  acknowledgeAgentProfile,
  errorText,
  fetchAgentProfiles,
  fetchAgentRuntimes,
  runAgentAdapterAction,
  saveAgentProfileInheritance,
  scanAgentProfiles,
  setDefaultAgentProfile,
} from '../../lib/ripple';
import type {
  AgentAdapterAction,
  AgentAdapterOption,
  AgentProfileMeta,
  AgentProfileState,
  AgentRuntimeCatalog,
  AgentRuntimeMeta,
} from '../../lib/ripple';
import { Feedback } from './Common';

const SUPPORTED = [
  { runtime: 'opencode', name: 'OpenCode' },
  { runtime: 'claude_code', name: 'Claude Code' },
  { runtime: 'codex', name: 'Codex' },
  { runtime: 'hermes', name: 'Hermes' },
] as const;

const INHERITANCE: Array<{ key: keyof AgentProfileMeta['inheritance']; label: string; title: string }> = [
  { key: 'rules', label: 'Rules', title: '继承 Rules / Instructions 文本' },
  { key: 'skills', label: 'Skills', title: '继承 Skill 名称目录；执行能力仍由 Ripple 授权' },
  { key: 'agents', label: 'Subagents', title: '继承 Subagent 名称目录' },
  { key: 'commands', label: 'Commands', title: '继承 Command 名称目录' },
  { key: 'mcp', label: 'MCP', title: '继承 MCP 名称，不复制连接信息或凭据' },
  { key: 'plugins', label: 'Plugins', title: '继承 Plugin 名称，不加载插件代码' },
  { key: 'model', label: 'Model', title: '沿用本机 Agent 当前模型' },
  { key: 'effort', label: 'Effort', title: '沿用本机 Agent 当前 Effort' },
  { key: 'memory', label: 'Memory', title: '读取明确扫描到的 Memory 文本；默认关闭' },
];

const CONNECTION_LABEL: Record<string, string> = {
  connected: '已连接', ready: '可用', needs_config: '待配置', auth_required: '需登录', needs_dependency: '缺少运行依赖', blocked: '已检测 · 暂不可用', detected: '已检测', not_installed: '未安装',
};

function fallbackRuntime(runtime: string, name: string): AgentRuntimeMeta {
  return { runtime, name, installed: false, healthy: false, connection_state: 'not_installed', capabilities: {}, adapters: [] };
}

function counts(profile?: AgentProfileMeta) {
  if (!profile) return [];
  return [
    ['Rules', profile.components?.rules?.count || 0], ['Skills', profile.components?.skills?.count || 0],
    ['Agents', profile.components?.agents?.count || 0], ['Commands', profile.components?.commands?.count || 0],
    ['MCP', profile.components?.mcp?.count || 0], ['Plugins', profile.components?.plugins?.count || 0],
  ].filter(([, value]) => Number(value) > 0);
}

export default function AgentRuntimeSettings() {
  const [runtimes, setRuntimes] = useState<AgentRuntimeCatalog | null>(null);
  const [profiles, setProfiles] = useState<AgentProfileState | null>(null);
  const [busy, setBusy] = useState(false);
  const [activeAction, setActiveAction] = useState('');
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = async () => {
    const [runtimeResult, profileResult] = await Promise.allSettled([fetchAgentRuntimes(), fetchAgentProfiles()]);
    if (runtimeResult.status === 'fulfilled') setRuntimes(runtimeResult.value);
    if (profileResult.status === 'fulfilled') setProfiles(profileResult.value);
    if (runtimeResult.status === 'rejected') setError('Agent 状态读取失败；支持的 Agent 仍会显示，可点击“重新扫描”重试。');
  };
  useEffect(() => { void load(); }, []);

  const displayed = useMemo(() => {
    const byId = new Map((runtimes?.items || []).map((row) => [row.runtime, row]));
    return SUPPORTED.map((item) => byId.get(item.runtime) || fallbackRuntime(item.runtime, item.name));
  }, [runtimes]);
  const profileByRuntime = useMemo(() => new Map((profiles?.profiles || []).map((row) => [row.runtime_id, row])), [profiles]);

  const scan = async () => {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const state = await scanAgentProfiles();
      setProfiles(state);
      setRuntimes(await fetchAgentRuntimes());
      setNotice('扫描完成。仅检测到的本机 Agent 会显示适配选项。');
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };

  const setDefault = async (profileId: string) => {
    setBusy(true); setError(''); setNotice('');
    try { setProfiles(await setDefaultAgentProfile(profileId)); setRuntimes(await fetchAgentRuntimes()); setNotice('默认 Agent 已更新。'); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };

  const toggle = async (profile: AgentProfileMeta, key: keyof AgentProfileMeta['inheritance'], value: boolean) => {
    setBusy(true); setError(''); setNotice('');
    try { setProfiles(await saveAgentProfileInheritance(profile.id, { [key]: value })); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };

  const acknowledge = async (profileId: string) => {
    setBusy(true); setError(''); setNotice('');
    try { setProfiles(await acknowledgeAgentProfile(profileId)); setNotice('配置变化已确认。'); }
    catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };

  const runAdapterAction = async (runtime: AgentRuntimeMeta, adapter: AgentAdapterOption, action: AgentAdapterAction) => {
    if (busy || activeAction) return;
    const key = `${runtime.runtime}:${adapter.id}:${action.id}`;
    setBusy(true); setActiveAction(key); setError(''); setNotice('');
    try {
      const result = await runAgentAdapterAction(runtime.runtime, adapter.id, action.id);
      setRuntimes(result.runtimes);
      setNotice(result.result?.detail || `${adapter.label} 操作完成。`);
    } catch (e) {
      setError(errorText(e));
    } finally {
      setBusy(false);
      setActiveAction('');
    }
  };

  return <section className="r2-settings-section" data-section="agent-runtimes">
    <div><h2>Agent</h2></div>
    <div className="r2-settings-form"><Feedback error={error} notice={notice} />
      <div className="r2-section-heading"><strong>本机 Agent</strong><button className="r2-button" disabled={busy} onClick={() => void scan()}>{busy && !activeAction ? '扫描中…' : '重新扫描'}</button></div>
      <div className="r2-agent-runtime-list">{displayed.map((runtime) => {
        const profile = profileByRuntime.get(runtime.runtime);
        const isDefault = runtimes?.default_runtime === runtime.runtime;
        const selectable = !!(runtime.selectable ?? runtime.healthy);
        const model = runtime.current_model || profile?.model || (runtime.runtime === 'opencode' ? '未配置' : '跟随 Agent 默认');
        const state = runtime.connection_state || (runtime.installed ? (selectable ? 'ready' : 'detected') : 'not_installed');
        const stateLabel = state === 'needs_dependency'
          ? (runtime.dependency === 'docker' ? '需要 Docker' : runtime.dependency === 'hermes_container' ? '需要 Hermes 容器' : '待完成安全验证')
          : (CONNECTION_LABEL[state] || state);
        return <div className={`r2-agent-runtime-row ${isDefault ? 'default' : ''}`} key={runtime.runtime}>
          <div className="r2-agent-runtime-main">
            <div className="r2-agent-runtime-title"><strong>{runtime.name}</strong>{isDefault && <span>默认</span>}</div>
            <small>{runtime.version || runtime.runtime}</small>
          </div>
          <div className={`r2-agent-bound-model r2-agent-state ${selectable ? 'ready' : ''}`}><span>状态</span><strong>{stateLabel}</strong></div>
          <div className="r2-agent-bound-model"><span>模型</span><strong title={model}>{model}</strong></div>
          <div className="r2-agent-runtime-actions">
            {profile && selectable && !isDefault && <button className="r2-text-button" disabled={busy} onClick={() => void setDefault(profile.id)}>设为默认</button>}
            {profile?.changed_since_review && <button className="r2-text-button" disabled={busy} onClick={() => void acknowledge(profile.id)}>确认更新</button>}
          </div>
          {runtime.detail && runtime.installed && !selectable && <div className="r2-agent-runtime-detail" title={runtime.detail}>{runtime.detail}</div>}
          {!!runtime.adapters?.length && <div className="r2-agent-adapter-list">
            {runtime.adapters.map((adapter) => <div className={`r2-agent-adapter ${adapter.state}`} key={adapter.id}>
              <div className="r2-agent-adapter-info">
                <div><strong>{adapter.label}</strong><span>{adapter.state_label}</span></div>
                <small>{adapter.package ? `${adapter.package}@${adapter.version}` : adapter.protocol}</small>
                <p>{adapter.detail || adapter.description}</p>
              </div>
              {!!adapter.actions.length && <div className="r2-agent-adapter-actions">
                {adapter.actions.map((action) => {
                  const key = `${runtime.runtime}:${adapter.id}:${action.id}`;
                  return <button
                    key={action.id}
                    className={action.danger ? 'r2-text-button' : 'r2-button'}
                    disabled={busy}
                    onClick={() => void runAdapterAction(runtime, adapter, action)}
                    title="仅操作 Ripple 私有适配目录，不修改 Agent 本体或全局配置"
                  >{activeAction === key ? '处理中…' : action.label}</button>;
                })}
              </div>}
            </div>)}
          </div>}
          {profile && profile.installed && <details className="r2-agent-inheritance">
            <summary>继承设置 {counts(profile).map(([label, value]) => `${label} ${value}`).join(' · ')}</summary>
            <div>{INHERITANCE.map((item) => <label key={item.key} title={item.title}>
              <input type="checkbox" disabled={busy || profile.changed_since_review} checked={!!profile.inheritance?.[item.key]} onChange={(e) => void toggle(profile, item.key, e.target.checked)} />
              <span>{item.label}</span>
            </label>)}</div>
          </details>}
        </div>;
      })}</div>
      <p className="r2-muted r2-agent-adapter-note">
        Ripple 不会预装 ACP。只有检测到本机 Agent 后，才会展示可信且已完成安全验证的按需安装项；没有可验证适配的 Agent 不会出现虚假下载按钮。
      </p>
    </div>
  </section>;
}
