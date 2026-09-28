import type { Channel } from './ripple';

export function channelPublishAvailable(channel?: Channel): boolean {
  return !!channel && (channel.publish_available ?? channel.adapter_available);
}

export function channelCapabilityText(channel?: Channel): string {
  if (!channel) return '能力待检查';
  if (channel.publish_available === false) return '可创作，发布尚未接入';
  if (channel.direct_publish) return '可发布';
  if (!channel.adapter_available) return channel.local_export ? '仅创作 / 导出' : '仅创作，发布尚未接入';
  if (channel.local_export) return '可导出，发布需连接目标';
  return '可创作，发布需连接账号并检查权限';
}
