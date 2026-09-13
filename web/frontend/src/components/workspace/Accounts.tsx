import { useCallback, useEffect, useRef, useState } from 'react';
import type { Page } from '../Sidebar';
import { api, base, dateText, errorText, ACCOUNT_LABELS } from '../../lib/ripple';
import { newId } from '../../lib/id';
import type { Account, BlogConnector, Channel, ExecutionNode, RippleStatus } from '../../lib/ripple';
import { Header, Feedback, Mark, Modal } from './Common';

interface XConfig { configured: boolean; client_id: string; callback_url: string; scopes: string[]; adapter: string; }
interface XConnectResult { account: Account; authorize_url: string; callback_url: string; }
type BrowserChannel = 'msedge' | 'chrome' | 'chromium';

function currentBrowserChannel(): BrowserChannel | undefined {
  const ua = navigator.userAgent;
  if (/\bEdg\//.test(ua)) return 'msedge';
  if (/\bChrome\//.test(ua) && !/\bOPR\//.test(ua)) return 'chrome';
  return undefined;
}

function browserLabel(channel?: string | null): string {
  if (channel === 'msedge') return 'Microsoft Edge';
  if (channel === 'chrome') return 'Google Chrome';
  if (channel === 'chromium') return 'Chromium';
  return '自动选择浏览器';
}

function blogErrorText(value: unknown): string {
  const message = errorText(value);
  return /not found/i.test(message)
    ? 'Blog 连接服务尚未加载。重启 Ripple 服务后会自动恢复；其他平台账号不受影响。'
    : `Blog 连接暂不可用：${message}`;
}

function LoginUrlRow({ label, value, open, onCopy }: { label: string; value: string; open?: boolean; onCopy: (value: string, label: string) => void }) {
  return <div className="r2-login-url-row"><span>{label}</span><code title={value}>{value}</code><div><button type="button" className="r2-text-button" aria-label={`复制${label}`} onClick={() => onCopy(value, label)}>复制</button>{open && <a className="r2-text-button" aria-label={`打开${label}`} href={value} target="_blank" rel="noopener noreferrer">打开 ↗</a>}</div></div>;
}

export default function Accounts({ onNavigate }: { onNavigate: (page: Page) => void }) {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [channels, setChannels] = useState<Channel[]>([]);
  const [blogs, setBlogs] = useState<BlogConnector[]>([]);
  const [nodes, setNodes] = useState<ExecutionNode[]>([]);
  const [blogError, setBlogError] = useState('');
  const [status, setStatus] = useState<RippleStatus | null>(null);
  const [xConfig, setXConfig] = useState<XConfig | null>(null);
  const [xClientId, setXClientId] = useState('');
  const [xAuthUrl, setXAuthUrl] = useState('');
  const [xCallbackUrl, setXCallbackUrl] = useState('');
  const [xMethod, setXMethod] = useState<'browser' | 'api'>('browser');
  const [xNodeId, setXNodeId] = useState('local');
  const [xBrowserChannel, setXBrowserChannel] = useState<BrowserChannel | ''>('');
  const [wechatAppId, setWechatAppId] = useState('');
  const [wechatSecret, setWechatSecret] = useState('');
  const [wechatEditId, setWechatEditId] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [add, setAdd] = useState<string | null>(null);
  const [label, setLabel] = useState('');
  const [consent, setConsent] = useState(false);
  const [blogOpenapi, setBlogOpenapi] = useState('');
  const [blogToken, setBlogToken] = useState('');
  const [blogEditId, setBlogEditId] = useState<string | null>(null);
  const [loginId, setLoginId] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [sms, setSms] = useState('');
  const creationKey = useRef(newId());
  const browserHint = currentBrowserChannel();

  const refresh = useCallback(async () => {
    const [a, c, s, x, n] = await Promise.all([
      api<Account[]>('/api/ripple/accounts'),
      api<Channel[]>('/api/ripple/channels'),
      api<RippleStatus>('/api/ripple/status'),
      api<XConfig>('/api/ripple/x/config'),
      api<{ items: ExecutionNode[] }>('/api/ripple/execution-nodes'),
    ]);
    setAccounts(a); setChannels(c); setStatus(s); setXConfig(x); setNodes(n.items);
    setXClientId(current => current || x.client_id);
    try {
      const b = await api<{ items: BlogConnector[] }>('/api/ripple/blog/connectors');
      setBlogs(b.items); setBlogError('');
    } catch (value) {
      setBlogError(blogErrorText(value));
    }
  }, []);

  useEffect(() => {
    let stopped = false, running = false;
    const load = async () => { if (stopped || running) return; running = true; try { await refresh(); } catch (e) { if (!stopped) setError(errorText(e)); } finally { running = false; } };
    void load();
    const timer = setInterval(() => void load(), 2500);
    return () => { stopped = true; clearInterval(timer); };
  }, [refresh]);

  useEffect(() => {
    if (add !== 'x' || xMethod !== 'browser') return;
    const node = nodes.find(item => item.id === xNodeId && item.online && item.capabilities.includes('x.login')) || nodes.find(item => item.online && item.capabilities.includes('x.login'));
    if (!node) { setXNodeId(''); setXBrowserChannel(''); return; }
    if (node.id !== xNodeId) setXNodeId(node.id);
    const browsers = node.interactive_browsers as BrowserChannel[];
    const preferred = browserHint && browsers.includes(browserHint) ? browserHint : browsers[0] || '';
    if (!browsers.includes(xBrowserChannel as BrowserChannel)) setXBrowserChannel(preferred);
  }, [add, browserHint, nodes, xBrowserChannel, xMethod, xNodeId]);

  const run = async (fn: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true; setBusy(true); setError(''); setNotice('');
    try { await fn(); await refresh(); } catch (e) { setError(errorText(e)); } finally { busyRef.current = false; setBusy(false); }
  };

  const openAdd = (platform: string) => {
    setAdd(platform); setLabel(''); setConsent(false); setBlogOpenapi(''); setBlogToken(''); setBlogEditId(null);
    setXAuthUrl(''); setXCallbackUrl(''); setXMethod('browser'); setXClientId(xConfig?.client_id || '');
    setWechatAppId(''); setWechatSecret(''); setWechatEditId(null);
    const node = nodes.find(item => item.online && item.capabilities.includes('x.login'));
    setXNodeId(node?.id || 'local');
    const available = (node?.interactive_browsers || []) as BrowserChannel[];
    setXBrowserChannel(browserHint && available.includes(browserHint) ? browserHint : available[0] || '');
    creationKey.current = newId();
  };

  const openBlog = (item?: BlogConnector) => {
    setAdd('blog'); setLabel(item?.label || ''); setBlogOpenapi(item?.openapi_url || ''); setBlogToken(''); setBlogEditId(item?.id || null); setConsent(false);
  };

  const openWechat = (item?: Account) => {
    setAdd('wechat'); setLabel(item?.label || ''); setConsent(false); setWechatSecret(''); setWechatEditId(item?.id || null);
    setWechatAppId(item?.identity?.remote_id || ''); creationKey.current = newId();
  };

  const copyUrl = (value: string, name: string) => {
    if (!navigator.clipboard) { setError(`无法自动复制${name}，请手动选择 URL。`); return; }
    void navigator.clipboard.writeText(value).then(() => setNotice(`${name}已复制。`)).catch(() => setError(`无法自动复制${name}，请手动选择 URL。`));
  };

  const connect = () => run(async () => {
    if (add === 'blog') {
      if (!blogOpenapi.trim()) throw new Error('请填写 Blog 的完整 OpenAPI URL。');
      if (!blogEditId && !blogToken.trim()) throw new Error('首次连接需要填写 Agent Token。');
      const path = blogEditId ? `/api/ripple/blog/connectors/${blogEditId}` : '/api/ripple/blog/connectors';
      await api<BlogConnector>(path, blogEditId ? 'PUT' : 'POST', { label: label.trim(), openapi_url: blogOpenapi.trim(), token: blogToken, confirmed: true });
      setAdd(null); setBlogToken(''); setBlogEditId(null); setNotice('Blog 已连接。平台版本中可以选择它作为发布目标。');
      return;
    }
    if (add === 'wechat') {
      if (!wechatAppId.trim() || !wechatSecret) throw new Error('请填写公众号 AppID 和 AppSecret。');
      const path = wechatEditId ? `/api/ripple/wechat/accounts/${wechatEditId}/connect` : '/api/ripple/wechat/connect';
      const payload = wechatEditId
        ? { app_id: wechatAppId.trim(), app_secret: wechatSecret, confirmed: true }
        : { label: label.trim(), app_id: wechatAppId.trim(), app_secret: wechatSecret, idempotency_key: creationKey.current, confirmed: true };
      const connected = await api<Account>(path, wechatEditId ? 'PUT' : 'POST', payload);
      setAdd(null); setWechatSecret(''); setWechatEditId(null);
      setNotice(connected.capabilities?.includes('freepublish')
        ? '微信公众号已连接：草稿箱与发布接口可用。'
        : connected.capabilities?.includes('draft') ? '微信公众号已连接：草稿箱可用；当前账号没有发布接口权限。' : 'AppID/AppSecret 已验证，但当前公众号没有草稿箱 API 权限。');
      return;
    }
    if (add === 'x') {
      if (xMethod === 'browser') {
        const node = nodes.find(item => item.id === xNodeId);
        if (!node?.online || !node.capabilities.includes('x.login')) throw new Error('请选择在线且支持人工登录的执行设备。');
        if (!xBrowserChannel || !node.interactive_browsers.includes(xBrowserChannel)) throw new Error('请选择执行设备上的 Chrome 或 Edge。');
        const a = await api<Account>('/api/ripple/accounts', 'POST', { platform: 'x', label: label.trim(), idempotency_key: creationKey.current, execution_node_id: node.id });
        setAdd(null); setLoginId(a.id);
        await api(`/api/ripple/accounts/${a.id}/login`, 'POST', { confirmed: true, headed: true, execution_node_id: node.id, browser_channel: xBrowserChannel });
        setNotice(node.kind === 'local'
          ? `已在 ${node.name} 用普通 ${browserLabel(xBrowserChannel)} 打开 Ripple 独立 Profile。完成 Google / X 登录后关闭浏览器，再检查状态。`
          : `登录任务已发送到 ${node.name}。登录态和 Profile 只保存在该设备。`);
        return;
      }
      const clientId = xClientId.trim();
      if (!clientId) throw new Error('请填写 X Developer App Client ID。');
      if (!xConfig?.configured || clientId !== xConfig.client_id) {
        const saved = await api<XConfig>('/api/ripple/x/config', 'PUT', { client_id: clientId, confirmed: true });
        setXConfig(saved);
      }
      const result = await api<XConnectResult>('/api/ripple/x/connect', 'POST', { label: label.trim(), idempotency_key: creationKey.current, confirmed: true });
      setAdd(null); setLoginId(result.account.id); setXAuthUrl(result.authorize_url); setXCallbackUrl(result.callback_url);
      setNotice('X 官方 API 授权链接已生成。点击“打开 X 授权页”，完成后返回 Ripple 即可。');
      return;
    }
    const a = await api<Account>('/api/ripple/accounts', 'POST', { platform: add, label: label.trim(), idempotency_key: creationKey.current });
    setAdd(null); setLoginId(a.id);
    await api(`/api/ripple/accounts/${a.id}/login`, 'POST', { confirmed: true, headed: true, browser_channel: browserHint });
    setNotice(add === 'bilibili' ? '正在生成 B 站登录二维码，请在此对话框中扫码。' : '已启动独立登录窗口。请本人扫码并完成平台确认。');
  });

  const reconnectX = (account: Account) => run(async () => {
    if (!window.confirm(`重新连接「${account.label}」到 X？原审批将失效。`)) return;
    const result = await api<XConnectResult>(`/api/ripple/x/accounts/${account.id}/connect`, 'POST', { confirmed: true, headed: true });
    setLoginId(account.id); setXAuthUrl(result.authorize_url); setXCallbackUrl(result.callback_url);
    setNotice('新的 X OAuth 授权链接已生成。');
  });

  const act = (account: Account, action: 'login' | 'probe' | 'disconnect') => run(async () => {
    const node = nodes.find(item => item.id === (account.execution_node_id || 'local'));
    const question = action === 'disconnect'
      ? `断开「${account.label}」？未执行任务的审批将失效。平台侧授权仍需到平台设置撤销。`
      : action === 'login'
        ? `在「${node?.name || '绑定设备'}」为「${account.label}」重新登录？新设备不会复制旧 Profile，原审批将失效。`
        : account.adapter === 'wechat-api' ? `检查「${account.label}」的微信公众号 API 权限状态？` : `使用绑定执行设备检查「${account.label}」的登录状态？`;
    if (!window.confirm(question)) return;
    await api(`/api/ripple/accounts/${account.id}/${action}`, 'POST', {
      confirmed: true,
      headed: action !== 'probe',
      execution_node_id: account.execution_node_id || 'local',
      browser_channel: account.browser_channel || account.operation?.browser_channel || undefined,
    });
    if (action === 'login' || (action === 'probe' && account.operation?.state === 'waiting_user')) setLoginId(account.id);
    setNotice(action === 'disconnect' ? '已断开账号。' : action === 'probe' ? '正在使用绑定执行设备检查账号状态。' : account.platform === 'bilibili' ? '正在生成登录二维码，请完成扫码。' : '登录任务已发送到绑定执行设备。');
  });

  const verifyLogin = (account: Account) => run(async () => {
    await api(`/api/ripple/accounts/${account.id}/probe`, 'POST', {
      confirmed: true,
      headed: false,
      execution_node_id: account.execution_node_id || 'local',
      browser_channel: account.browser_channel || account.operation?.browser_channel || undefined,
    });
    setNotice('正在读取同一个 Ripple 独立 Profile 的 X 登录状态。');
  });

  const removeAccount = (account: Account) => run(async () => {
    await api(`/api/ripple/accounts/${account.id}`, 'DELETE', { confirmed: true });
    if (loginId === account.id) setLoginId(null);
    setDeleteId(null);
    setNotice(`已删除账号「${account.label}」及 Ripple Server 上的账号记录。`);
  });

  const probeBlog = (item: BlogConnector) => run(async () => { await api(`/api/ripple/blog/connectors/${item.id}/probe`, 'POST', {}); setNotice(`Blog「${item.label}」连接正常。`); });
  const removeBlog = (item: BlogConnector) => run(async () => { if (!window.confirm(`断开 Blog「${item.label}」？已绑定的平台版本需要先改用其他目标。`)) return; await api(`/api/ripple/blog/connectors/${item.id}`, 'DELETE', { confirmed: true }); setNotice(`已断开 Blog「${item.label}」。`); });

  const login = accounts.find(a => a.id === loginId);
  const deleting = accounts.find(a => a.id === deleteId);
  const selectedXNode = nodes.find(item => item.id === xNodeId);
  const nodeFor = (account: Account) => nodes.find(item => item.id === (account.execution_node_id || 'local'));
  const formatNames: Record<string, string> = { text: '文字', images: '图文', video: '视频', markdown: 'Markdown', media: '素材' };

  return <div className="page-scroll r2-page">
    <Header title="账号与平台" subtitle="管理平台账号与 Blog 连接"><span className="r2-environment">浏览器 {status?.environment.browser ? browserLabel(status.environment.browser) : '未检测到'} · B站发布 {status?.environment.biliup ? '可用' : '未就绪'}</span></Header>
    <Feedback error={error} notice={notice} />{blogError && <div className="r2-inline-warning">{blogError}</div>}
    <div className="r2-account-help">X 浏览器模式使用执行设备上的 Ripple 独立 Profile。首次认证由普通 Chrome / Edge 完成人工 Google / X 登录，随后 Ripple 才用同一 Profile 检查状态和执行自动化；不会读取你日常 Chrome 的 Default / Profile 1 等现有 Profile，也不会把 Cookie 上传到 Ripple Server。<button className="r2-text-button" onClick={() => onNavigate('integrations')}>执行节点与环境设置 →</button></div>
    <div className="r2-channel-list">{channels.map(c => {
      const rows = accounts.filter(a => a.platform === c.id);
      return <section className="r2-channel" key={c.id} data-platform={c.id}><header><Mark platform={c.id} /><div><h2>{c.name}</h2><span>{c.formats.map(f => formatNames[f] || f).join(' / ') || ((c.connection_options?.length || 0) > 0 ? '接入规划' : '尚未接入')}{c.id === 'blog' ? ` · OpenAPI / Markdown${blogs.length ? ` · ${blogs.length} 个连接` : ' · 未连接'}` : c.adapter_available ? ` · ${c.adapter === 'x-multi' ? '浏览器 / 官方 API' : c.adapter === 'wechat-api' ? '官方 API' : c.adapter === 'biliup' ? '本地上传程序' : '独立浏览器'}${rows.length === 0 ? ' · 未连接' : ''}` : ''}</span></div><div className="r2-channel-action">{c.id === 'blog' ? <button className="r2-button" disabled={busy} onClick={() => openBlog()}>连接 Blog</button> : c.id === 'wechat' && c.adapter_available ? <button className="r2-button" disabled={busy} onClick={() => openWechat()}>连接公众号</button> : c.adapter_available ? <button className="r2-button" disabled={busy} onClick={() => openAdd(c.id)}>连接账号</button> : c.local_export ? <button className="r2-button" onClick={() => { sessionStorage.setItem('ripple_new_mode', 'blog'); onNavigate('publish'); }}>创建导出</button> : <span className="r2-muted">{(c.connection_options?.length || 0) > 0 ? '计划中' : '尚未接入'}</span>}</div></header>
        {!c.adapter_available && (c.connection_options?.length || 0) > 0 && <div className="r2-connection-options">{c.connection_options!.map(method => <div className="r2-connection-option" key={method.id}><div><strong>{method.label}</strong><span className={method.status === 'planned' ? 'planned' : ''}>{method.status === 'planned' ? '计划接入' : method.status === 'available' ? '可用' : '不可用'}</span></div><p>{method.requirements}</p><small>{method.execution_scope === 'browser_node' ? '执行位置：Browser Node' : method.execution_scope === 'server' ? '执行位置：Ripple Server' : '执行位置：本机'}</small></div>)}</div>}
        {c.id === 'blog' ? blogs.length > 0 ? <table className="r2-table r2-account-table"><thead><tr><th>Blog</th><th>状态</th><th>能力</th><th>操作</th></tr></thead><tbody>{blogs.map(item => <tr key={item.id}><td><strong>{item.label}</strong><small title={item.openapi_url}>{item.openapi_url}</small></td><td><span className="r2-account-state connected">已连接</span><small>更新 {dateText(item.updated_at)}</small></td><td><small>{item.content_types.join(' / ')}</small><small>{item.capabilities.includes('media.upload') ? '支持媒体上传' : '正文媒体能力未声明'}</small></td><td><div className="r2-row-actions"><button className="r2-text-button" disabled={busy} onClick={() => void probeBlog(item)}>检查连接</button><button className="r2-text-button" disabled={busy} onClick={() => openBlog(item)}>编辑</button><button className="r2-text-button danger" disabled={busy} onClick={() => void removeBlog(item)}>断开</button></div></td></tr>)}</tbody></table> : <p className="r2-channel-empty">Blog 平台版本随时可创建；连接兼容 OpenAPI 后可以直接发布，不连接时仍可导出 Markdown 与素材。</p> : rows.length > 0 ? <table className="r2-table r2-account-table"><thead><tr><th>账号</th><th>登录状态</th><th>最近检查</th><th>操作</th></tr></thead><tbody>{rows.map(a => {
          const node = nodeFor(a);
          const running = !!a.operation && ['running', 'recovery_required', 'waiting_node'].includes(a.operation.state);
          const waitingUser = a.operation?.state === 'waiting_user';
          return <tr key={a.id}><td><strong>{a.label}</strong>{a.adapter === 'x-browser' && <><small>X 浏览器模式 · Ripple 独立 Profile {a.profile_id ? a.profile_id.slice(0, 8) : ''}</small><small>执行设备：{node?.name || '未知设备'} · {node?.online ? '在线' : '离线'} · {browserLabel(a.browser_channel || a.operation?.browser_channel)}</small>{node?.kind === 'remote' && <small className="r2-warning">Profile / Cookie 只在该设备；当前远程发布传输尚未启用</small>}</>}{a.adapter === 'x-api' && <small>X 官方 API · OAuth 2.0</small>}{a.adapter === 'wechat-api' && <small>微信公众号官方 API · {a.capabilities?.includes('draft') ? '草稿箱可用' : '无草稿权限'} · {a.capabilities?.includes('freepublish') ? '发布可用' : '无发布权限'}</small>}{a.adapter === 'aitoearn-rest' && <small>旧版外部渠道账号 · 仅兼容保留</small>}<small>{a.identity?.name ? (a.adapter === 'wechat-api' ? a.identity.name : `@${a.identity.name}`) : '尚未取得平台身份'}</small></td><td><span className={`r2-account-state ${a.status}`}>{ACCOUNT_LABELS[a.status] || a.status}</span>{waitingUser && <small>等待人工登录完成</small>}{a.operation?.state === 'waiting_node' && <small>等待执行设备</small>}{a.operation?.state === 'running' && <small>{a.operation.kind === 'publish' ? '发布任务运行中' : '连接操作进行中'}</small>}{a.operation?.state === 'recovery_required' && <small>上次发布待核对</small>}</td><td>{dateText(a.checked_at)}</td><td><div className="r2-row-actions">{running && a.operation?.kind === 'publish' ? <button className="r2-text-button" onClick={() => onNavigate('publish')}>查看进度</button> : a.status === 'deleting' ? <button className="r2-text-button danger" disabled={busy} onClick={() => setDeleteId(a.id)}>继续删除</button> : <>{waitingUser ? <button className="r2-text-button" disabled={busy} onClick={() => void verifyLogin(a)}>已关闭浏览器，检查状态</button> : a.adapter === 'x-api' ? <button className="r2-text-button" disabled={busy} onClick={() => void reconnectX(a)}>{a.status === 'connected' ? '重新连接' : '连接 X'}</button> : a.adapter === 'wechat-api' ? <button className="r2-text-button" disabled={busy} onClick={() => openWechat(a)}>重新连接</button> : a.adapter !== 'aitoearn-rest' ? <button className="r2-text-button" disabled={busy || a.operation?.state === 'waiting_node'} onClick={() => void act(a, 'login')}>{a.status === 'connected' ? '重新登录' : '登录'}</button> : null}<button className="r2-text-button" disabled={busy || a.status === 'disconnected' || a.operation?.state === 'waiting_node'} onClick={() => void act(a, 'probe')}>检查状态</button><button className="r2-text-button muted" disabled={busy} onClick={() => void act(a, 'disconnect')}>断开</button><button className="r2-text-button danger" disabled={busy} onClick={() => setDeleteId(a.id)}>删除</button></>}</div></td></tr>;
        })}</tbody></table> : !c.adapter_available ? <p className="r2-channel-empty">{c.reason}</p> : null}
        {c.adapter_available && !c.environment_ready && <p className="r2-channel-foot">执行环境尚未就绪，请到设置中检查。</p>}
      </section>;
    })}</div>

    {add === 'blog' && <Modal title={blogEditId ? '编辑 Blog 连接' : '连接 Blog'} busy={busy} onClose={() => { setAdd(null); setBlogToken(''); setBlogEditId(null); }}><p>填写 Blog 提供的 OpenAPI 文档地址。Ripple 会读取语义能力声明，不依赖具体 CMS 名称或固定 API 路径。</p><label className="r2-field">连接名称<input autoFocus value={label} maxLength={80} placeholder="例如：个人 Blog" onChange={e => setLabel(e.target.value)} /></label><label className="r2-field">OpenAPI URL<input value={blogOpenapi} maxLength={2048} placeholder="https://your-blog.example/openapi.json" onChange={e => setBlogOpenapi(e.target.value)} /></label><label className="r2-field">Agent Token<input type="password" autoComplete="off" value={blogToken} maxLength={4096} placeholder={blogEditId ? '留空则继续使用当前 Token' : 'Bearer Token'} onChange={e => setBlogToken(e.target.value)} /></label><p className="r2-muted">Token 会使用系统密钥加密后保存在 Ripple 私有目录，保存后不会再次显示明文。公网 Blog 只允许 HTTPS；本机服务可使用 HTTP。</p><label className="r2-checkbox"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />允许 Ripple 读取该 OpenAPI、验证 Token，并保存此 Blog 连接</label><footer><button className="r2-button" disabled={busy} onClick={() => { setAdd(null); setBlogToken(''); setBlogEditId(null); }}>取消</button><button className="r2-button primary" disabled={busy || !label.trim() || !blogOpenapi.trim() || !consent || (!blogEditId && !blogToken.trim())} onClick={() => void connect()}>{busy ? '正在验证…' : blogEditId ? '保存连接' : '验证并连接'}</button></footer></Modal>}

    {add === 'wechat' && <Modal title={wechatEditId ? '重新连接微信公众号' : '连接微信公众号'} busy={busy} onClose={() => { setAdd(null); setWechatSecret(''); setWechatEditId(null); }}><p>使用微信公众平台提供的 AppID / AppSecret 连接官方 API。Ripple 会先验证接口调用凭据并读取草稿 / 发布权限，不会用浏览器模拟登录。</p>{!wechatEditId && <label className="r2-field">连接名称<input autoFocus value={label} maxLength={80} placeholder="例如：个人公众号" onChange={e => setLabel(e.target.value)} /></label>}<label className="r2-field">AppID<input autoFocus={!!wechatEditId} value={wechatAppId} maxLength={66} placeholder="wx…" onChange={e => setWechatAppId(e.target.value)} autoComplete="off" /></label><label className="r2-field">AppSecret<input type="password" value={wechatSecret} maxLength={128} placeholder="微信公众平台 AppSecret" onChange={e => setWechatSecret(e.target.value)} autoComplete="new-password" /></label><div className="r2-inline-warning"><strong>服务器 IP 白名单</strong><br />微信公众号获取接口凭据通常要求把 Ripple Server 的公网出口 IP 加入公众平台开发配置。若缺失，微信会返回 errcode 40164，Ripple 会直接显示该错误。</div><p className="r2-muted">AppSecret 使用系统密钥加密后保存在 Ripple 私有账号目录，不会回显到页面或写入普通状态文件。连接后会分别探测草稿箱与 freepublish 权限。</p><label className="r2-checkbox"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />允许 Ripple 保存加密 AppSecret，并代表此公众号调用草稿/发布官方 API</label><Feedback error={error} /><footer><button className="r2-button" disabled={busy} onClick={() => { setAdd(null); setWechatSecret(''); setWechatEditId(null); }}>取消</button><button className="r2-button primary" disabled={busy || !consent || !wechatAppId.trim() || !wechatSecret || (!wechatEditId && !label.trim())} onClick={() => void connect()}>{busy ? '正在验证…' : '验证并连接'}</button></footer></Modal>}

    {add && add !== 'blog' && add !== 'wechat' && <Modal title={`连接${channels.find(c => c.id === add)?.name || '平台'}账号`} busy={busy} onClose={() => { setAdd(null); setXAuthUrl(''); setXCallbackUrl(''); }}><p>{add === 'x' ? '选择 X 的连接方式。浏览器方式会在选定执行设备创建独立 Ripple Profile；官方 API 适合需要稳定 API 自动化的账号。' : '为这个账号建立单独的登录空间。你需要在平台页面完成扫码、手机确认或验证。'}</p>{add === 'x' && <div className="r2-x-methods"><label className={xMethod === 'browser' ? 'active' : ''}><input type="radio" name="x-method" checked={xMethod === 'browser'} onChange={() => { setXMethod('browser'); setXAuthUrl(''); setXCallbackUrl(''); }} /><strong>浏览器登录</strong><span>普通 Chrome / Edge 人工认证 · Ripple 独立 Profile</span></label><label className={xMethod === 'api' ? 'active' : ''}><input type="radio" name="x-method" checked={xMethod === 'api'} onChange={() => setXMethod('api')} /><strong>官方 API</strong><span>需要 X Developer App · API 权限/额度/费用由 X 计划决定</span></label></div>}<label className="r2-field">账号备注<input autoFocus aria-label="账号备注" value={label} maxLength={80} placeholder="例如：个人账号、品牌账号" onChange={e => setLabel(e.target.value)} /></label>{add === 'x' && xMethod === 'browser' && <><label className="r2-field"><span>执行设备</span><select aria-label="X 执行设备" value={xNodeId} onChange={e => { setXNodeId(e.target.value); setXBrowserChannel(''); }}>{nodes.filter(item => item.capabilities.includes('x.login')).map(item => <option key={item.id} value={item.id} disabled={!item.online}>{item.name} · {item.online ? '在线' : '离线'}{item.kind === 'local' ? ' · Ripple Server' : ' · Browser Node'}</option>)}</select></label><label className="r2-field"><span>人工登录浏览器</span><select aria-label="X 登录浏览器" value={xBrowserChannel} onChange={e => setXBrowserChannel(e.target.value as BrowserChannel)}><option value="">请选择</option>{(selectedXNode?.interactive_browsers || []).map(value => <option key={value} value={value}>{browserLabel(value)}</option>)}</select></label><div className="r2-inline-warning"><strong>登录阶段不使用 Playwright</strong><br />Ripple 会在所选设备直接启动普通 Chrome / Edge，并创建这个账号自己的 Ripple Profile。Google / X 登录由你人工完成；现有 Chrome Profile 不会被枚举或复用。之后自动化只复用这个独立 Profile。</div>{selectedXNode?.kind === 'remote' && <p className="r2-muted">Profile、Cookie 和 Google/X 登录态只保存在 {selectedXNode.name}。当前版本的远程节点已支持登录/检查协议；远程发布传输尚未启用。</p>}</>}{add === 'x' && xMethod === 'api' && <><label className="r2-field">X Developer App Client ID<input aria-label="X Client ID" value={xClientId} maxLength={256} placeholder="OAuth 2.0 Client ID" onChange={e => setXClientId(e.target.value)} autoComplete="off" /></label><label className="r2-field">Callback URL<input aria-label="X Callback URL" value={xConfig?.callback_url || 'http://127.0.0.1:7860/api/ripple/x/oauth/callback'} readOnly /></label><p className="r2-muted">请在 X Developer Console 启用 OAuth 2.0 Public Client，并把上面的 Callback URL 原样登记。授权范围：tweet.read / tweet.write / users.read / media.write / offline.access。</p>{xAuthUrl && <a className="r2-button primary" href={xAuthUrl} target="_blank" rel="noopener noreferrer">打开 X 授权页 ↗</a>}</>}<label className="r2-checkbox"><input type="checkbox" checked={consent} onChange={e => setConsent(e.target.checked)} />{add === 'x' ? xMethod === 'browser' ? '允许 Ripple 在所选执行设备创建独立 Profile 并打开普通浏览器；登录态不会上传到 Server' : '允许保存 Client ID，并把 X OAuth token 加密保存在本机' : '允许打开目标平台并在本机保存此账号的登录状态'}</label><footer><button className="r2-button" disabled={busy} onClick={() => { setAdd(null); setXAuthUrl(''); setXCallbackUrl(''); }}>取消</button><button className="r2-button primary" disabled={busy || !label.trim() || !consent || (add === 'x' && xMethod === 'browser' && (!selectedXNode?.online || !xBrowserChannel)) || (add === 'x' && xMethod === 'api' && !xClientId.trim())} onClick={() => void connect()}>{busy ? '正在启动…' : add === 'x' ? xMethod === 'browser' ? '打开普通浏览器登录' : '生成授权链接' : '打开登录'}</button></footer></Modal>}

    {loginId && <Modal title={login ? `登录 · ${login.label}` : '登录进度'} onClose={() => setLoginId(null)}><p>{login?.message || '正在取得连接状态…'}</p>
      {login?.adapter === 'x-api' ? <div className="r2-login-details"><dl><dt>方式</dt><dd>X 官方 API · OAuth 2.0 PKCE</dd><dt>状态</dt><dd>{login.status === 'connected' ? '已完成 OAuth 回调' : '等待 X OAuth 回调'}</dd></dl>{xAuthUrl && <LoginUrlRow label="授权 URL" value={xAuthUrl} open onCopy={copyUrl} />}<LoginUrlRow label="Callback URL" value={xCallbackUrl || xConfig?.callback_url || 'http://127.0.0.1:7860/api/ripple/x/oauth/callback'} onCopy={copyUrl} /></div> : login?.adapter === 'x-browser' ? <div className="r2-login-details"><dl><dt>方式</dt><dd>普通浏览器人工登录 → Ripple 自动化检查</dd><dt>执行设备</dt><dd>{nodeFor(login)?.name || '未知设备'} · {nodeFor(login)?.online ? '在线' : '离线'}</dd><dt>浏览器</dt><dd>{browserLabel(login.browser_channel || login.operation?.browser_channel)}</dd><dt>Profile</dt><dd>Ripple 独立 Profile · {login.profile_id?.slice(0, 8) || login.id.slice(0, 8)}</dd><dt>隐私</dt><dd>不会读取现有 Chrome Profile；Profile / Cookie 留在执行设备</dd></dl>{login.login_state === 'waiting_user' && <div className="r2-inline-warning">完成 Google / X 登录后，请先关闭这次打开的 Chrome / Edge 窗口，再让 Ripple 检查同一个独立 Profile。</div>}{login.login_state === 'waiting_node' && <div className="r2-inline-warning">等待 {nodeFor(login)?.name || '远程执行设备'} 处理登录任务。该设备必须运行 Ripple Browser Node 客户端。</div>}</div> : login?.login_url && <div className="r2-login-details"><dl><dt>方式</dt><dd>独立平台登录</dd><dt>Callback URL</dt><dd>无需 Callback URL</dd></dl><LoginUrlRow label="登录页面" value={login.login_url} onCopy={copyUrl} /></div>}
      {login?.qr_available && login.operation && <img className="r2-login-qr" src={`${base}/api/ripple/accounts/${login.id}/qr/${login.operation.id}`} alt="平台登录二维码" />}
      {login?.login_state === 'sms_required' && <form onSubmit={e => { e.preventDefault(); void run(async () => { await api(`/api/ripple/accounts/${login.id}/sms`, 'POST', { operation_id: login.operation!.id, code: sms }); setSms(''); }); }}><label className="r2-field">手机验证码<input type="password" inputMode="numeric" autoComplete="one-time-code" aria-label="手机验证码" maxLength={8} value={sms} onChange={e => setSms(e.target.value.replace(/\D/g, ''))} /></label><button className="r2-button primary" disabled={busy || sms.length < 4}>提交验证</button></form>}
      {login?.status === 'connected' && <p className="r2-connection-success">已连接 {login.identity?.name || login.label}。可在内容工作台的平台版本中选择此账号。</p>}
      <Feedback error={error} /><footer>{login && <button className="r2-button danger" disabled={busy} onClick={() => { setLoginId(null); setDeleteId(login.id); }}>删除这个账号</button>}{login?.adapter === 'x-browser' && login.login_state === 'waiting_user' && <button className="r2-button primary" disabled={busy} onClick={() => void verifyLogin(login)}>已完成并关闭浏览器，检查状态</button>}<button className="r2-button" onClick={() => setLoginId(null)}>关闭</button></footer></Modal>}

    {deleting && <Modal title={`删除账号 · ${deleting.label}`} busy={busy} onClose={() => setDeleteId(null)}><p>此操作会从 Ripple 移除这个账号。历史内容与发布记录会保留。</p><ul className="r2-delete-list"><li>删除 Ripple Server 上的账号记录</li>{deleting.adapter === 'wechat-api' ? <><li>删除本机加密保存的微信公众号 AppSecret</li><li>清除内存中的 access_token 缓存</li></> : <><li>{nodeFor(deleting)?.kind === 'remote' ? '远程执行设备上的独立 Profile 当前不会由 Server 自动删除' : '删除本机浏览器独立 Profile / Cookie'}</li><li>删除本机 OAuth access / refresh token</li><li>删除未完成的 OAuth 授权状态</li></>}</ul>{deleting.adapter === 'x-api' && <p className="r2-muted">如需撤销 X 平台侧授权，请继续到 X 的应用授权设置中撤销。</p>}{nodeFor(deleting)?.kind === 'remote' && <p className="r2-muted">远程 Profile 从未上传 Ripple Server；请在对应 Browser Node 设备上单独清理。</p>}<p className="r2-muted">如果该账号有正在发布或结果待核对的真实发布，Ripple 会拒绝删除。</p><Feedback error={error} /><footer><button className="r2-button" disabled={busy} onClick={() => setDeleteId(null)}>取消</button><button className="r2-button danger" disabled={busy} onClick={() => void removeAccount(deleting)}>删除账号</button></footer></Modal>}
  </div>;
}
