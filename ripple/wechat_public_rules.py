"""Read-only parsers for first-party public WeChat creator incentive rules."""
from __future__ import annotations

import html as html_lib
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
from playwright.sync_api import sync_playwright

from .campaign_enrichment import empty_submission_spec, evidence_fingerprint
from .publishing import WorkflowError

MAX_PUBLIC_BYTES = 2 * 1024 * 1024
OFFICIAL_PAGES = {
    ("ad.weixin.qq.com", "/docs/45"): ("wechat", "微信公众号流量主"),
    ("ad.weixin.qq.com", "/docs/76"): ("weixin-channels", "微信视频号流量主"),
    ("ad.weixin.qq.com", "/docs/273"): ("weixin-channels", "微信视频号创作分成计划"),
    ("support.weixin.qq.com", "/cgi-bin/mmsupportacctnodeweb-bin/pages/flrux77QlRxPwwhY"): (
        "weixin-channels", "视频号创作分成计划说明手册",
    ),
}
_BROWSER_SUFFIXES = ("qq.com", "weixin.qq.com", "wxs.qq.com", "gtimg.com", "qpic.cn")


def _clean_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        raise WorkflowError("微信公开规则链接格式无效。", 422) from None
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host or parsed.port not in {None, 443}:
        raise WorkflowError("微信公开规则只读取已验证的 HTTPS 官方页面。", 422)
    path = parsed.path.rstrip("/") or "/"
    if (host, path) not in OFFICIAL_PAGES and host != "mp.weixin.qq.com":
        raise WorkflowError("该链接尚未纳入微信公开规则安全解析范围，可保留链接后手工填写或粘贴原文。", 422)
    return urlunsplit(("https", host, parsed.path, parsed.query if host == "mp.weixin.qq.com" else "", ""))


def _strip_html(raw: str) -> str:
    raw = re.sub(r"(?is)<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", "\n", raw)
    raw = html_lib.unescape(raw)
    return "\n".join(line.strip() for line in raw.splitlines() if line.strip())[:40000]


def _http_text(url: str) -> str:
    try:
        response = httpx.get(
            url, timeout=httpx.Timeout(15, connect=6), follow_redirects=False, trust_env=False,
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html,application/xhtml+xml"},
        )
        response.raise_for_status()
    except httpx.HTTPError:
        raise WorkflowError("微信公开规则页面读取失败，请稍后重试。", 502) from None
    if len(response.content) > MAX_PUBLIC_BYTES:
        raise WorkflowError("微信公开规则页面超过安全读取上限。", 502)
    if "html" not in str(response.headers.get("content-type") or "").lower():
        raise WorkflowError("微信公开规则页面不是可解析的 HTML。", 502)
    return _strip_html(response.text)


def _render_text(url: str) -> str:
    last_error: Exception | None = None
    for _attempt in range(2):
        try:
            with sync_playwright() as runtime:
                browser = None
                launch_error: Exception | None = None
                for channel in ("msedge", "chrome"):
                    try:
                        browser = runtime.chromium.launch(channel=channel, headless=True)
                        break
                    except Exception as exc:
                        launch_error = exc
                if browser is None:
                    try:
                        browser = runtime.chromium.launch(headless=True)
                    except Exception:
                        raise launch_error or RuntimeError("browser unavailable")
                try:
                    context = browser.new_context()
                    page = context.new_page()

                    def guard(route):
                        request = route.request
                        parsed = urlsplit(request.url)
                        host = (parsed.hostname or "").lower()
                        if parsed.scheme not in {"https", "data", "blob"}:
                            route.abort()
                            return
                        if parsed.scheme == "https" and not any(host == suffix or host.endswith("." + suffix) for suffix in _BROWSER_SUFFIXES):
                            route.abort()
                            return
                        if request.method not in {"GET", "HEAD"}:
                            route.abort()
                            return
                        route.continue_()

                    context.route("**/*", guard)
                    page.goto(url, wait_until="domcontentloaded", timeout=20000)
                    page.wait_for_timeout(1500)
                    text = page.locator("body").inner_text(timeout=4000)
                    return "\n".join(line.strip() for line in text.splitlines() if line.strip())[:40000]
                finally:
                    browser.close()
        except Exception as exc:
            last_error = exc
    raise WorkflowError(
        f"微信官方规则页需要浏览器渲染，但本次读取失败（{type(last_error).__name__ if last_error else 'unknown'}）。",
        502,
    ) from None


def _official_text(url: str) -> tuple[str, str, str]:
    safe = _clean_url(url)
    parsed = urlsplit(safe)
    host = parsed.hostname or ""
    path = parsed.path.rstrip("/") or "/"
    if host == "ad.weixin.qq.com":
        text = _render_text(safe)
    else:
        text = _http_text(safe)
    if host == "mp.weixin.qq.com":
        return safe, "", text
    platform, title = OFFICIAL_PAGES[(host, path)]
    required = {
        "/docs/45": ("公众号流量主", "开通流程"),
        "/docs/76": ("视频号流量主", "创作分成计划"),
        "/docs/273": ("创作分成计划", "参与门槛"),
        "/cgi-bin/mmsupportacctnodeweb-bin/pages/flrux77QlRxPwwhY": ("创作分成计划", "收益"),
    }[path]
    if any(marker not in text for marker in required):
        raise WorkflowError("微信官方规则页已返回，但关键规则结构未识别；已停止自动提取。", 502)
    return safe, platform, text


def can_parse_public_wechat_url(url: str) -> bool:
    try:
        parsed = urlsplit(str(url or "").strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    path = parsed.path.rstrip("/") or "/"
    return parsed.scheme == "https" and ((host, path) in OFFICIAL_PAGES or host == "mp.weixin.qq.com")


def _evidence(texts: list[str], source: str) -> dict[str, Any]:
    return {"source": source, "originals": texts[:20]}


def _base_draft(platform: str, title: str, url: str) -> dict[str, Any]:
    return {
        "title": title[:240], "platform": platform, "organizer": "微信", "organizer_type": "platform",
        "activity_type": "长期创作变现计划", "reward_type": "创作收益", "reward_summary": "",
        "summary": "", "starts_at": "", "signup_deadline": "", "submit_deadline": "", "stats_deadline": "",
        "timezone": "Asia/Shanghai", "eligibility": [], "qualification_state": "unknown",
        "content_requirements": [], "reward_rules": [], "prizes": [], "winning_conditions": [],
        "required_topics": [], "submission_spec": empty_submission_spec(), "ai_policy": "unknown",
        "source_url": url, "note": "公开规则已读取；当前账号是否开通、受邀及实际收益需账号内核验。", "status": "active",
        "account_id": "",
    }


def preview_public_wechat_rule(url: str, target_platform: str) -> dict[str, Any]:
    safe, detected_platform, text = _official_text(url)
    if (urlsplit(safe).hostname or "") == "mp.weixin.qq.com":
        title_match = re.search(r"(?m)^(.{4,120})$", text)
        title = title_match.group(1).strip() if title_match else "微信公众号公开文章"
        draft = _base_draft(target_platform, title, safe)
        draft["activity_type"] = "待确认活动/激励"
        draft["summary"] = re.sub(r"\s+", " ", text)[:1200]
        draft["note"] = text[:6000]
        return {
            "draft": draft,
            "evidence_text": text[:24000],
            "evidence_fingerprint": evidence_fingerprint({"url": safe, "text": text[:24000]}),
            "warning": "已读取公众号公开文章正文，但尚未确认它描述的是哪类活动；目标平台沿用当前选择，请确认后再创建。",
            "detected_platform": "",
            "field_evidence": {},
        }

    platform = detected_platform
    path = urlsplit(safe).path.rstrip("/") or "/"
    field_evidence: dict[str, Any] = {}
    if path == "/docs/45":
        draft = _base_draft("wechat", "微信公众号流量主", safe)
        draft["summary"] = "微信公众号创作者可通过程序化广告、返佣商品与互选广告等官方能力获得内容变现收入。"
        match = re.search(r"公众号关注用户达到\s*(\d+)\s*人", text)
        if match:
            draft["eligibility"].append(f"公众号关注用户达到 {match.group(1)} 人")
        draft["eligibility"].append("符合平台运营规范")
        draft["reward_rules"] = [
            "程序化广告：公众号指定流量展示广告后获得广告收入",
            "返佣商品：按实际成交订单金额的一定比例获得广告分成",
            "公众号互选广告：接受广告主合作邀约并按约定报价获得广告收入",
        ]
        draft["reward_summary"] = "广告展示、返佣商品及互选合作收入"
        draft["note"] = "公开规则已读取；是否已开通流量主及具体订单/收入需登录公众号后台核验。"
        field_evidence["eligibility"] = _evidence(draft["eligibility"], "wechat_ads_official")
        field_evidence["reward_rules"] = _evidence(draft["reward_rules"], "wechat_ads_official")
    elif path in {"/docs/273", "/cgi-bin/mmsupportacctnodeweb-bin/pages/flrux77QlRxPwwhY"}:
        draft = _base_draft("weixin-channels", "微信视频号创作分成计划", safe)
        draft["summary"] = "符合条件的优质原创视频号作者可通过原创视频评论区或相关视频流中的广告/推广内容获取分成收入。"
        match = re.search(r"(?:有效关注人数|粉丝数)[^\d]{0,20}(\d+)\s*(?:人)?", text)
        if match:
            draft["eligibility"].append(f"有效关注人数达到 {match.group(1)} 人及以上")
        draft["eligibility"].extend(["符合内容规范", "优质原创作者"])
        if "分批邀请" in text or "未收到邀请" in text:
            draft["eligibility"].append("内测/开放阶段由平台分批邀请，实际入口以账号内展示为准")
        draft["content_requirements"] = ["发表公开的优质原创视频，并按计划要求开启原创声明"]
        draft["reward_rules"] = ["收益与视频质量、播放、投稿数量及广告匹配等因素有关，不保证固定金额"]
        draft["reward_summary"] = "原创视频广告/推广内容分成"
        spec = empty_submission_spec()
        spec["formats"] = ["video"]
        spec["original_required"] = True
        spec["submission_method"] = "加入计划后在视频号发表视频并开启原创声明"
        draft["submission_spec"] = spec
        draft["note"] = "公开规则已读取；当前账号是否受邀/开通、具体收益和权益状态需在视频号创作者中心核验。"
        field_evidence["eligibility"] = _evidence(draft["eligibility"], "wechat_channels_official")
        field_evidence["content_requirements"] = _evidence(draft["content_requirements"], "wechat_channels_official")
        field_evidence["reward_rules"] = _evidence(draft["reward_rules"], "wechat_channels_official")
    else:  # /docs/76
        draft = _base_draft("weixin-channels", "微信视频号流量主", safe)
        draft["summary"] = "视频号创作者可选择创作分成计划或视频号互选广告等官方变现方式。"
        draft["reward_rules"] = [
            "创作分成计划：声明原创的视频有机会展示广告并产生分成收益",
            "视频号互选广告：接受广告主合作邀约，按约定报价制作并发布创意内容",
        ]
        draft["reward_summary"] = "创作分成或互选合作收入"
        draft["note"] = "该页面是视频号变现能力总览；具体创作分成资格与互选订单需进入对应账号页面核验。"
        field_evidence["reward_rules"] = _evidence(draft["reward_rules"], "wechat_ads_official")

    warning = ""
    if target_platform and target_platform != platform:
        warning = f"该微信官方页面对应{platform}，草稿已按官方规则平台生成；请确认平台后再创建。"
    return {
        "draft": draft,
        "evidence_text": text[:24000],
        "evidence_fingerprint": evidence_fingerprint({"url": safe, "text": text[:24000]}),
        "warning": warning,
        "detected_platform": platform,
        "field_evidence": field_evidence,
    }


def official_program_urls(platform: str) -> list[str]:
    if platform == "wechat":
        return ["https://ad.weixin.qq.com/docs/45"]
    if platform == "weixin-channels":
        return ["https://ad.weixin.qq.com/docs/273"]
    return []
