import { test } from 'node:test';
import assert from 'node:assert/strict';
import { apiErrorMessage } from '../src/lib/apiError.ts';
import { platformContentProblems, taskNeedsAttention, taskStatusText } from '../src/components/workspace/variantPublishing.ts';
import type { VariantContent, Task } from '../src/lib/ripple.ts';

const content: VariantContent = { project_id: 'local', title: '测试', body: '正文', media: [], tags: '', target_id: 'test-account', delivery: 'remote', scheduled_local: null, timezone: 'Asia/Shanghai', fold: null, options: {} };

test('结构化错误保留字段原因，版本不匹配有恢复提示', () => {
  assert.match(apiErrorMessage({ detail: [{ loc: ['body', 'manual'], type: 'extra_forbidden', msg: 'Extra inputs are not permitted' }] }, '失败'), /接口版本不一致/);
  assert.equal(apiErrorMessage({ detail: [{ loc: ['body', 'title'], msg: '不能为空' }] }, '失败'), 'title：不能为空');
  assert.equal(apiErrorMessage({ detail: { message: '服务不可用' } }, '失败'), '服务不可用');
  assert.equal(apiErrorMessage({ detail: { unexpected: true } }, '失败'), '失败');
});

test('X 发布按加权字符校验，长 URL 使用固定权重', () => {
  assert.deepEqual(platformContentProblems('x', { ...content, body: '测'.repeat(140) }), []);
  assert.match(platformContentProblems('x', { ...content, body: '测'.repeat(141) })[0], /282/);
  assert.deepEqual(platformContentProblems('x', { ...content, body: 'https://example.com/' + 'a'.repeat(350) }), []);
  assert.match(platformContentProblems('x', { ...content, tags: '测试' })[0], /话题/);
});

test('公众号标题和 B 站版权参数在创建任务前检查', () => {
  assert.deepEqual(platformContentProblems('wechat', { ...content, title: '测'.repeat(32) }), []);
  assert.match(platformContentProblems('wechat', { ...content, title: '测'.repeat(33) })[0], /32/);
  assert.equal(platformContentProblems('bilibili', content).length, 2);
  assert.match(platformContentProblems('bilibili', { ...content, options: { bilibili_tid: 36, bilibili_copyright: 2 } })[0], /来源/);
  assert.deepEqual(platformContentProblems('bilibili', { ...content, options: { bilibili_tid: 36, bilibili_copyright: 2, bilibili_source: '来源作品' } }), []);
});

test('预检失败显示在待处理列表，回执来源区分接口与人工', () => {
  const failed = { status: 'draft', preflight_problems: ['尚未接入'], receipt: null };
  assert.equal(taskNeedsAttention(failed), true);
  assert.equal(taskStatusText(failed), '预检未通过');
  assert.equal(taskNeedsAttention({ ...failed, preflight_problems: [] }), false);
  assert.equal(taskStatusText({ status: 'published', receipt: { evidence: 'remote_api' } as Task['receipt'] }), '已发布 · 接口已确认');
  assert.equal(taskStatusText({ status: 'published', receipt: { verification: 'manual_user_confirmation' } as Task['receipt'] }), '已发布 · 人工已确认');
});
