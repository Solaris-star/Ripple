import type { Task, XhsNoteSummary } from './ripple';

function noteIdFromUrl(value: string | null | undefined): string {
  const match = String(value || '').match(/\/(?:explore|discovery\/item|item)\/([0-9A-Za-z]+)/);
  return match?.[1] || '';
}

export function findLinkedXhsTask(
  note: XhsNoteSummary, accountId: string, accountRemoteId: string, tasks: Task[],
): Task | undefined {
  if (!note.note_id || !accountId || !accountRemoteId) return undefined;
  return tasks.find(task => {
    if (task.content.mode !== 'real' || task.content.platform !== 'xiaohongshu'
      || task.content.account_id !== accountId) return false;
    const receipt = task.receipt;
    if (!receipt || receipt.account_remote_id !== accountRemoteId) return false;
    const remoteId = receipt.remote_id || noteIdFromUrl(receipt.public_url);
    return remoteId === note.note_id;
  });
}
