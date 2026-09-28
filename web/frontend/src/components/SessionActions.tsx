import { useEffect, useRef, useState } from 'react';
import type { ChatSession } from '../lib/store';

export type SessionAction = 'rename' | 'pin' | 'archive' | 'restore' | 'delete';

interface Props {
  session: ChatSession;
  running?: boolean;
  onAction: (id: string, action: SessionAction, title?: string) => Promise<string | null>;
}

export default function SessionActions({ session, running, onAction }: Props) {
  const [open, setOpen] = useState(false);
  const [dialog, setDialog] = useState<'rename' | 'delete' | null>(null);
  const [name, setName] = useState(session.title);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [position, setPosition] = useState({ left: 0, top: 0 });
  const trigger = useRef<HTMLButtonElement>(null);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === 'Escape') { setOpen(false); trigger.current?.focus(); } };
    document.addEventListener('pointerdown', dismiss);
    document.addEventListener('keydown', escape);
    return () => { document.removeEventListener('pointerdown', dismiss); document.removeEventListener('keydown', escape); };
  }, [open]);

  const perform = async (action: SessionAction, title?: string) => {
    setBusy(true); setError('');
    const message = await onAction(session.id, action, title);
    setBusy(false);
    if (message) { setError(message); return; }
    setDialog(null); setOpen(false); trigger.current?.focus();
  };

  return <div className="focus-session-actions" ref={root}>
    <button ref={trigger} className="focus-session-more" aria-label={`${session.title}的更多操作`} aria-haspopup="menu" aria-expanded={open} onClick={() => {
      if (open) { setOpen(false); return; }
      const rect = trigger.current?.getBoundingClientRect();
      if (rect) setPosition({ left: Math.max(8, Math.min(rect.right + 6, window.innerWidth - 245)), top: Math.max(8, Math.min(rect.top, window.innerHeight - 225)) });
      setOpen(true); setError('');
    }}>···</button>
    {open && <div role="menu" className="focus-session-menu" style={position}>
      <div className="focus-menu-title">{session.title}</div>
      <button role="menuitem" onClick={() => { setName(session.title); setDialog('rename'); setOpen(false); }}>重命名</button>
      {!session.archived && <button role="menuitem" onClick={() => void perform('pin')}>{session.pinned ? '取消置顶' : '置顶会话'}</button>}
      <button role="menuitem" disabled={running} onClick={() => void perform(session.archived ? 'restore' : 'archive')}>{session.archived ? '恢复会话' : '归档会话'}</button>
      <button role="menuitem" className="danger" disabled={running} onClick={() => { setDialog('delete'); setOpen(false); }}>删除会话</button>
      {running && <small>生成中，请先停止后归档或删除。</small>}
      {error && <small role="alert">{error}</small>}
    </div>}
    {dialog && <div className="focus-session-dialog-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setDialog(null); }}>
      <section role="dialog" aria-modal="true" aria-label={dialog === 'rename' ? '重命名会话' : '删除会话'} className="focus-session-dialog" onKeyDown={(event) => { if (event.key === 'Escape' && !busy) setDialog(null); }}>
        <h2>{dialog === 'rename' ? '重命名会话' : '删除这段会话？'}</h2>
        {dialog === 'rename' ? <label>会话名称<input aria-label="会话名称" autoFocus maxLength={80} value={name} onChange={(event) => setName(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && name.trim()) void perform('rename', name); }} /></label>
          : <><strong>{session.title}</strong><p>删除后，会话消息无法恢复。{session.contentContext ? `关联主稿「${session.contentContext.title}」会保留。` : '已有主稿不会被删除。'}</p></>}
        {error && <p role="alert" className="focus-session-error">{error}</p>}
        <div className="focus-dialog-actions"><button disabled={busy} onClick={() => setDialog(null)}>取消</button><button className={dialog === 'delete' ? 'danger' : 'primary'} disabled={busy || (dialog === 'rename' && !name.trim())} onClick={() => void perform(dialog, name)}>{busy ? '处理中…' : dialog === 'rename' ? '保存名称' : '删除会话'}</button></div>
      </section>
    </div>}
  </div>;
}
