import { useState, useEffect } from 'react';
import { copyContentProfile, deletePersona, fetchContentProfile, fetchContentProfileContext, fetchPersonaFiles, renameContentProfile, saveContentProfileRevision } from '../lib/api';
import type { ContentProfileDetail, PersonaFile } from '../lib/api';
import { renderMarkdown } from '../lib/sanitize';

interface ProfilePageProps {
  persona: string;
  onNewProfile: () => void;
  onDeleted: (name: string) => void;
  onDirtyChange?: (dirty: boolean) => void;
}

const DIM_META: Record<string, { label: string; icon: string }> = {
  'identity.md': { label: '身份定位', icon: '🪪' },
  'style.md': { label: '内容风格', icon: '🎨' },
  'audience.md': { label: '目标受众', icon: '👥' },
  'platforms.md': { label: '平台运营', icon: '📱' },
  'preferences.md': { label: '偏好与红线', icon: '⚖️' },
  'memory.md': { label: '经验沉淀', icon: '🧠' },
};

export default function ProfilePage({ persona, onNewProfile, onDeleted, onDirtyChange }: ProfilePageProps) {
  const [files, setFiles] = useState<PersonaFile[]>([]);
  const [contentProfile, setContentProfile] = useState<ContentProfileDetail | null>(null);
  const [displayName, setDisplayName] = useState('');
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [editing, setEditing] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [toast, setToast] = useState('');

  useEffect(() => {
    if (!persona) { setFiles([]); return; }
    let ignore = false;   // 切 persona 丢弃旧请求（F2）
    setLoading(true);
    setError('');
    setEditing(false);
    Promise.all([fetchPersonaFiles(persona), fetchContentProfileContext({ profile_name: persona })])
      .then(async ([d, context]) => {
        if (ignore) return;
        setFiles(d.files);
        setDrafts(Object.fromEntries(d.files.map((f) => [f.filename, f.content])));
        const summary = context.profiles.find((item) => item.legacy_name === persona);
        if (summary) {
          const detail = await fetchContentProfile(summary.id);
          if (ignore) return;
          setContentProfile(detail);
          setDisplayName(detail.display_name);
        } else {
          setContentProfile(null);
          setDisplayName(persona);
        }
      })
      .catch(() => { if (!ignore) setError('加载画像失败'); })
      .finally(() => { if (!ignore) setLoading(false); });
    return () => { ignore = true; };
  }, [persona]);

  const showToast = (msg: string) => { setToast(msg); setTimeout(() => setToast(''), 2500); };

  const dirty = files.some((file) => (drafts[file.filename] ?? '') !== file.content);
  const nameDirty = !!contentProfile && displayName.trim() !== contentProfile.display_name;
  const hasUnsavedChanges = dirty || nameDirty;

  useEffect(() => {
    onDirtyChange?.(hasUnsavedChanges);
    return () => onDirtyChange?.(false);
  }, [hasUnsavedChanges, onDirtyChange]);

  useEffect(() => {
    if (!hasUnsavedChanges) return;
    const unloadGuard = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ''; };
    const navigationGuard = (event: Event) => {
      if (!window.confirm('还有未保存的画像修改。离开当前画像会放弃这些修改，继续吗？')) event.preventDefault();
    };
    window.addEventListener('beforeunload', unloadGuard);
    window.addEventListener('ripple:before-navigate', navigationGuard);
    return () => {
      window.removeEventListener('beforeunload', unloadGuard);
      window.removeEventListener('ripple:before-navigate', navigationGuard);
    };
  }, [hasUnsavedChanges]);

  const handleRename = async () => {
    if (!contentProfile || !nameDirty || !displayName.trim()) return;
    setSaving(true);
    try {
      const next = await renameContentProfile(contentProfile.id, displayName.trim());
      setContentProfile(next);
      setDisplayName(next.display_name);
      window.dispatchEvent(new Event('ripple:profile-context-changed'));
      showToast('画像名称已更新；稳定 ID 与历史引用保持不变');
    } catch (e) {
      showToast(e instanceof Error ? e.message : '改名失败');
    } finally {
      setSaving(false);
    }
  };

  const handleCopy = async () => {
    if (!contentProfile || saving) return;
    const name = window.prompt('复制为新的账号画像名称', `${contentProfile.display_name} 副本`)?.trim();
    if (!name) return;
    setSaving(true);
    try {
      const created = await copyContentProfile(contentProfile.id, name);
      window.dispatchEvent(new Event('ripple:profile-context-changed'));
      showToast(`已创建独立画像「${created.display_name}」`);
    } catch (e) {
      showToast(e instanceof Error ? e.message : '复制失败');
    } finally {
      setSaving(false);
    }
  };

  const handleSave = async () => {
    if (!contentProfile || !dirty) return;
    setSaving(true);
    try {
      const next = await saveContentProfileRevision(contentProfile.id, {
        expected_revision: contentProfile.current_revision,
        files: drafts,
        note: '用户在画像页面确认修改。',
        confirm: true,
      });
      setContentProfile(next);
      setFiles(Object.entries(next.files).map(([filename, content]) => ({ filename, content })));
      setDrafts(next.files);
      setEditing(false);
      window.dispatchEvent(new Event('ripple:profile-context-changed'));
      showToast(`画像已确认生效 · V${next.current_revision}`);
    } catch (e) {
      showToast(e instanceof Error ? e.message : '保存失败');
    } finally {
      setSaving(false);
    }
  };

  const toggleEditing = () => {
    if (editing && (dirty || nameDirty) && !window.confirm('还有未保存的画像修改。退出编辑将放弃这些修改，继续吗？')) return;
    if (editing && dirty) setDrafts(Object.fromEntries(files.map((file) => [file.filename, file.content])));
    if (editing && nameDirty && contentProfile) setDisplayName(contentProfile.display_name);
    setEditing((value) => !value);
  };

  const handleDelete = async () => {
    if (!window.confirm(`确定删除画像「${persona}」吗？\n此操作不可恢复，将删除该画像的全部六维文件。`)) return;
    setDeleting(true);
    try {
      await deletePersona(persona);
      onDeleted(persona);
      showToast(`已删除画像「${persona}」`);
    } catch (e) {
      showToast(e instanceof Error ? e.message : '删除失败');
    } finally {
      setDeleting(false);
    }
  };

  if (!persona) {
    return (
      <div className="profile-page">
        <h1 className="page-title">账号画像</h1>
        <div className="empty-state" style={{ height: '70%' }}>
          <div className="empty-icon">👤</div>
          <h3>还没有选择画像</h3>
          <p>画像沉淀账号定位、受众、表达方式与红线，可以先建画像再绑定平台账号。</p>
          <button className="btn btn-primary" onClick={onNewProfile}>+ 新建画像</button>
        </div>
      </div>
    );
  }

  return (
    <div className="profile-page">
      <div className="profile-head">
        <div>
          {editing ? <div className="profile-name-edit"><input aria-label="画像显示名称" maxLength={120} value={displayName} onChange={(e) => setDisplayName(e.target.value)} /><button className="btn btn-sm" disabled={!nameDirty || saving || !displayName.trim()} onClick={() => void handleRename()}>保存名称</button></div> : <h1 className="page-title">{contentProfile?.display_name || persona}</h1>}
          <p className="page-subtitle">账号画像 · {contentProfile ? `V${contentProfile.current_revision}` : '读取中'} · {contentProfile?.bindings.length || 0} 个关联账号{hasUnsavedChanges ? ' · 有未保存修改' : ''}</p>
        </div>
        <div style={{ display: 'flex', gap: 8 }}>
          {editing && <button className="btn btn-primary" disabled={!dirty || saving || !contentProfile} onClick={() => void handleSave()}>{saving ? '保存中…' : dirty ? '保存并确认生效' : '没有修改'}</button>}
          <button className={`btn ${editing ? '' : 'btn-primary'}`} disabled={saving} onClick={toggleEditing}>
            {editing ? '退出编辑' : '✏️ 编辑画像'}
          </button>
          <button className="btn" disabled={saving || !contentProfile} onClick={() => void handleCopy()}>复制画像</button>
          <button className="btn" style={{ color: 'var(--red)', borderColor: 'var(--red)' }}
            disabled={deleting || saving} onClick={handleDelete}>
            {deleting ? '删除中…' : '🗑 删除画像'}
          </button>
        </div>
      </div>

      {error && <div style={{ color: 'var(--red)', fontSize: 14, marginTop: 12 }}>{error}</div>}

      {loading ? (
        <div className="loading"><div className="spinner" />加载中…</div>
      ) : (
        files.map((f) => {
          const meta = DIM_META[f.filename] || { label: f.filename, icon: '📄' };
          const dirty = editing && (drafts[f.filename] ?? '') !== f.content;
          return (
            <div key={f.filename} className="profile-dim">
              <div className="profile-dim-head">
                <div className="profile-dim-title">{meta.icon} {meta.label}</div>
                {editing && <span className="r2-muted">{dirty ? '有修改' : '已保存'}</span>}
              </div>
              {editing ? (
                <textarea
                  className="field"
                  style={{ minHeight: 150, fontFamily: "'SF Mono','Consolas',monospace", fontSize: 13 }}
                  value={drafts[f.filename] ?? ''}
                  onChange={(e) => setDrafts((p) => ({ ...p, [f.filename]: e.target.value }))}
                />
              ) : (
                <div className="card" style={{ padding: '14px 18px' }}>
                  <div className="profile-content"
                    dangerouslySetInnerHTML={{ __html: renderMarkdown(f.content || '_（空）_') }} />
                </div>
              )}
            </div>
          );
        })
      )}

      {toast && <div className="toast ok"><span className="toast-icon">✓</span>{toast}</div>}
    </div>
  );
}
