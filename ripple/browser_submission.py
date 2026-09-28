"""浏览器最终提交的持久化边界。"""
import json
import os
from pathlib import Path


def mark_submission(path: str, record: dict) -> None:
    if not path:
        raise ValueError('missing_submission_path')
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # 独占创建可阻止同一操作因 Worker 重启而再次点击最终提交。
    with target.open('x', encoding='utf-8') as stream:
        json.dump(record, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
