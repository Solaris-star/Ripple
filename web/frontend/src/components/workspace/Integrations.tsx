import { useEffect, useState } from 'react';
import { api, errorText } from '../../lib/ripple';
import type { RippleStatus } from '../../lib/ripple';
import { Feedback, Header } from './Common';
import ModelSettings from './ModelSettings';
import MediaModelSettings from './MediaModelSettings';
import AgentExtensionsSettings from './AgentExtensionsSettings';
import AgentRuntimeSettings from './AgentRuntimeSettings';
import EnvironmentSettings from './EnvironmentSettings';
import ExecutionNodeSettings from './ExecutionNodeSettings';
import UserAccessSettings from './UserAccessSettings';

export default function Integrations() {
  const [status, setStatus] = useState<RippleStatus | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    void api<RippleStatus>('/api/ripple/status').then(setStatus).catch(e => setError(errorText(e)));
  }, []);

  return <div className="page-scroll r2-page r2-settings">
    <Header title="设置" subtitle="" />
    <Feedback error={error} />
    <section className="r2-settings-section">
      <div><h2>用户与访问</h2></div>
      <UserAccessSettings />
    </section>
    <AgentRuntimeSettings />
    <ModelSettings />
    <AgentExtensionsSettings />
    <MediaModelSettings />
    <section className="r2-settings-section">
      <div><h2>执行节点</h2></div>
      <ExecutionNodeSettings />
    </section>
    <section className="r2-settings-section">
      <div><h2>本地环境</h2></div>
      <EnvironmentSettings />
    </section>
    <section className="r2-settings-section">
      <div><h2>关于 Ripple</h2></div>
      <div><span className="r2-muted">版本 {status?.version || '0.2.6'}</span></div>
    </section>
  </div>;
}
