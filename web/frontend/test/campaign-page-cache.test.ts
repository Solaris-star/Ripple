import { test } from 'node:test';
import assert from 'node:assert/strict';
import { CampaignPageCache } from '../src/lib/campaignPageCache.ts';

test('平台、账号、排序、页码、筛选和关键词各自隔离缓存', () => {
  const cache = new CampaignPageCache<string>();
  const base = ['xiaohongshu', 'account-a', 'default', 1, 'all', 'all', 'all', 'all', '科技'];
  const key = JSON.stringify(base);
  cache.set(key, '原视图', 0, 100);
  for (let i = 0; i < base.length; i++) {
    const other = [...base]; other[i] = i === 3 ? 2 : 'changed';
    assert.equal(cache.get(JSON.stringify(other), 200), undefined);
  }
  assert.equal(cache.get(key, 200), '原视图');
});

test('60 秒内复用列表，过期后必须重新读取', () => {
  const cache = new CampaignPageCache<number>();
  cache.set('x', 1, 0, 100);
  assert.equal(cache.get('x', 60_099), 1);
  assert.equal(cache.get('x', 60_100), undefined);
});

test('空搜索结果也是有效缓存，不反复请求', () => {
  const cache = new CampaignPageCache<unknown[]>();
  cache.set('没有匹配的关键词', [], 0, 0);
  assert.deepEqual(cache.get('没有匹配的关键词', 1), []);
});

test('刷新、收藏和编辑后作废缓存', () => {
  const cache = new CampaignPageCache<number>();
  cache.set('a', 1); cache.set('b', 2);
  cache.clear();
  assert.equal(cache.size, 0);
  assert.equal(cache.get('a'), undefined);
  assert.equal(cache.generation, 1);
});

test('变更前发出的迟到响应不能恢复旧缓存', () => {
  const cache = new CampaignPageCache<string>();
  const generation = cache.generation;
  cache.clear();
  assert.equal(cache.set('a', '过时响应', generation), false);
  assert.equal(cache.get('a'), undefined);
  assert.equal(cache.set('a', '新响应', cache.generation), true);
  assert.equal(cache.get('a'), '新响应');
});

test('缓存有容量上限，更新同一视图不会增加容量', () => {
  const cache = new CampaignPageCache<number>(60_000, 2);
  cache.set('a', 1); cache.set('b', 2); cache.set('a', 3); cache.set('c', 4);
  assert.equal(cache.size, 2);
  assert.equal(cache.get('b'), undefined);
  assert.equal(cache.get('a'), 3);
  assert.equal(cache.get('c'), 4);
});
