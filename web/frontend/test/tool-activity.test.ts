import assert from 'node:assert/strict';
import test from 'node:test';
import { finishToolActivity } from '../src/lib/toolActivity.ts';

test('已结束工具只留下最终状态', () => {
  assert.equal(finishToolActivity('调用 image · pending\n调用 read · completed\n调用 image · error'), '调用 read · completed\n调用 image · error');
});
test('中断工具不会继续显示等待中，也不伪造成功', () => {
  assert.equal(finishToolActivity('调用 image · running'), '调用 image · 已结束，未收到结果');
  assert.equal(finishToolActivity('生成开始\n调用 read · completed'), '生成开始\n调用 read · completed');
});
