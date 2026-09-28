import { test } from 'node:test';
import assert from 'node:assert/strict';
import { workspaceUrl } from '../src/lib/workspaceNavigation.ts';
import { publishResultText, variantDraftChanged } from '../src/components/workspace/variantPublishing.ts';
import type { VariantContent } from '../src/lib/ripple.ts';
import { ideaContentContext } from '../src/lib/creationIntent.ts';
import type { Idea } from '../src/lib/api.ts';

const saved: VariantContent = { project_id: 'local', title: '桌面整理', body: '正文', tags: '收纳', media: ['a.png', 'b.png'], target_id: 'account-1', delivery: 'remote', timezone: 'Asia/Shanghai', scheduled_local: null, fold: null, options: { a: true, nested: { b: 2, a: 1 } } };

test('平台稿恢复原文后清除未保存状态，媒体顺序和来源版本变化仍算修改', () => {
  assert.equal(variantDraftChanged({ ...saved, title: '新标题' }, saved, 'v1', 'v1'), true);
  assert.equal(variantDraftChanged({ ...saved }, saved, 'v1', 'v1'), false);
  assert.equal(variantDraftChanged({ ...saved, options: { nested: { a: 1, b: 2 }, a: true } }, saved, 'v1', 'v1'), false);
  assert.equal(variantDraftChanged({ ...saved, media: ['b.png', 'a.png'] }, saved, 'v1', 'v1'), true);
  assert.equal(variantDraftChanged(saved, saved, 'v2', 'v1'), true);
  assert.equal(variantDraftChanged({ ...saved, target_id: 'other' }, saved, 'v1', 'v1'), true);
});

test('发布详情按 URL 定位原稿和平台稿，不依赖浏览器存储', () => {
  const editor = workspaceUrl('http://localhost/?page=publish&task=old', 'contents', { contentId: 'source-1', variantId: 'variant-1' });
  assert.equal(editor.search, '?page=contents&content=source-1&variant=variant-1');
  const task = workspaceUrl(editor.href, 'publish', { taskId: 'task-1' });
  assert.equal(task.search, '?page=publish&task=task-1');
  assert.equal(workspaceUrl(editor.href, 'contents', { contentId: 'source-2' }).searchParams.has('variant'), false);
});

test('手动写稿保留平台和选题引用，并明确不调用 AI', () => {
  const idea = { id: 'idea-1', title: '选题', target_platforms: ['xiaohongshu'], requirements: ['本地素材'] } as Idea;
  const context = ideaContentContext(idea, null, 'content-1', false);
  assert.equal(context.autoStart, false);
  assert.equal(context.contentId, 'content-1');
  assert.equal(context.ideaId, 'idea-1');
  assert.deepEqual(context.targetPlatforms, ['xiaohongshu']);
  assert.equal(context.brief, undefined);
});

test('平台接收、公众号草稿和未提交结果不会被误报为发布成功', () => {
  assert.equal(publishResultText({ status: 'accepted', receipt: null }), '平台已接收，尚未确认发布成功。');
  assert.match(publishResultText({ status: 'published', receipt: { draft_only: true } }), /尚未公开发布/);
  assert.match(publishResultText({ status: 'verification_required', receipt: { not_submitted: true } }), /尚未提交/);
});
