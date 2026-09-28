import { test } from 'node:test';
import assert from 'node:assert/strict';
import type { Task } from '../src/lib/ripple.ts';
import { bodyLength, canDispatchVariantTask, needsResultCheck, publishResultText, reorderMedia, selectVariantTask, taskStatusText, xhsContentProblems, xhsTitleLength } from '../src/components/workspace/variantPublishing.ts';

test('小红书标题计数与后端 UTF-16 加权规则一致', () => {
  assert.equal(xhsTitleLength(' 中文 ABC😀 '), 6);
  assert.equal(xhsTitleLength('a'.repeat(40)), 20);
  assert.equal(xhsTitleLength('a'.repeat(41)), 21);
  assert.equal(xhsTitleLength('中'.repeat(20)), 20);
  assert.equal(xhsTitleLength('😀'.repeat(10)), 20);
});

test('小红书正文按码点计数，去除保存时裁掉的首尾空白', () => {
  assert.equal(bodyLength(' 😀中a\n '), 3);
  assert.equal(bodyLength('😀'.repeat(1000)), 1000);
  const problems = xhsContentProblems({ title: '测试', body: '😀'.repeat(1001), media: ['a.png'] });
  assert.deepEqual(problems, ['小红书正文超出 1 字，请缩短至 1000 字以内。']);
});

test('超限标题保留输入并给出超出数量，临界值可发布', () => {
  assert.deepEqual(xhsContentProblems({ title: '中'.repeat(20), body: '正'.repeat(1000), media: ['a.png'] }), []);
  assert.deepEqual(xhsContentProblems({ title: '中'.repeat(21), body: '', media: ['a.png'] }), ['小红书标题超出 1 字，请缩短至 20 字以内。']);
});

test('小红书媒体校验覆盖缺媒体、数量上限及图视频混用', () => {
  assert.match(xhsContentProblems({ title: '测试', body: '', media: [] })[0], /请先添加素材/);
  assert.deepEqual(xhsContentProblems({ title: '测试', body: '', media: Array.from({ length: 9 }, (_, index) => `${index}.png`) }), []);
  assert.match(xhsContentProblems({ title: '测试', body: '', media: Array.from({ length: 10 }, (_, index) => `${index}.png`) })[0], /最多支持 9 张/);
  assert.deepEqual(xhsContentProblems({ title: '测试', body: '', media: ['a.MP4'] }), []);
  assert.match(xhsContentProblems({ title: '测试', body: '', media: ['a.png', 'a.mp4'] })[0], /不能混用/);
  assert.match(xhsContentProblems({ title: '测试', body: '', media: ['a.mov', 'b.webm'] })[0], /只允许一个视频/);
});

const task = (status: string, notSubmitted = false, mode = 'real') => ({ status, content: { mode }, receipt: notSubmitted ? { not_submitted: true } : null }) as Pick<Task, 'status' | 'content' | 'receipt'>;

test('未知或正在执行的真实发布结果阻止重复准备', () => {
  for (const status of ['accepted', 'unknown_result', 'verification_required', 'dispatching']) assert.equal(needsResultCheck(task(status)), true, status);
  assert.equal(needsResultCheck(task('dispatching', true)), true);
  for (const status of ['draft', 'review_ready', 'approved', 'published', 'failed_retryable']) assert.equal(needsResultCheck(task(status)), false, status);
});

test('已确认未提交或本地模拟不会误判为未知真实结果', () => {
  assert.equal(needsResultCheck(task('verification_required', true)), false);
  assert.equal(needsResultCheck(task('unknown_result', false, 'simulation')), false);
});

test('平台审核与私密范围不会显示公开发布成功', () => {
  const reviewing = { status: 'accepted', receipt: { remote_status: 'reviewing' } } as Pick<Task, 'status' | 'receipt' | 'preflight_problems'>;
  assert.equal(taskStatusText(reviewing), '审核中');
  assert.match(publishResultText(reviewing), /审核中/);
  const privatePost = { status: 'accepted', receipt: { remote_status: 'published', visibility: 'private' } } as Pick<Task, 'status' | 'receipt' | 'preflight_problems'>;
  assert.equal(taskStatusText(privatePost), '仅自己可见');
  assert.match(publishResultText(privatePost), /未公开发布/);
});

const variantTask = (status: string, version = 'version-1') => ({ ...task(status), id: `${status}-${version}`, variant_version_id: version }) as Task;

test('审核任务只允许执行当前已保存且没有正在上传的版本', () => {
  const approved = variantTask('approved');
  assert.equal(canDispatchVariantTask(approved, 'version-1', false, false), true);
  assert.equal(canDispatchVariantTask(approved, 'version-1', true, false), false);
  assert.equal(canDispatchVariantTask(approved, 'version-2', false, false), false);
  assert.equal(canDispatchVariantTask(approved, 'version-1', false, true), false);
  assert.equal(canDispatchVariantTask(null, 'version-1', false, false), false);
  assert.equal(canDispatchVariantTask(variantTask('draft'), 'version-1', false, false), false);
  assert.equal(canDispatchVariantTask(variantTask('unknown_result'), 'version-1', false, false), false);
});

test('旧版本活动排期优先显示，不被新版本草稿或审核任务遮住', () => {
  const oldScheduled = variantTask('scheduled');
  const newApproved = variantTask('approved', 'version-2');
  assert.equal(selectVariantTask([newApproved, oldScheduled], 'version-2'), oldScheduled);
  assert.equal(canDispatchVariantTask(oldScheduled, 'version-2', false, false), false);
  assert.equal(selectVariantTask([newApproved, variantTask('cancelled')], 'version-2'), newApproved);
});

test('待核对结果优先提示，避免活动排期掩盖未知提交', () => {
  const unknown = variantTask('unknown_result');
  assert.equal(selectVariantTask([variantTask('scheduled', 'version-2'), unknown], 'version-3'), unknown);
  assert.equal(selectVariantTask([variantTask('cancelled'), variantTask('failed_terminal')], 'version-1'), null);
});

test('调整素材顺序不修改原数组，边界操作保留全部素材', () => {
  const media = ['封面.png', '内容.png', '结尾.png'];
  assert.deepEqual(reorderMedia(media, 1, -1), ['内容.png', '封面.png', '结尾.png']);
  assert.deepEqual(reorderMedia(media, 1, 1), ['封面.png', '结尾.png', '内容.png']);
  assert.deepEqual(reorderMedia(media, 0, -1), media);
  assert.deepEqual(reorderMedia(media, 2, 1), media);
  assert.deepEqual(media, ['封面.png', '内容.png', '结尾.png']);
});
