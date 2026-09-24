import { useEffect, useMemo, useState } from 'react';
import { api, errorText } from '../../lib/ripple';
import type { RippleStatus } from '../../lib/ripple';
import { fetchAuthStatus } from '../../lib/auth';
import type { RippleAuthStatus } from '../../lib/auth';
import type { Page } from '../Sidebar';
import { Feedback, Header } from './Common';
import Accounts from './Accounts';
import ModelSettings from './ModelSettings';
import MediaModelSettings from './MediaModelSettings';
import AgentExtensionsSettings from './AgentExtensionsSettings';
import AgentRuntimeSettings from './AgentRuntimeSettings';
import EnvironmentSettings from './EnvironmentSettings';
import ExecutionNodeSettings from './ExecutionNodeSettings';
import UserAccessSettings from './UserAccessSettings';

type SettingsSection = 'accounts' | 'agent' | 'model' | 'mcp' | 'members' | 'about';
const SECTIONS = new Set<SettingsSection>(['accounts', 'agent', 'model', 'mcp', 'members', 'about']);

export default function Integrations({
  initialSection = 'accounts',
  onNavigate,
  onNewProfile,
  onEditProfile,
  selectedProfileId,
  onSelectProfile,
}: {
  initialSection?: SettingsSection;
  onNavigate: (page: Page) => void;
  onNewProfile: () => void;
  onEditProfile: (name: string) => void;
  selectedProfileId?: string;
  onSelectProfile: (profileId: string) => void;
}) {
  const fromUrl = new URLSearchParams(location.search).get('section') as SettingsSection | null;
  const [status, setStatus] = useState<RippleStatus | null>(null);
  const [auth, setAuth] = useState<RippleAuthStatus | null>(null);
  const [error, setError] = useState('');
  const [section, setSection] = useState<SettingsSection>(fromUrl && SECTIONS.has(fromUrl) ? fromUrl : initialSection);

  useEffect(() => {
    void Promise.allSettled([
      api<RippleStatus>('/api/ripple/status').then(setStatus),
      fetchAuthStatus().then(setAuth),
    ]).then((rows) => {
      const failure = rows.find((row) => row.status === 'rejected');
      if (failure?.status === 'rejected') setError(errorText(failure.reason));
    });
  }, []);

  useEffect(() => {
    if (section === 'members' && auth?.local) setSection('accounts');
  }, [auth?.local, section]);

  const tabs = useMemo<Array<[SettingsSection, string]>>(() => [
    ['accounts', '账号'],
    ['agent', 'Agent'],
    ['model', '模型'],
    ['mcp', 'MCP'],
    ...(!auth?.local && auth?.mode === 'server' ? [['members', '成员'] as [SettingsSection, string]] : []),
    ['about', '关于'],
  ], [auth?.local, auth?.mode]);

  const selectSection = (next: SettingsSection) => {
    setSection(next);
    const url = new URL(location.href);
    url.searchParams.set('page', 'integrations');
    url.searchParams.set('section', next);
    history.replaceState({}, '', url);
  };

  return <div className="page-scroll r2-page r2-settings focus-settings-page">
    <Header title="设置" subtitle="账号、Agent、模型、MCP 与运行信息" />
    <Feedback error={error} />
    <div className="focus-settings-layout">
      <nav className="focus-settings-nav" aria-label="设置分类">
        {tabs.map(([id, label]) => <button key={id} className={section === id ? 'active' : ''} onClick={() => selectSection(id)}>{label}</button>)}
      </nav>
      <div className="focus-settings-content">
        {section === 'accounts' && <Accounts embedded onNavigate={onNavigate} onNewProfile={onNewProfile} onEditProfile={onEditProfile} selectedProfileId={selectedProfileId} onSelectProfile={onSelectProfile} />}
        {section === 'agent' && <>
          <AgentRuntimeSettings />
          <section className="r2-settings-section">
            <details className="r2-settings-details">
              <summary>运行环境与设备</summary>
              <p className="r2-muted">登录浏览器、远程执行节点和本地依赖属于运行诊断，不影响账号画像本身。</p>
              <ExecutionNodeSettings />
              <EnvironmentSettings />
            </details>
          </section>
        </>}
        {section === 'model' && <><ModelSettings /><MediaModelSettings /></>}
        {section === 'mcp' && <AgentExtensionsSettings />}
        {section === 'members' && auth?.mode === 'server' && <section className="r2-settings-section">
          <div><h2>成员</h2><p className="r2-muted">管理当前 Workspace 的登录成员与角色。</p></div>
          <UserAccessSettings />
        </section>}
        {section === 'about' && <section className="r2-settings-section">
          <div><h2>关于</h2></div>
          <div className="r2-about-grid">
            <span>Ripple 版本</span><strong>{status?.version || '读取中…'}</strong>
            <span>运行模式</span><strong>{auth?.local ? '本机' : auth?.mode === 'server' ? 'Server' : '读取中…'}</strong>
            <span>AI 能力</span><strong>{status?.ai_enabled ? '已启用' : '未启用'}</strong>
          </div>
        </section>}
      </div>
    </div>
  </div>;
}
