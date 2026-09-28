import { test } from 'node:test';
import assert from 'node:assert/strict';
import { contentDraftChanged, mergeContentDraft } from '../src/lib/contentDraftMerge.ts';

const base = { title: '标题', body: '正文', tags: '旅行', media: ['cover.png'], project_id: 'local' };

test('AI 新素材到达时保留用户正在编辑的正文', () => {
  const local = { ...base, body: '用户正在写的正文' };
  const remote = { ...base, title: '新标题', media: ['cover.png', 'generated.png'] };
  const merged = mergeContentDraft(base, local, remote);
  assert.equal(merged.body, local.body);
  assert.equal(merged.title, remote.title);
  assert.deepEqual(merged.media, remote.media);
  assert.equal(contentDraftChanged(merged, remote), true);
});

test('同时上传和生成图片时保留两者且不恢复已删除的素材', () => {
  const local = { ...base, media: ['upload.png'] };
  const remote = { ...base, media: ['cover.png', 'generated.png'] };
  assert.deepEqual(mergeContentDraft(base, local, remote).media, ['upload.png', 'generated.png']);
});

test('未编辑时完整采用新版本且不标为未保存', () => {
  const remote = { ...base, body: 'AI 更新正文', media: [] };
  const merged = mergeContentDraft(base, base, remote);
  assert.deepEqual(merged, remote);
  assert.equal(contentDraftChanged(merged, remote), false);
});

test('用户调整素材顺序时新图追加到末尾', () => {
  const before = { ...base, media: ['a.png', 'b.png'] };
  assert.deepEqual(mergeContentDraft(before, { ...before, media: ['b.png', 'a.png'] },
    { ...before, media: ['a.png', 'b.png', 'c.png'] }).media, ['b.png', 'a.png', 'c.png']);
});
