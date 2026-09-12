import { type FormEvent, type ReactNode, useCallback, useEffect, useState } from 'react';
import { api, errorText } from '../lib/ripple';
import { AUTH_CHANGED_EVENT, fetchAuthStatus } from '../lib/auth';
import type { RippleAuthStatus } from '../lib/auth';
import { activateBrowserAuthScope, clearBrowserAuthScope } from '../lib/store';

function AuthCard({ title, subtitle, children }: { title: string; subtitle: string; children: ReactNode }) {
  return <main className="r2-auth-page"><section className="r2-auth-card"><div className="r2-auth-brand"><img src="./static/ripple-mark.svg" alt="" /><strong>Ripple</strong></div><h1>{title}</h1><p>{subtitle}</p>{children}</section></main>;
}

export default function AuthBoundary({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<RippleAuthStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [workspace, setWorkspace] = useState('Ripple Workspace');
  const [password, setPassword] = useState('');
  const [setupCode, setSetupCode] = useState('');

  const refresh = useCallback(async () => {
    try {
      const next = await fetchAuthStatus();
      if (next.local) activateBrowserAuthScope('local');
      else if (next.authenticated && next.user) activateBrowserAuthScope(`${next.user.workspace_id}:${next.user.id}`);
      else clearBrowserAuthScope();
      setStatus(next); setError('');
    } catch (value) { setError(errorText(value)); }
  }, []);
  useEffect(() => { void refresh(); }, [refresh]);
  useEffect(() => {
    const listener = () => void refresh();
    window.addEventListener(AUTH_CHANGED_EVENT, listener);
    return () => window.removeEventListener(AUTH_CHANGED_EVENT, listener);
  }, [refresh]);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!status || busy) return;
    setBusy(true); setError('');
    try {
      if (status.setup_required) {
        await api('/api/ripple/auth/setup', 'POST', { username: username.trim(), email: email.trim(), workspace_name: workspace.trim(), password, setup_code: setupCode, confirmed: true });
      } else {
        await api('/api/ripple/auth/login', 'POST', { username: username.trim(), password });
      }
      setPassword(''); setSetupCode(''); await refresh();
    } catch (value) { setError(errorText(value)); } finally { setBusy(false); }
  };

  if (!status) return <AuthCard title="正在连接 Ripple" subtitle="正在读取部署模式和访问状态。">{error && <div className="notice-error">{error}</div>}</AuthCard>;
  if (status.local || status.authenticated) return <>{children}</>;
  if (status.setup_required && (!status.security.configured || !status.security.bootstrap_configured)) return <AuthCard title="Server 安全配置未完成" subtitle="公开部署前必须建立 HTTPS、可信 Host 和一次性初始化边界。"><div className="r2-inline-warning">请配置 <code>RIPPLE_DEPLOYMENT_MODE=server</code>、HTTPS 的 <code>RIPPLE_PUBLIC_ORIGIN</code>、<code>RIPPLE_TRUSTED_HOSTS</code>，以及至少 24 字符的 <code>RIPPLE_BOOTSTRAP_CODE</code>，然后重启 Ripple。首次 Owner 创建完成后可从运行环境移除初始化码。</div></AuthCard>;

  return <AuthCard title={status.setup_required ? '创建 Ripple 管理员' : '登录 Ripple'} subtitle={status.setup_required ? '这是此 Server 的第一个 Owner，并会创建默认 Workspace。' : '使用 Ripple Server 账号进入你的 Workspace。'}>
    <form className="r2-auth-form" onSubmit={submit} autoComplete="on">
      <label><span>{status.setup_required ? '用户名' : '用户名或 Email'}</span><input autoFocus value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" maxLength={200} required /></label>
      {status.setup_required && <label><span>Email（可选）</span><input type="email" value={email} onChange={e => setEmail(e.target.value)} autoComplete="email" maxLength={200} /></label>}
      {status.setup_required && <label><span>Workspace 名称</span><input value={workspace} onChange={e => setWorkspace(e.target.value)} maxLength={100} required /></label>}
      {status.setup_required && <label><span>Server 初始化码</span><input type="password" value={setupCode} onChange={e => setSetupCode(e.target.value)} autoComplete="off" minLength={24} maxLength={512} required /></label>}
      <label><span>密码</span><input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete={status.setup_required ? 'new-password' : 'current-password'} minLength={12} maxLength={256} required /></label>
      {status.setup_required && <small>至少 12 个字符。密码只用于当前 Ripple Server，不会写入浏览器存储。</small>}
      {error && <div className="notice-error">{error}</div>}
      <button className="r2-button primary" disabled={busy || !username.trim() || password.length < 12 || (status.setup_required && (!workspace.trim() || setupCode.length < 24))}>{busy ? '处理中…' : status.setup_required ? '创建并进入 Ripple' : '登录'}</button>
    </form>
  </AuthCard>;
}
