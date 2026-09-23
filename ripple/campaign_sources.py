"""Automatic campaign-source adapters for Ripple's activity center.

Every provider is read-only. Paid fallbacks are opt-in and never invoked merely
because a free/self-hosted source is missing.
"""
from __future__ import annotations

import base64
import html as html_lib
import hashlib
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlsplit
import uuid

import httpx

from filelock import FileLock, Timeout as FileLockTimeout

from .campaign_schedule import CampaignPaidSchedule, DAILY_TIMES, TIMEZONE
from .ai_providers import AIProviderService
from .campaign_enrichment import evidence_fingerprint, infer_submission_spec, normalize_submission_spec
from .publishing import WorkflowError
from .douyin_campaigns import detail_url as douyin_detail_url, display_date as douyin_display_date
from .secrets import protect
from .wechat_public_rules import can_parse_public_wechat_url, official_program_urls, preview_public_wechat_rule


SYNC_INTERVALS = {
    "bilibili": 30 * 60,
    "x": 0,  # Billed discovery uses daily slots, never a rolling interval.
    "xiaohongshu": 60 * 60,
    "douyin": 60 * 60,
    "wechat": 6 * 60 * 60,
    "weixin-channels": 6 * 60 * 60,
}
MAX_RESPONSE = 4 * 1024 * 1024
CREATOR_WORDS = ("创作", "创作者", "征稿", "投稿", "激励", "奖金", "UP主", "视频", "内容", "挑战")
X_QUERY = '("creator challenge" OR "creator rewards" OR "creator program" OR "creator incentive" OR "call for creators" OR "creator contest" OR "submissions open") -is:retweet'
BILI_API = "https://api.bilibili.com/x/activity/page/list"
TIKHUB_API = "https://api.tikhub.io"
BILI_DETAIL_TTL = 24 * 60 * 60
BILI_PRIORITY_DETAIL_TTL = 6 * 60 * 60
BILI_EVA_MARKER = re.compile(r"window\.__BILIACT_EVAPAGEDATA__\s*=\s*")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read(path: Path, limit: int = 1024 * 1024) -> dict:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _read_list(path: Path, limit: int = 4 * 1024 * 1024) -> list[dict[str, Any]]:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return []
        value = json.loads(path.read_text(encoding="utf-8"))
        return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []
    except (OSError, ValueError, TypeError):
        return []


def _safe_url(value: str, roots: tuple[str, ...]) -> str:
    raw = str(value or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return ""
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return ""
    if not any(host == root or host.endswith("." + root) for root in roots):
        return ""
    return raw[:2048]


def _date_from_unix(value: Any) -> str:
    try:
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        if stamp <= 0:
            return ""
        return datetime.fromtimestamp(stamp, tz=timezone.utc).date().isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return str(value or "")[:40] if value else ""


def _json_fragment(text: str) -> Any:
    raw = str(text or "").strip()
    starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    end = max(raw.rfind("}"), raw.rfind("]"))
    if end < start:
        return None
    try:
        return json.loads(raw[start:end + 1])
    except (ValueError, TypeError):
        return None


def _x_zh_text(value: Any, width: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    return text[:width] if re.search(r"[\u4e00-\u9fff]", text) else ""


def _x_zh_list(values: Any, *, limit: int = 30, width: int = 240) -> list[str]:
    if not isinstance(values, list):
        return []
    rows: list[str] = []
    for value in values:
        text = _x_zh_text(value, width)
        if text and text not in rows:
            rows.append(text)
        if len(rows) >= limit:
            break
    return rows


def _x_iso_date(value: Any) -> str:
    text = str(value or "").strip()
    return text if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text) else ""


def _x_submission_spec(value: Any) -> dict[str, Any]:
    spec = normalize_submission_spec(value)
    spec["content_directions"] = _x_zh_list(spec.get("content_directions"), limit=16, width=180)
    spec["style_requirements"] = _x_zh_list(spec.get("style_requirements"), limit=16, width=180)
    method = _x_zh_text(spec.get("submission_method"), 300)
    spec["submission_method"] = method
    live = spec.get("live") if isinstance(spec.get("live"), dict) else {}
    if live.get("required_category") and not re.search(r"[\u4e00-\u9fff]", str(live.get("required_category"))):
        live["required_category"] = ""
    spec["live"] = live
    return spec


def _generic_activity_rows(value: Any, limit: int = 60) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    title_keys = ("activity_name", "activityName", "event_name", "eventName", "task_name", "taskName",
                  "mission_name", "missionName", "title", "name")
    id_keys = ("activity_id", "activityId", "event_id", "eventId", "task_id", "taskId",
               "mission_id", "missionId", "id")
    start_keys = ("start_time", "startTime", "begin_time", "beginTime", "start_at", "startAt")
    end_keys = ("end_time", "endTime", "deadline", "expire_time", "expireTime", "submit_deadline")
    url_keys = ("jump_url", "jumpUrl", "url", "link", "h5_url", "h5Url", "pc_url")
    desc_keys = ("description", "desc", "summary", "sub_title", "subTitle")

    def walk(node: Any, depth: int = 0) -> None:
        if len(rows) >= limit or depth > 7:
            return
        if isinstance(node, list):
            for child in node[:300]:
                walk(child, depth + 1)
            return
        if not isinstance(node, dict):
            return
        title = next((str(node.get(k) or "").strip() for k in title_keys if str(node.get(k) or "").strip()), "")
        activityish = any(k in node for k in id_keys + start_keys + end_keys) or any(
            word in title for word in ("活动", "征稿", "激励", "创作", "任务", "招募", "挑战")
        )
        if title and activityish:
            external_id = next((str(node.get(k) or "").strip() for k in id_keys if str(node.get(k) or "").strip()), "")
            key = (external_id or title).casefold()
            if key not in seen:
                seen.add(key)
                rows.append({
                    "external_id": external_id[:160],
                    "title": title[:240],
                    "url": next((str(node.get(k) or "").strip() for k in url_keys if str(node.get(k) or "").strip()), "")[:2048],
                    "starts_at": next((str(node.get(k) or "").strip() for k in start_keys if str(node.get(k) or "").strip()), "")[:80],
                    "ends_at": next((str(node.get(k) or "").strip() for k in end_keys if str(node.get(k) or "").strip()), "")[:80],
                    "description": next((str(node.get(k) or "").strip() for k in desc_keys if str(node.get(k) or "").strip()), "")[:3000],
                })
        for child in node.values():
            if isinstance(child, (dict, list)):
                walk(child, depth + 1)
    walk(value)
    return rows[:limit]


def _unique_text(values: list[Any], limit: int = 20) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = re.sub(r"\s+", " ", str(value or "")).strip(" ，,；;")
        if not text:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(text[:600])
        if len(result) >= limit:
            break
    return result


def _bili_nonzero_prize(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if re.fullmatch(r"(?:瓜分|奖励)?\s*0(?:\.0+)?\s*元", text):
        return False
    return True


def _bili_condition_label(value: str) -> bool:
    text = str(value or "")
    if not text or any(token in text for token in ("筛选用", "发奖用")):
        return False
    return bool(re.search(r"(?:≥|≤|＜|＞|>=|<=|TOP|top|累计|单稿|播放|投币|点赞|评论|收藏|投稿|粉丝|时长|排名)", text))


def _bili_reward_summary(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""
    if not re.search(r"(奖励|奖金|奖池|奖品|瓜分|现金|流量扶持|创作金|激励金|福利|\d+(?:\.\d+)?\s*(?:万)?元)", text):
        return ""
    return text[:600]


def _bilibili_rule_fingerprint_text(lines: list[str]) -> str:
    signals = ("规则", "投稿", "作品", "奖励", "奖金", "奖池", "奖品", "参与", "报名", "要求", "条件", "门槛", "原创", "首发", "视频", "图文", "直播", "时长", "话题", "分区", "风格", "方向", "比例", "分辨率", "至少", "不超过", "≥", "≤")
    dynamic = re.compile(r"^(?:浏览|播放|点赞|评论|收藏|排名|热度|人气)(?:量|数)?\s*[:：]?\s*[\d.,万wW亿]+$")
    kept: list[str] = []
    for raw in lines:
        text = re.sub(r"\s+", " ", str(raw or "")).strip()
        if not text or dynamic.fullmatch(text):
            continue
        if any(token in text for token in signals):
            kept.append(text)
    return "\n".join(dict.fromkeys(kept))[:24000]


def _bilibili_fallback_evidence_text(html: str) -> str:
    raw = str(html or "")[:MAX_RESPONSE]
    pieces: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"[\"']([^\"'<>]{2,420}[\u4e00-\u9fff][^\"'<>]{0,180})[\"']", raw):
        text = html_lib.unescape(match.group(1)).replace("\\n", " ").replace("\\t", " ")
        text = re.sub(r"\\s+", " ", text).strip()
        if len(text) > 500 or text.startswith(("http://", "https://", "//")):
            continue
        if not any(word in text for word in ("活动", "投稿", "作品", "奖励", "奖金", "视频", "图文", "直播", "原创", "首发", "话题", "报名", "播放", "粉丝", "时长")):
            continue
        key = text.casefold()
        if key not in seen:
            seen.add(key); pieces.append(text)
        if len(pieces) >= 180:
            break
    return "\n".join(pieces)[:24000]


def _bilibili_detail_from_html(html: str) -> dict[str, Any]:
    """Extract only explicit, machine-readable rule data from Bilibili EVA pages.

    Image-only rule panels are intentionally ignored: Ripple must not infer text
    that is not present in the page's structured payload.
    """
    match = BILI_EVA_MARKER.search(str(html or ""))
    if not match:
        return {}
    try:
        data, _ = json.JSONDecoder().raw_decode(html[match.end():])
    except (ValueError, TypeError, json.JSONDecodeError):
        return {}

    nodes: list[dict[str, Any]] = []

    def walk(node: Any, depth: int = 0) -> None:
        if depth > 20:
            return
        if isinstance(node, dict):
            if isinstance(node.get("name"), str) and isinstance(node.get("props"), dict):
                nodes.append(node)
            for child in node.values():
                if isinstance(child, (dict, list)):
                    walk(child, depth + 1)
        elif isinstance(node, list):
            for child in node[:1000]:
                walk(child, depth + 1)

    walk(data)
    evidence_strings: list[str] = []
    evidence_seen: set[str] = set()

    def collect_strings(node: Any, depth: int = 0) -> None:
        if depth > 18 or len(evidence_strings) >= 240:
            return
        if isinstance(node, dict):
            for value in node.values():
                collect_strings(value, depth + 1)
        elif isinstance(node, list):
            for value in node[:500]:
                collect_strings(value, depth + 1)
        elif isinstance(node, str):
            text = re.sub(r"\s+", " ", node).strip()
            if not (2 <= len(text) <= 500):
                return
            if text.startswith(("http://", "https://", "//", "data:")) or re.fullmatch(r"[0-9A-Fa-f_-]{16,}", text):
                return
            if not (re.search(r"[\u4e00-\u9fff]", text) or re.search(r"(?:投稿|活动|奖励|奖金|视频|图文|直播|原创|首发|时长|话题|播放|粉丝|报名|作品)", text)):
                return
            key = text.casefold()
            if key not in evidence_seen:
                evidence_seen.add(key); evidence_strings.append(text)

    collect_strings(data)
    evidence_text = "\n".join(evidence_strings)[:24000]
    fingerprint_text = _bilibili_rule_fingerprint_text(evidence_strings)
    topics: list[str] = []
    eligibility: list[str] = []
    content_requirements: list[str] = []
    prizes: list[str] = []
    winning_conditions: list[str] = []
    reward_rules: list[str] = []
    image_only_rule_panels = 0

    def add_task_item(task: Any) -> None:
        if not isinstance(task, dict):
            return
        task_name = re.sub(r"\s+", " ", str(task.get("taskName") or "")).strip()
        checkpoints = task.get("checkpoints") if isinstance(task.get("checkpoints"), list) else []
        checkpoint_names = [re.sub(r"\s+", " ", str(row.get("alias") or "")).strip() for row in checkpoints if isinstance(row, dict) and str(row.get("alias") or "").strip()]
        combined = " ".join([task_name, *checkpoint_names])
        if not re.search(r"(投稿|视频|直播|图文|创作|作品|开播|发布|稿件)", combined):
            return
        if task_name:
            content_requirements.append(task_name)
        for checkpoint in checkpoint_names:
            if checkpoint and checkpoint != task_name:
                content_requirements.append(checkpoint)
        for text in [task_name, *checkpoint_names]:
            for topic in re.findall(r"#([^#\s]+)", text):
                topics.append(topic.strip("，。；;、"))
        award = re.sub(r"\s+", " ", str(task.get("awardName") or "")).strip()
        if award and award not in {"0", "无"}:
            prizes.append(award)
            condition = task_name or (checkpoint_names[-1] if checkpoint_names else "")
            if condition:
                winning_conditions.append(condition)
                reward_rules.append(f"{award}：{condition}")


    for node in nodes:
        name = str(node.get("name") or "")
        alias = str(node.get("alias") or "").strip()
        props = node.get("props") if isinstance(node.get("props"), dict) else {}

        if name == "EraVideoSourcePc":
            config = props.get("config") if isinstance(props.get("config"), dict) else {}
            topic = str(config.get("topic_name") or "").strip()
            if topic:
                topics.append(topic)
            pools = config.get("poolList") if isinstance(config.get("poolList"), list) else []
            for pool in pools[:100]:
                if not isinstance(pool, dict):
                    continue
                bonus = str(pool.get("bonus") or "").strip()
                label = str(pool.get("label") or "").strip()
                rule = str(pool.get("rule") or "").strip()
                internal_pool = any(token in label for token in ("筛选用", "发奖用"))
                if internal_pool:
                    continue
                placeholder_one_yuan = bool(
                    re.fullmatch(r"瓜分\s*1(?:\.0+)?\s*元", bonus)
                    and not rule and label in {"", "瓜分奖"}
                )
                if _bili_nonzero_prize(bonus) and not placeholder_one_yuan:
                    prizes.append(bonus)
                condition = rule or (label if _bili_condition_label(label) else "")
                condition = re.sub(r"[,，]\s*", "、", condition).strip()
                if condition:
                    winning_conditions.append(condition)
                    if _bili_nonzero_prize(bonus) and not placeholder_one_yuan:
                        reward_rules.append(f"{bonus}：{condition}")

        if name == "EraTasklistPc":
            tasklist = props.get("tasklist") if isinstance(props.get("tasklist"), list) else []
            for task in tasklist[:100]:
                add_task_item(task)

        if name == "EvaTaskButton":
            add_task_item(props.get("taskItem"))

        if name == "EvaText":
            text = re.sub(r"\s+", " ", str(props.get("content") or "")).strip()
            if text and re.search(r"(投稿|视频|直播|图文|创作|作品|原创|首发|时长|分区|话题)", text):
                content_requirements.append(text)

        if name == "EvaModal":
            modal = props.get("modalContentLayoutContainerProps") if isinstance(props.get("modalContentLayoutContainerProps"), dict) else {}
            background = modal.get("background") if isinstance(modal.get("background"), dict) else {}
            if str(background.get("src") or "").strip():
                image_only_rule_panels += 1

        if name == "EvaLinkButton":
            button_text = ""
            button_props = props.get("buttonProps") if isinstance(props.get("buttonProps"), dict) else {}
            for candidate in (alias, props.get("text"), props.get("content"), button_props.get("content")):
                if str(candidate or "").strip():
                    button_text = str(candidate).strip()
                    break
            if re.search(r"(报名|招募|申请)", button_text):
                eligibility.append(f"需通过活动页面完成{re.search(r'(报名|招募|申请)', button_text).group(1)}")

    required_topics = _unique_text(topics, 12)
    for topic in required_topics:
        content_requirements.append(f"投稿需关联活动话题：#{topic.lstrip('#')}")

    prizes = _unique_text(prizes, 12)
    winning_conditions = _unique_text(winning_conditions, 16)
    reward_rules = _unique_text(reward_rules, 16)
    eligibility = _unique_text(eligibility, 12)
    content_requirements = _unique_text(content_requirements, 16)

    summary_parts: list[str] = []
    if required_topics:
        summary_parts.append(f"围绕 #{required_topics[0].lstrip('#')} 参与投稿")
    if prizes:
        summary_parts.append(f"页面明确的奖励包括：{'、'.join(prizes[:3])}")
    if winning_conditions:
        summary_parts.append("奖励设置了明确的数据或创作门槛")
    return {
        "summary_hint": "；".join(summary_parts)[:1000],
        "eligibility": eligibility,
        "content_requirements": content_requirements,
        "prizes": prizes,
        "winning_conditions": winning_conditions,
        "reward_rules": reward_rules,
        "required_topics": required_topics,
        "submission_spec": infer_submission_spec(evidence_text),
        "_agent_evidence_text": evidence_text,
        "rule_evidence_fingerprint": evidence_fingerprint({"text": fingerprint_text, "topics": required_topics, "prizes": prizes, "winning_conditions": winning_conditions, "submission_spec": infer_submission_spec(fingerprint_text)}),
        "image_only_rule_panels": image_only_rule_panels,
    }


class CampaignSourceService:
    def __init__(self, workspace, ai_providers: AIProviderService):
        self.workspace = workspace
        self.ai = ai_providers
        self.path = workspace.private / "integrations" / "campaign-sources.json"
        self.secret_path = workspace.private / "integrations" / "campaign-source-tikhub.secret"
        self.bilibili_detail_cache_path = workspace.private / "integrations" / "campaign-bilibili-details.json"
        self._lock = threading.Lock()
        self.paid_schedule = CampaignPaidSchedule(workspace.private / "integrations" / "campaign-paid-slots.json")
        self._refresh_lock_path = workspace.private / "integrations" / "campaign-refresh.lock"

    def _empty(self) -> dict[str, Any]:
        return {
            "schema": 2,
            "revision": 0,
            "x": {"method": "", "x_api_account_id": "", "fallback_method": "", "fallback_enabled": False},
            "xiaohongshu": {"account_id": "", "official_snapshots": {}},
            "douyin": {"account_id": "", "tikhub_enabled": False},
            "last_sync": {},
            "updated_at": _now(),
        }

    def _state(self) -> dict[str, Any]:
        raw = _read(self.path)
        state = self._empty()
        if raw:
            for key in ("x", "xiaohongshu", "douyin", "last_sync"):
                if isinstance(raw.get(key), dict):
                    state[key].update(raw[key]) if key != "last_sync" else state.__setitem__("last_sync", raw[key])
            state["revision"] = max(0, int(raw.get("revision") or 0))
            state["updated_at"] = str(raw.get("updated_at") or state["updated_at"])
            legacy_xai = state["x"].get("method") == "xai" or state["x"].get("fallback_method") == "xai"
            if state["x"].get("method") == "xai":
                state["x"]["method"] = "prompt"
            if state["x"].get("fallback_method") == "xai":
                state["x"]["fallback_method"] = "prompt"
            if legacy_xai:
                state["last_sync"].pop("x", None)
        return state

    def _write(self, state: dict[str, Any]) -> None:
        state = deepcopy(state)
        state["schema"] = 2
        state["revision"] = int(state.get("revision") or 0) + 1
        state["updated_at"] = _now()
        _atomic(self.path, state)

    def _record_xhs_official_snapshots(self, account_id: str, payload: dict[str, Any]) -> None:
        account_id = str(account_id or '')[:32]
        if not account_id:
            return
        state = self._state()
        snapshots = state['xiaohongshu'].get('official_snapshots')
        if not isinstance(snapshots, dict):
            snapshots = {}
        account_snapshots = deepcopy(snapshots.get(account_id)) if isinstance(snapshots.get(account_id), dict) else {}
        orders = payload.get('orders') if isinstance(payload.get('orders'), dict) else {}
        attempted_at = int(time.time())
        fetched_at = str(payload.get('fetched_at') or _now())[:80]

        for sort_name in ('default', 'latest'):
            incoming = orders.get(sort_name) if isinstance(orders.get(sort_name), dict) else {}
            if incoming.get('observed') is True:
                ids: list[str] = []
                seen: set[str] = set()
                for raw in incoming.get('activity_ids', []) if isinstance(incoming.get('activity_ids'), list) else []:
                    activity_id = str(raw or '').strip()[:160]
                    if not activity_id:
                        continue
                    external_id = activity_id if activity_id.startswith('xhs:') else 'xhs:' + activity_id
                    if external_id not in seen:
                        seen.add(external_id)
                        ids.append(external_id)
                observed_at = int(incoming.get('observed_at') or attempted_at)
                unique_count = max(0, int(incoming.get('unique_count') or len(ids)))
                incoming_truncated = bool(incoming.get('truncated')) or unique_count > len(ids)
                previous = deepcopy(account_snapshots.get(sort_name)) if isinstance(account_snapshots.get(sort_name), dict) else {}
                if (
                    incoming_truncated
                    and previous.get('activity_ids')
                    and not bool(previous.get('truncated'))
                    and len(previous.get('activity_ids') or []) > len(ids)
                ):
                    previous.update({
                        'status': 'stale',
                        'last_attempt_at': attempted_at,
                        'error': '本次官方活动列表被调用方截断，已保留上次完整排序快照。',
                        'last_observed_source_count': unique_count,
                    })
                    account_snapshots[sort_name] = previous
                    continue
                digest_payload = json.dumps(
                    {
                        'account_id': account_id,
                        'sort': sort_name,
                        'ids': ids,
                        'query': incoming.get('query') if isinstance(incoming.get('query'), dict) else {},
                    },
                    ensure_ascii=False, separators=(',', ':'), sort_keys=True,
                )
                account_snapshots[sort_name] = {
                    'snapshot_id': hashlib.sha256(digest_payload.encode('utf-8')).hexdigest()[:24],
                    'status': 'fresh',
                    'activity_ids': ids,
                    'source_count': max(len(ids), unique_count),
                    'raw_count': max(0, int(incoming.get('raw_count') or 0)),
                    'unique_count': unique_count,
                    'truncated': incoming_truncated,
                    'query': deepcopy(incoming.get('query')) if isinstance(incoming.get('query'), dict) else {},
                    'observed_at': observed_at,
                    'fetched_at': fetched_at,
                    'last_attempt_at': attempted_at,
                    'error': '',
                }
            else:
                previous = deepcopy(account_snapshots.get(sort_name)) if isinstance(account_snapshots.get(sort_name), dict) else {}
                error = str(incoming.get('error') or f'{sort_name}_sort_not_observed')[:160]
                if previous.get('activity_ids'):
                    previous.update({'status': 'stale', 'last_attempt_at': attempted_at, 'error': error})
                    account_snapshots[sort_name] = previous
                else:
                    account_snapshots[sort_name] = {
                        'snapshot_id': '', 'status': 'missing', 'activity_ids': [],
                        'source_count': 0, 'raw_count': 0, 'unique_count': 0, 'truncated': False,
                        'query': deepcopy(incoming.get('query')) if isinstance(incoming.get('query'), dict) else {},
                        'observed_at': 0, 'fetched_at': '', 'last_attempt_at': attempted_at, 'error': error,
                    }

        account_snapshots['last_attempt_at'] = attempted_at
        snapshots[account_id] = account_snapshots
        # Keep a small bounded set of account snapshots. Account IDs are opaque and
        # no browser credentials or headers are persisted here.
        if len(snapshots) > 8:
            snapshots = dict(sorted(
                snapshots.items(),
                key=lambda pair: int((pair[1] or {}).get('last_attempt_at') or 0),
                reverse=True,
            )[:8])
        state['xiaohongshu']['official_snapshots'] = snapshots
        self._write(state)

    def xiaohongshu_official_snapshot(self, account_id: str = '', sort_name: str = 'default') -> dict[str, Any]:
        if sort_name not in {'default', 'latest'}:
            raise WorkflowError("小红书官方排序只支持默认排序或最新排序。", 422)
        state = self._state()
        selected = str(account_id or state['xiaohongshu'].get('account_id') or '')[:32]
        snapshots = state['xiaohongshu'].get('official_snapshots')
        snapshots = snapshots if isinstance(snapshots, dict) else {}
        if not selected:
            account, _ = self._effective_account('xiaohongshu', '')
            if account:
                selected = str(account.get('id') or '')[:32]
            elif len(snapshots) == 1:
                selected = str(next(iter(snapshots.keys())))[:32]
        account_snapshots = snapshots.get(selected) if selected and isinstance(snapshots.get(selected), dict) else {}
        row = deepcopy(account_snapshots.get(sort_name)) if isinstance(account_snapshots.get(sort_name), dict) else {}
        if not row:
            return {'account_id': selected, 'sort': sort_name, 'status': 'missing',
                    'activity_ids': [], 'source_count': 0, 'snapshot_id': '', 'fetched_at': '',
                    'truncated': False, 'error': '官方排序快照尚未同步。'}
        row['account_id'] = selected
        row['sort'] = sort_name
        return row

    def _save_tikhub_key(self, value: str) -> None:
        key = value.strip()
        if not key:
            return
        protected = base64.b64encode(protect(key.encode("utf-8"))).decode("ascii")
        _atomic(self.secret_path, {"protected": protected, "updated_at": _now()})

    def _tikhub_key(self) -> str:
        row = _read(self.secret_path, 64_000)
        try:
            blob = base64.b64decode(row["protected"], validate=True)
            return protect(blob, decrypt=True).decode("utf-8")
        except (KeyError, ValueError, UnicodeError, TypeError):
            return ""

    def configure(self, platform: str, value: dict[str, Any]) -> dict[str, Any]:
        state = self._state()
        if platform == "x":
            method = str(value.get("method") or "")
            method = "prompt" if method == "xai" else method
            if method not in {"", "prompt", "x_search", "x_api"}:
                raise WorkflowError("X 活动源只支持 Grok Prompt、原生 X Search Tool 或 X Developer API。", 422)
            fallback = str(value.get("fallback_method") or "")
            fallback = "prompt" if fallback == "xai" else fallback
            if fallback not in {"", "prompt", "x_search", "x_api"} or fallback == method:
                fallback = ""
            previous_method = str(state["x"].get("method") or "")
            state["x"].update({
                "method": method,
                "x_api_account_id": str(value.get("x_api_account_id") or "")[:32],
                "fallback_method": fallback,
                "fallback_enabled": bool(value.get("fallback_enabled") and fallback),
            })
            if method != previous_method:
                state["last_sync"].pop("x", None)
        elif platform == "xiaohongshu":
            state["xiaohongshu"]["account_id"] = str(value.get("account_id") or "")[:32]
        elif platform == "douyin":
            state["douyin"]["account_id"] = str(value.get("account_id") or "")[:32]
            if "tikhub_enabled" in value:
                state["douyin"]["tikhub_enabled"] = bool(value.get("tikhub_enabled"))
            if str(value.get("tikhub_api_key") or "").strip():
                self._save_tikhub_key(str(value["tikhub_api_key"]))
        else:
            raise WorkflowError("该平台当前没有可配置的自动活动源。", 422)
        self._write(state)
        return self.public_state()

    def _accounts(self, platform: str, *, adapter: str | None = None) -> list[dict[str, Any]]:
        rows = [a for a in self.workspace.accounts.list() if a.get("platform") == platform]
        if adapter is not None:
            rows = [a for a in rows if a.get("adapter") == adapter]
        return rows

    def _effective_account(self, platform: str, configured_id: str, *, adapter: str | None = None) -> tuple[dict[str, Any] | None, str]:
        rows = self._accounts(platform, adapter=adapter)
        if configured_id:
            row = next((a for a in rows if a.get("id") == configured_id), None)
            if not row:
                return None, "needs_config"
            return row, "ready" if row.get("status") == "connected" else "needs_login"
        connected = [a for a in rows if a.get("status") == "connected"]
        if len(connected) == 1:
            return connected[0], "ready"
        if len(connected) > 1:
            return None, "needs_config"
        return None, "needs_login" if rows else "needs_config"

    def _x_method_state(self, method: str, state: dict[str, Any]) -> tuple[str, str]:
        if method == "prompt":
            cfg = self.ai.resolved("x_campaign_discovery", fallback_to_default=False)
            if not cfg:
                return "needs_config", "请在 AI Provider 中配置“X 活动发现”路由。"
            return "ready", f"{cfg.get('provider_name')} / {cfg.get('model')} · Prompt"
        if method == "x_search":
            cfg = self.ai.resolved("x_campaign_discovery", fallback_to_default=False)
            if not cfg:
                return "needs_config", "请在 AI Provider 中配置“X 活动发现”路由。"
            evidence = ((cfg.get("capability_evidence") or {}).get("x_search") or {})
            if (cfg.get("capabilities") or {}).get("x_search") != "verified" or evidence.get("model_id") != cfg.get("model"):
                return "needs_config", "原生 X Search Tool 模式需要先执行严格 X Search 能力测试。"
            return "ready", f"{cfg.get('provider_name')} / {cfg.get('model')} · 原生 X Search Tool"
        if method == "x_api":
            account, status = self._effective_account("x", str(state["x"].get("x_api_account_id") or ""), adapter="x-api")
            return status, (account.get("label") if account else "请选择已连接的 X Developer API 账号")
        return "needs_config", "选择 Grok Prompt、原生 X Search Tool 或 X Developer API。"

    def sync_interval(self, platform: str) -> int:
        return int(SYNC_INTERVALS.get(platform, 60 * 60))

    def _paid_only(self, platform: str) -> bool:
        if platform == "x":
            return True
        if platform == "douyin":
            cfg = self._state()["douyin"]
            _, status = self._effective_account("douyin", str(cfg.get("account_id") or ""))
            return status != "ready" and bool(cfg.get("tikhub_enabled")) and bool(self._tikhub_key())
        return False

    def _paid_key(self, platform: str) -> str:
        return {"x": "x:discovery", "bilibili": "bilibili:rules", "douyin": "douyin:fallback"}.get(platform, "")

    def next_sync_at(self, platform: str) -> int:
        if self._paid_only(platform):
            return self.paid_schedule.next_at(self._paid_key(platform))
        row = self._state()["last_sync"].get(platform, {})
        at = int(row.get("last_attempt_at") or row.get("at") or 0)
        return at + self.sync_interval(platform) if at else 0

    def public_state(self) -> dict[str, Any]:
        state = self._state()
        x_method = str(state["x"].get("method") or "")
        x_status, x_detail = self._x_method_state(x_method, state)
        xhs_account, xhs_status = self._effective_account("xiaohongshu", str(state["xiaohongshu"].get("account_id") or ""))
        douyin_account, douyin_status = self._effective_account("douyin", str(state["douyin"].get("account_id") or ""))
        tikhub_set = bool(self._tikhub_key())
        tikhub_enabled = bool(state["douyin"].get("tikhub_enabled"))
        if douyin_status != "ready" and tikhub_enabled and tikhub_set:
            douyin_display_status = "ready_fallback"
        else:
            douyin_display_status = douyin_status
        rows = [
            {
                "id": "bilibili_public", "platform": "bilibili", "label": "B站",
                "status": "ready", "automatic": True, "mode": "built_in",
                "detail": "B站公开活动列表 · 零配置",
                "billing": "free", "last_sync": state["last_sync"].get("bilibili", {}),
            },
            {
                "id": "x_campaigns", "platform": "x", "label": "X",
                "status": x_status, "automatic": x_status == "ready", "mode": x_method or "unconfigured",
                "detail": x_detail,
                "billing": "paid_or_plan_dependent" if x_method in {"prompt", "x_search", "x_api"} else "unknown",
                "cost_note": "Prompt 模式按模型 Provider 计费；原生 X Search Tool 与 X Developer API 按对应服务商规则计费。",
                "method": x_method,
                "x_api_account_id": str(state["x"].get("x_api_account_id") or ""),
                "fallback_method": str(state["x"].get("fallback_method") or ""),
                "fallback_enabled": bool(state["x"].get("fallback_enabled")),
                "last_sync": state["last_sync"].get("x", {}),
            },
            {
                "id": "xiaohongshu_creator_events", "platform": "xiaohongshu", "label": "小红书",
                "status": xhs_status, "automatic": xhs_status == "ready", "mode": "creator_login",
                "detail": (f"{xhs_account.get('label')} · creator.xiaohongshu.com/new/events" if xhs_account else
                           "连接一个小红书创作者账号；多个已连接账号时需指定账号。"),
                "billing": "free", "account_id": xhs_account.get("id") if xhs_account else "",
                "last_sync": state["last_sync"].get("xiaohongshu", {}),
            },
            {
                "id": "douyin_creator_events", "platform": "douyin", "label": "抖音",
                "status": douyin_display_status, "automatic": douyin_display_status in {"ready", "ready_fallback"},
                "mode": "creator_login_with_optional_tikhub",
                "detail": (f"{douyin_account.get('label')} · 创作者后台优先" if douyin_account else
                           "连接抖音创作者账号；可显式启用 TikHub 收费 fallback。"),
                "billing": "free_primary_paid_fallback",
                "cost_note": "创作者后台读取不额外收费；TikHub 仅在显式启用后作为 fallback，按其当前 endpoint 计费。",
                "account_id": douyin_account.get("id") if douyin_account else "",
                "tikhub_enabled": tikhub_enabled, "tikhub_api_key_set": tikhub_set,
                "last_sync": state["last_sync"].get("douyin", {}),
                "scope_note": str(state["douyin"].get("scope_note") or "仅同步当前账号创作者中心活动日历；登录就绪不代表已完成活动采集。"),
            },
            {
                "id": "wechat_campaigns", "platform": "wechat", "label": "微信公众号",
                "status": "ready", "automatic": True, "mode": "official_public_rules",
                "detail": "微信营销官方公开规则 · 无需登录；账号是否开通仍需账户内核验。",
                "billing": "free", "last_sync": state["last_sync"].get("wechat", {}),
            },
            {
                "id": "weixin_channels_campaigns", "platform": "weixin-channels", "label": "微信视频号",
                "status": "ready", "automatic": True, "mode": "official_public_rules",
                "detail": "微信营销 / 视频号团队公开规则 · 无需登录；受邀与收益状态需账户内核验。",
                "billing": "free", "last_sync": state["last_sync"].get("weixin-channels", {}),
            },
        ]
        for row in rows:
            platform = str(row.get("platform") or "")
            paid_only = self._paid_only(platform)
            row["sync_interval_seconds"] = 0 if paid_only else self.sync_interval(platform)
            row["next_sync_at"] = self.next_sync_at(platform) if row.get("automatic") else 0
            row["schedule"] = {
                "mode": "daily_slots" if paid_only else "interval" if row.get("automatic") else "manual",
                "timezone": TIMEZONE, "times": list(DAILY_TIMES),
                "interval_seconds": row["sync_interval_seconds"],
                "next_run_at": row["next_sync_at"],
                "cost": "paid" if paid_only else "free_primary",
            }
            if platform == "bilibili":
                row["schedule"]["paid_note"] = "自动 Agent 规则补全仅在 09:00、14:00、20:00（北京时间）执行"
            if platform == "douyin" and tikhub_enabled:
                row["schedule"]["paid_note"] = "收费备用仅在 09:00、14:00、20:00（北京时间）自动尝试"
            row["last_sync"] = {**row.get("last_sync", {}), "next_run_at": row["next_sync_at"]}
        return {"items": rows, "automatic_count": sum(1 for row in rows if row["automatic"]),
                "revision": state["revision"], "server_now": int(time.time())}

    def _record_sync(self, platform: str, *, status: str, count: int = 0, error: str = "",
                     provider: str = "", fallback_used: bool = False) -> None:
        state = self._state(); previous = state["last_sync"].get(platform, {})
        now = int(time.time())
        success_at = now if status == "fresh" else int(previous.get("last_success_at") or (previous.get("at") if previous.get("status") == "fresh" else 0) or 0)
        success_count = int(count) if status == "fresh" else int(previous.get("last_success_count") or (previous.get("count") if previous.get("status") == "fresh" else 0) or 0)
        state["last_sync"][platform] = {
            "at": now, "last_attempt_at": now, "last_success_at": success_at,
            "last_success_count": success_count,
            "status": status, "count": int(count), "error": str(error or "")[:300],
            "provider": provider, "fallback_used": bool(fallback_used),
        }
        self._write(state)

    def _recent(self, platform: str) -> bool:
        row = self._state()["last_sync"].get(platform, {})
        # Successful and failed attempts both observe the platform interval.
        # Manual force refresh is the explicit bypass; the background scheduler
        # must never hammer a broken or paid provider every two seconds.
        if self._paid_only(platform):
            return not self.paid_schedule.available(self._paid_key(platform))
        if platform == "bilibili" and self.paid_schedule.available("bilibili:rules"):
            return False
        if platform == "douyin" and self._state()["douyin"].get("tikhub_enabled") and self.paid_schedule.available("douyin:fallback"):
            return False
        return bool(
            float(row.get("last_attempt_at") or row.get("at") or 0) > 0
            and time.time() - float(row.get("last_attempt_at") or row.get("at") or 0) < self.sync_interval(platform)
        )

    def due_platforms(self) -> list[str]:
        state = self.public_state()
        return [
            str(row["platform"]) for row in state["items"]
            if row.get("automatic") and not self._recent(str(row.get("platform") or ""))
        ]

    def _bilibili_priority_urls(self) -> set[str]:
        campaigns = _read_list(self.workspace.outputs / "_campaigns.json")
        ideas = _read_list(self.workspace.outputs / "_ideas.json")
        linked_ids = {
            str(row.get("campaign_id") or "") for row in ideas
            if str(row.get("campaign_id") or "")
        }
        urls: set[str] = set()
        for campaign in campaigns:
            if campaign.get("platform") != "bilibili":
                continue
            if not (campaign.get("saved") or str(campaign.get("id") or "") in linked_ids):
                continue
            url = _safe_url(str(campaign.get("source_url") or ""), ("bilibili.com", "www.bilibili.com"))
            if url:
                urls.add(url)
        return urls

    @staticmethod
    def _bilibili_deadline_soon(value: Any) -> bool:
        try:
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000
            delta = stamp - time.time()
            return 0 <= delta <= 7 * 24 * 60 * 60
        except (TypeError, ValueError, OSError, OverflowError):
            return False

    def _bilibili_detail(self, client: httpx.Client, url: str, cache: dict[str, Any], *,
                           force: bool = False, strict: bool = False,
                           ttl: int = BILI_DETAIL_TTL) -> dict[str, Any]:
        now = int(time.time())
        cached = cache.get(url) if isinstance(cache.get(url), dict) else {}
        if not force and cached and now - int(cached.get("fetched_at") or 0) < max(0, int(ttl)):
            detail = cached.get("detail")
            # Pre-enrichment cache rows did not carry a rule fingerprint. Re-fetch
            # them once so script-first Agent fallback can start immediately after upgrade.
            if isinstance(detail, dict) and "rule_evidence_fingerprint" in detail:
                return detail
        try:
            current = url
            response = None
            for _hop in range(4):
                response = client.get(current, headers={"Accept": "text/html,application/xhtml+xml"}, follow_redirects=False)
                if response.status_code not in {301, 302, 303, 307, 308}:
                    break
                location = str(response.headers.get("location") or "").strip()
                target = _safe_url(urljoin(current, location), ("bilibili.com", "www.bilibili.com"))
                if not target:
                    raise WorkflowError("B站活动页重定向到了非 B站地址，已拒绝继续读取。", 422)
                current = target
            else:
                raise WorkflowError("B站活动页重定向次数过多。", 502)
            if response is None:
                raise WorkflowError("B站活动详情暂时无法读取。", 502)
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE:
                if strict:
                    raise WorkflowError("B站活动详情响应超过安全上限。", 502)
                return {}
            detail = _bilibili_detail_from_html(response.text)
            if not isinstance(detail, dict):
                detail = {}
            if not str(detail.get("_agent_evidence_text") or "").strip():
                evidence_text = _bilibili_fallback_evidence_text(response.text)
                detail["_agent_evidence_text"] = evidence_text
                detail["submission_spec"] = infer_submission_spec(evidence_text)
                fp_text = _bilibili_rule_fingerprint_text(evidence_text.splitlines())
                detail["rule_evidence_fingerprint"] = evidence_fingerprint({"text": fp_text, "submission_spec": infer_submission_spec(fp_text)}) if fp_text else ""
            title_match = re.search(r"<title[^>]*>(.*?)</title>", response.text, flags=re.I | re.S)
            if title_match:
                detail["_page_title"] = re.sub(r"\s+", " ", html_lib.unescape(re.sub(r"<[^>]+>", " ", title_match.group(1)))).strip()[:240]
        except WorkflowError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            if strict:
                raise WorkflowError("B站活动详情暂时无法重新核验，请稍后重试。", 502) from None
            return {}
        cache[url] = {"fetched_at": now, "detail": detail}
        return detail


    def bilibili_page_evidence(self, url: str, *, force: bool = False) -> dict[str, Any]:
        safe = _safe_url(str(url or ""), ("bilibili.com", "www.bilibili.com"))
        if not safe:
            raise WorkflowError("仅支持读取 B站官方活动 URL。", 422)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/blackboard/activity-list.html",
            "Accept": "text/html,application/xhtml+xml",
        }
        with self._lock:
            cache = _read(self.bilibili_detail_cache_path, 4 * 1024 * 1024)
            with httpx.Client(timeout=httpx.Timeout(20, connect=8), trust_env=False, follow_redirects=True, headers=headers) as client:
                detail = self._bilibili_detail(client, safe, cache, force=force, strict=True, ttl=0 if force else BILI_DETAIL_TTL)
            _atomic(self.bilibili_detail_cache_path, dict(list(cache.items())[-200:]))
        return {
            "platform": "bilibili", "source_url": safe,
            "page_title": str(detail.get("_page_title") or "")[:240],
            "evidence_text": str(detail.get("_agent_evidence_text") or "")[:24000],
            "evidence_fingerprint": str(detail.get("rule_evidence_fingerprint") or ""),
            "structured": {
                key: deepcopy(detail.get(key)) for key in (
                    "eligibility", "content_requirements", "prizes", "winning_conditions",
                    "reward_rules", "required_topics", "submission_spec", "summary_hint",
                ) if detail.get(key)
            },
        }

    def preview_bilibili_url(self, url: str) -> dict[str, Any]:
        evidence = self.bilibili_page_evidence(url, force=True)
        title = re.sub(r"(?:[-_]?(?:哔哩哔哩|bilibili).*)$", "", str(evidence.get("page_title") or ""), flags=re.I).strip(" -_|·")
        structured = evidence.get("structured") if isinstance(evidence.get("structured"), dict) else {}
        return {
            "title": title or "B站创作活动", "platform": "bilibili", "platform_label": "B站",
            "organizer": "B站", "organizer_type": "platform", "activity_type": "创作活动",
            "summary": str(structured.get("summary_hint") or "")[:1200],
            "eligibility": structured.get("eligibility", []),
            "content_requirements": structured.get("content_requirements", []),
            "prizes": structured.get("prizes", []),
            "winning_conditions": structured.get("winning_conditions", []),
            "reward_rules": structured.get("reward_rules", []),
            "required_topics": structured.get("required_topics", []),
            "submission_spec": structured.get("submission_spec", {}),
            "source_url": evidence["source_url"], "source_type": "user_import",
            "rule_evidence_fingerprint": evidence.get("evidence_fingerprint", ""),
            "_agent_evidence": evidence,
        }


    @staticmethod
    def _manual_import_draft(target_platform: str, *, url: str = "", text: str = "") -> dict[str, Any]:
        labels = {
            "x": "X", "xiaohongshu": "小红书", "douyin": "抖音", "bilibili": "B站",
            "wechat": "微信公众号", "weixin-channels": "微信视频号",
        }
        clean_text = re.sub(r'\r\n?', '\n', str(text or '')).strip()[:20000]
        first_line = next((line.strip() for line in clean_text.splitlines() if line.strip()), "")
        summary = re.sub(r'\s+', ' ', clean_text)[:1200] if clean_text else ""
        return {
            "title": first_line[:240],
            "platform": target_platform,
            "platform_label": labels.get(target_platform, target_platform),
            "organizer": "",
            "organizer_type": "unknown",
            "activity_type": "征稿/活动",
            "reward_type": "",
            "reward_summary": "",
            "summary": summary,
            "starts_at": "",
            "signup_deadline": "",
            "submit_deadline": "",
            "stats_deadline": "",
            "timezone": "",
            "eligibility": [],
            "qualification_state": "unknown",
            "content_requirements": [],
            "reward_rules": [],
            "prizes": [],
            "winning_conditions": [],
            "required_topics": [],
            "submission_spec": normalize_submission_spec({}),
            "ai_policy": "unknown",
            "source_url": str(url or "")[:2000],
            "note": clean_text[:6000],
            "status": "unknown",
            "account_id": "",
        }

    def _preview_xiaohongshu_url(self, url: str) -> dict[str, Any]:
        safe = _safe_url(str(url or ""), ("xiaohongshu.com", "creator.xiaohongshu.com"))
        if not safe:
            raise WorkflowError("小红书导入仅读取 xiaohongshu.com 官方详情链接。", 422)
        base = self._manual_import_draft("xiaohongshu", url=safe)
        existing = next((
            row for row in _read_list(self.workspace.outputs / "_campaigns.json")
            if row.get("platform") == "xiaohongshu" and str(row.get("source_url") or "") == safe
        ), None)
        if isinstance(existing, dict):
            for field in (
                "title", "organizer", "organizer_type", "activity_type", "reward_type", "reward_summary",
                "summary", "starts_at", "signup_deadline", "submit_deadline", "stats_deadline", "timezone",
                "eligibility", "content_requirements", "reward_rules", "prizes", "winning_conditions",
                "required_topics", "submission_spec", "ai_policy", "note", "status", "account_id",
            ):
                if field in existing:
                    base[field] = deepcopy(existing[field])
        state = self._state()
        account, status = self._effective_account(
            "xiaohongshu", str(state["xiaohongshu"].get("account_id") or "")
        )
        if status != "ready" or not account or safe.rstrip("/") == "https://creator.xiaohongshu.com/new/events":
            return {
                "draft": base,
                "evidence_text": "",
                "evidence_fingerprint": "",
                "warning": "已保留小红书链接；连接创作者账号并使用具体活动详情页后可读取更多规则。",
                "detected_platform": "xiaohongshu",
                "field_evidence": {},
            }
        detail = self.workspace.xhs_ops.event_detail(
            str(account["id"]), url=safe, activity_id="", force=False,
        )
        for field in (
            "eligibility", "content_requirements", "reward_rules", "prizes",
            "winning_conditions", "required_topics", "submission_spec",
        ):
            if field in detail:
                base[field] = deepcopy(detail[field])
        base["qualification_state"] = str(detail.get("qualification_state") or "unknown")
        if detail.get("qualification_basis"):
            base["note"] = str(detail.get("qualification_basis") or "")[:6000]
        if not base.get("title"):
            base["title"] = "小红书创作活动（请确认标题）"
        evidence_parts: list[str] = []
        for field in ("eligibility", "content_requirements", "reward_rules", "prizes", "winning_conditions"):
            value = detail.get(field)
            if isinstance(value, list):
                evidence_parts.extend(str(item) for item in value if str(item).strip())
        evidence_text = "\n".join(evidence_parts)[:24000]
        return {
            "draft": base,
            "evidence_text": evidence_text,
            "evidence_fingerprint": evidence_fingerprint({"url": safe, "text": evidence_text}),
            "warning": "已使用当前小红书创作者账号只读解析详情；标题、资格和图片规则仍请在创建前核对。",
            "detected_platform": "xiaohongshu",
            "field_evidence": deepcopy(detail.get("field_evidence")) if isinstance(detail.get("field_evidence"), dict) else {},
        }

    def preview_import(self, target_platform: str, input_kind: str, *, url: str = "", text: str = "") -> dict[str, Any]:
        if target_platform not in {"x", "xiaohongshu", "douyin", "bilibili", "wechat", "weixin-channels"}:
            raise WorkflowError("不支持的活动目标平台。", 422)
        if input_kind == "text":
            draft = self._manual_import_draft(target_platform, url=url, text=text)
            evidence_text = str(text or "").strip()[:24000]
            return {
                "draft": draft,
                "evidence_text": evidence_text,
                "evidence_fingerprint": evidence_fingerprint({"platform": target_platform, "text": evidence_text}) if evidence_text else "",
                "warning": "已保留你粘贴的规则原文；未自动猜测缺失字段。",
                "detected_platform": "",
                "field_evidence": {},
            }
        if input_kind != "url":
            raise WorkflowError("导入方式只支持链接或粘贴原文。", 422)
        raw_url = str(url or "").strip()
        try:
            parsed = urlsplit(raw_url)
        except ValueError:
            raise WorkflowError("活动链接格式无效。", 422) from None
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in {"http", "https"} or not host:
            raise WorkflowError("活动链接只允许 http/https 地址。", 422)
        if host == "bilibili.com" or host.endswith(".bilibili.com"):
            draft = self.preview_bilibili_url(raw_url)
            evidence = draft.pop("_agent_evidence", {}) if isinstance(draft.get("_agent_evidence"), dict) else {}
            return {
                "draft": draft,
                "evidence_text": str(evidence.get("evidence_text") or "")[:24000],
                "evidence_fingerprint": str(evidence.get("evidence_fingerprint") or ""),
                "warning": "",
                "detected_platform": "bilibili",
                "field_evidence": {},
            }
        if host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"):
            return self._preview_xiaohongshu_url(raw_url)
        if can_parse_public_wechat_url(raw_url):
            return preview_public_wechat_rule(raw_url, target_platform)
        draft = self._manual_import_draft(target_platform, url=raw_url)
        return {
            "draft": draft,
            "evidence_text": "",
            "evidence_fingerprint": "",
            "warning": "该链接尚未支持自动解析，已保留链接和当前平台；可粘贴规则原文或直接手工填写。",
            "detected_platform": "",
            "field_evidence": {},
        }

    def _wechat_public(self, platform: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for url in official_program_urls(platform):
            parsed = preview_public_wechat_rule(url, platform)
            draft = parsed["draft"]
            fingerprint = str(parsed.get("evidence_fingerprint") or "")
            external = hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]
            rows.append({
                "provider_id": "wechat_public_rules",
                "platform": platform,
                "external_id": f"{platform}:official:{external}",
                "title": str(draft.get("title") or "")[:240],
                "organizer": str(draft.get("organizer") or "微信")[:160],
                "organizer_type": str(draft.get("organizer_type") or "platform")[:40],
                "activity_type": str(draft.get("activity_type") or "长期创作变现计划")[:80],
                "reward_type": str(draft.get("reward_type") or "")[:120],
                "reward_summary": str(draft.get("reward_summary") or "")[:600],
                "summary": str(draft.get("summary") or "")[:3000],
                "starts_at": "", "signup_deadline": "", "submit_deadline": "", "stats_deadline": "",
                "timezone": str(draft.get("timezone") or "Asia/Shanghai")[:80],
                "eligibility": list(draft.get("eligibility") or [])[:20],
                "content_requirements": list(draft.get("content_requirements") or [])[:30],
                "reward_rules": list(draft.get("reward_rules") or [])[:30],
                "prizes": [], "winning_conditions": [],
                "required_topics": list(draft.get("required_topics") or [])[:20],
                "submission_spec": deepcopy(draft.get("submission_spec") or {}),
                "qualification_state": "unknown",
                "qualification_basis": "微信官方公开规则已读取；当前账号是否开通、受邀及实际收益需账号内核验。",
                "source_url": url,
                "source_type": "wechat_public_official",
                "source_status": "verified",
                "note": str(draft.get("note") or "")[:3000],
                "field_evidence": deepcopy(parsed.get("field_evidence")) if isinstance(parsed.get("field_evidence"), dict) else {},
                "evidence": {
                    "kind": "platform_public_detail",
                    "provider": "wechat_public_rules",
                    "evidence_fingerprint": fingerprint,
                    "url": url,
                },
            })
        return rows

    def verify_bilibili_campaign(self, campaign: dict[str, Any]) -> dict[str, Any]:
        url = _safe_url(str(campaign.get("source_url") or ""), ("bilibili.com", "www.bilibili.com"))
        if not url:
            raise WorkflowError("当前活动没有可核验的 B站官方详情链接。", 422)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/blackboard/activity-list.html",
            "Accept": "text/html,application/xhtml+xml",
        }
        with self._lock:
            cache = _read(self.bilibili_detail_cache_path, 4 * 1024 * 1024)
            with httpx.Client(timeout=httpx.Timeout(20, connect=8), trust_env=False,
                              follow_redirects=True, headers=headers) as client:
                detail = self._bilibili_detail(client, url, cache, force=True, strict=True, ttl=0)
            cache[url] = {"fetched_at": int(time.time()), "detail": detail}
            _atomic(self.bilibili_detail_cache_path, dict(list(cache.items())[-200:]))
        return {
            "provider_id": "bilibili_public",
            "platform": "bilibili",
            "external_id": next(
                (str(v) for v in (campaign.get("external_ids") or {}).values() if str(v).startswith("bilibili:")),
                "",
            ),
            "title": str(campaign.get("title") or "")[:240],
            "organizer": str(campaign.get("organizer") or "B站")[:160],
            "organizer_type": str(campaign.get("organizer_type") or "platform")[:40],
            "activity_type": str(campaign.get("activity_type") or "创作活动")[:80],
            "reward_type": str(campaign.get("reward_type") or "")[:120],
            "reward_summary": str(campaign.get("reward_summary") or "")[:600],
            "summary": str(campaign.get("summary") or "")[:1200],
            "starts_at": str(campaign.get("starts_at") or "")[:40],
            "submit_deadline": str(campaign.get("submit_deadline") or "")[:40],
            "eligibility": detail.get("eligibility", []),
            "content_requirements": detail.get("content_requirements", []),
            "prizes": detail.get("prizes", []),
            "winning_conditions": detail.get("winning_conditions", []),
            "reward_rules": detail.get("reward_rules", []),
            "required_topics": detail.get("required_topics", []),
            "submission_spec": detail.get("submission_spec", {}),
            "rule_evidence_fingerprint": str(detail.get("rule_evidence_fingerprint") or ""),
            "source_url": url,
            "source_type": "platform_public",
            "source_status": "verified",
            "note": str(campaign.get("note") or "")[:3000],
            "evidence": {"kind": "platform_public_detail", "url": url},
        }

    def _bilibili(self, *, force_priority_details: bool = False) -> list[dict[str, Any]]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/blackboard/activity-list.html",
            "Accept": "application/json, text/plain, */*",
        }
        rows: list[dict[str, Any]] = []
        detail_cache = _read(self.bilibili_detail_cache_path, 4 * 1024 * 1024)
        priority_urls = self._bilibili_priority_urls()
        try:
            with httpx.Client(timeout=httpx.Timeout(20, connect=8), trust_env=False,
                              follow_redirects=True, headers=headers) as client:
                for page in (1, 2):
                    response = client.get(BILI_API, params={"plat": "1,3", "mold": 0, "http": 3, "pn": page, "ps": 50})
                    response.raise_for_status()
                    data = response.json()
                    values = ((data.get("data") or {}).get("list") or []) if isinstance(data, dict) else []
                    for item in values if isinstance(values, list) else []:
                        if not isinstance(item, dict):
                            continue
                        title = str(item.get("name") or item.get("page_name") or "").strip()
                        desc = str(item.get("desc") or "").strip()
                        if not title or not any(word in title + desc for word in CREATOR_WORDS):
                            continue
                        url = _safe_url(str(item.get("pc_url") or item.get("h5_url") or ""),
                                        ("bilibili.com", "www.bilibili.com"))
                        priority = bool(url and (url in priority_urls or self._bilibili_deadline_soon(item.get("etime"))))
                        detail = self._bilibili_detail(
                            client, url, detail_cache,
                            force=bool(force_priority_details and priority),
                            ttl=BILI_PRIORITY_DETAIL_TTL if priority else BILI_DETAIL_TTL,
                        ) if url else {}
                        detail_hint = str(detail.get("summary_hint") or "").strip()
                        summary_parts = [part for part in (desc[:700], detail_hint[:700]) if part]
                        summary = "；".join(dict.fromkeys(summary_parts))[:1200]
                        rows.append({
                            "provider_id": "bilibili_public", "platform": "bilibili",
                            "external_id": f"bilibili:{item.get('id')}", "title": title[:240],
                            "organizer": "B站", "organizer_type": "platform",
                            "activity_type": "创作活动", "reward_type": "",
                            "reward_summary": _bili_reward_summary(desc), "summary": summary,
                            "starts_at": _date_from_unix(item.get("stime")),
                            "submit_deadline": _date_from_unix(item.get("etime")),
                            "eligibility": detail.get("eligibility", []),
                            "content_requirements": detail.get("content_requirements", []),
                            "prizes": detail.get("prizes", []),
                            "winning_conditions": detail.get("winning_conditions", []),
                            "reward_rules": detail.get("reward_rules", []),
                            "required_topics": detail.get("required_topics", []),
                            "submission_spec": detail.get("submission_spec", {}),
                            "rule_evidence_fingerprint": str(detail.get("rule_evidence_fingerprint") or ""),
                            "source_url": url, "source_type": "platform_public",
                            "source_status": "verified", "note": desc[:3000],
                            "evidence": {"kind": "platform_public_detail" if detail else "platform_public_list", "url": url,
                                         "external_id": str(item.get("id") or "")[:160]},
                        })
                    if len(values) < 50:
                        break
        except (httpx.HTTPError, ValueError, TypeError):
            raise WorkflowError("B站公开活动源暂时无法读取。", 502) from None
        if detail_cache:
            # Bound the cache so long-running self-hosted instances do not grow forever.
            bounded = dict(sorted(
                ((key, value) for key, value in detail_cache.items() if isinstance(value, dict)),
                key=lambda pair: int(pair[1].get("fetched_at") or 0),
                reverse=True,
            )[:200])
            _atomic(self.bilibili_detail_cache_path, bounded)
        return rows[:100]

    def _x_prompt(self) -> list[dict[str, Any]]:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=7)
        prompt = (
            f"你是 Ripple 的 X 创作活动发现器。当前日期 {end.isoformat()}。"
            f"请使用这个模型或网关自身具备的 X/实时搜索能力，只做活动发现：查找 {start.isoformat()} 至 {end.isoformat()} "
            "最近发布、且现在仍可能值得创作者参与的真实创作活动、征集、挑战、创作者激励。"
            "最多返回 8 条，优先 X 官方、品牌官方、创作者项目官方账号。"
            "这一阶段不要分析完整规则、不要翻译长文，只确认活动存在并返回帖子身份。"
            "每条必须给出真实的 x.com 或 twitter.com /status/<数字> 帖子 URL；无法确认就不要返回。"
            "如果当前模型实际上无法访问 X，返回 search_available=false 和空 campaigns。"
            "只输出 JSON，不要 Markdown："
            '{"search_available":true,"reason":"","campaigns":[{"title":"","organizer":"","source_url":"https://x.com/.../status/...","external_id":""}]}'
        )
        result = self.ai.prompt_route("x_campaign_discovery", prompt, timeout=75, max_tokens=1200)
        parsed = _json_fragment(result.get("text", ""))
        if isinstance(parsed, dict) and parsed.get("search_available") is False:
            return []
        values = parsed.get("campaigns", []) if isinstance(parsed, dict) else parsed if isinstance(parsed, list) else []
        rows = []
        for item in values[:8] if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            url = _safe_url(str(item.get("source_url") or item.get("url") or ""),
                            ("x.com", "twitter.com"))
            title = str(item.get("title") or "").strip()
            if not url or not title or not re.search(r"/status/\d+", url):
                continue
            match = re.search(r"/status/(\d+)", url)
            external = str(item.get("external_id") or (match.group(1) if match else ""))[:160]
            rows.append({
                "provider_id": "x_model_prompt", "platform": "x", "external_id": f"x:{external}" if external else "",
                "title": title[:240], "organizer": str(item.get("organizer") or "")[:160],
                "organizer_type": "unknown", "activity_type": "创作活动",
                "reward_type": "", "reward_summary": "", "summary": "",
                "starts_at": "", "signup_deadline": "", "submit_deadline": "", "stats_deadline": "",
                "eligibility": [], "content_requirements": [], "prizes": [], "winning_conditions": [],
                "reward_rules": [], "required_topics": [], "submission_spec": normalize_submission_spec({}),
                "ai_policy": "unknown",
                "source_url": url, "source_type": "x_model_prompt", "source_status": "model_reported",
                "note": "",
                "evidence": {"kind": "model_prompt_x_discovery", "url": url,
                             "provider": result.get("provider_id", ""), "model": result.get("model", "")},
            })
        return rows

    def enrich_x_prompt_campaign(self, campaign: dict[str, Any]) -> dict[str, Any]:
        url = _safe_url(str(campaign.get("source_url") or ""), ("x.com", "twitter.com"))
        match = re.search(r"/status/(\d+)", url or "")
        if not url or not match:
            raise WorkflowError("X 规则整理需要有效的 /status/<id> 帖子 URL。", 422)
        title = str(campaign.get("title") or "").strip()[:240]
        organizer = str(campaign.get("organizer") or "").strip()[:160]
        prompt = (
            "你是 Ripple 的 X 创作活动规则整理器。只处理下面这一条已发现的 X 活动。"
            f"帖子 URL：{url}。已知活动名：{title or '未说明'}。已知主办方：{organizer or '未说明'}。"
            "请使用这个模型或网关自身具备的 X/实时搜索能力，阅读该帖子，以及能明确关联到该活动的同线程、引用帖或官方规则说明。"
            "所有面向用户展示的文字必须使用简体中文；品牌名、产品名、官方活动名、@账号、#标签和 URL 可保留原文。"
            "英文规则准确翻译，不扩写。只提取有明确依据的信息；原帖/官方规则没有说明的字段必须留空、空数组或 null，绝对不能猜。"
            "总奖池不能表述成单人奖金；达到门槛不等于必然获奖。日期只有明确年份时才填写 YYYY-MM-DD。"
            "submission_spec.formats 只允许 video、short_video、long_video、image_text、text、image、live、audio、any。"
            "ai_policy 未明确时写 unknown。只输出 JSON，不要 Markdown："
            '{"title":"","organizer":"","summary":"","starts_at":"","signup_deadline":"","submit_deadline":"","stats_deadline":"",'
            '"eligibility":[],"content_requirements":[],"prizes":[],"reward_summary":"","winning_conditions":[],"reward_rules":[],"required_topics":[],'
            '"submission_spec":{"formats":[],"content_directions":[],"style_requirements":[],"duration_seconds":{"min":null,"max":null},'
            '"aspect_ratios":[],"resolutions":[],"orientation":null,"image_count":{"min":null,"max":null},"text_length":{"min":null,"max":null},'
            '"live":{"min_duration_seconds":null,"required_category":"","title_keywords":[]},"original_required":null,"first_publish_required":null,'
            '"exclusive_required":null,"min_entries":null,"max_entries":null,"submission_method":"","required_mentions":[],"required_music":[]},'
            '"ai_policy":"unknown"}'
        )
        result = self.ai.prompt_route("x_campaign_discovery", prompt, timeout=90, max_tokens=2200)
        item = _json_fragment(result.get("text", ""))
        if not isinstance(item, dict):
            raise WorkflowError("X 规则整理模型没有返回有效 JSON。", 502)
        return {
            "provider_id": "x_model_prompt", "platform": "x",
            "external_id": f"x:{match.group(1)}",
            "title": str(item.get("title") or title)[:240],
            "organizer": str(item.get("organizer") or organizer)[:160],
            "organizer_type": "unknown", "activity_type": "创作活动",
            "reward_type": "", "reward_summary": _x_zh_text(item.get("reward_summary"), 600),
            "summary": _x_zh_text(item.get("summary"), 1200),
            "starts_at": _x_iso_date(item.get("starts_at")),
            "signup_deadline": _x_iso_date(item.get("signup_deadline")),
            "submit_deadline": _x_iso_date(item.get("submit_deadline")),
            "stats_deadline": _x_iso_date(item.get("stats_deadline")),
            "eligibility": _x_zh_list(item.get("eligibility"), limit=20),
            "content_requirements": _x_zh_list(item.get("content_requirements"), limit=30),
            "prizes": _x_zh_list(item.get("prizes"), limit=30),
            "winning_conditions": _x_zh_list(item.get("winning_conditions"), limit=30),
            "reward_rules": _x_zh_list(item.get("reward_rules"), limit=30),
            "required_topics": [str(x)[:120] for x in item.get("required_topics", []) if str(x).strip()][:20],
            "submission_spec": _x_submission_spec(item.get("submission_spec")),
            "ai_policy": ("unknown" if str(item.get("ai_policy") or "").strip().lower() in {"", "unknown"} else _x_zh_text(item.get("ai_policy"), 600) or "unknown"),
            "source_url": url, "source_type": "x_model_prompt", "source_status": "model_reported",
            "note": str(item.get("summary") or "")[:3000],
            "evidence": {"kind": "model_prompt_x_rules", "url": url,
                         "provider": result.get("provider_id", ""), "model": result.get("model", "")},
        }

    def _x_search_tool(self) -> list[dict[str, Any]]:
        end = datetime.now(timezone.utc).date()
        start = end - timedelta(days=7)
        prompt = (
            "使用 X Search 查找最近 7 天仍值得创作者参与或刚公布的真实创作活动、征集、挑战、创作者激励。"
            "优先平台官方、品牌官方、创作者项目官方账号。只返回实际搜索到且带 X 帖子 URL 的结果。"
            "不要猜测奖励、截止日期或资格。严格输出 JSON："
            '{"campaigns":[{"title":"","organizer":"","source_url":"https://x.com/.../status/...",'
            '"external_id":"","description":"","submit_deadline":"","reward_summary":""}]}'
        )
        result = self.ai.x_search(prompt, from_date=start.isoformat(), to_date=end.isoformat())
        parsed = _json_fragment(result.get("text", ""))
        values = parsed.get("campaigns", []) if isinstance(parsed, dict) else parsed if isinstance(parsed, list) else []
        rows = []
        for item in values[:50] if isinstance(values, list) else []:
            if not isinstance(item, dict):
                continue
            url = _safe_url(str(item.get("source_url") or item.get("url") or ""),
                            ("x.com", "twitter.com"))
            title = str(item.get("title") or "").strip()
            if not url or not title:
                continue
            match = re.search(r"/status/(\d+)", url)
            external = str(item.get("external_id") or (match.group(1) if match else ""))[:160]
            rows.append({
                "provider_id": "xai_x_search", "platform": "x", "external_id": f"x:{external}" if external else "",
                "title": title[:240], "organizer": str(item.get("organizer") or "")[:160],
                "organizer_type": "unknown", "activity_type": "创作活动",
                "reward_type": "", "reward_summary": str(item.get("reward_summary") or "")[:600],
                "summary": str(item.get("description") or "")[:1200],
                "starts_at": "", "submit_deadline": str(item.get("submit_deadline") or "")[:40],
                "source_url": url, "source_type": "x_search", "source_status": "verified",
                "note": str(item.get("description") or "")[:3000],
                "evidence": {"kind": "x_post", "url": url, "provider": result.get("provider_id", ""),
                             "model": result.get("model", "")},
            })
        return rows

    def _x_api(self, account_id: str) -> list[dict[str, Any]]:
        if not account_id:
            raise WorkflowError("请选择 X Developer API 账号。", 409)
        payload = self.workspace.x.search_recent(account_id, X_QUERY, 50)
        rows = []
        for item in payload.get("items", []):
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            title = re.split(r"[\n。.!?]", text, maxsplit=1)[0].strip() or text[:180]
            rows.append({
                "provider_id": "x_developer_api", "platform": "x",
                "external_id": f"x:{item.get('id')}", "title": title[:240],
                "organizer": ("@" + str(item.get("username") or ""))[:160],
                "organizer_type": "unknown", "activity_type": "活动公告候选",
                "reward_type": "", "reward_summary": "", "summary": text[:1200],
                "starts_at": str(item.get("created_at") or "")[:40],
                "submit_deadline": "", "source_url": str(item.get("url") or "")[:2048],
                "source_type": "x_developer_api", "source_status": "verified", "note": text[:3000],
                "evidence": {"kind": "x_post", "url": str(item.get("url") or "")[:2048],
                             "external_id": str(item.get("id") or "")[:160]},
            })
        return rows

    def _x_method(self, method: str, state: dict[str, Any]) -> list[dict[str, Any]]:
        if method == "prompt":
            return self._x_prompt()
        if method == "x_search":
            return self._x_search_tool()
        if method == "x_api":
            account, status = self._effective_account("x", str(state["x"].get("x_api_account_id") or ""), adapter="x-api")
            if status != "ready" or not account:
                raise WorkflowError("X Developer API 账号未就绪。", 409)
            return self._x_api(str(account["id"]))
        raise WorkflowError("X 活动发现尚未配置来源。", 409)

    def _xiaohongshu_candidate(self, account: dict[str, Any], item: dict[str, Any], source_type: str) -> dict[str, Any]:
        title = str(item.get("title") or "").strip()
        url = _safe_url(str(item.get("url") or ""), ("xiaohongshu.com", "creator.xiaohongshu.com"))
        topic_names = [str(x.get("name") or "")[:120] for x in item.get("topics", []) if isinstance(x, dict) and str(x.get("name") or "").strip()]
        topic_ids = [str(x.get("id") or "")[:100] for x in item.get("topics", []) if isinstance(x, dict) and str(x.get("id") or "").strip()]
        required_topics = [str(x)[:120] for x in item.get("required_topics", []) if str(x).strip()]
        for topic in topic_names:
            if topic and topic not in required_topics:
                required_topics.append(topic)

        prizes = [str(x)[:240] for x in item.get("prizes", []) if str(x).strip()][:30]
        reward_rules = [str(x)[:500] for x in item.get("reward_rules", []) if str(x).strip()][:30]
        promotion_summary = str(item.get("promotion_summary") or item.get("description") or "")[:1200]
        reward_summary = str(item.get("reward_summary") or "")[:600]
        if prizes:
            reward_summary = "、".join(prizes[:4])[:600]

        content_requirements = [str(x)[:500] for x in item.get("content_requirements", []) if str(x).strip()][:30]
        submission_spec = deepcopy(item.get("submission_spec")) if isinstance(item.get("submission_spec"), dict) else {}
        publish_url = _safe_url(str(item.get("publish_url") or ""), ("creator.xiaohongshu.com", "xiaohongshu.com"))
        if publish_url and required_topics:
            if not content_requirements:
                content_requirements = [f"发布时带 #{topic} 话题" for topic in required_topics[:12]]
            if not str(submission_spec.get("submission_method") or "").strip():
                submission_spec["submission_method"] = "通过活动中心指定投稿入口发布并带指定话题"
            if not isinstance(submission_spec.get("content_directions"), list) or not submission_spec.get("content_directions"):
                submission_spec["content_directions"] = [f"围绕 #{topic} 创作" for topic in required_topics[:12]]
            submission_spec.setdefault("formats", [])
            submission_spec.setdefault("style_requirements", [])
            submission_spec.setdefault("required_mentions", [])
            submission_spec.setdefault("required_music", [])

        qualification = str(item.get("qualification_state") or "unknown")
        if qualification not in {"eligible", "ineligible", "unknown"}:
            qualification = "unknown"
        detail_status = str(item.get("xhs_detail_status") or "")
        detail_version = max(0, int(item.get("xhs_detail_version") or 0))
        authoritative_detail = detail_version >= 3 and detail_status in {"parsed", "no_structured_rules", "needs_visual_review"}
        field_evidence = deepcopy(item.get("field_evidence")) if isinstance(item.get("field_evidence"), dict) else {}
        if publish_url and submission_spec:
            field_evidence.setdefault("submission_spec", {
                "source": "xiaohongshu_activity_center_list",
                "originals": [publish_url, *required_topics[:12]],
            })
            if content_requirements:
                field_evidence.setdefault("content_requirements", {
                    "source": "xiaohongshu_activity_center_list",
                    "originals": content_requirements[:30],
                })
            if required_topics:
                field_evidence.setdefault("required_topics", {
                    "source": "xiaohongshu_activity_center_list",
                    "originals": required_topics[:20],
                })
        if promotion_summary:
            field_evidence.setdefault("summary", {
                "source": "xiaohongshu_activity_center_list",
                "originals": [promotion_summary],
            })

        return {
            "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
            "external_id": ("xhs:" + str(item.get("external_id"))) if item.get("external_id") else "",
            "title": title[:240], "organizer": "小红书创作服务平台", "organizer_type": "platform",
            "activity_type": "创作活动", "reward_type": "", "reward_summary": reward_summary,
            "summary": promotion_summary,
            "starts_at": _date_from_unix(item.get("detail_start_time") or item.get("starts_at")),
            "submit_deadline": _date_from_unix(item.get("detail_end_time") or item.get("ends_at")),
            "eligibility": [str(x)[:240] for x in item.get("eligibility", []) if str(x).strip()][:20],
            "content_requirements": content_requirements,
            "prizes": prizes,
            "winning_conditions": [str(x)[:500] for x in item.get("winning_conditions", []) if str(x).strip()][:30],
            "reward_rules": reward_rules,
            "required_topics": required_topics[:20],
            "submission_spec": submission_spec,
            "qualification_state": qualification,
            "qualification_basis": str(item.get("qualification_basis") or "unknown")[:600],
            "source_url": url or "https://creator.xiaohongshu.com/new/events",
            "source_type": str(source_type or "creator_events"),
            "source_status": "verified", "note": promotion_summary[:3000],
            "account_id": str(account["id"]),
            "field_evidence": field_evidence,
            "xhs_detail_status": detail_status or "not_fetched",
            "xhs_detail_version": detail_version,
            "xhs_detail_fetched_at": max(0, int(item.get("xhs_detail_fetched_at") or 0)),
            "xhs_detail_error": str(item.get("xhs_detail_error") or "")[:300],
            "evidence": {
                "kind": "platform_public_detail" if authoritative_detail else "creator_account_activity",
                "account_id": str(account["id"]),
                "activity_id": str(item.get("external_id") or "")[:160],
                "page_id": str(item.get("page_id") or "")[:100],
                "instance_id": str(item.get("instance_id") or "")[:100],
                "topic_ids": topic_ids[:20],
                "publish_url": publish_url,
                "detail_source": str(item.get("detail_source") or "")[:160],
                "detail_status": detail_status or "not_fetched",
                "detail_version": detail_version,
                "task_progress": item.get("task_progress") if isinstance(item.get("task_progress"), dict) else {},
                "url": url or "https://creator.xiaohongshu.com/new/events",
            },
        }

    def _xiaohongshu(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        account, status = self._effective_account("xiaohongshu", str(state["xiaohongshu"].get("account_id") or ""))
        if status != "ready" or not account:
            raise WorkflowError("小红书活动源需要已连接的创作者账号。", 409)
        payload = self.workspace.xhs_ops.events(str(account["id"]), 500, detail_limit=12)
        self._record_xhs_official_snapshots(str(account["id"]), payload)
        if not payload.get("items") and payload.get("source") == "creator_events_dom" and not payload.get("api_observed"):
            raise WorkflowError("小红书创作者活动列表接口本次未返回数据，请稍后重试。", 502)
        rows = []
        for item in payload.get("items", []):
            if not isinstance(item, dict) or not str(item.get("title") or "").strip():
                continue
            rows.append(self._xiaohongshu_candidate(account, item, str(payload.get("source") or "creator_events")))
        return rows

    def verify_xiaohongshu_campaign(self, campaign: dict[str, Any]) -> dict[str, Any]:
        account_id = str(campaign.get("account_id") or "")
        account, status = self._effective_account("xiaohongshu", account_id)
        if status != "ready" or not account:
            raise WorkflowError("小红书活动规则读取需要已连接的创作者账号。", 409)
        ids = campaign.get("external_ids") if isinstance(campaign.get("external_ids"), dict) else {}
        external_id = str(ids.get("xiaohongshu_creator_events") or "")
        activity_id = external_id[4:] if external_id.startswith("xhs:") else external_id
        url = _safe_url(str(campaign.get("source_url") or ""), ("xiaohongshu.com", "creator.xiaohongshu.com"))
        if not url or url == "https://creator.xiaohongshu.com/new/events":
            raise WorkflowError("该活动缺少可读取的官方详情链接。", 409)
        detail = self.workspace.xhs_ops.event_detail(
            str(account["id"]), url=url, activity_id=activity_id, force=True,
        )
        base = {
            "external_id": activity_id,
            "title": str(campaign.get("title") or "")[:240],
            "url": url,
            "starts_at": str(campaign.get("starts_at") or "")[:40],
            "ends_at": str(campaign.get("submit_deadline") or "")[:40],
            "description": str(campaign.get("summary") or campaign.get("note") or "")[:1200],
            "promotion_summary": str(campaign.get("summary") or campaign.get("note") or "")[:600],
            "topics": [{"id": "", "name": str(topic)[:120], "link": ""} for topic in campaign.get("required_topics", []) if str(topic).strip()],
            **detail,
        }
        return self._xiaohongshu_candidate(account, base, "creator_event_detail")

    def _douyin_portal(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        account, status = self._effective_account("douyin", str(state["douyin"].get("account_id") or ""))
        if status != "ready" or not account:
            raise WorkflowError("抖音创作者活动源需要已连接的创作者账号。", 409)
        operation = account.get("operation") or {}
        if operation.get("state") in {"running", "recovery_required"}:
            raise WorkflowError("该抖音账号正在执行其他浏览器任务，请稍后刷新活动。", 429)
        result = self.workspace.accounts.run(
            account, "campaign_read", uuid.uuid4().hex, headed=False,
            campaign_action="events", campaign_params={"limit": 200},
        )
        state_name = str(result.get("state") or "")
        if state_name == "verification_required":
            raise WorkflowError(str(result.get("message") or "抖音创作者登录态需要重新验证。"), 409)
        if state_name != "success":
            raise WorkflowError(str(result.get("message") or "抖音创作者活动源读取失败。"), 502)
        payload = result.get("data") if isinstance(result.get("data"), dict) else {}
        if payload.get("source") != "creator_activity_api_v2" or not isinstance(payload.get("items"), list):
            raise WorkflowError("抖音采集返回的活动结构无效，已保留上次数据。", 502)
        window = payload.get("window") if isinstance(payload.get("window"), dict) else {}
        start, end = (douyin_display_date(window.get(key))[:10] for key in ("start_time", "end_time"))
        scope_note = "创作者中心活动日历" + (f" · {start}～{end}（北京时间）" if start and end else " · 当前页面时间范围")
        scope_note += "；仅代表当前账号该窗口的列表结果。"
        if payload.get("truncated"):
            scope_note += "部分条目未读取或未通过校验。"
        rows, seen = [], set()
        for order, item in enumerate(payload["items"]):
            if not isinstance(item, dict):
                raise WorkflowError("抖音活动条目格式错误。", 502)
            title, external_id = str(item.get("title") or "").strip(), str(item.get("external_id") or "")
            if not title or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", external_id):
                raise WorkflowError("抖音活动缺少可靠名称或 ID。", 502)
            if external_id in seen:
                continue
            seen.add(external_id)
            url = douyin_detail_url(item.get("url"))
            fields = ("eligibility", "content_requirements", "prizes", "winning_conditions", "reward_rules")
            detail_fields = {key: [str(v)[:240] for v in item.get(key, []) if isinstance(v, str)][:20]
                             for key in fields}
            detail_status = str(item.get("detail_status") or "not_fetched")
            if detail_status not in {"parsed", "no_structured_rules", "needs_visual_review", "failed", "app_only", "not_fetched"}:
                detail_status = "not_fetched"
            spec = infer_submission_spec("\n".join(detail_fields["content_requirements"]))
            listing = {"display_starts_at": str(item.get("display_starts_at") or "")[:40],
                       "display_ends_at": str(item.get("display_ends_at") or "")[:40],
                       "detail_status": detail_status, "scope_note": scope_note,
                       "challenge_ids": [str(v) for v in item.get("challenge_ids", []) if re.fullmatch(r"[1-9][0-9]{0,24}", str(v))][:20],
                       "source_order": order, "fetched_at": int(payload.get("fetched_at") or time.time())}
            rows.append({
                "provider_id": "douyin_creator_portal", "platform": "douyin",
                "external_id": "douyin:" + external_id,
                "title": title[:240], "organizer": "抖音创作者中心", "organizer_type": "platform",
                "activity_type": "创作活动", "reward_type": "", "reward_summary": "",
                "summary": str(item.get("description") or "")[:1200],
                "starts_at": "", "signup_deadline": str(item.get("signup_deadline") or "")[:40],
                "submit_deadline": str(item.get("submit_deadline") or "")[:40],
                "source_url": url, "source_type": "creator_activity_api_v2", "source_status": "verified",
                "note": str(item.get("description") or "")[:3000], "account_id": str(account["id"]),
                "qualification_state": "unknown", "qualification_basis": "列表对该账号可见；参与资格仍需核对具体活动规则。",
                "submission_spec": spec, **detail_fields, "douyin_listing": listing,
                "field_evidence": {key: {"source": "official_detail", "originals": values} for key, values in detail_fields.items() if values},
                "evidence": {"kind": "platform_public_detail" if detail_status == "parsed" else "platform_public_list",
                             "account_id": str(account["id"]), "url": url, "endpoint": "/web/api/v2/creator/activity/pc/list",
                             "activity_id": external_id, "scope_note": scope_note},
            })
        latest = self._state()
        latest["douyin"]["scope_note"] = scope_note
        self._write(latest)
        return rows

    def _tikhub(self) -> list[dict[str, Any]]:
        key = self._tikhub_key()
        if not key:
            raise WorkflowError("TikHub fallback 未配置 API Key。", 409)
        now = int(time.time())
        start = now - 90 * 24 * 3600
        try:
            with httpx.Client(base_url=TIKHUB_API, timeout=httpx.Timeout(30, connect=8), trust_env=False,
                              follow_redirects=False, headers={"Authorization": "Bearer " + key,
                              "Accept": "application/json"}) as client:
                response = client.get("/api/v1/douyin/creator/fetch_creator_activity_list",
                                      params={"start_time": start, "end_time": now})
                response.raise_for_status()
                if len(response.content) > MAX_RESPONSE:
                    raise WorkflowError("TikHub 活动响应超过安全上限。", 502)
                data = response.json()
        except WorkflowError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise WorkflowError("TikHub 抖音活动 fallback 调用失败，请检查 Token、额度与当前计费状态。", 502) from None
        rows = []
        for item in _generic_activity_rows(data, 60):
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            url = _safe_url(str(item.get("url") or ""), ("douyin.com", "creator.douyin.com"))
            rows.append({
                "provider_id": "tikhub_douyin", "platform": "douyin",
                "external_id": ("douyin:" + str(item.get("external_id"))) if item.get("external_id") else "",
                "title": title[:240], "organizer": "抖音", "organizer_type": "platform",
                "activity_type": "创作活动", "reward_type": "", "reward_summary": "",
                "summary": str(item.get("description") or "")[:1200],
                "starts_at": _date_from_unix(item.get("starts_at")),
                "submit_deadline": _date_from_unix(item.get("ends_at")),
                "source_url": url, "source_type": "third_party_api", "source_status": "verified",
                "note": str(item.get("description") or "")[:3000],
                "evidence": {"kind": "third_party_api", "provider": "TikHub",
                             "endpoint": "/api/v1/douyin/creator/fetch_creator_activity_list"},
            })
        return rows

    def refresh(self, platforms: list[str] | None = None, *, force: bool = False,
                allow_paid: bool = False) -> dict[str, Any]:
        capabilities = {row["platform"]: row for row in self.public_state()["items"]}
        if platforms is not None and (not platforms or any(p not in capabilities for p in platforms)):
            raise WorkflowError("必须明确选择有效的活动平台，空列表不会刷新全部来源。", 422)
        wanted = list(dict.fromkeys(platforms)) if platforms is not None else [
            p for p, row in capabilities.items() if row.get("automatic")
        ]
        results = []
        self._refresh_lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Do not queue duplicate refreshes behind a slow browser/model call.
        lock = FileLock(str(self._refresh_lock_path), timeout=0)
        try:
            lock.acquire()
        except FileLockTimeout:
            return {"results": [{"platform": p, "status": "busy", "items": [], "count": 0,
                                  "error": "活动采集正在执行，请稍后重试。"} for p in wanted],
                    "sources": self.public_state()}
        try:
            state = self._state()
            admitted_at = time.time()
            recent = {p: self._recent(p) for p in wanted if capabilities[p].get("automatic")}
            for platform in wanted:
                capability = capabilities[platform]
                if not capability.get("automatic"):
                    results.append({"platform": platform, "status": capability.get("status", "manual"),
                                    "items": [], "count": 0, "error": capability.get("detail", "来源未就绪")})
                    continue
                if not force and recent.get(platform):
                    results.append({"platform": platform, "status": "cached", "items": [], "count": 0})
                    continue
                try:
                    paid_key = self._paid_key(platform)
                    # Capture one admission time for the whole serial batch.
                    paid_slot = self.paid_schedule.claim(paid_key, admitted_at) if paid_key and (not force or platform == "x") else False
                    if self._paid_only(platform) and not force and not paid_slot:
                        results.append({"platform": platform, "status": "cached", "items": [], "count": 0})
                        continue
                    fallback_used = False
                    provider = ""
                    if platform == "bilibili":
                        items = self._bilibili(force_priority_details=force); provider = "bilibili_public"
                    elif platform == "x":
                        method = str(state["x"].get("method") or "")
                        try:
                            items = self._x_method(method, state)
                            provider = "x_model_prompt" if method == "prompt" else "xai_x_search" if method == "x_search" else "x_developer_api"
                        except WorkflowError:
                            fallback = str(state["x"].get("fallback_method") or "")
                            if not state["x"].get("fallback_enabled") or not fallback:
                                raise
                            items = self._x_method(fallback, state)
                            provider = "x_model_prompt" if fallback == "prompt" else "xai_x_search" if fallback == "x_search" else "x_developer_api"
                            fallback_used = True
                    elif platform == "xiaohongshu":
                        items = self._xiaohongshu(state); provider = "xiaohongshu_creator_events"
                    elif platform == "douyin":
                        try:
                            items = self._douyin_portal(state); provider = "douyin_creator_portal"
                        except WorkflowError:
                            if not state["douyin"].get("tikhub_enabled") or not self._tikhub_key():
                                raise
                            if not paid_slot and not (force and allow_paid):
                                raise WorkflowError("免费后台读取失败；收费备用将在 09:00、14:00、20:00（北京时间）尝试，当前未调用。", 503) from None
                            items = self._tikhub(); provider = "tikhub_douyin"; fallback_used = True
                    else:
                        items = self._wechat_public(platform); provider = "wechat_public_rules"
                    self._record_sync(platform, status="fresh", count=len(items), provider=provider,
                                      fallback_used=fallback_used)
                    results.append({"platform": platform, "status": "fresh", "items": items,
                                    "count": len(items), "provider": provider,
                                    "rules_allowed": bool(paid_slot or (platform == "x" and force and allow_paid)),
                                    "fallback_used": fallback_used})
                except (WorkflowError, OSError, ValueError) as exc:
                    previous = self._state()["last_sync"].get(platform, {})
                    status = "stale" if previous.get("last_success_at") or previous.get("count") else (
                        "needs_login" if isinstance(exc, WorkflowError) and exc.status == 409 else "error")
                    self._record_sync(platform, status=status, count=int(previous.get("count") or 0),
                                      error=str(exc), provider=str(previous.get("provider") or ""))
                    results.append({"platform": platform, "status": status, "items": [], "count": 0,
                                    "error": str(exc)[:300]})
        finally:
            lock.release()
        return {"results": results, "sources": self.public_state()}
