import { useCallback, useEffect, useState } from 'react';
import type { ChatArtifact } from '../lib/store';
import { api, errorText } from '../lib/ripple';
import type { Mother } from '../lib/ripple';

type Proposal = {
  id: string;
  content_id: string;
  base_version_id: string;
  before: Mother['content'];
  after: Mother['content'];
  status: 'pending' | 'applied' | 'dismissed' | 'undone';
  applied_version_id: string | null;
  undone_version_id: string | null;
};

export default function ContentProposalCard({ artifact, canApply = true, onChanged }: {
  artifact: ChatArtifact;
  canApply?: boolean;
  onChanged?: (contentId: string) => void;
}) {
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [currentVersion, setCurrentVersion] = useState('');
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const load = useCallback(async () => {
    if (!artifact.proposal_id) return;
    const [next, source] = await Promise.all([
      api<Proposal>(`/api/ripple/content-proposals/${encodeURIComponent(artifact.proposal_id)}`),
      api<Mother>(`/api/ripple/contents/${encodeURIComponent(artifact.id)}`),
    ]);
    setProposal(next); setCurrentVersion(source.version_id);
  }, [artifact.id, artifact.proposal_id]);
  useEffect(() => { void load().catch((cause) => setError(errorText(cause))); }, [load]);

  const act = async (kind: 'apply' | 'dismiss' | 'undo') => {
    if (!proposal || busy) return;
    setBusy(true); setError('');
    try {
      if (kind === 'undo') {
        if (currentVersion !== proposal.applied_version_id) throw new Error('主稿已有新编辑，请先比较当前版本。');
        await api<Proposal>(`/api/ripple/content-proposals/${encodeURIComponent(proposal.id)}/undo`, 'POST', { expected_version: proposal.applied_version_id });
      } else {
        await api<Proposal>(`/api/ripple/content-proposals/${encodeURIComponent(proposal.id)}/${kind}`, 'POST',
          kind === 'apply' ? { expected_version: proposal.base_version_id } : undefined);
      }
      await load();
      onChanged?.(proposal.content_id);
    } catch (cause) { setError(errorText(cause)); }
    finally { setBusy(false); }
  };

  return <section className="focus-proposal-card" aria-label="主稿修改建议">
    <header><span>✧ 主稿修改建议</span><strong>{proposal?.status === 'applied' ? '已应用' : proposal?.status === 'dismissed' ? '已放弃' : proposal?.status === 'undone' ? '已撤销' : '待确认'}</strong></header>
    <p>{proposal ? `修改「${proposal.before.title}」的标题、正文、话题或素材。` : '正在读取修改建议…'}</p>
    {proposal && <>
      <button className="focus-proposal-view" onClick={() => setOpen((value) => !value)}>{open ? '收起差异' : '查看差异'}</button>
      {open && <div className="focus-proposal-diff"><div><span>修改前</span><strong>{proposal.before.title}</strong><p>{proposal.before.body}</p></div><div><span>建议修改</span><strong>{proposal.after.title}</strong><p>{proposal.after.body}</p></div></div>}
      {proposal.status === 'pending' && <div className="focus-proposal-actions"><button disabled={busy} onClick={() => void act('dismiss')}>放弃</button><button className="primary" disabled={busy || !canApply} onClick={() => void act('apply')}>应用到主稿</button></div>}
      {proposal.status === 'applied' && <div className="focus-proposal-actions"><button disabled={busy || currentVersion !== proposal.applied_version_id} onClick={() => void act('undo')}>撤销这次修改</button></div>}
      {!canApply && proposal.status === 'pending' && <small>请先保存当前未保存的主稿，再应用 AI 建议。</small>}
    </>}
    {error && <p role="alert" className="focus-proposal-error">{error}</p>}
  </section>;
}
