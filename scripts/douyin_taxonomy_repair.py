"""Preview or quarantine one proven legacy taxonomy record, never clear a platform.

Use --apply only with Ripple stopped, an exact record ID and the preview SHA.
The byte-for-byte backup stays outside the public outputs directory.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import uuid

# IDs and names observed in the legacy tags/query response, not activity IDs.
TAXONOMY = {
    "100001": "综合", "314": "随拍", "300": "文化教育", "305": "时尚", "333": "美食", "302": "游戏",
    "337": "娱乐", "308": "剧情", "306": "才艺", "296": "二次元", "298": "体育", "335": "汽车",
    "315": "亲子", "718": "三农", "307": "动植物", "334": "旅行", "336": "科技", "311": "明星",
    "669": "财经", "299": "泛生活", "10": "政务媒体", "301": "校园", "926": "公益", "320": "医疗健康",
    "400": "人文社科", "401": "科普", "402": "传统文化", "403": "艺术", "404": "法律", "405": "音乐",
    "406": "舞蹈", "407": "休闲娱乐", "408": "母婴", "409": "综艺", "411": "电视剧", "412": "家装",
    "413": "生活家居", "414": "生活记录", "415": "摄影摄像",
}


def proven_taxonomy(row: dict) -> bool:
    external = str((row.get("external_ids") or {}).get("douyin_creator_portal") or "")
    tag = external.removeprefix("douyin:")
    if (row.get("platform") != "douyin" or row.get("source_type") != "creator_activity_api"
            or row.get("source_url") != "https://creator.douyin.com/" or TAXONOMY.get(tag) != row.get("title")
            or row.get("saved") or row.get("user_confirmed_fields") or row.get("field_evidence")
            or row.get("douyin_listing") or row.get("updated_at") != row.get("last_seen_at")):
        return False
    if any(row.get(key) for key in ("summary", "note", "eligibility", "content_requirements", "prizes", "reward_summary",
                                   "winning_conditions", "reward_rules", "required_topics", "starts_at", "submit_deadline")):
        return False
    history = row.get("rule_history") or []
    return all(isinstance(item, dict) and item.get("title") in TAXONOMY.values() for item in history)


def inspect(path: Path, campaign_id: str, references: Path) -> tuple[bytes, list[dict], dict]:
    raw = path.read_bytes()
    data = json.loads(raw)
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise ValueError("活动文件结构异常，未更改数据。")
    matches = [row for row in data if row.get("id") == campaign_id]
    if len(matches) != 1 or not proven_taxonomy(matches[0]):
        raise ValueError("目标记录未满足分类污染的全部校验，未更改数据。")
    refs = []
    for file in references.rglob("*.json"):
        if file.resolve() == path.resolve():
            continue
        if file.is_symlink() or file.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("存在无法安全检查的业务引用文件，未更改数据。")
        if campaign_id.encode("utf-8") in file.read_bytes():
            refs.append(str(file.relative_to(references)))
    if refs:
        raise ValueError("目标记录存在业务引用，需单独确认后修复：" + ", ".join(refs[:10]))
    return raw, data, {"id": campaign_id, "sha256": hashlib.sha256(raw).hexdigest(),
                       "quarantine_count": 1, "remaining_count": len(data) - 1, "references": 0}


def quarantine(path: Path, campaign_id: str, expected_sha: str, references: Path, backups: Path) -> dict:
    raw, data, report = inspect(path, campaign_id, references)
    if report["sha256"] != expected_sha:
        raise ValueError("活动文件已变化，需重新预览；未更改数据。")
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / ("campaigns-before-taxonomy-" + expected_sha + ".json")
    if backup.exists():
        if backup.read_bytes() != raw:
            raise ValueError("备份内容不匹配，未更改数据。")
    else:
        with backup.open("xb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    if path.read_bytes() != raw:
        raise ValueError("备份期间活动文件发生变化，未更改数据。")
    temp = path.with_name(uuid.uuid4().hex + ".json.tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            json.dump([row for row in data if row.get("id") != campaign_id], stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        if path.read_bytes() != raw:
            raise ValueError("活动文件发生并发变化，未更改数据。")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    return {**report, "applied": True, "backup": str(backup)}


def main():
    args = argparse.ArgumentParser(description=__doc__)
    args.add_argument("--campaign-id", required=True)
    args.add_argument("--expected-sha", default="")
    args.add_argument("--apply", action="store_true")
    options = args.parse_args()
    if not re.fullmatch(r"[a-f0-9]{12}", options.campaign_id):
        raise SystemExit("Invalid record ID")
    root = Path(__file__).resolve().parents[1]
    path, refs = root / "outputs/_campaigns.json", root / "outputs"
    if options.apply:
        with socket.socket() as probe:
            probe.settimeout(.5)
            if probe.connect_ex(("127.0.0.1", 7860)) == 0:
                raise SystemExit("请先停止 Ripple 再应用修复，未修改数据。")
        result = quarantine(path, options.campaign_id, options.expected_sha, refs,
                            root / ".ripple-private/outputs/recovery")
    else:
        _, _, result = inspect(path, options.campaign_id, refs)
        result["applied"] = False
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
