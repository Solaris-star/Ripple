"""Deterministic helpers for Bilibili campaign enrichment."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
import time
from typing import Any

from .publishing import WorkflowError

FORMAT_VALUES = {
    "video", "short_video", "long_video", "image_text", "text",
    "image", "live", "audio", "any",
}
MAX_FIELD_ITEMS = 30


def _strings(values: Any, *, limit: int = MAX_FIELD_ITEMS, width: int = 500) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for value in values[: limit * 3]:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text[:width])
        if len(result) >= limit:
            break
    return result


def empty_submission_spec() -> dict[str, Any]:
    return {
        "formats": [],
        "content_directions": [],
        "style_requirements": [],
        "duration_seconds": {"min": None, "max": None},
        "aspect_ratios": [],
        "resolutions": [],
        "orientation": None,
        "image_count": {"min": None, "max": None},
        "text_length": {"min": None, "max": None},
        "live": {"min_duration_seconds": None, "required_category": "", "title_keywords": []},
        "original_required": None,
        "first_publish_required": None,
        "exclusive_required": None,
        "min_entries": None,
        "max_entries": None,
        "submission_method": "",
        "required_mentions": [],
        "required_music": [],
    }


def _integer(value: Any, maximum: int) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= maximum else None


def normalize_submission_spec(value: Any) -> dict[str, Any]:
    base = empty_submission_spec()
    if not isinstance(value, dict):
        return base
    base["formats"] = [
        key for key in dict.fromkeys(str(x or "").strip() for x in value.get("formats", []))
        if key in FORMAT_VALUES
    ][:8]
    base["content_directions"] = _strings(value.get("content_directions"), limit=16, width=180)
    base["style_requirements"] = _strings(value.get("style_requirements"), limit=16, width=180)
    duration = value.get("duration_seconds") if isinstance(value.get("duration_seconds"), dict) else {}
    dmin, dmax = _integer(duration.get("min"), 86400), _integer(duration.get("max"), 86400)
    if dmin is not None and dmax is not None and dmin > dmax:
        dmin, dmax = dmax, dmin
    base["duration_seconds"] = {"min": dmin, "max": dmax}
    base["aspect_ratios"] = _strings(value.get("aspect_ratios"), limit=8, width=24)
    base["resolutions"] = _strings(value.get("resolutions"), limit=8, width=40)
    orientation = str(value.get("orientation") or "").strip().lower()
    base["orientation"] = orientation if orientation in {"vertical", "horizontal", "square"} else None
    images = value.get("image_count") if isinstance(value.get("image_count"), dict) else {}
    base["image_count"] = {"min": _integer(images.get("min"), 100), "max": _integer(images.get("max"), 100)}
    text = value.get("text_length") if isinstance(value.get("text_length"), dict) else {}
    base["text_length"] = {"min": _integer(text.get("min"), 1000000), "max": _integer(text.get("max"), 1000000)}
    live = value.get("live") if isinstance(value.get("live"), dict) else {}
    base["live"] = {
        "min_duration_seconds": _integer(live.get("min_duration_seconds"), 86400),
        "required_category": str(live.get("required_category") or "").strip()[:120],
        "title_keywords": _strings(live.get("title_keywords"), limit=12, width=80),
    }
    for key in ("original_required", "first_publish_required", "exclusive_required"):
        base[key] = value.get(key) if isinstance(value.get(key), bool) else None
    base["min_entries"] = _integer(value.get("min_entries"), 1000)
    base["max_entries"] = _integer(value.get("max_entries"), 1000)
    base["submission_method"] = str(value.get("submission_method") or "").strip()[:300]
    base["required_mentions"] = _strings(value.get("required_mentions"), limit=12, width=100)
    base["required_music"] = _strings(value.get("required_music"), limit=12, width=160)
    return base


def submission_spec_has_data(value: Any) -> bool:
    spec = normalize_submission_spec(value)
    return bool(
        spec["formats"] or spec["content_directions"] or spec["style_requirements"]
        or spec["duration_seconds"]["min"] is not None or spec["duration_seconds"]["max"] is not None
        or spec["aspect_ratios"] or spec["resolutions"] or spec["orientation"]
        or spec["image_count"]["min"] is not None or spec["image_count"]["max"] is not None
        or spec["text_length"]["min"] is not None or spec["text_length"]["max"] is not None
        or spec["live"]["min_duration_seconds"] is not None or spec["live"]["required_category"]
        or spec["live"]["title_keywords"] or spec["original_required"] is not None
        or spec["first_publish_required"] is not None or spec["exclusive_required"] is not None
        or spec["min_entries"] is not None or spec["max_entries"] is not None
        or spec["submission_method"] or spec["required_mentions"] or spec["required_music"]
    )


def campaign_missing_fields(campaign: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    public_wechat = str(campaign.get("source_type") or "") == "wechat_public_official"
    douyin_detail = (campaign.get("platform") == "douyin"
                     and str((campaign.get("douyin_listing") or {}).get("detail_status") or "") == "parsed")
    if douyin_detail:
        activity_type = str(campaign.get("activity_type") or "")
        if activity_type == "创作投稿" and not submission_spec_has_data(campaign.get("submission_spec")):
            missing.append("submission_spec")
        if not campaign.get("prizes") and not str(campaign.get("reward_summary") or "").strip():
            missing.append("prizes")
        if activity_type == "创作投稿" and not campaign.get("winning_conditions"):
            missing.append("winning_conditions")
        return missing
    if not campaign.get("eligibility"):
        missing.append("eligibility")
    if public_wechat:
        if (
            campaign.get("platform") == "weixin-channels"
            and "创作分成" in str(campaign.get("title") or "")
            and not submission_spec_has_data(campaign.get("submission_spec"))
        ):
            missing.append("submission_spec")
        return missing
    if not submission_spec_has_data(campaign.get("submission_spec")):
        missing.append("submission_spec")
    if not campaign.get("prizes") and not str(campaign.get("reward_summary") or "").strip():
        missing.append("prizes")
    if not campaign.get("winning_conditions"):
        missing.append("winning_conditions")
    return missing


def evidence_fingerprint(value: Any) -> str:
    try:
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = str(value or "")
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def should_agent_enrich(campaign: dict[str, Any], *, force: bool = False, now: int | None = None) -> tuple[bool, str]:
    if campaign.get("platform") != "bilibili":
        return False, "unsupported_platform"
    if campaign.get("source_type") == "user_import" and not force:
        return False, "manual_import"
    if not campaign_missing_fields(campaign):
        return False, "complete"
    fingerprint = str(campaign.get("rule_evidence_fingerprint") or "")
    if not fingerprint:
        return False, "no_evidence"
    if force:
        return True, "forced"
    if str(campaign.get("last_agent_fingerprint") or "") == fingerprint:
        return False, "already_analyzed"
    current = int(time.time()) if now is None else int(now)
    if str(campaign.get("agent_attempt_fingerprint") or "") == fingerprint:
        attempts = int(campaign.get("agent_attempt_count") or 0)
        if attempts >= 2:
            return False, "retry_exhausted"
        if current < int(campaign.get("agent_next_retry_at") or 0):
            return False, "retry_backoff"
    return True, "new_evidence"


def filter_draft_by_evidence(draft: dict[str, Any], evidence_text: str) -> dict[str, Any]:
    """Keep Agent fields only when at least one cited excerpt exists in fetched evidence."""
    result = deepcopy(draft)
    normalized_source = re.sub(r"\s+", " ", str(evidence_text or "")).casefold()
    evidence = draft.get("field_evidence") if isinstance(draft.get("field_evidence"), dict) else {}
    kept: dict[str, list[str]] = {}
    for key, excerpts in evidence.items():
        valid = []
        for excerpt in _strings(excerpts, limit=8, width=300):
            needle = re.sub(r"\s+", " ", excerpt).casefold()
            if needle and needle in normalized_source:
                valid.append(excerpt)
        if valid:
            kept[key] = valid
    result["field_evidence"] = kept
    for key in ("summary", "starts_at", "signup_deadline", "submit_deadline", "stats_deadline", "eligibility", "content_requirements", "prizes", "winning_conditions", "reward_rules", "required_topics", "submission_spec"):
        if key not in kept:
            result[key] = empty_submission_spec() if key == "submission_spec" else ([] if key in {"eligibility", "content_requirements", "prizes", "winning_conditions", "reward_rules", "required_topics"} else "")
    return result


def apply_agent_draft(campaign: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(campaign)
    locked = {str(x) for x in result.get("user_confirmed_fields", []) if str(x)}
    summary = str(draft.get("summary") or "").strip()
    if summary and not str(result.get("summary") or "").strip() and "summary" not in locked:
        result["summary"] = summary[:1600]
    for key in ("starts_at", "signup_deadline", "submit_deadline", "stats_deadline"):
        incoming_date = str(draft.get(key) or "").strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", incoming_date) and not str(result.get(key) or "").strip() and key not in locked:
            result[key] = incoming_date
    for key in ("eligibility", "content_requirements", "prizes", "winning_conditions", "reward_rules", "required_topics"):
        incoming = _strings(draft.get(key))
        current = result.get(key) if isinstance(result.get(key), list) else []
        if incoming and not current and key not in locked:
            result[key] = incoming
    if "submission_spec" not in locked:
        current_spec = normalize_submission_spec(result.get("submission_spec"))
        incoming_spec = normalize_submission_spec(draft.get("submission_spec"))
        if not submission_spec_has_data(current_spec) and submission_spec_has_data(incoming_spec):
            result["submission_spec"] = incoming_spec
    existing_evidence = result.get("field_evidence") if isinstance(result.get("field_evidence"), dict) else {}
    agent_evidence = draft.get("field_evidence") if isinstance(draft.get("field_evidence"), dict) else {}
    for key, excerpts in agent_evidence.items():
        if excerpts and key not in existing_evidence:
            existing_evidence[key] = {"source": "agent_bilibili_page", "excerpts": deepcopy(excerpts)}
    result["field_evidence"] = existing_evidence
    return result


def infer_submission_spec(text: str) -> dict[str, Any]:
    """Extract only explicit format constraints; unknown stays unknown."""
    source = re.sub(r"\s+", " ", str(text or ""))[:24000]
    spec = empty_submission_spec()
    formats: list[str] = []
    if "图文" in source:
        formats.append("image_text")
    if "直播" in source:
        formats.append("live")
    if "短视频" in source:
        formats.append("short_video")
    elif "长视频" in source:
        formats.append("long_video")
    elif "视频" in source:
        formats.append("video")
    if any(token in source for token in ("文章", "文字稿", "文字内容")):
        formats.append("text")
    spec["formats"] = list(dict.fromkeys(formats))

    def seconds(number: str, unit: str) -> int:
        value = float(number)
        return int(value * 60) if unit.startswith("分") else int(value)

    interval = re.search(
        r"(\d+(?:\.\d+)?)\s*(秒|分钟|分)\s*(?:[-~～至到—]+)\s*(\d+(?:\.\d+)?)\s*(秒|分钟|分)",
        source,
    )
    if interval:
        spec["duration_seconds"] = {
            "min": seconds(interval.group(1), interval.group(2)),
            "max": seconds(interval.group(3), interval.group(4)),
        }
    else:
        minimum = re.search(r"(?:不少于|至少|≥|>=)\s*(\d+(?:\.\d+)?)\s*(秒|分钟|分)", source)
        maximum = re.search(r"(?:不超过|至多|≤|<=)\s*(\d+(?:\.\d+)?)\s*(秒|分钟|分)", source)
        spec["duration_seconds"] = {
            "min": seconds(minimum.group(1), minimum.group(2)) if minimum else None,
            "max": seconds(maximum.group(1), maximum.group(2)) if maximum else None,
        }

    ratios = re.findall(
        r"(?<!\d)(9\s*[:：]\s*16|16\s*[:：]\s*9|1\s*[:：]\s*1|3\s*[:：]\s*4|4\s*[:：]\s*3)(?!\d)",
        source,
    )
    spec["aspect_ratios"] = [re.sub(r"\s+", "", item).replace("：", ":") for item in dict.fromkeys(ratios)]
    if "竖屏" in source:
        spec["orientation"] = "vertical"
    elif "横屏" in source:
        spec["orientation"] = "horizontal"
    spec["resolutions"] = [token for token in ("4K", "2160P", "2K", "1440P", "1080P", "720P") if token.casefold() in source.casefold()]
    if "原创" in source:
        spec["original_required"] = True
    if "首发" in source:
        spec["first_publish_required"] = True
    if any(token in source for token in ("独家", "独占")):
        spec["exclusive_required"] = True
    minimum_entries = re.search(r"(?:至少|不少于)\s*(\d+)\s*(?:个|篇|条|件|稿)?\s*(?:作品|投稿|稿件)?", source)
    maximum_entries = re.search(r"(?:最多|不超过|至多)\s*(\d+)\s*(?:个|篇|条|件|稿)?\s*(?:作品|投稿|稿件)?", source)
    spec["min_entries"] = int(minimum_entries.group(1)) if minimum_entries else None
    spec["max_entries"] = int(maximum_entries.group(1)) if maximum_entries else None
    return normalize_submission_spec(spec)


def parse_agent_output(raw: str) -> dict[str, Any]:
    """Parse one strict JSON object and discard fields without cited evidence."""
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise WorkflowError("Agent 活动补全没有返回 JSON。", 502)
    try:
        value = json.loads(text[start:end + 1])
    except (ValueError, TypeError) as exc:
        raise WorkflowError("Agent 活动补全返回了无效 JSON。", 502) from exc
    if not isinstance(value, dict):
        raise WorkflowError("Agent 活动补全结果必须是对象。", 502)
    evidence = value.get("field_evidence") if isinstance(value.get("field_evidence"), dict) else {}
    cleaned: dict[str, list[str]] = {}
    allowed = {
        "summary", "starts_at", "signup_deadline", "submit_deadline", "stats_deadline",
        "eligibility", "content_requirements", "prizes", "winning_conditions", "reward_rules", "required_topics", "submission_spec",
    }
    for key, excerpts in evidence.items():
        if key in allowed:
            rows = _strings(excerpts, limit=8, width=300)
            if rows:
                cleaned[key] = rows
    return {
        "summary": str(value.get("summary") or "").strip()[:1600] if "summary" in cleaned else "",
        "starts_at": str(value.get("starts_at") or "").strip()[:10] if "starts_at" in cleaned and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value.get("starts_at") or "").strip()) else "",
        "signup_deadline": str(value.get("signup_deadline") or "").strip()[:10] if "signup_deadline" in cleaned and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value.get("signup_deadline") or "").strip()) else "",
        "submit_deadline": str(value.get("submit_deadline") or "").strip()[:10] if "submit_deadline" in cleaned and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value.get("submit_deadline") or "").strip()) else "",
        "stats_deadline": str(value.get("stats_deadline") or "").strip()[:10] if "stats_deadline" in cleaned and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value.get("stats_deadline") or "").strip()) else "",
        "eligibility": _strings(value.get("eligibility")) if "eligibility" in cleaned else [],
        "content_requirements": _strings(value.get("content_requirements")) if "content_requirements" in cleaned else [],
        "prizes": _strings(value.get("prizes")) if "prizes" in cleaned else [],
        "winning_conditions": _strings(value.get("winning_conditions")) if "winning_conditions" in cleaned else [],
        "reward_rules": _strings(value.get("reward_rules")) if "reward_rules" in cleaned else [],
        "required_topics": _strings(value.get("required_topics"), limit=20, width=120) if "required_topics" in cleaned else [],
        "submission_spec": normalize_submission_spec(value.get("submission_spec")) if "submission_spec" in cleaned else empty_submission_spec(),
        "field_evidence": cleaned,
    }
