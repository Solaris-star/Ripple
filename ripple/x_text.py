"""X 标准帖计数，与前端 twitter-text 使用同一 v3 规则。"""
import json
from pathlib import Path
import re
import unicodedata

from ._twitter_text.extract_urls import extract_urls_with_indices
from ._twitter_text.has_invalid_characters import has_invalid_characters
from .publishing import WorkflowError

_DATA = json.loads((Path(__file__).parent / 'data/x_emoji_sequences.json').read_text(encoding='utf-8'))
_EMOJI = re.compile('|'.join(re.escape(value) for value in _DATA['sequences']))


def _plain_weight(text: str) -> int:
    # 仅转换计数副本；文本形式的版权与注册符号按 v3 权重为 1。
    text = _EMOJI.sub(lambda match: 'a' if match.group() in {'©', '®'} else '😀', text)
    return sum(1 if any(start <= ord(char) <= end for start, end in _DATA['single_weight_ranges']) else 2 for char in text)


def count_reply(text: str) -> dict:
    normalized = unicodedata.normalize('NFC', text.strip()).replace('\r\n', '\n').replace('\r', '\n')
    weight, offset = 0, 0
    for url in extract_urls_with_indices(normalized):
        start, end = url['indices']
        weight += _plain_weight(normalized[offset:start]) + _DATA['url_weight']
        offset = end
    weight += _plain_weight(normalized[offset:])
    valid = (0 < weight <= _DATA['max_weighted_length'] and not has_invalid_characters(text)
             and not any(0xD800 <= ord(char) <= 0xDFFF for char in text))
    return {"weighted_length": weight, "valid": valid}


def validate_reply(text: str) -> dict:
    result = count_reply(text)
    if not result["valid"]:
        raise WorkflowError(f"X 回复须为有效纯文字，最多 280 加权字符；当前为 {result['weighted_length']}。", 422)
    return result
