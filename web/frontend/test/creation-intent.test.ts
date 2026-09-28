import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ideaContentContext, preferredVariantTargets, sampleMetricSummary } from '../src/lib/creationIntent.ts';
import type { Idea, IdeaBrief } from '../src/lib/api.ts';
import type { SessionWorkScope } from '../src/lib/store.ts';

const scope: SessionWorkScope = {
  targetKind: 'account', accountId: 'xhs-current', platform: 'xiaohongshu', accountLabel: '当前账号',
  profileId: 'profile-a', profileRevision: 2, profileName: '我的画像', bindingRevision: 1, overrides: {},
};
const channels = [{ id: 'xiaohongshu' }, { id: 'douyin' }, { id: 'blog' }];
const accounts = [{ id: 'xhs-other', platform: 'xiaohongshu' }, { id: 'xhs-current', platform: 'xiaohongshu' }];

test('新稿生成保留已确认策划、来源与约束，已有稿不重复自动生成', () => {
  const idea: Idea = {
    id: 'idea-a', title: '用手机记录通勤路线', note: '', source: '用户灵感', status: 'doing', created: 1,
    target_platforms: ['xiaohongshu'], campaign_id: 'campaign-a', campaign_rule_version: 3,
    requirements: ['只使用本人照片'], pending_checks: ['核实车票价格'], trend_refs: ['低碳出行'],
  };
  const brief: IdeaBrief = {
    idea_id: idea.id, revision: 4, status: 'confirmed', locked_fields: ['core_thesis'], source: 'user', created_at: '',
    data: { audience: '上班族', objective: '提供实用路线', core_thesis: '减少换乘', differentiation: '亲自体验',
      title_directions: ['我的通勤路线'], hook: '每天少换一次车', outline: [{ title: '路线', purpose: '说明方案', evidence_needed: ['地图'] }],
      platform_plans: [], evidence_checks: ['车票价格'], production_tasks: ['拍摄封面'], open_questions: [], source_refs: ['source-a'] },
  };
  const context = ideaContentContext(idea, brief, 'content-a', true);
  assert.equal(context.autoStart, true);
  assert.equal(context.contentId, 'content-a');
  assert.deepEqual(context.brief, brief.data);
  assert.deepEqual(context.targetPlatforms, ['xiaohongshu']);
  assert.deepEqual(context.requirements, ['只使用本人照片']);
  assert.deepEqual(context.pendingChecks, ['核实车票价格']);
  assert.equal(context.campaignRuleVersion, 3);
  assert.equal(ideaContentContext(idea, brief, 'content-a', false).autoStart, false);
});

test('版本优先当前账号，不默认选择同平台其他账号', () => {
  assert.deepEqual(preferredVariantTargets(channels, accounts, scope, ['douyin']), ['xiaohongshu|xhs-current']);
  assert.deepEqual(preferredVariantTargets(channels, accounts, { ...scope, accountId: 'missing' }), ['xiaohongshu|']);
});

test('无账号范围时只延续有效目标平台，Blog 使用平台规划', () => {
  assert.deepEqual(preferredVariantTargets(channels, accounts, undefined, ['douyin', 'douyin', 'unsupported']), ['douyin|']);
  assert.deepEqual(preferredVariantTargets(channels, accounts), []);
  assert.deepEqual(preferredVariantTargets(channels, accounts, { ...scope, targetKind: 'blog', platform: 'blog' }), ['blog|']);
});

test('指标的零值有效，缺失和无效数据均保持未知', () => {
  const partial = sampleMetricSummary({ likes: 0, collects: null, comments: Number.NaN, views: -1 });
  assert.equal(partial.known, 1);
  assert.equal(partial.total, 4);
  assert.equal(partial.text, '赞 0 · 藏 未读取 · 评 未读取 · 浏览 未读取');
  assert.equal(sampleMetricSummary({}).known, 0);
  assert.equal(sampleMetricSummary({ likes: 1, collects: 2, comments: 3, views: 4 }).known, 4);
});
