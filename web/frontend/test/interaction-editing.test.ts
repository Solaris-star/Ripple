import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readReplyEdits, replyKey, interactionStats, interactionTargetUrl } from '../src/lib/replyEditing.ts';

test('恢复同 ID 评论在不同账号或作品下的独立编辑', () => {
  const saved = { [replyKey('account-a-work-1', '101')]: '第一份', [replyKey('account-b-work-1', '101')]: '第二份' };
  assert.deepEqual(readReplyEdits({ getItem: () => JSON.stringify(saved) }), saved);
  assert.deepEqual(readReplyEdits({ getItem: () => '{损坏内容' }), {});
  assert.deepEqual(readReplyEdits({ getItem: () => '["无效"]' }), {});
  assert.deepEqual(readReplyEdits({ getItem: () => '{"source:101":{},"source:102":"有效"}' }), { 'source:102': '有效' });
  assert.deepEqual(readReplyEdits({ getItem: () => { throw new Error('存储不可用'); } }), {});
});

test('范围内统计不会重复计算同一评论，也保留未知结果数', () => {
  const sources = [{ id: 'a', count: 3 }];
  const tasks = [
    { source_id: 'a', kind: 'reply', status: 'verified', item_results: [{ id: '101', status: 'verified' }] },
    { source_id: 'a', kind: 'reply', status: 'verified', item_results: [{ id: '101', status: 'verified' }] },
    { source_id: 'a', kind: 'reply', status: 'unknown_result' },
    { source_id: 'a', kind: 'reply', status: 'draft' },
  ];
  assert.deepEqual(interactionStats(sources, tasks), { comments: 3, processed: 1, pending: 2, sources: 1, drafts: 1, unknown: 1 });
});

test('X 核对链接定位评论，小红书回到原作品，忽略无效地址', () => {
  assert.equal(interactionTargetUrl('x', 'https://x.com/i/status/100', '101'), 'https://x.com/i/status/101');
  assert.equal(interactionTargetUrl('xiaohongshu', 'https://www.xiaohongshu.com/explore/abc', 'comment'), 'https://www.xiaohongshu.com/explore/abc');
  assert.equal(interactionTargetUrl('xiaohongshu', 'javascript:alert(1)'), '');
  assert.equal(interactionTargetUrl('x', 'https://example.com'), '');
});
