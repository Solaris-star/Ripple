import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { countXReply } from '../src/lib/xText.ts';
import { replyKey, mergeReplySuggestions, toggleReplySelection } from '../src/lib/replyEditing.ts';
import { syncMotherFields } from '../src/components/workspace/variantPublishing.ts';
import type { VariantContent } from '../src/lib/ripple.ts';

test('选择最多 20 条，取消后可再次选择', () => {
  let selected = new Set<string>();
  for (let i = 0; i < 21; i++) selected = toggleReplySelection(selected, String(i));
  assert.equal(selected.size, 20);
  assert.equal(selected.has('20'), false);
  selected = toggleReplySelection(selected, '1');
  assert.equal(toggleReplySelection(selected, '20').size, 20);
});

test('AI 建议不覆盖等待期间的手工编辑，账号与作品状态隔离', () => {
  const first = replyKey('source-a', '101'), second = replyKey('source-b', '101');
  const merged = mergeReplySuggestions({ [first]: '手工稿', [second]: '另一账号的稿' }, { [first]: 2 }, { [first]: 1 }, { [first]: 'AI 旧建议', [replyKey('source-a', '102')]: 'AI 建议' });
  assert.equal(merged[first], '手工稿');
  assert.equal(merged[second], '另一账号的稿');
  assert.equal(merged[replyKey('source-a', '102')], 'AI 建议');
});

test('只同步素材保持平台正文、标题、话题及发布配置', () => {
  const current = { title: '平台标题', body: '平台改写', tags: '平台话题', media: ['old.png'], target_id: 'account-a', options: { keep: true } } as unknown as VariantContent;
  const mother = { title: '新标题', body: '新正文', tags: '新话题', media: ['new.png'] };
  assert.deepEqual(syncMotherFields(current, mother, []), current);
  assert.deepEqual(syncMotherFields(current, mother, ['media']), { ...current, media: ['new.png'] });
  assert.deepEqual(current.media, ['old.png']);
});

const cases = JSON.parse(readFileSync(new URL('../../../tests/fixtures/x_text_cases.json', import.meta.url), 'utf8')) as { text: string; repeat?: number; suffix?: string; length: number; valid: boolean }[];
for (const [index, entry] of cases.entries()) test(`X 共享字符计数样例 ${index + 1}`, () => {
  const result = countXReply(entry.text.repeat(entry.repeat || 1) + (entry.suffix || ''));
  assert.equal(result.weightedLength, entry.length);
  assert.equal(result.valid, entry.valid);
});

test('共享表中所有 Emoji 与紧邻 URL 的计数一致', () => {
  const data = JSON.parse(readFileSync(new URL('../../../ripple/data/x_emoji_sequences.json', import.meta.url), 'utf8')) as { sequences: string[] };
  for (const emoji of data.sequences) {
    const expected = emoji === '©' || emoji === '®' ? 1 : 2;
    assert.equal(countXReply(emoji).weightedLength, expected, emoji);
    assert.equal(countXReply(emoji + 'https://example.com').weightedLength, expected + 23, emoji);
  }
  assert.deepEqual(countXReply('🤏🏻'.repeat(140)), { weightedLength: 280, valid: true });
  assert.deepEqual(countXReply('🤏🏻'.repeat(141)), { weightedLength: 282, valid: false });
  assert.deepEqual(countXReply('https://example.com/' + 'a'.repeat(1500)), { weightedLength: 23, valid: true });
  assert.deepEqual(countXReply('a\r\nb'), { weightedLength: 3, valid: true });
});
