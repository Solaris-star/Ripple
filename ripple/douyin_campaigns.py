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
            "detail_status": "not_fetched" if url else "app_only", "description": "",
        })
    if raw and not rows:
        raise DouyinCampaignError("抖音返回的条目缺少活动 ID 或 show_name，未按活动导入。")
    limit = max(1, min(int(limit), 500))
    has_more = bool(value.get("has_more") or value.get("hasMore"))
    return {"items": rows[:limit], "source_count": len(rows), "rejected_count": rejected,
            "truncated": len(rows) > limit or has_more or rejected > 0,
            "scope": "creator_calendar_window", "source": "creator_activity_api_v2"}


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
