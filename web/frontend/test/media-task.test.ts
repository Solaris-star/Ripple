import { test } from 'node:test';
import assert from 'node:assert/strict';
import { finishMediaTasks, imageRetryPrompt, mediaElapsed, mergeMediaTask, parseMediaTask, type MediaTask } from '../src/lib/mediaTask.ts';

const running: MediaTask = { id: 'image-1', tool: 'ripple_generate_image', status: 'running', started_at: 1000 };

test('图片事件兼容 SSE 二次序列化并保留成功产物', () => {
  const value = { ...running, status: 'completed', finished_at: 61000, path: 'AI媒体生成/ok.png' };
  assert.deepEqual(parseMediaTask(JSON.stringify(JSON.stringify(value))), value);
  assert.equal(mediaElapsed(value as MediaTask, 99000), '1 分 0 秒');
});

test('无产物和越界路径不显示图片成功', () => {
  for (const path of [undefined, '../secret.png', '/tmp/x.png', 'C:/secret.png', '_private/a.png', 'a/x.html']) {
    const task = parseMediaTask(JSON.stringify({ ...running, status: 'completed', path }));
    assert.equal(task?.status, 'interrupted');
    assert.equal(task?.path, undefined);
  }
});

test('轮次结束将未决图片保留为未完成，不篡改已知失败或产物', () => {
  const failed: MediaTask = { ...running, id: 'image-2', status: 'failed', error: '失败信息' };
  const done: MediaTask = { ...running, id: 'image-3', status: 'completed', path: 'AI媒体生成/ok.webp' };
  const result = finishMediaTasks([running, failed, done], 9000);
  assert.equal(result[0].status, 'interrupted');
  assert.equal(result[0].finished_at, 9000);
  assert.equal(result[1], failed);
  assert.equal(result[2], done);
  assert.equal(running.status, 'running');
});

test('重放同一工具状态不会添加重复卡片', () => {
  const done: MediaTask = { ...running, status: 'completed', path: 'AI媒体生成/ok.png' };
  assert.deepEqual(mergeMediaTask(mergeMediaTask([], running), done), [done]);
});

test('图片重试保留原始要求并明确只重试图片步骤', () => {
  const original = '写一篇正文并生成风景配图。\n保留这段原话。';
  const retry = imageRetryPrompt(original);
  assert.ok(retry.endsWith(original));
  assert.match(retry, /仅重试上一轮未完成的图片生成步骤/);
  assert.match(retry, /保留当前标题、正文和已有素材/);
});
