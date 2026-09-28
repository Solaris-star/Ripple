import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { Task, XhsNoteSummary } from '../src/lib/ripple.ts';
import { findLinkedXhsTask } from '../src/lib/remotePostLink.ts';

const note = { note_id: '6ab8fb3700000000140029d1', title: '同名作品', url: '',
  scope: 'account_notes', metrics: {} } as XhsNoteSummary;
const task = (id: string, accountId: string, remoteId: string, owner: string) => ({
  id, status: 'accepted', content: { mode: 'real', platform: 'xiaohongshu',
    account_id: accountId, title: '同名作品' },
  receipt: { remote_id: remoteId, account_remote_id: owner, public_url: null },
}) as Task;

test('同名作品仅凭账号身份和远端 ID 关联', () => {
  const wrongId = task('wrong-id', 'account-a', 'different-note', 'owner-a');
  const wrongAccount = task('wrong-account', 'account-b', note.note_id, 'owner-b');
  const missingOwner = task('missing-owner', 'account-a', note.note_id, '');
  assert.equal(findLinkedXhsTask(note, 'account-a', 'owner-a',
    [wrongId, wrongAccount, missingOwner]), undefined);
  const correct = task('correct', 'account-a', note.note_id, 'owner-a');
  assert.equal(findLinkedXhsTask(note, 'account-a', 'owner-a',
    [wrongId, correct])?.id, 'correct');
});

test('已核对账号归属的历史链接可按作品 ID 关联', () => {
  const linked = task('historical', 'account-a', '', 'owner-a');
  linked.receipt!.public_url = 'https://www.xiaohongshu.com/explore/' + note.note_id;
  assert.equal(findLinkedXhsTask(note, 'account-a', 'owner-a', [linked])?.id, 'historical');
  assert.equal(findLinkedXhsTask(note, 'account-a', 'other-owner', [linked]), undefined);
});
