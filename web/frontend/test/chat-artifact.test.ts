import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseChatArtifact } from '../src/lib/chatArtifact.ts';

const contentId = 'a'.repeat(32);
const versionId = 'b'.repeat(64);
const proposalId = 'c'.repeat(32);

test('流式建议保留 proposal_id，显示为待确认修改', () => {
  const value = parseChatArtifact(JSON.stringify({ kind: 'content_draft', id: contentId, version_id: versionId,
    proposal_id: proposalId, title: '建议标题', status: 'proposal' }));
  assert.deepEqual(value, { kind: 'content_draft', id: contentId, version_id: versionId,
    proposal_id: proposalId, title: '建议标题', status: 'proposal' });
});

test('原有新建主稿产物仍作为 draft 读取', () => {
  const value = parseChatArtifact(JSON.stringify({ kind: 'content_draft', id: contentId, version_id: versionId,
    title: '新稿', status: 'draft' }));
  assert.equal(value?.status, 'draft');
  assert.equal(value?.proposal_id, undefined);
});

test('无效的建议 ID 不进入修改流程', () => {
  const value = parseChatArtifact(JSON.stringify({ kind: 'content_draft', id: contentId, version_id: versionId,
    proposal_id: 'not-valid', title: '无效建议' }));
  assert.equal(value?.status, 'draft');
  assert.equal(value?.proposal_id, undefined);
});
