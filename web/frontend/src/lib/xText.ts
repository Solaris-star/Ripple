import twitterText from 'twitter-text';
import emojiData from '../../../../ripple/data/x_emoji_sequences.json' with { type: 'json' };

const emojiPattern = new RegExp(emojiData.sequences.map(text => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'gu');
const normalizeEmoji = (text: string) => text.replace(emojiPattern, match => match === '©' || match === '®' ? 'a' : '😀');
const plainWeight = (text: string) => Array.from(normalizeEmoji(text)).reduce((sum, char) => {
  const code = char.codePointAt(0)!;
  return sum + (emojiData.single_weight_ranges.some(([start, end]) => code >= start && code <= end) ? 1 : 2);
}, 0);

export function countXReply(text: string): { weightedLength: number; valid: boolean } {
  const normalized = text.trim().normalize('NFC').replace(/\r\n?/g, '\n');
  let offset = 0, weightedLength = 0;
  for (const url of twitterText.extractUrlsWithIndices(normalized)) {
    weightedLength += plainWeight(normalized.slice(offset, url.indices[0])) + emojiData.url_weight;
    offset = url.indices[1];
  }
  weightedLength += plainWeight(normalized.slice(offset));
  const valid = weightedLength > 0 && weightedLength <= emojiData.max_weighted_length && !twitterText.hasInvalidCharacters(text)
    && !Array.from(text).some(char => char.codePointAt(0)! >= 0xD800 && char.codePointAt(0)! <= 0xDFFF);
  return { weightedLength, valid };
}
