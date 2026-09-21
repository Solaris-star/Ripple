"""Automatic campaign-source adapters for Ripple's activity center.

Every provider is read-only. Paid fallbacks are opt-in and never invoked merely
because a free/self-hosted source is missing.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import threading
import time
from typing import Any
from urllib.parse import urlsplit
import uuid

import httpx

from .ai_providers import AIProviderService
from .publishing import WorkflowError
from .secrets import protect


SYNC_TTL = 30 * 60
MAX_RESPONSE = 4 * 1024 * 1024
CREATOR_WORDS = ("创作", "创作者", "征稿", "投稿", "激励", "奖金", "UP主", "视频", "内容", "挑战")
X_QUERY = '("creator challenge" OR "creator rewards" OR "creator program" OR "creator incentive" OR "call for creators" OR "creator contest" OR "submissions open") -is:retweet'
BILI_API = "https://api.bilibili.com/x/activity/page/list"
TIKHUB_API = "https://api.tikhub.io"
BILI_DETAIL_TTL = 24 * 60 * 60
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
    topics: list[str] = []
    eligibility: list[str] = []
    content_requirements: list[str] = []
    prizes: list[str] = []
    winning_conditions: list[str] = []
    reward_rules: list[str] = []

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
    }


class CampaignSourceService:
    def __init__(self, workspace, ai_providers: AIProviderService):
        self.workspace = workspace
        self.ai = ai_providers
        self.path = workspace.private / "integrations" / "campaign-sources.json"
        self.secret_path = workspace.private / "integrations" / "campaign-source-tikhub.secret"
        self.bilibili_detail_cache_path = workspace.private / "integrations" / "campaign-bilibili-details.json"
        self._lock = threading.Lock()

    def _empty(self) -> dict[str, Any]:
        return {
            "schema": 2,
            "revision": 0,
            "x": {"method": "", "x_api_account_id": "", "fallback_method": "", "fallback_enabled": False},
            "xiaohongshu": {"account_id": ""},
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
        return state

    def _write(self, state: dict[str, Any]) -> None:
        state = deepcopy(state)
        state["schema"] = 2
        state["revision"] = int(state.get("revision") or 0) + 1
        state["updated_at"] = _now()
        _atomic(self.path, state)

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
            if method not in {"", "xai", "x_api"}:
                raise WorkflowError("X 活动源只支持 xAI X Search 或 X Developer API。", 422)
            fallback = str(value.get("fallback_method") or "")
            if fallback not in {"", "xai", "x_api"} or fallback == method:
                fallback = ""
            state["x"].update({
                "method": method,
                "x_api_account_id": str(value.get("x_api_account_id") or "")[:32],
                "fallback_method": fallback,
                "fallback_enabled": bool(value.get("fallback_enabled") and fallback),
            })
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
        if method == "xai":
            cfg = self.ai.resolved("x_campaign_discovery", fallback_to_default=False)
            if not cfg:
                return "needs_config", "请在 AI Provider 中配置“X 活动发现”路由。"
            if cfg.get("kind") != "xai":
                return "needs_config", "X Search 路由必须绑定显式配置为 xAI 的 Provider。"
            if (cfg.get("capabilities") or {}).get("x_search") != "verified":
                return "needs_config", "请先在模型设置中执行一次 xAI X Search 能力测试。"
            return "ready", f"{cfg.get('provider_name')} / {cfg.get('model')}"
        if method == "x_api":
            account, status = self._effective_account("x", str(state["x"].get("x_api_account_id") or ""), adapter="x-api")
            return status, (account.get("label") if account else "请选择已连接的 X Developer API 账号")
        return "needs_config", "选择 xAI X Search 或 X Developer API。"

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
                "billing": "paid_or_plan_dependent" if x_method in {"xai", "x_api"} else "unknown",
                "cost_note": "xAI X Search 按检索内容计费；X Developer API 取决于开发者计划与额度。",
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
            },
            {
                "id": "wechat_campaigns", "platform": "wechat", "label": "微信公众号",
                "status": "manual", "automatic": False, "mode": "manual",
                "detail": "本阶段保留导入；公众号公告订阅源将在后续 Provider 中接入。",
                "billing": "unknown", "last_sync": state["last_sync"].get("wechat", {}),
            },
            {
                "id": "weixin_channels_campaigns", "platform": "weixin-channels", "label": "微信视频号",
                "status": "manual", "automatic": False, "mode": "manual",
                "detail": "本阶段保留导入；视频号活动源尚未确认稳定官方 Feed。",
                "billing": "unknown", "last_sync": state["last_sync"].get("weixin-channels", {}),
            },
        ]
        return {"items": rows, "automatic_count": sum(1 for row in rows if row["automatic"]),
                "revision": state["revision"]}

    def _record_sync(self, platform: str, *, status: str, count: int = 0, error: str = "",
                     provider: str = "", fallback_used: bool = False) -> None:
        state = self._state()
        state["last_sync"][platform] = {
            "at": int(time.time()), "status": status, "count": int(count),
            "error": str(error or "")[:300], "provider": provider,
            "fallback_used": bool(fallback_used),
        }
        self._write(state)

    def _recent(self, platform: str) -> bool:
        row = self._state()["last_sync"].get(platform, {})
        return bool(row.get("status") == "fresh" and time.time() - float(row.get("at") or 0) < SYNC_TTL)

    def _bilibili_detail(self, client: httpx.Client, url: str, cache: dict[str, Any]) -> dict[str, Any]:
        now = int(time.time())
        cached = cache.get(url) if isinstance(cache.get(url), dict) else {}
        if cached and now - int(cached.get("fetched_at") or 0) < BILI_DETAIL_TTL:
            detail = cached.get("detail")
            return detail if isinstance(detail, dict) else {}
        try:
            response = client.get(url, headers={"Accept": "text/html,application/xhtml+xml"})
            response.raise_for_status()
            if len(response.content) > MAX_RESPONSE:
                return {}
            detail = _bilibili_detail_from_html(response.text)
        except (httpx.HTTPError, ValueError, TypeError):
            return {}
        cache[url] = {"fetched_at": now, "detail": detail}
        return detail


    def _bilibili(self) -> list[dict[str, Any]]:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/blackboard/activity-list.html",
            "Accept": "application/json, text/plain, */*",
        }
        rows: list[dict[str, Any]] = []
        detail_cache = _read(self.bilibili_detail_cache_path, 4 * 1024 * 1024)
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
                        detail = self._bilibili_detail(client, url, detail_cache) if url else {}
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

    def _x_xai(self) -> list[dict[str, Any]]:
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
        if method == "xai":
            return self._x_xai()
        if method == "x_api":
            account, status = self._effective_account("x", str(state["x"].get("x_api_account_id") or ""), adapter="x-api")
            if status != "ready" or not account:
                raise WorkflowError("X Developer API 账号未就绪。", 409)
            return self._x_api(str(account["id"]))
        raise WorkflowError("X 活动发现尚未配置来源。", 409)

    def _xiaohongshu(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        account, status = self._effective_account("xiaohongshu", str(state["xiaohongshu"].get("account_id") or ""))
        if status != "ready" or not account:
            raise WorkflowError("小红书活动源需要已连接的创作者账号。", 409)
        payload = self.workspace.xhs_ops.events(str(account["id"]), 40)
        rows = []
        for item in payload.get("items", []):
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            url = _safe_url(str(item.get("url") or ""), ("xiaohongshu.com", "creator.xiaohongshu.com"))
            rows.append({
                "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
                "external_id": ("xhs:" + str(item.get("external_id"))) if item.get("external_id") else "",
                "title": title[:240], "organizer": "小红书创作服务平台", "organizer_type": "platform",
                "activity_type": "创作活动", "reward_type": "", "reward_summary": "",
                "summary": str(item.get("description") or "")[:1200],
                "starts_at": _date_from_unix(item.get("starts_at")),
                "submit_deadline": _date_from_unix(item.get("ends_at")),
                "source_url": url or "https://creator.xiaohongshu.com/new/events",
                "source_type": str(payload.get("source") or "creator_events"),
                "source_status": "verified", "note": str(item.get("description") or "")[:3000],
                "account_id": str(account["id"]),
                "evidence": {"kind": "creator_account", "account_id": str(account["id"]),
                             "url": url or "https://creator.xiaohongshu.com/new/events"},
            })
        return rows

    def _douyin_portal(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        account, status = self._effective_account("douyin", str(state["douyin"].get("account_id") or ""))
        if status != "ready" or not account:
            raise WorkflowError("抖音创作者活动源需要已连接的创作者账号。", 409)
        operation = account.get("operation") or {}
        if operation.get("state") in {"running", "recovery_required"}:
            raise WorkflowError("该抖音账号正在执行其他浏览器任务，请稍后刷新活动。", 429)
        result = self.workspace.accounts.run(
            account, "campaign_read", uuid.uuid4().hex, headed=False,
            campaign_action="events", campaign_params={"limit": 40},
        )
        state_name = str(result.get("state") or "")
        if state_name == "verification_required":
            raise WorkflowError(str(result.get("message") or "抖音创作者登录态需要重新验证。"), 409)
        if state_name != "success":
            raise WorkflowError(str(result.get("message") or "抖音创作者活动源读取失败。"), 502)
        payload = result.get("data") if isinstance(result.get("data"), dict) else {}
        rows = []
        for item in payload.get("items", []) if isinstance(payload.get("items"), list) else []:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            url = _safe_url(str(item.get("url") or ""), ("douyin.com", "creator.douyin.com"))
            rows.append({
                "provider_id": "douyin_creator_portal", "platform": "douyin",
                "external_id": ("douyin:" + str(item.get("external_id"))) if item.get("external_id") else "",
                "title": title[:240], "organizer": "抖音创作者中心", "organizer_type": "platform",
                "activity_type": "创作活动", "reward_type": "", "reward_summary": "",
                "summary": str(item.get("description") or "")[:1200],
                "starts_at": _date_from_unix(item.get("starts_at")),
                "submit_deadline": _date_from_unix(item.get("ends_at")),
                "source_url": url or "https://creator.douyin.com/",
                "source_type": str(payload.get("source") or "creator_activity"),
                "source_status": "verified", "note": str(item.get("description") or "")[:3000],
                "account_id": str(account["id"]),
                "evidence": {"kind": "creator_account", "account_id": str(account["id"]),
                             "url": url or "https://creator.douyin.com/"},
            })
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

    def refresh(self, platforms: list[str] | None = None, *, force: bool = False) -> dict[str, Any]:
        wanted = [p for p in (platforms or ["bilibili", "x", "xiaohongshu", "douyin"])
                  if p in {"bilibili", "x", "xiaohongshu", "douyin"}]
        state = self._state()
        results = []
        with self._lock:
            for platform in wanted:
                if not force and self._recent(platform):
                    results.append({"platform": platform, "status": "cached", "items": [], "count": 0})
                    continue
                try:
                    fallback_used = False
                    provider = ""
                    if platform == "bilibili":
                        items = self._bilibili(); provider = "bilibili_public"
                    elif platform == "x":
                        method = str(state["x"].get("method") or "")
                        try:
                            items = self._x_method(method, state)
                            provider = "xai_x_search" if method == "xai" else "x_developer_api"
                        except WorkflowError:
                            fallback = str(state["x"].get("fallback_method") or "")
                            if not state["x"].get("fallback_enabled") or not fallback:
                                raise
                            items = self._x_method(fallback, state)
                            provider = "xai_x_search" if fallback == "xai" else "x_developer_api"
                            fallback_used = True
                    elif platform == "xiaohongshu":
                        items = self._xiaohongshu(state); provider = "xiaohongshu_creator_events"
                    else:
                        try:
                            items = self._douyin_portal(state); provider = "douyin_creator_portal"
                        except WorkflowError:
                            if not state["douyin"].get("tikhub_enabled") or not self._tikhub_key():
                                raise
                            items = self._tikhub(); provider = "tikhub_douyin"; fallback_used = True
                    self._record_sync(platform, status="fresh", count=len(items), provider=provider,
                                      fallback_used=fallback_used)
                    results.append({"platform": platform, "status": "fresh", "items": items,
                                    "count": len(items), "provider": provider,
                                    "fallback_used": fallback_used})
                except WorkflowError as exc:
                    previous = self._state()["last_sync"].get(platform, {})
                    status = "stale" if previous.get("status") == "fresh" or previous.get("count") else (
                        "needs_login" if exc.status == 409 else "error")
                    self._record_sync(platform, status=status, count=int(previous.get("count") or 0),
                                      error=str(exc), provider=str(previous.get("provider") or ""))
                    results.append({"platform": platform, "status": status, "items": [], "count": 0,
                                    "error": str(exc)[:300]})
        return {"results": results, "sources": self.public_state()}
