"""从固定 Unicode 版本重建共享 Emoji 计数表，不访问社交账号。"""
import hashlib
import json
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
URL = 'https://www.unicode.org/Public/17.0.0/emoji/emoji-test.txt'


def main():
    with httpx.Client(timeout=45, follow_redirects=True) as client:
        response = client.get(URL)
        response.raise_for_status()
        raw = response.content
        text = raw.decode('utf-8')
        if '# Version: 17.0' not in text:
            raise ValueError('Unicode 数据版本不符，未写入。')
        sequences = []
        for line in text.splitlines():
            if not line or line.startswith('#'):
                continue
            codes = line.split(';', 1)[0]
            sequences.append(''.join(chr(int(code, 16)) for code in codes.split()))
        data = {'unicode_version': '17.0', 'source': URL, 'sha256': hashlib.sha256(raw).hexdigest(),
                'sequences': sorted(set(sequences), key=lambda value: (-len(value), value)),
                'single_weight_ranges': [[0, 4351], [8192, 8205], [8208, 8223], [8242, 8247]],
                'url_weight': 23, 'max_weighted_length': 280}
        path = ROOT / 'ripple/data/x_emoji_sequences.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
        license_response = client.get('https://www.unicode.org/license.txt')
        license_response.raise_for_status()
        (ROOT / 'LICENSES/Unicode-3.0.txt').write_text(license_response.text, encoding='utf-8')
        print(f"已生成 {len(data['sequences'])} 条 Emoji 记录。")


if __name__ == '__main__':
    main()
