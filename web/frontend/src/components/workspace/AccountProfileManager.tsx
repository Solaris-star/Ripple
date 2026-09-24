import { useEffect, useMemo, useState } from 'react';
import {
  applyProfileAnalysis,
  bindContentProfile,
  fetchContentProfile,
  fetchContentProfileContext,
  fetchProfileAnalysis,
  fetchProfileAnalysisCapability,
  startProfileAnalysis,
  unbindContentProfile,
} from '../../lib/api';
import type {
  ContentProfileBinding,
  ContentProfileContext,
  ContentProfileDetail,
  ContentProfileSummary,
  ProfileAnalysisCapability,
  ProfileAnalysisRun,
} from '../../lib/api';
import type { Account, BlogConnector } from '../../lib/ripple';
import { dateText, errorText } from '../../lib/ripple';
import { platformDisplayName } from '../../lib/platforms';
import { Feedback, Mark, Modal } from './Common';

type Target = {
  id: string;
  kind: 'account' | 'blog';
  platform: string;
  label: string;
  status: string;
  identityName: string;
  checkedAt: string;
};

type AnalysisState = {
  target: Target;
  capability: ProfileAnalysisCapability | null;
  profileId: string;
  displayName: string;
  sampleText: string;
  useAccountHistory: boolean;
  historyLimit: number;
  run: ProfileAnalysisRun | null;
};

type OverrideState = {
  target: Target;
  binding: ContentProfileBinding;
  tone: string;
  formats: string;
  notes: string;
};

function sampleRows(raw: string): Array<Record<string, unknown>> {
  return raw
    .split(/\n\s*---+\s*\n/g)
    .map((part) => part.trim())
    .filter(Boolean)
    .slice(0, 30)
    .map((part) => {
      const [first, ...rest] = part.split('\n');
      return {
        title: (first || '').trim().slice(0, 300),
        body: (rest.join('\n') || first || '').trim().slice(0, 12000),
        kind: 'user_import',
      };
    });
}

function plainMarkdown(value: string, limit = 180): string {
  const text = String(value || '')
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/!\[[^\]]*\]\([^)]*\)/g, ' ')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/^#{1,6}\s+.*$/gm, ' ')
    .replace(/^[\s>*+-]+/gm, '')
    .replace(/[*_~`|]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  if (!text) return '';
  return text.length > limit ? `${text.slice(0, limit).trim()}…` : text;
}

function firstLine(value: string, limit = 80): string {
  const text = plainMarkdown(value, 500);
  if (!text) return '';
  const first = text.split(/[。！？!?；;]/)[0]?.trim() || text;
  return first.length > limit ? `${first.slice(0, limit).trim()}…` : first;
}

function profileDescription(detail: ContentProfileDetail | null): string {
  if (!detail) return '正在读取画像描述…';
  return plainMarkdown(detail.files?.['identity.md'] || detail.files?.['audience.md'] || '', 220)
    || '这个画像还没有定位描述，可点击“编辑”补充。';
}

function profileTags(detail: ContentProfileDetail | null): string {
  if (!detail) return '';
  return [firstLine(detail.files?.['audience.md'] || '', 48), firstLine(detail.files?.['style.md'] || '', 48)].filter(Boolean).join(' · ');
}

function profileFormat(detail: ContentProfileDetail | null): string {
  if (!detail) return '读取中';
  return firstLine(detail.files?.['platforms.md'] || detail.files?.['style.md'] || '', 54) || '尚未设置';
}

function statusText(target: Target): string {
  if (target.status === 'connected') return '已连接';
  if (target.status === 'disconnected') return '已断开';
  return target.status || '未连接';
}

export default function AccountProfileManager({
  accounts,
  blogs,
  onNewProfile,
  onEditProfile,
  onBindNewAccount,
  onManageConnections,
  onChanged,
}: {
  accounts: Account[];
  blogs: BlogConnector[];
  onNewProfile: () => void;
  onEditProfile: (name: string) => void;
  onBindNewAccount: (profileId: string, profileName: string) => void;
  onManageConnections: () => void;
  onChanged?: () => void;
}) {
  const [context, setContext] = useState<ContentProfileContext | null>(null);
  const [activeProfileId, setActiveProfileId] = useState('');
  const [activeDetail, setActiveDetail] = useState<ContentProfileDetail | null>(null);
  const [analysis, setAnalysis] = useState<AnalysisState | null>(null);
  const [overrideEditor, setOverrideEditor] = useState<OverrideState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const load = async () => {
    const next = await fetchContentProfileContext();
    setContext(next);
    setActiveProfileId((current) => {
      const fromUrl = new URLSearchParams(location.search).get('profile_id') || '';
      if (fromUrl && next.profiles.some((item) => item.id === fromUrl)) return fromUrl;
      if (current && next.profiles.some((item) => item.id === current)) return current;
      return next.profiles[0]?.id || '';
    });
  };
  useEffect(() => { void load().catch((e) => setError(errorText(e))); }, [accounts.length, blogs.length]);
  useEffect(() => {
    if (!activeProfileId) { setActiveDetail(null); return; }
    let cancelled = false;
    void fetchContentProfile(activeProfileId)
      .then((value) => { if (!cancelled) setActiveDetail(value); })
      .catch((e) => { if (!cancelled) setError(errorText(e)); });
    return () => { cancelled = true; };
  }, [activeProfileId]);

  const targets = useMemo<Target[]>(() => [
    ...accounts.map((account) => ({
      id: account.id,
      kind: 'account' as const,
      platform: account.platform,
      label: account.label,
      status: account.status,
      identityName: account.identity?.name || '',
      checkedAt: account.checked_at ? dateText(account.checked_at) : '',
    })),
    ...blogs.map((blog) => ({
      id: blog.id,
      kind: 'blog' as const,
      platform: 'blog',
      label: blog.label,
      status: blog.status,
      identityName: '',
      checkedAt: blog.updated_at ? dateText(blog.updated_at) : '',
    })),
  ], [accounts, blogs]);

  const bindingFor = (target: Target): ContentProfileBinding | undefined =>
    context?.bindings.find((item) => item.target_kind === target.kind && item.account_id === target.id);
  const profileFor = (profileId?: string): ContentProfileSummary | undefined =>
    context?.profiles.find((item) => item.id === profileId);
  const activeProfile = profileFor(activeProfileId);
  const boundTargets = targets.filter((target) => bindingFor(target)?.profile_id === activeProfileId);
  const unassignedTargets = targets.filter((target) => !bindingFor(target));

  const selectProfile = (profileId: string) => {
    setActiveProfileId(profileId);
    setError(''); setNotice('');
    const url = new URL(location.href);
    url.searchParams.set('page', 'integrations');
    url.searchParams.set('section', 'accounts');
    url.searchParams.set('profile_id', profileId);
    history.replaceState({}, '', url);
  };

  const changeBinding = async (target: Target, profileId: string) => {
    setBusy(true); setError(''); setNotice('');
    try {
      const current = bindingFor(target);
      if (!profileId) await unbindContentProfile(target.kind, target.id);
      else await bindContentProfile(target.kind, target.id, {
        profile_id: profileId,
        expected_binding_revision: current?.binding_revision,
      });
      await load();
      window.dispatchEvent(new Event('ripple:profile-context-changed'));
      setNotice(profileId ? '账号画像关联已更新。后续新任务会使用新的关联。' : '已解除账号画像关联；平台登录状态未改变。');
      onChanged?.();
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };

  const openOverrides = (target: Target) => {
    const binding = bindingFor(target);
    if (!binding) return;
    setOverrideEditor({
      target,
      binding,
      tone: String(binding.overrides?.tone || ''),
      formats: String(binding.overrides?.formats || ''),
      notes: String(binding.overrides?.notes || ''),
    });
  };

  const saveOverrides = async () => {
    if (!overrideEditor) return;
    setBusy(true); setError(''); setNotice('');
    try {
      const { target, binding } = overrideEditor;
      await bindContentProfile(target.kind, target.id, {
        profile_id: binding.profile_id,
        expected_binding_revision: binding.binding_revision,
        overrides: {
          ...(binding.overrides || {}),
          tone: overrideEditor.tone.trim(),
          formats: overrideEditor.formats.trim(),
          notes: overrideEditor.notes.trim(),
        },
      });
      await load();
      window.dispatchEvent(new Event('ripple:profile-context-changed'));
      setOverrideEditor(null);
      setNotice('账号专属差异已保存；后续新任务会使用新的 binding revision。');
      onChanged?.();
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };

  const openAnalysis = async (target: Target, mode: 'current' | 'new' = 'current') => {
    setError(''); setNotice('');
    const binding = bindingFor(target);
    setAnalysis({
      target,
      capability: null,
      profileId: mode === 'current' ? (binding?.profile_id || activeProfileId) : '',
      displayName: mode === 'new'
        ? `${target.identityName || target.label}画像`
        : (binding ? profileFor(binding.profile_id)?.display_name || '' : activeProfile?.display_name || ''),
      sampleText: '',
      useAccountHistory: false,
      historyLimit: 30,
      run: null,
    });
    try {
      const capability = await fetchProfileAnalysisCapability(target.kind, target.id);
      setAnalysis((current) => current && current.target.id === target.id ? { ...current, capability } : current);
    } catch (e) { setError(errorText(e)); }
  };

  const startAnalysis = async () => {
    if (!analysis) return;
    const samples = sampleRows(analysis.sampleText);
    if (!samples.length && !analysis.useAccountHistory) { setError('请粘贴代表作品，或选择读取当前账号已发布作品。'); return; }
    setBusy(true); setError(''); setNotice('');
    try {
      let run = await startProfileAnalysis({
        target_kind: analysis.target.kind,
        account_id: analysis.target.id,
        profile_id: analysis.profileId,
        display_name: analysis.displayName.trim(),
        samples,
        use_account_history: analysis.useAccountHistory,
        history_limit: analysis.historyLimit,
        confirmed: true,
      });
      setAnalysis((current) => current ? { ...current, run } : current);
      if (run.status === 'running') {
        for (let i = 0; i < 45; i += 1) {
          await new Promise((resolve) => setTimeout(resolve, 1500));
          run = await fetchProfileAnalysis(run.id);
          setAnalysis((current) => current ? { ...current, run } : current);
          if (!['running', 'queued'].includes(run.status)) break;
        }
      }
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };

  const applyAnalysis = async () => {
    if (!analysis?.run || analysis.run.status !== 'succeeded') return;
    setBusy(true); setError(''); setNotice('');
    try {
      const profile = analysis.profileId ? profileFor(analysis.profileId) : undefined;
      const result = await applyProfileAnalysis(analysis.run.id, {
        profile_id: analysis.profileId,
        display_name: analysis.displayName.trim(),
        expected_revision: profile?.current_revision || 0,
        bind_target: true,
      });
      await load();
      if (result.profile?.id) selectProfile(result.profile.id);
      window.dispatchEvent(new Event('ripple:profile-context-changed'));
      setNotice('画像提案已确认并关联到该账号。');
      setAnalysis(null);
      onChanged?.();
    } catch (e) { setError(errorText(e)); } finally { setBusy(false); }
  };

  return <section className="account-profile-manager">
    <div className="account-profile-header">
      <div>
        <h2>账号画像</h2>
        <p className="r2-muted">选择画像后，下方平台账号与内容定位同步切换。</p>
      </div>
      <button className="r2-button primary" type="button" onClick={onNewProfile}>+ 新建画像</button>
    </div>
    <Feedback error={error} notice={notice} />

    {!context?.profiles.length ? <div className="r2-empty">还没有账号画像。可以先建立定位，再绑定平台账号。</div> : <div className="profile-driven-layout">
      <div className="profile-selector-list" aria-label="账号画像列表">
        {context.profiles.map((profile) => {
          const active = profile.id === activeProfileId;
          return <div className={`profile-selector-row${active ? ' active' : ''}`} key={profile.id}>
            <button className="profile-selector-main" type="button" aria-pressed={active} onClick={() => selectProfile(profile.id)}>
              <span className="profile-selector-dot" aria-hidden="true" />
              <span className="profile-selector-copy"><strong>{profile.display_name}</strong><small>{profile.account_count || 0} 个平台账号</small></span>
            </button>
            <button className="profile-selector-edit" type="button" onClick={() => onEditProfile(profile.legacy_name)}>编辑</button>
          </div>;
        })}
      </div>
      <article className="active-profile-summary">
        <div className="active-profile-summary-head">
          <div><strong>{activeProfile?.display_name || '未选择画像'}</strong>{profileTags(activeDetail) && <span>{profileTags(activeDetail)}</span>}</div>
          {activeProfile && <span className="profile-version-badge">V{activeProfile.current_revision}</span>}
        </div>
        <p>{profileDescription(activeDetail)}</p>
        <div className="active-profile-stats">
          <div><small>平台账号</small><strong>{boundTargets.length}</strong></div>
          <div><small>主要内容</small><strong>{profileFormat(activeDetail)}</strong></div>
          <div><small>画像状态</small><strong>{activeProfile?.state === 'confirmed' ? '已确认' : activeProfile?.state || '—'}</strong></div>
        </div>
      </article>
    </div>}

    {activeProfile && <div className="profile-account-section">
      <div className="profile-account-section-head">
        <div><h3>平台账号</h3><p className="r2-muted">以下账号全部属于「{activeProfile.display_name}」。切换画像，列表会整体切换。</p></div>
        <button className="r2-button primary" type="button" onClick={() => onBindNewAccount(activeProfile.id, activeProfile.display_name)}>+ 绑定新平台账号</button>
      </div>
      {!boundTargets.length ? <div className="profile-account-empty"><strong>这个画像还没有平台账号</strong><span>可以先做选题和内容，也可以现在绑定平台账号。</span><button className="r2-button primary" type="button" onClick={() => onBindNewAccount(activeProfile.id, activeProfile.display_name)}>绑定第一个账号</button></div> : <div className="profile-account-list">
        {boundTargets.map((target) => {
          const binding = bindingFor(target)!;
          const hasOverrides = Object.values(binding.overrides || {}).some(Boolean);
          return <article className="profile-account-card" key={`${target.kind}:${target.id}`}>
            <div className="profile-account-identity"><Mark platform={target.platform} /><div><strong>{platformDisplayName(target.platform)} · {target.identityName || target.label}</strong><small>{target.identityName && target.label !== target.identityName ? target.label : target.kind === 'blog' ? 'Blog 连接' : `账号 ID ${target.id.slice(0, 8)}`}</small><span><i className={`profile-status-dot${target.status === 'connected' ? '' : ' warn'}`} />{statusText(target)}{target.checkedAt ? ` · 最近检查 ${target.checkedAt}` : ''}</span></div></div>
            <div className="profile-account-override"><small>账号差异</small><strong>{hasOverrides ? String(binding.overrides?.formats || binding.overrides?.tone || '已设置账号差异') : '继承基础画像'}</strong></div>
            <div className="profile-account-actions"><button className="r2-text-button" type="button" disabled={busy} onClick={() => openOverrides(target)}>账号差异</button><button className="r2-text-button" type="button" disabled={busy} onClick={() => void openAnalysis(target)}>Agent 分析</button><button className="r2-text-button" type="button" onClick={onManageConnections}>账号设置</button></div>
          </article>;
        })}
      </div>}
    </div>}

    {!!unassignedTargets.length && <section className="unassigned-account-section">
      <div className="unassigned-account-head"><div><h3>未归类账号 <span>{unassignedTargets.length}</span></h3><p className="r2-muted">这些账号已经连接，但还没有账号画像。这里只作为待办处理。</p></div></div>
      <div className="unassigned-account-list">{unassignedTargets.map((target) => <article className="unassigned-account-row" key={`${target.kind}:${target.id}`}>
        <div className="profile-account-identity"><Mark platform={target.platform} /><div><strong>{platformDisplayName(target.platform)} · {target.identityName || target.label}</strong><small>{statusText(target)}</small></div></div>
        <div className="unassigned-account-actions">{activeProfile && <button className="r2-button" disabled={busy} onClick={() => void changeBinding(target, activeProfile.id)}>关联到当前画像</button>}<select aria-label={`为 ${target.identityName || target.label} 选择画像`} disabled={busy} value="" onChange={(event) => { if (event.target.value) void changeBinding(target, event.target.value); }}><option value="">选择其他画像…</option>{(context?.profiles || []).map((profile) => <option key={profile.id} value={profile.id}>{profile.display_name}</option>)}</select><button className="r2-text-button" type="button" disabled={busy} onClick={() => void openAnalysis(target, 'new')}>Agent 创建画像</button></div>
      </article>)}</div>
    </section>}

    {overrideEditor && <Modal title={`账号差异 · ${platformDisplayName(overrideEditor.target.platform)} · ${overrideEditor.target.identityName || overrideEditor.target.label}`} busy={busy} onClose={() => setOverrideEditor(null)}>
      <p>这些设置只覆盖当前账号的软性表达偏好；平台规则、活动规则、画像红线和用户锁定字段仍优先生效。</p>
      <label className="r2-field"><span>表达差异</span><textarea rows={3} value={overrideEditor.tone} onChange={(e) => setOverrideEditor((current) => current ? { ...current, tone: e.target.value } : current)} placeholder="例如：更口语、开头先给结论、减少营销表达" /></label>
      <label className="r2-field"><span>栏目 / 内容形式</span><textarea rows={3} value={overrideEditor.formats} onChange={(e) => setOverrideEditor((current) => current ? { ...current, formats: e.target.value } : current)} placeholder="例如：录屏实测、60 秒内、固定工具对比栏目" /></label>
      <label className="r2-field"><span>账号备注</span><textarea rows={3} value={overrideEditor.notes} onChange={(e) => setOverrideEditor((current) => current ? { ...current, notes: e.target.value } : current)} placeholder="仅用于这个账号的长期提醒" /></label>
      <footer><button className="r2-button" disabled={busy} onClick={() => setOverrideEditor(null)}>取消</button><button className="r2-button primary" disabled={busy} onClick={() => void saveOverrides()}>保存账号差异</button></footer>
    </Modal>}

    {analysis && <Modal title={`Agent 建议画像 · ${platformDisplayName(analysis.target.platform)} · ${analysis.target.identityName || analysis.target.label}`} busy={busy} onClose={() => setAnalysis(null)}>
      <p>{analysis.capability?.note || '正在读取分析能力…'}</p>
      {analysis.capability && !analysis.capability.automatic_history_supported && <div className="r2-inline-warning">
        当前不会因为账号已登录就自动读取全部历史。请粘贴你确认可以用于分析的代表作品；后续接入平台只读历史能力时仍会在运行前确认样本范围。
      </div>}
      <label className="r2-field"><span>应用到</span><select value={analysis.profileId} onChange={(e) => setAnalysis((current) => current ? { ...current, profileId: e.target.value } : current)}>
        <option value="">新建画像</option>
        {(context?.profiles || []).map((item) => <option key={item.id} value={item.id}>{item.display_name} · V{item.current_revision}</option>)}
      </select></label>
      {!analysis.profileId && <label className="r2-field"><span>新画像名称</span><input value={analysis.displayName} maxLength={120} onChange={(e) => setAnalysis((current) => current ? { ...current, displayName: e.target.value } : current)} /></label>}
      {analysis.capability?.automatic_history_supported && <label className="r2-checkbox profile-history-option"><input type="checkbox" checked={analysis.useAccountHistory} onChange={(e) => setAnalysis((current) => current ? { ...current, useAccountHistory: e.target.checked } : current)} />读取当前账号已发布作品（只读）</label>}
      {analysis.capability?.automatic_history_supported && analysis.useAccountHistory && <label className="r2-field"><span>读取数量</span><select value={analysis.historyLimit} onChange={(e) => setAnalysis((current) => current ? { ...current, historyLimit: Number(e.target.value) } : current)}><option value={10}>最近 10 条</option><option value={20}>最近 20 条</option><option value={30}>最近 30 条</option></select></label>}
      <label className="r2-field"><span>代表作品</span><textarea rows={10} value={analysis.sampleText} onChange={(e) => setAnalysis((current) => current ? { ...current, sampleText: e.target.value } : current)} placeholder={'标题或第一行\n作品正文……\n\n---\n\n另一条作品标题\n作品正文……'} /></label>
      <p className="r2-muted">样本只作为资料。Agent 会把“历史观察 / 推测 / 待确认”分开，生成提案后仍需你确认。</p>
      {analysis.run?.error && <div className="r2-inline-warning">{analysis.run.error}</div>}
      {analysis.run?.proposal && Object.keys(analysis.run.proposal).length > 0 && <div className="profile-analysis-proposal">
        <strong>分析提案</strong>
        {analysis.run.proposal.sample_summary && <p>{analysis.run.proposal.sample_summary}</p>}
        {!!analysis.run.proposal.observations?.length && <><h4>历史观察</h4><ul>{analysis.run.proposal.observations.map((item) => <li key={item}>{item}</li>)}</ul></>}
        {!!analysis.run.proposal.assumptions?.length && <><h4>AI 推测 · 待确认</h4><ul>{analysis.run.proposal.assumptions.map((item) => <li key={item}>{item}</li>)}</ul></>}
        {!!analysis.run.proposal.open_questions?.length && <><h4>还需要你确认</h4><ul>{analysis.run.proposal.open_questions.map((item) => <li key={item}>{item}</li>)}</ul></>}
      </div>}
      <Feedback error={error} />
      <footer>
        <button className="r2-button" type="button" disabled={busy} onClick={() => setAnalysis(null)}>取消</button>
        {analysis.run?.status === 'succeeded'
          ? <button className="r2-button primary" type="button" disabled={busy || (!analysis.profileId && !analysis.displayName.trim())} onClick={() => void applyAnalysis()}>确认并应用提案</button>
          : <button className="r2-button primary" type="button" disabled={busy || (!analysis.sampleText.trim() && !analysis.useAccountHistory) || (!analysis.profileId && !analysis.displayName.trim())} onClick={() => void startAnalysis()}>{busy ? '分析中…' : '开始分析'}</button>}
      </footer>
    </Modal>}
  </section>;
}
