import type {
  AgentCapabilityCatalog,
  AgentProfileState,
  AgentRuntimeCatalog,
  AgentSessionConfig,
} from '../lib/ripple';

const EFFORT_LABEL: Record<string, string> = {
  auto: 'Auto', low: 'Low', medium: 'Medium', high: 'High', extra_high: 'Extra High',
};
const ALL_EFFORTS = ['auto', 'low', 'medium', 'high', 'extra_high'];

type Props = {
  config: AgentSessionConfig | null;
  catalog: AgentCapabilityCatalog | null;
  runtimes: AgentRuntimeCatalog | null;
  profiles: AgentProfileState | null;
  isStreaming?: boolean;
  messageCount?: number;
  onChange: (patch: Partial<AgentSessionConfig>) => Promise<void> | void;
  className?: string;
  agentMode?: 'select' | 'label';
};

export default function AgentSessionSelectors({
  config, catalog, runtimes, profiles, isStreaming = false, messageCount = 0, onChange, className = '', agentMode = 'select',
}: Props) {
  const selectedRuntime = runtimes?.items.find((row) => row.runtime === config?.runtime_id);
  const runtimeProfiles = (profiles?.profiles || []).filter((row) => row.installed && row.runtime_id === config?.runtime_id);
  const selectedProfile = runtimeProfiles.find((row) => row.id === config?.profile_id);
  const runtimeLocked = !!config?.runtime_locked || messageCount > 0;
  const nativeRuntime = !!config && config.runtime_id !== 'opencode';
  const reviewed = !selectedProfile?.changed_since_review;
  const inheritedModel = nativeRuntime && reviewed && selectedProfile?.inheritance?.model
    ? (selectedRuntime?.current_model || selectedProfile.model || '')
    : '';
  const inheritedEffort = nativeRuntime && reviewed && selectedProfile?.inheritance?.effort
    ? (selectedRuntime?.current_effort || selectedProfile.effort || '')
    : '';
  const runtimeModels = (selectedRuntime?.models?.length ? selectedRuntime.models : (config?.runtime_id === 'opencode' ? (catalog?.models || []) : []));
  const modelOptions = runtimeModels.length ? runtimeModels : (selectedRuntime?.current_model ? [{ id: selectedRuntime.current_model, name: selectedRuntime.current_model, effort_levels: ['auto'] }] : []);
  const effectiveModel = modelOptions.some((row) => row.id === config?.model)
    ? (config?.model || '')
    : (inheritedModel || selectedRuntime?.current_model || modelOptions[0]?.id || '');
  const activeModel = modelOptions.find((row) => row.id === effectiveModel);
  const effortOptions = activeModel?.effort_levels?.length ? [...new Set(['auto', ...activeModel.effort_levels])] : ALL_EFFORTS;

  const selectRuntime = async (runtimeId: string) => {
    if (!config || runtimeLocked || isStreaming) return;
    const runtime = runtimes?.items.find((row) => row.runtime === runtimeId);
    if (!runtime?.selectable && !runtime?.healthy) return;
    const candidates = (profiles?.profiles || []).filter((row) => row.installed && row.runtime_id === runtimeId);
    const preferred = candidates.find((row) => row.id === profiles?.default_profile)
      || candidates.find((row) => !row.changed_since_review)
      || candidates[0];
    await onChange({ runtime_id: runtimeId, ...(preferred ? { profile_id: preferred.id } : {}), effort: 'auto' });
  };

  return <div className={`r2-agent-selectors ${className}`.trim()}>
    {agentMode === 'label'
      ? <span className="r2-agent-runtime-label" aria-label="Agent" title={runtimeLocked ? '当前会话已绑定此 Agent' : '新会话跟随设置中的默认 Agent'}>{selectedRuntime?.name || 'Agent'}</span>
      : <select aria-label="Agent" title={runtimeLocked ? '当前会话已锁定 Agent；新建会话后可切换' : '选择 Agent'}
          value={config?.runtime_id || ''} disabled={!config || isStreaming || runtimeLocked}
          onChange={(e) => void selectRuntime(e.target.value)}>
          {(runtimes?.items || []).map((runtime) => <option key={runtime.runtime} value={runtime.runtime}
            disabled={!(runtime.selectable ?? runtime.healthy)}>
            {runtime.name}{(runtime.selectable ?? runtime.healthy) ? '' : ' · 不可用'}
          </option>)}
        </select>}

    <select aria-label="模型" value={effectiveModel} disabled={!config || isStreaming || !modelOptions.length}
      onChange={(e) => void onChange({ model: e.target.value, effort: 'auto' })}>
      {modelOptions.map((model) => <option key={model.id} value={model.id}>{model.name}</option>)}
    </select>

    <select aria-label="Effort" value={config?.effort || 'auto'} disabled={!config || isStreaming}
      onChange={(e) => void onChange({ effort: e.target.value })}>
      {effortOptions.map((level) => <option key={level} value={level}>
        {level === 'auto' && inheritedEffort ? `Auto · ${EFFORT_LABEL[inheritedEffort] || inheritedEffort}` : (EFFORT_LABEL[level] || level)}
      </option>)}
    </select>
  </div>;
}
