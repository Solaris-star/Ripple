import { type FormEvent, useCallback, useEffect, useState } from 'react';
import { api, errorText } from '../../lib/ripple';
import { AUTH_CHANGED_EVENT, fetchAuthStatus } from '../../lib/auth';
import type { RippleAuthStatus } from '../../lib/auth';
import { clearBrowserAuthScope } from '../../lib/store';
import { Feedback, Modal } from './Common';

interface ManagedUser { id: string; username: string; email?: string; role: 'owner' | 'admin' | 'member'; disabled?: boolean; created_at: string; }

export default function UserAccessSettings() {
  const [status, setStatus] = useState<RippleAuthStatus | null>(null);
  const [users, setUsers] = useState<ManagedUser[]>([]);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [role, setRole] = useState<'admin' | 'member'>('member');

  const refresh = useCallback(async () => {
    const next = await fetchAuthStatus();
    setStatus(next);
    if (next.mode === 'server' && next.authenticated && ['owner', 'admin'].includes(next.user?.role || '')) {
      const result = await api<{ items: ManagedUser[] }>('/api/ripple/auth/users');
      setUsers(result.items);
    } else setUsers([]);
  }, []);
  useEffect(() => { void refresh().catch(value => setError(errorText(value))); }, [refresh]);

  const run = async (fn: () => Promise<void>) => {
    if (busy) return;
    setBusy(true); setError(''); setNotice('');
    try { await fn(); await refresh(); } catch (value) { setError(errorText(value)); } finally { setBusy(false); }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void run(async () => {
      await api('/api/ripple/auth/users', 'POST', { username: username.trim(), email: email.trim(), password, role });
      setUsername(''); setEmail(''); setPassword(''); setRole('member'); setAdding(false); setNotice('用户已加入当前 Workspace。');
    });
  };

  const logout = () => run(async () => {
    await api('/api/ripple/auth/logout', 'POST', {});
    clearBrowserAuthScope();
    window.dispatchEvent(new Event(AUTH_CHANGED_EVENT));
  });

  if (!status) return <p className="r2-muted">正在读取访问模式…</p>;
  if (status.local) return <div className="r2-access-summary"><div><strong>本机模式</strong><span>仅本机使用 · 默认免登录</span></div><p>切换到 Server 模式后，Ripple 会要求用户登录，并把内容、账号、执行节点和发布任务放在 Workspace 访问边界内。</p></div>;

  const canManage = ['owner', 'admin'].includes(status.user?.role || '');
  return <div className="r2-access-settings">
    <Feedback error={error} notice={notice} />
    <div className="r2-access-current"><div><strong>{status.user?.username}</strong><span>{status.user?.role} · {status.user?.workspace_name}</span></div><button className="r2-button" disabled={busy} onClick={() => void logout()}>退出登录</button></div>
    {canManage && <><div className="r2-section-heading"><h3>Workspace 用户</h3><button className="r2-button" disabled={busy} onClick={() => setAdding(true)}>添加用户</button></div><div className="r2-user-list">{users.map(user => <div key={user.id}><div><strong>{user.username}</strong><span>{user.email || '未填写 Email'} · {user.role}</span></div>{status.user?.role === 'owner' && user.id !== status.user.id && <button className="r2-text-button danger" disabled={busy} onClick={() => { if (window.confirm(`移除用户「${user.username}」？该用户的 Server Session 会立即失效。`)) void run(async () => { await api(`/api/ripple/auth/users/${user.id}`, 'DELETE', { confirmed: true }); setNotice('用户已移除。'); }); }}>移除</button>}</div>)}</div></>}
    {adding && <Modal title="添加 Workspace 用户" busy={busy} onClose={() => { setAdding(false); setPassword(''); }}><form className="r2-auth-form" onSubmit={submit}><label><span>用户名</span><input autoFocus value={username} onChange={e => setUsername(e.target.value)} autoComplete="off" minLength={3} maxLength={80} required /></label><label><span>Email（可选）</span><input type="email" value={email} onChange={e => setEmail(e.target.value)} autoComplete="off" maxLength={200} /></label><label><span>角色</span><select value={role} onChange={e => setRole(e.target.value as 'admin' | 'member')}><option value="member">Member</option><option value="admin">Admin</option></select></label><label><span>初始密码</span><input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="new-password" minLength={12} maxLength={256} required /></label><p className="r2-muted">密码不会写入浏览器存储。用户加入当前 Workspace；Owner 可以随时移除其访问权限。</p><footer><button type="button" className="r2-button" disabled={busy} onClick={() => { setAdding(false); setPassword(''); }}>取消</button><button className="r2-button primary" disabled={busy || !username.trim() || password.length < 12}>创建用户</button></footer></form></Modal>}
  </div>;
}
