"""Resilient multi-provider trend aggregation for Ripple.

Public direct-source field mappings are adapted from imsyy/DailyHotApi (MIT,
2023 imsyy). Xiaohongshu read-only browser extraction follows the public-page
query-trending/SSR strategy documented by openweb-org/openweb (MIT, 2025
contributors). Ripple never reuses a connected Xiaohongshu account unless the
caller explicitly supplies that account profile for this read-only request.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from html.parser import HTMLParser
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from filelock import FileLock, Timeout

from .catalog import browser_channel

CACHE_TTL = 300
XHS_CACHE_TTL = 600
MAX_STALE = 6 * 3600
MAX_BODY = 2 * 1024 * 1024
FAILURE_TTL = 120
TREND_LINK_HOSTS = {
    "weibo": {"weibo.com"}, "douyin": {"douyin.com"}, "xiaohongshu": {"xiaohongshu.com"},
    "zhihu": {"zhihu.com"}, "bilibili": {"bilibili.com", "b23.tv"},
    "baidu": {"baidu.com"}, "toutiao": {"toutiao.com", "toutiaoapi.com"},
}

LABELS = {
    "weibo": "微博", "douyin": "抖音", "xiaohongshu": "小红书", "zhihu": "知乎",
    "bilibili": "B站", "baidu": "百度", "toutiao": "头条",
}


def _hot_text(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", text):
            return text
        value = float(text)
    if isinstance(value, (int, float)):
        if value >= 100_000_000:
            number = f"{value / 100_000_000:.1f}".rstrip("0").rstrip(".")
            return number + "亿"
        if value >= 10_000:
            return f"{value / 10_000:.0f}万"
        return str(int(value) if float(value).is_integer() else value)
    return str(value)


def _read_json(url: str, *, headers: dict | None = None, timeout: int = 10):
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123 Safari/537.36",
        "Accept": "application/json,text/plain,*/*",
        **(headers or {}),
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError("response_too_large")
        charset = response.headers.get_content_charset() or "utf-8"
        return json.loads(raw.decode(charset, "replace"))


def _read_text(url: str, *, timeout: int = 10) -> str:
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/123 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read(MAX_BODY + 1)
        if len(raw) > MAX_BODY:
            raise ValueError("response_too_large")
        charset = response.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, "replace")


class _TrendTableParser(HTMLParser):
    def __init__(self, tbody_id: str | None = None):
        super().__init__(convert_charrefs=True)
        self.tbody_id = tbody_id
        self.in_target = tbody_id is None
        self.in_row = False
        self.in_cell = False
        self.cells: list[str] = []
        self.cell_parts: list[str] = []
        self.links: list[str] = []
        self.rows: list[tuple[list[str], list[str]]] = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "tbody" and self.tbody_id is not None and values.get("id") == self.tbody_id:
            self.in_target = True
        if not self.in_target:
            return
        if tag == "tr":
            self.in_row = True; self.cells = []; self.links = []
        elif tag == "td" and self.in_row:
            self.in_cell = True; self.cell_parts = []
        elif tag == "a" and self.in_row and values.get("href"):
            self.links.append(values["href"])

    def handle_data(self, data):
        if self.in_target and self.in_row and self.in_cell and data.strip():
            self.cell_parts.append(data.strip())

    def handle_endtag(self, tag):
        if tag == "td" and self.in_row and self.in_cell:
            self.cells.append(" ".join(self.cell_parts).strip()); self.in_cell = False; self.cell_parts = []
        elif tag == "tr" and self.in_row:
            if self.cells:
                self.rows.append((self.cells[:], self.links[:]))
            self.in_row = False
        elif tag == "tbody" and self.tbody_id is not None and self.in_target:
            self.in_target = False


def _safe_reason(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, urllib.error.URLError):
        return "network_error"
    name = type(exc).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if isinstance(exc, (KeyError, TypeError, ValueError, json.JSONDecodeError)):
        return "parse_error"
    return "provider_error"


def _safe_item_url(platform: str, value) -> str:
    try:
        parsed = urllib.parse.urlsplit(str(value or "").strip())
        host = (parsed.hostname or "").lower().rstrip(".")
        allowed = TREND_LINK_HOSTS.get(platform, set())
        if parsed.scheme != "https" or not host or parsed.username or parsed.password or parsed.fragment:
            return ""
        if not any(host == root or host.endswith("." + root) for root in allowed):
            return ""
        return parsed.geturl()[:2048]
    except ValueError:
        return ""


def _generic_items(obj, *, platform: str = "") -> list[dict]:
    data = obj.get("data") if isinstance(obj, dict) else obj
    if isinstance(data, dict):
        for key in ("data", "list", "items", "realtime", "word_list"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        if isinstance(item, str):
            title = item.strip()
            if not title:
                continue
            url = ""
            encoded = urllib.parse.quote(title)
            if platform == "bilibili":
                url = f"https://search.bilibili.com/all?keyword={encoded}"
            out.append({"title": title, "hot": "", "url": url})
            continue
        if not isinstance(item, dict):
            continue
        title = item.get("title") or item.get("word") or item.get("name") or item.get("keyword")
        if not title:
            continue
        hot = item.get("hot") or item.get("hot_value") or item.get("hotScore") or item.get("num") or ""
        out.append({
            "title": str(title).strip(), "hot": _hot_text(hot),
            "url": str(item.get("url") or item.get("link") or item.get("mobileUrl") or item.get("mobil_url") or ""),
        })
    return out


def _xhs_46la() -> list[dict]:
    parser = _TrendTableParser("hot-list")
    parser.feed(_read_text("https://www.46.la/tool/xiaohongshu-hot"))
    out = []
    for cells, links in parser.rows:
        if len(cells) < 3:
            continue
        title = cells[1].strip()
        if not title:
            continue
        url = links[0] if links else ""
        out.append({"title": title, "hot": cells[2].strip(), "url": url})
    return out[:30]


def _xhs_newrank() -> list[dict]:
    html = _read_text("https://nav.newrank.cn/platformTrend")
    marker = html.find("小红书 · 热点")
    if marker < 0:
        return []
    table_end = html.find("</table>", marker)
    if table_end < 0:
        raise ValueError("parse_error")
    parser = _TrendTableParser()
    parser.feed(html[marker:table_end + len("</table>")])
    out = []
    for cells, _links in parser.rows:
        if len(cells) < 3:
            continue
        title = cells[1].strip()
        if title:
            out.append({"title": title, "hot": cells[2].strip(), "url": ""})
    return out[:30]


def _direct_zhihu() -> list[dict]:
    obj = _read_json("https://api.zhihu.com/topstory/hot-lists/total?limit=50", headers={"Referer": "https://www.zhihu.com/hot"})
    out = []
    for item in obj.get("data", []):
        if not isinstance(item, dict) or not isinstance(item.get("target"), dict):
            continue
        target = item["target"]
        title = str(target.get("title") or "").strip()
        if not title:
            continue
        url = str(target.get("url") or "")
        question_id = url.rstrip("/").split("/")[-1]
        detail = str(item.get("detail_text") or "").strip()
        hot_match = re.match(r"([0-9.]+)\s*万", detail)
        hot = f"{hot_match.group(1)}万" if hot_match else detail
        out.append({"title": title, "hot": hot, "url": f"https://www.zhihu.com/question/{question_id}" if question_id.isdigit() else "https://www.zhihu.com/hot"})
    return out


def _direct_toutiao() -> list[dict]:
    obj = _read_json("https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc", headers={"Referer": "https://www.toutiao.com/"})
    out = []
    for item in obj.get("data", []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("Title") or "").strip()
        cluster = str(item.get("ClusterIdStr") or item.get("ClusterId") or "")
        if title:
            out.append({"title": title, "hot": _hot_text(item.get("HotValue")), "url": str(item.get("Url") or (f"https://www.toutiao.com/trending/{cluster}/" if cluster else ""))})
    return out


def _direct_bilibili() -> list[dict]:
    obj = _read_json("https://api.bilibili.com/x/web-interface/popular?pn=1&ps=30", headers={"Referer": "https://www.bilibili.com/v/popular/all"})
    if obj.get("code") != 0:
        return []
    out = []
    for item in (obj.get("data") or {}).get("list", []):
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        bvid = str(item.get("bvid") or "")
        if title:
            out.append({"title": title, "hot": _hot_text((item.get("stat") or {}).get("view")), "url": str(item.get("short_link_v2") or (f"https://www.bilibili.com/video/{bvid}" if bvid else ""))})
    return out


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    fetch: Callable[[], list[dict]]


@dataclass(frozen=True)
class XhsContext:
    profile: Path | None = None
    lock_path: Path | None = None
    account_id: str = ""
    account_label: str = ""


def _url_provider(key: str, label: str, url: str, platform: str) -> Provider:
    return Provider(key, label, lambda: _generic_items(_read_json(url), platform=platform))


class TrendService:
    def __init__(self, cache_path: Path, private_dir: Path):
        self.cache_path = cache_path
        self.private_dir = private_dir
        self._lock = threading.RLock()
        self._cache = self._load_cache()
        self._failures: dict[str, dict] = {}

    def _load_cache(self):
        try:
            if not self.cache_path.is_file() or self.cache_path.stat().st_size > 4 * 1024 * 1024:
                return {}
            value = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_cache(self):
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.cache_path.with_suffix(".tmp")
        text = json.dumps(self._cache, ensure_ascii=False, indent=2)
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        os.replace(tmp, self.cache_path)

    def _dailyhot(self, platform: str) -> list[Provider]:
        base = os.environ.get("RIPPLE_TRENDS_DAILYHOT_BASE", "").strip().rstrip("/")
        if not base:
            return []
        if not (base.startswith("https://") or base.startswith("http://127.0.0.1") or base.startswith("http://localhost")):
            return []
        return [_url_provider("dailyhot", "自托管 DailyHot", f"{base}/{platform}", platform)]

    def _providers(self, platform: str) -> list[Provider]:
        if platform == "xiaohongshu":
            return [
                Provider("46la-xhs", "四六啦 · 第三方聚合", _xhs_46la),
                Provider("newrank-xhs", "新榜 · 游客热点", _xhs_newrank),
            ]
        direct = {
            "zhihu": [Provider("zhihu-direct", "知乎官方热榜", _direct_zhihu)],
            "toutiao": [Provider("toutiao-direct", "头条官方热榜", _direct_toutiao)],
            "bilibili": [Provider("bilibili-direct", "B站官方热门", _direct_bilibili)],
            "weibo": [], "douyin": [], "baidu": [],
        }
        providers = list(direct.get(platform, []))
        providers += self._dailyhot(platform)
        xxapi = {
            "weibo": "https://v2.xxapi.cn/api/weibohot", "douyin": "https://v2.xxapi.cn/api/douyinhot",
            "bilibili": "https://v2.xxapi.cn/api/bilibilihot", "baidu": "https://v2.xxapi.cn/api/baiduhot",
        }
        if platform in xxapi:
            providers.append(_url_provider("xxapi", "XXAPI 备用源", xxapi[platform], platform))
        sixty = {
            "weibo": "https://60s.viki.moe/v2/weibo", "douyin": "https://60s.viki.moe/v2/douyin",
            "zhihu": "https://60s.viki.moe/v2/zhihu", "bilibili": "https://60s.viki.moe/v2/bili",
            "baidu": "https://60s.viki.moe/v2/baidu/hot", "toutiao": "https://60s.viki.moe/v2/toutiao",
        }
        if platform in sixty:
            providers.append(_url_provider("60s", "60s 备用源", sixty[platform], platform))
        return providers

    def _xhs_fetch(self, context: XhsContext | None) -> list[dict]:
        from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

        channel = browser_channel()
        if not channel:
            raise RuntimeError("browser_unavailable")
        explicit = bool(context and context.profile)
        profile = context.profile if explicit else self.private_dir / "xiaohongshu-anonymous"
        lock_path = context.lock_path if explicit and context.lock_path else self.private_dir / "xiaohongshu-anonymous.lock"
        profile.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(str(lock_path), timeout=0):
                with sync_playwright() as p:
                    browser_type = p.chromium
                    kwargs = {"headless": True, "args": ["--no-first-run", "--no-default-browser-check"], "chromium_sandbox": True}
                    if channel != "chromium":
                        kwargs["channel"] = channel
                    ctx = browser_type.launch_persistent_context(str(profile), **kwargs)
                    try:
                        page = ctx.pages[0] if ctx.pages else ctx.new_page()
                        response = None
                        try:
                            with page.expect_response(lambda r: "/api/sns/web/v1/search/querytrending" in r.url and r.status == 200, timeout=15000) as info:
                                page.goto("https://www.xiaohongshu.com/explore", wait_until="domcontentloaded", timeout=30000)
                                page.wait_for_timeout(1500)
                                page.locator("#search-input, .search-input").first.click(timeout=5000)
                                page.wait_for_timeout(800)
                            response = info.value
                        except PlaywrightTimeoutError:
                            pass
                        url = page.url
                        if "error_code=300012" in url or "/captcha" in url:
                            raise RuntimeError("risk_control")
                        items = []
                        if response:
                            try:
                                payload = response.json()
                                data = payload.get("data") if isinstance(payload, dict) else None
                                if isinstance(data, dict):
                                    candidates = [data.get("items"), data.get("list"), data.get("trendings")]
                                    candidates += [v for v in data.values() if isinstance(v, list)]
                                    raw = next((v for v in candidates if isinstance(v, list) and v), [])
                                    for i, item in enumerate(raw):
                                        if not isinstance(item, dict):
                                            continue
                                        word = item.get("name") or item.get("word") or item.get("keyword") or item.get("title")
                                        if word:
                                            items.append({"title": str(word), "hot": _hot_text(item.get("score") or item.get("hot_value")), "url": f"https://www.xiaohongshu.com/search_result?keyword={urllib.parse.quote(str(word))}"})
                            except Exception:
                                items = []
                        if not items:
                            raw = page.evaluate("""() => { const s=window.__INITIAL_STATE__; const x=s&&s.search&&(s.search.trending||s.search.hotList||s.search.queryTrending); const a=x&&(x._rawValue||x); return Array.isArray(a)?a:[]; }""")
                            if isinstance(raw, list):
                                for item in raw:
                                    if not isinstance(item, dict):
                                        continue
                                    word = item.get("name") or item.get("word") or item.get("keyword") or item.get("title")
                                    if word:
                                        items.append({"title": str(word), "hot": _hot_text(item.get("score") or item.get("hot_value")), "url": f"https://www.xiaohongshu.com/search_result?keyword={urllib.parse.quote(str(word))}"})
                        return items
                    finally:
                        ctx.close()
        except Timeout as exc:
            raise RuntimeError("account_busy") from exc

    def get_group(self, platform: str, limit: int = 15, *, force: bool = False, xhs_context: XhsContext | None = None) -> dict:
        now = time.time()
        ttl = XHS_CACHE_TTL if platform == "xiaohongshu" else CACHE_TTL
        cache_key = platform + (f":account:{xhs_context.account_id}" if platform == "xiaohongshu" and xhs_context and xhs_context.profile and xhs_context.account_id else ":public")
        with self._lock:
            failed = self._failures.get(cache_key)
            if not force and failed and now - failed.get("at", 0) < FAILURE_TTL:
                cached = self._cache.get(cache_key)
                if cached and now - cached.get("fetched_at", 0) <= MAX_STALE:
                    return self._group(platform, cached["items"], "stale", cached.get("source", "缓存"), cached.get("fetched_at", 0), failed.get("attempts", []), limit, error=failed.get("error", ""))
                return self._group(platform, [], "error", "", 0, failed.get("attempts", []), limit, error=failed.get("error", ""))
        with self._lock:
            cached = self._cache.get(cache_key)
            if not force and cached and now - cached.get("fetched_at", 0) < ttl:
                return self._group(platform, cached["items"], "fresh", cached.get("source", "缓存"), cached.get("fetched_at", 0), [], limit, cached=True)
        attempts = []
        if platform == "xiaohongshu" and xhs_context is None:
            for provider in self._providers(platform):
                try:
                    items = provider.fetch()
                    if items:
                        return self._store_and_group(cache_key, platform, items, provider.label, now, attempts, limit)
                    attempts.append({"provider": provider.label, "status": "empty"})
                except Exception as exc:
                    attempts.append({"provider": provider.label, "status": _safe_reason(exc)})
        elif platform == "xiaohongshu":
            try:
                items = self._xhs_fetch(xhs_context)
                if items:
                    return self._store_and_group(cache_key, platform, items, "小红书已连接账号 · 只读", now, attempts, limit)
                attempts.append({"provider": "xiaohongshu-page", "status": "empty"})
            except Exception as exc:
                code = str(exc) if str(exc) in {"risk_control", "account_busy", "browser_unavailable"} else _safe_reason(exc)
                attempts.append({"provider": "xiaohongshu-page", "status": code})
        else:
            for provider in self._providers(platform):
                try:
                    items = provider.fetch()
                    if items:
                        return self._store_and_group(cache_key, platform, items, provider.label, now, attempts, limit)
                    attempts.append({"provider": provider.label, "status": "empty"})
                except Exception as exc:
                    attempts.append({"provider": provider.label, "status": _safe_reason(exc)})
        error = self._error_message(platform, attempts, xhs_context)
        with self._lock:
            self._failures[cache_key] = {"at": now, "attempts": attempts[-5:], "error": error}
        with self._lock:
            cached = self._cache.get(cache_key)
        if cached and now - cached.get("fetched_at", 0) <= MAX_STALE:
            return self._group(platform, cached["items"], "stale", cached.get("source", "缓存"), cached.get("fetched_at", 0), attempts, limit, error=error)
        return self._group(platform, [], "error", "", 0, attempts, limit, error=error)

    def _store_and_group(self, cache_key, platform, items, source, now, attempts, limit):
        clean = [{"title": str(x.get("title", ""))[:300], "hot": str(x.get("hot", ""))[:80], "url": _safe_item_url(platform, x.get("url", ""))} for x in items if x.get("title")]
        with self._lock:
            self._cache[cache_key] = {"fetched_at": now, "source": source, "items": clean[:100]}
            self._failures.pop(cache_key, None)
            self._save_cache()
        return self._group(platform, clean, "fresh", source, now, attempts, limit)

    @staticmethod
    def _error_message(platform, attempts, context):
        if platform == "xiaohongshu":
            codes = {a["status"] for a in attempts}
            if context and context.profile:
                if "risk_control" in codes:
                    return "小红书仍返回风控页面，请稍后重试或重新检查账号登录状态。"
                if "account_busy" in codes:
                    return "该小红书账号正在登录或发布，热点读取暂缓，避免抢占浏览器会话。"
                if "browser_unavailable" in codes:
                    return "未检测到可用浏览器，无法执行已连接账号的只读采样。"
                return "所选小红书账号暂未返回可用热点。"
            return "公开小红书热点源暂不可用；已有缓存会继续标注原抓取时间，不会伪装成刚刚更新。"
        return "当前数据源均未返回可用数据。已有缓存会标记为“缓存数据”，不会伪装成刚刚更新。"

    @staticmethod
    def _group(platform, items, status, source, fetched_at, attempts, limit, *, error="", cached=False):
        return {
            "platform": platform, "label": LABELS.get(platform, platform), "items": items[:max(1, min(limit, 30))],
            "status": status, "source": source, "fetched_at": int(fetched_at), "error": error,
            "attempts": attempts[-5:], "cached": cached,
        }
