import { test } from 'node:test';
import assert from 'node:assert/strict';
import { channelCapabilityText, channelPublishAvailable } from '../src/lib/channelCapability.ts';
import type { Channel } from '../src/lib/ripple.ts';

test('账号可连接但发布模块缺失时，禁止发布并显示原因', () => {
  const channel = { adapter_available: true, publish_available: false, connected: true, direct_publish: false } as Channel;
  assert.equal(channelPublishAvailable(channel), false);
  assert.equal(channelCapabilityText(channel), '可创作，发布尚未接入');
});

test('兼容旧服务的能力字段，并在没有能力信息时禁用发布', () => {
  assert.equal(channelPublishAvailable({ adapter_available: true } as Channel), true);
  assert.equal(channelPublishAvailable({ adapter_available: false } as Channel), false);
  assert.equal(channelPublishAvailable(undefined), false);
  assert.equal(channelCapabilityText(undefined), '能力待检查');
});
