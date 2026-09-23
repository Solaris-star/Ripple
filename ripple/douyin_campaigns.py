"""Strict, read-only Douyin activity-list and public-rule parsing.

Only the observed creator calendar contract is accepted. Display dates are not
submission deadlines, and taxonomy rows are never a fallback activity source.
"""
from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import ipaddress
import re
import socket
import time
from typing import Any
from zoneinfo import ZoneInfo
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

import httpx

LIST_PATH = "/web/api/v2/creator/activity/pc/list"
DETAIL_PATH = "/web/api/v2/creator/activity/detail/"
HOME = "https://creator.douyin.com/creator-micro/home"
MAX_RESPONSE = 4 * 1024 * 1024
DETAIL_HOSTS = {"creator.douyin.com", "www.douyin.com", "api.amemv.com"}


class DouyinCampaignError(ValueError):
    pass


def official_list_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "creator.douyin.com"
                and parsed.port in {None, 443} and not parsed.username and not parsed.password
                and parsed.path == LIST_PATH)
    except ValueError:
        return False


def official_detail_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "creator.douyin.com"
                and parsed.port in {None, 443} and not parsed.username and not parsed.password
                and parsed.path == DETAIL_PATH)
    except ValueError:
        return False


def detail_url(value: Any) -> str:
    """Unwrap only observed webview links; do not execute an app protocol."""
    if not isinstance(value, str) or len(value) > 8192 or re.search(r"[\x00-\x20\\]", value):
        return ""
    try:
        for _ in range(3):
            parsed = urlsplit(value)
            if parsed.scheme == "sslocal" and parsed.netloc == "webview":
                nested = parse_qs(parsed.query).get("url", [])
                if len(nested) != 1:
                    return ""
                value = nested[0]
                if re.search(r"[\x00-\x20\\]", value):
                    return ""
                continue
            if (parsed.scheme != "https" or parsed.hostname not in DETAIL_HOSTS
                    or parsed.port not in {None, 443} or parsed.username or parsed.password):
                return ""
            if parsed.hostname == "api.amemv.com" and not parsed.path.startswith("/magic/eco/runtime/release/"):
                return ""
            # Retain page identity, never transient authentication or tracking values.
            query = parse_qs(parsed.query)
            kept = [(k, v[0]) for k, v in query.items()
                    if k in {"activity", "magic_page_no", "magic_source", "appType"} and len(v) == 1]
            return urlunsplit(("https", parsed.hostname, parsed.path, urlencode(kept), ""))[:2000]
    except (ValueError, TypeError):
        pass
    return ""


def display_date(value: Any) -> str:
    try:
        if isinstance(value, bool):
            return ""
        stamp = int(value)
        if not 946684800 <= stamp <= 4102444800:
            return ""
        return datetime.fromtimestamp(stamp, timezone.utc).astimezone(ZoneInfo("Asia/Shanghai")).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def parse_activity_list(value: Any, *, limit: int = 200) -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("status_code")) is not int or value["status_code"] != 0:
        raise DouyinCampaignError("抖音官方活动接口未返回成功状态，请检查登录或平台验证。")
    raw = value.get("list")
    if not isinstance(raw, list) or len(raw) > 2000:
        raise DouyinCampaignError("抖音官方活动列表结构已变化，已保留上次数据。")
    rows, seen, rejected = [], set(), 0
    for item in raw:
        if not isinstance(item, dict):
            rejected += 1
            continue
        identity = item.get("activity_id")
        title = item.get("show_name")
        if (isinstance(identity, bool) or not isinstance(identity, (str, int))
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", str(identity))
                or not isinstance(title, str) or not title.strip() or len(title) > 300):
            rejected += 1
            continue
        identity = str(identity)
        if identity in seen:
            continue
        seen.add(identity)
        url = detail_url(item.get("jump_link"))
        challenges = item.get("challenge_ids") if isinstance(item.get("challenge_ids"), list) else []
        challenge_ids = list(dict.fromkeys(str(v) for v in challenges
                             if isinstance(v, (str, int)) and not isinstance(v, bool)
                             and re.fullmatch(r"[1-9][0-9]{0,24}", str(v))))[:20]
        rows.append({
            "external_id": identity, "title": title.strip()[:240], "url": url,
            "display_starts_at": display_date(item.get("show_start_time")),
            "display_ends_at": display_date(item.get("show_end_time")),
            "challenge_ids": challenge_ids, "jump_type": item.get("jump_type"),
            "detail_status": "not_fetched", "description": "",
        })
    if raw and not rows:
        raise DouyinCampaignError("抖音返回的条目缺少活动 ID 或 show_name，未按活动导入。")
    limit = max(1, min(int(limit), 500))
    has_more = bool(value.get("has_more") or value.get("hasMore"))
    return {"items": rows[:limit], "source_count": len(rows), "rejected_count": rejected,
            "truncated": len(rows) > limit or has_more or rejected > 0,
            "scope": "creator_calendar_window", "source": "creator_activity_api_v2"}


def _detail_date(value: Any) -> str:
    text = str(value or "").strip()
    match = re.fullmatch(r"(20\d{2})[.\-/](\d{1,2})[.\-/](\d{1,2})", text)
    if not match:
        return ""
    try:
        return datetime(int(match[1]), int(match[2]), int(match[3])).date().isoformat()
    except ValueError:
        return ""


def _reward_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    text = value.strip()
    try:
        parsed = __import__("json").loads(text)
        if isinstance(parsed, dict) and isinstance(parsed.get("text"), str):
            text = parsed["text"].strip()
    except (ValueError, TypeError):
        pass
    return re.sub(r"\s+", " ", text)[:1200]


def _submission_requirements(description: str) -> list[str]:
    rows = []
    for line in re.split(r"[\n。！？；]+", description):
        line = re.sub(r"\s+", " ", line).strip(" ，,。；;：:")
        if not line:
            continue
        if re.search(r"(?:投稿|上传|发布|带活动话题|原创视频|首次发布|时长[≥<=]|@抖音)", line):
            rows.append(line[:500])
    return list(dict.fromkeys(rows))[:12]


def _submission_window(description: str, info: dict[str, Any], has_submission: bool) -> tuple[str, str]:
    if not has_submission:
        return "", ""
    year_match = re.search(r"^(20\d{2})[.]", str(info.get("show_start_time") or ""))
    default_year = int(year_match[1]) if year_match else None
    full = re.search(
        r"(20\d{2})年(\d{1,2})月(\d{1,2})日(?:\d{1,2}:\d{2})?\s*(?:至|到|[-~～—])\s*"
        r"(?:(20\d{2})年)?(\d{1,2})月(\d{1,2})日(?:\d{1,2}:\d{2})?",
        description,
    )
    short = re.search(r"(?:^|[，。\s])(\d{1,2})月(\d{1,2})日\s*(?:至|到|[-~～—])\s*(\d{1,2})月(\d{1,2})日", description)
    try:
        if full:
            start_year = int(full[1]); end_year = int(full[4] or start_year)
            return (datetime(start_year, int(full[2]), int(full[3])).date().isoformat(),
                    datetime(end_year, int(full[5]), int(full[6])).date().isoformat())
        if short and default_year:
            start = datetime(default_year, int(short[1]), int(short[2])).date()
            end_year = default_year + (1 if (int(short[3]), int(short[4])) < (start.month, start.day) else 0)
            end = datetime(end_year, int(short[3]), int(short[4])).date()
            return start.isoformat(), end.isoformat()
    except ValueError:
        pass
    if default_year:
        start_only = re.search(r"(?:^|[，。\s])(\d{1,2})月(\d{1,2})日起", description)
        if start_only:
            try:
                return datetime(default_year, int(start_only[1]), int(start_only[2])).date().isoformat(), ""
            except ValueError:
                pass
    return "", ""


def _reward_condition(reward: str) -> list[str]:
    if not reward:
        return []
    match = re.match(r"(.{1,500}?)(?:，|,)?(?:将获得|可以获得|可获得|有机会获得|可享受)", reward)
    if not match:
        return []
    value = match[1].strip(" ，,。；;：:")
    return [value[:500]] if value else []


def _activity_type(title: str, description: str, reward: str, requirements: list[str]) -> str:
    text = f"{title}\n{description}\n{reward}"
    if re.search(r"(?:名单.{0,8}(?:出炉|公示)|中奖名单|公示期)", text) and not requirements:
        return "结果公示"
    if requirements:
        return "创作投稿"
    if re.search(r"(?:星愿卡|卡池|星守护|抽卡|签名照|拍立得|演唱会门票)", text):
        return "粉丝福利"
    return "平台活动"


def _reward_type(reward: str) -> str:
    if not reward:
        return ""
    traffic = "流量" in reward
    benefits = bool(re.search(r"(?:礼物|门票|周边|签名照|拍立得|身份|资格|权益)", reward))
    if traffic and benefits:
        return "流量 + 礼品/权益"
    if traffic:
        return "流量激励"
    if benefits:
        return "礼品/权益"
    return "活动奖励"


def _participation_conditions(description: str, activity_type: str) -> list[str]:
    if activity_type != "粉丝福利" or not description:
        return []
    rows = []
    for line in re.split(r"[\n。！？；]+", description):
        line = re.sub(r"\s+", " ", line).strip(" ，,。；;：:")
        if not line:
            continue
        if re.search(r"(?:开通|关注|参与|完成).{0,80}(?:即可|可获得|可参与|获得)", line):
            rows.append(line[:500])
    return list(dict.fromkeys(rows))[:8]


def parse_activity_detail(value: Any, *, expected_id: str = "") -> dict[str, Any]:
    if not isinstance(value, dict) or type(value.get("status_code")) is not int or value["status_code"] != 0:
        raise DouyinCampaignError("抖音官方活动详情未返回成功状态。")
    info = value.get("activity_info")
    if not isinstance(info, dict):
        raise DouyinCampaignError("抖音官方活动详情结构已变化。")
    identity = str(info.get("activity_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identity) or (expected_id and identity != expected_id):
        raise DouyinCampaignError("抖音官方活动详情 ID 与列表不一致。")
    title = str(info.get("activity_name") or "").strip()[:240]
    description = re.sub(r"\s+", " ", str(value.get("activity_description") or "")).strip()[:3000]
    reward = _reward_text(value.get("reward_rules"))
    raw_topics = value.get("topics") if isinstance(value.get("topics"), list) else []
    topics = [str(x).strip()[:120] for x in raw_topics if isinstance(x, str) and str(x).strip()][:20]
    hashtags = re.findall(r"#([^\s#，。！？；、]{1,60})", description)
    required_topics = list(dict.fromkeys([*topics, *hashtags]))[:20]
    requirements = _submission_requirements(description)
    publish_start, publish_end = _submission_window(description, info, bool(requirements))
    jump = detail_url(info.get("jump_link") or info.get("post_url"))
    display_start = _detail_date(info.get("show_start_time"))
    display_end = _detail_date(info.get("show_end_time"))
    challenge_values = info.get("challenge_ids") if isinstance(info.get("challenge_ids"), list) else []
    challenge_ids = list(dict.fromkeys(str(v) for v in challenge_values
                         if isinstance(v, (str, int)) and not isinstance(v, bool)
                         and re.fullmatch(r"[1-9][0-9]{0,24}", str(v))))[:20]
    winning = _reward_condition(reward)
    activity_type = _activity_type(title, description, reward, requirements)
    reward_type = _reward_type(reward)
    eligibility = _participation_conditions(description, activity_type)
    return {
        "external_id": identity,
        "title": title,
        "description": description,
        "starts_at": publish_start,
        "submit_deadline": publish_end,
        "display_starts_at": display_start,
        "display_ends_at": display_end,
        "url": jump,
        "required_topics": required_topics,
        "challenge_ids": challenge_ids,
        "content_requirements": requirements,
        "reward_summary": reward,
        "reward_rules": [reward] if reward else [],
        "winning_conditions": winning,
        "eligibility": eligibility,
        "prizes": [reward] if reward else [],
        "activity_type": activity_type,
        "reward_type": reward_type,
        "detail_status": "parsed" if any((description, reward, required_topics, requirements, display_start, display_end)) else "no_structured_rules",
        "detail_source": "creator_activity_detail_api",
        "activity_type_code": info.get("activity_type"),
        "reward_type_code": info.get("reward_type"),
        "activity_status_code": info.get("activity_status"),
    }


class _PageText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts: list[str] = []
        self.images = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip += 1
        if not self.skip:
            if tag in {"p", "div", "li", "br", "h1", "h2", "h3", "h4", "section"}:
                self.parts.append("\n")
            if tag == "img":
                self.images += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript", "svg"}:
            self.skip = max(0, self.skip - 1)
        if not self.skip and tag in {"p", "div", "li", "h1", "h2", "h3", "h4", "section"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def parse_public_rules(html: str) -> dict[str, Any]:
    parser = _PageText()
    parser.feed(html[:MAX_RESPONSE])
    lines = [re.sub(r"\s+", " ", line).strip() for line in "".join(parser.parts).splitlines()]
    lines = list(dict.fromkeys(line for line in lines if line))[:600]
    headings = {
        "参与条件": "eligibility", "参与资格": "eligibility", "报名条件": "eligibility",
        "作品要求": "content_requirements", "投稿要求": "content_requirements", "内容要求": "content_requirements",
        "奖品设置": "prizes", "活动奖励": "prizes", "奖励设置": "prizes",
        "获奖条件": "winning_conditions", "评选规则": "winning_conditions", "奖励规则": "reward_rules",
    }
    result: dict[str, Any] = {field: [] for field in set(headings.values())}
    current, remaining = "", 0
    for line in lines:
        matched = re.match(r"^(参与条件|参与资格|报名条件|作品要求|投稿要求|内容要求|奖品设置|活动奖励|奖励设置|获奖条件|评选规则|奖励规则)\s*[:：]?\s*(.*)$", line)
        if matched:
            current, remaining = headings[matched[1]], 12
            line = matched[2]
        elif re.match(r"^(活动时间|活动介绍|免责声明|注意事项|联系方式|活动规则|主办方|报名方式|参与方式)\s*[:：]?", line):
            current, remaining = "", 0
        if current and remaining > 0 and line:
            result[current].append(line[:240])
            remaining -= 1
        date = re.match(r"^(报名截止|投稿截止|作品提交截止)(?:时间)?\s*[:：]\s*(20\d{2})[-年/](\d{1,2})[-月/](\d{1,2})日?\s*$", line)
        if date:
            try:
                day = datetime(int(date[2]), int(date[3]), int(date[4])).date().isoformat()
                result["signup_deadline" if date[1] == "报名截止" else "submit_deadline"] = day
            except ValueError:
                pass
    has_rules = any(result.values())
    result.update({"detail_status": "parsed" if has_rules else "needs_visual_review" if parser.images else "no_structured_rules",
                   "description": "\n".join(lines)[:3000] if has_rules else "",
                   "rule_text": "\n".join(lines)[:12000] if has_rules else ""})
    return result


def _public_host(url: str) -> None:
    host = urlsplit(url).hostname
    if not host or not detail_url(url):
        raise DouyinCampaignError("活动详情地址不在已确认的官方范围。")
    addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise DouyinCampaignError("活动详情地址不能指向本机或私有网络。")


def fetch_public_detail(url: str) -> dict[str, Any]:
    """No cookies, API keys, browser profile or model requests leave this function."""
    target = detail_url(url)
    if not target:
        return {"detail_status": "app_only", "description": ""}
    deadline = time.monotonic() + 15
    try:
        with httpx.Client(timeout=httpx.Timeout(8, connect=4), trust_env=False, follow_redirects=False) as client:
            for _ in range(3):
                _public_host(target)
                with client.stream("GET", target, headers={"Accept": "text/html"}) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        from urllib.parse import urljoin
                        target = detail_url(urljoin(target, response.headers.get("location", "")))
                        if not target:
                            raise DouyinCampaignError("官方详情跳转到了未允许的地址。")
                        continue
                    response.raise_for_status()
                    if "text/html" not in response.headers.get("content-type", "").lower():
                        raise DouyinCampaignError("活动详情没有返回网页内容。")
                    body = bytearray()
                    for block in response.iter_bytes():
                        body.extend(block)
                        if len(body) > MAX_RESPONSE or time.monotonic() > deadline:
                            raise DouyinCampaignError("活动详情读取超过安全上限。")
                    result = parse_public_rules(body.decode("utf-8", errors="replace"))
                    result["detail_url"] = target
                    return result
        raise DouyinCampaignError("活动详情跳转次数超过上限。")
    except (httpx.HTTPError, OSError, ValueError):
        return {"detail_status": "failed", "description": "", "detail_error": "官方详情暂时无法读取，已保留列表信息。"}
