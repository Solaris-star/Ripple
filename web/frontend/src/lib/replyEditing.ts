export const REPLY_BATCH_LIMIT = 20;
export const REPLY_EDITS_STORAGE_KEY = 'interaction_reply_edits_v1';
export const replyKey = (sourceId: string, commentId: string) => `${sourceId}:${commentId}`;

export function readReplyEdits(storage: Pick<Storage, 'getItem'>, key = REPLY_EDITS_STORAGE_KEY): Record<string, string> {
  try {
    const value: unknown = JSON.parse(storage.getItem(key) || '{}');
    if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
    return Object.fromEntries(Object.entries(value).filter(([key, text]) => key.includes(':') && typeof text === 'string' && text.length <= 50000));
  } catch { return {}; }
}

export function interactionStats(sources: { id: string; count: number }[], tasks: { source_id: string; kind: string; status: string; item_results?: { id: string; status: string }[] }[]) {
  const done = new Map<string, Set<string>>();
  for (const task of tasks) {
    if (task.kind !== 'reply') continue;
    const ids = done.get(task.source_id) || new Set<string>();
    for (const item of task.item_results || []) if (item.status === 'verified') ids.add(item.id);
    done.set(task.source_id, ids);
  }
  const comments = sources.reduce((sum, item) => sum + (item.count || 0), 0);
  const processed = sources.reduce((sum, item) => sum + Math.min(item.count || 0, done.get(item.id)?.size || 0), 0);
  return { comments, processed, pending: Math.max(0, comments - processed), sources: sources.length,
    drafts: tasks.filter(task => task.status === 'draft').length,
    unknown: tasks.filter(task => task.status === 'unknown_result').length };
}

export function interactionTargetUrl(platform: string, targetUrl: string, commentId?: string): string {
  if (platform === 'x' && commentId && /^\d{1,30}$/.test(commentId)) return `https://x.com/i/status/${commentId}`;
  try {
    const url = new URL(targetUrl);
    const hosts = platform === 'x' ? ['x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'] : ['www.xiaohongshu.com', 'xiaohongshu.com'];
    return url.protocol === 'https:' && !url.username && !url.password && hosts.includes(url.hostname) ? url.href : '';
  } catch { return ''; }
}

export function toggleReplySelection(current: Set<string>, id: string): Set<string> {
  const next = new Set(current);
  if (next.has(id)) next.delete(id);
  else if (next.size < REPLY_BATCH_LIMIT) next.add(id);
  return next;
}

export function mergeReplySuggestions(current: Record<string, string>, revisions: Record<string, number>, before: Record<string, number>, suggestions: Record<string, string>) {
  const next = { ...current };
  for (const [key, text] of Object.entries(suggestions)) {
    if ((revisions[key] || 0) === (before[key] || 0)) next[key] = text;
  }
  return next;
}
