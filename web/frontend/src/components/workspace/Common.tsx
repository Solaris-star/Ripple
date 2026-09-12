import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import { GLYPHS, LABELS } from '../../lib/ripple';

export function Mark({ platform }: { platform: string }) {
  return <span className={`r2-mark r2-mark-${platform}`} aria-hidden="true">{GLYPHS[platform] || '·'}</span>;
}
export function Status({ status }: { status: string }) { return <span className={`r2-status r2-status-${status}`}><i />{LABELS[status] || status}</span>; }
export function Empty({ title, description, children }: { title: string; description?: string; children?: ReactNode }) {
  return <div className="r2-empty"><strong>{title}</strong>{description && <p>{description}</p>}{children}</div>;
}
export function Header({ title, subtitle, children }: { title: string; subtitle?: string; children?: ReactNode }) {
  return <header className="r2-header"><div><h1>{title}</h1>{subtitle && <p>{subtitle}</p>}</div><div className="r2-toolbar">{children}</div></header>;
}
export function Feedback({ error, notice }: { error?: string; notice?: string }) {
  return <>{error && <div className="r2-feedback r2-error" role="alert">{error}</div>}{notice && <div className="r2-feedback" role="status">{notice}</div>}</>;
}
export function Modal({ title, onClose, children, busy = false, className = '' }: { title: string; onClose: () => void; children: ReactNode; busy?: boolean; className?: string }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    return () => previous?.focus();
  }, []);
  return <div className="r2-overlay"><div ref={ref} tabIndex={-1} className={`r2-dialog ${className}`.trim()} role="dialog" aria-modal="true" aria-label={title}
    onKeyDown={e => {
      if (e.key === 'Escape' && !busy) onClose();
      if (e.key !== 'Tab') return;
      const els = ref.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), a[href], [tabindex="0"]');
      if (!els?.length) { e.preventDefault(); return; }
      const first = els[0], last = els[els.length - 1];
      if (e.shiftKey && (document.activeElement === first || document.activeElement === ref.current)) { e.preventDefault(); last.focus(); }
      if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }}><header><h2>{title}</h2><button className="r2-icon-button" aria-label="关闭对话框" disabled={busy} onClick={onClose}>×</button></header>{children}</div></div>;
}
