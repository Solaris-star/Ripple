import type { Idea, IdeaBrief, TopicUseContext } from './api';
import type { Account, Channel, XhsMetrics } from './ripple';
import type { SessionWorkScope } from './store';

export function ideaContentContext(idea: Idea, brief: IdeaBrief | null, contentId: string, created: boolean): TopicUseContext {
  return {
    title: idea.title, ideaId: idea.id, contentId, brief: brief?.data,
    // 已有稿只继续编辑，避免重复打开策划时再次调用模型。
    autoStart: created,
    angle: idea.angle, reason: idea.reason, campaignId: idea.campaign_id,
    campaignRuleVersion: idea.campaign_rule_version, trendRefs: idea.trend_refs,
    targetPlatforms: idea.target_platforms, requirements: idea.requirements,
    pendingChecks: idea.pending_checks, source: idea.source,
  };
}

export function preferredVariantTargets(
  channels: Pick<Channel, 'id'>[], accounts: Pick<Account, 'id' | 'platform'>[],
  workScope?: SessionWorkScope, targetPlatforms: string[] = [],
): string[] {
  const supported = new Set(channels.map((channel) => channel.id));
  if (workScope && supported.has(workScope.platform)) {
    const account = workScope.targetKind === 'account'
      ? accounts.find((item) => item.id === workScope.accountId && item.platform === workScope.platform)
      : undefined;
    return [`${workScope.platform}|${account?.id || ''}`];
  }
  return [...new Set(targetPlatforms)].filter((platform) => supported.has(platform)).map((platform) => `${platform}|`);
}

const METRICS = [['赞', 'likes'], ['藏', 'collects'], ['评', 'comments'], ['浏览', 'views']] as const;

export function sampleMetricSummary(metrics: XhsMetrics): { text: string; known: number; total: number } {
  let known = 0;
  const text = METRICS.map(([label, key]) => {
    const value = metrics[key];
    if (typeof value !== 'number' || !Number.isFinite(value) || value < 0) return `${label} 未读取`;
    known += 1;
    return `${label} ${value.toLocaleString()}`;
  }).join(' · ');
  return { text, known, total: METRICS.length };
}
