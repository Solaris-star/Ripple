export interface ChatArtifactRef {
  kind: 'content_draft';
  id: string;
  version_id: string;
  title: string;
  status: 'draft' | 'proposal';
  proposal_id?: string;
}

export function parseChatArtifact(data: string): ChatArtifactRef | null {
  try {
    let value: unknown = JSON.parse(data);
    if (typeof value === 'string') value = JSON.parse(value);
    if (!value || typeof value !== 'object') return null;
    const row = value as Record<string, unknown>;
    if (row.kind !== 'content_draft' || typeof row.id !== 'string' || !/^[a-f0-9]{32}$/.test(row.id)
        || typeof row.version_id !== 'string' || !/^[a-f0-9]{64}$/.test(row.version_id)) return null;
    const proposalId = typeof row.proposal_id === 'string' && /^[a-f0-9]{32}$/.test(row.proposal_id) ? row.proposal_id : undefined;
    return { kind: 'content_draft', id: row.id, version_id: row.version_id,
      title: typeof row.title === 'string' ? row.title.slice(0, 200) : '',
      status: proposalId ? 'proposal' : 'draft', ...(proposalId ? { proposal_id: proposalId } : {}) };
  } catch { return null; }
}
