"""Read-only Douyin Creator Center activity discovery using an isolated Ripple profile."""
from __future__ import annotations

from pathlib import Path
from typing import Any


class DouyinBrowserError(RuntimeError):
    pass


EVENTS_JS = r"""(limit) => {
  const out=[]; const seen=new Set();
  const nodes=Array.from(document.querySelectorAll("a[href], [class*=activity], [class*=task], [class*=mission], [class*=card]"));
  for(const node of nodes) {
    if(out.length>=limit) break;
    const text=(node.innerText||node.textContent||'').replace(/\s+/g,' ').trim();
    if(!text || text.length<4 || !/(活动|征稿|激励|创作|任务|招募|挑战|话题)/.test(text)) continue;
    const a=node.matches?.('a[href]') ? node : node.querySelector?.('a[href]');
    const href=a?.href||'';
    const title=(node.querySelector?.("[class*=title],[class*=name],h1,h2,h3,h4")?.textContent||text.split('  ')[0]||text).trim().slice(0,200);
    const key=(href||title).toLowerCase(); if(!key||seen.has(key)) continue; seen.add(key);
    out.push({title, url:href, text:text.slice(0,1600)});
  }
  return out;
}"""


def _launch(directory: Path, *, headed: bool = False, browser_channel: str | None = None):
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    profile = directory / "browser" / "DouyinProfile"
    profile.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "headless": not headed, "locale": "zh-CN",
        "args": ["--no-first-run", "--no-default-browser-check"], "chromium_sandbox": True,
    }
    if browser_channel in {"msedge", "chrome", "chromium"}:
        kwargs["channel"] = browser_channel
    context = None
    try:
        context = p.chromium.launch_persistent_context(str(profile), **kwargs)
        if context is None:
            raise DouyinBrowserError("browser_launch_failed")
        page = context.pages[0] if context.pages else context.new_page()
        return p, context, page
    except Exception:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        p.stop()
        raise


def _close(p, context) -> None:
    try:
        context.close()
    finally:
        p.stop()


def _login_required(page) -> bool:
    url = (page.url or "").lower()
    body = ""
    try:
        body = (page.locator("body").inner_text(timeout=1200) or "")[:5000]
    except Exception:
        pass
    return "login" in url or ("登录" in body and ("扫码" in body or "手机号" in body))


def _candidates(value: Any, *, limit: int = 60) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    title_keys = ("activity_name", "activityName", "task_name", "taskName", "mission_name",
                  "missionName", "event_name", "eventName", "title", "name")
    id_keys = ("activity_id", "activityId", "task_id", "taskId", "mission_id", "missionId", "id")
    start_keys = ("start_time", "startTime", "begin_time", "beginTime", "start_at", "startAt")
    end_keys = ("end_time", "endTime", "deadline", "expire_time", "expireTime", "submit_deadline")
    url_keys = ("jump_url", "jumpUrl", "url", "link", "h5_url", "h5Url")
    desc_keys = ("description", "desc", "summary", "sub_title", "subTitle")

    def walk(node: Any, depth: int = 0) -> None:
        if len(rows) >= limit or depth > 7:
            return
        if isinstance(node, list):
            for child in node[:250]:
                walk(child, depth + 1)
            return
        if not isinstance(node, dict):
            return
        title = next((str(node.get(k) or "").strip() for k in title_keys if str(node.get(k) or "").strip()), "")
        activityish = any(k in node for k in id_keys + start_keys + end_keys) or any(
            word in title for word in ("活动", "征稿", "激励", "创作", "任务", "招募", "挑战")
        )
        if title and activityish and len(title) <= 300:
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


def _identity(page, fallback: str = "") -> dict[str, Any]:
    if "creator.douyin.com" not in (page.url or "").lower() or _login_required(page):
        return {"logged_in": False, "name": "", "remote_id": ""}
    name = ""
    for selector in ("[class*=user-name]", "[class*=nickname]", "[class*=account-name]", "[class*=creator-name]"):
        try:
            value = (page.locator(selector).first.inner_text(timeout=500) or "").strip()
            if value:
                name = value[:80]
                break
        except Exception:
            pass
    return {"logged_in": True, "name": name or str(fallback or "抖音创作者")[:80], "remote_id": ""}


def login(directory: Path, *, headed: bool = True, browser_channel: str | None = None,
          timeout: int = 240, fallback_name: str = "") -> dict[str, Any]:
    import time
    p, context, page = _launch(directory, headed=headed, browser_channel=browser_channel)
    try:
        page.goto("https://creator.douyin.com/creator-micro/home", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1800)
        if _login_required(page):
            if not headed:
                raise DouyinBrowserError("login_required")
            deadline = time.monotonic() + max(10, min(timeout, 300))
            while time.monotonic() < deadline:
                page.wait_for_timeout(1000)
                if not _login_required(page):
                    break
        identity = _identity(page, fallback_name)
        if not identity["logged_in"]:
            raise DouyinBrowserError("login_required")
        return identity
    finally:
        _close(p, context)


def probe(directory: Path, *, browser_channel: str | None = None, fallback_name: str = "") -> dict[str, Any]:
    p, context, page = _launch(directory, headed=False, browser_channel=browser_channel)
    try:
        page.goto("https://creator.douyin.com/creator-micro/home", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1600)
        return _identity(page, fallback_name)
    finally:
        _close(p, context)


def events(directory: Path, limit: int = 30, *, browser_channel: str | None = None) -> dict[str, Any]:
    p, context, page = _launch(directory, browser_channel=browser_channel)
    payloads: list[Any] = []
    try:
        def on_response(response):
            lower = response.url.lower()
            if not any(token in lower for token in ("activity", "mission", "task", "inspire", "campaign")):
                return
            try:
                if "json" in (response.headers.get("content-type") or "").lower():
                    value = response.json()
                    if isinstance(value, (dict, list)) and len(payloads) < 50:
                        payloads.append(value)
            except Exception:
                pass

        page.on("response", on_response)
        page.goto("https://creator.douyin.com/creator-micro/home", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(2600)
        if _login_required(page):
            raise DouyinBrowserError("login_required")

        # A read-only navigation to a visible activity/task entry can trigger the
        # Creator Center's activity API without depending on a hard-coded endpoint.
        for label in ("活动", "热门活动", "任务中心", "创作任务", "激励"):
            try:
                link = page.get_by_text(label, exact=False).first
                if link.count() and link.is_visible():
                    link.click()
                    page.wait_for_timeout(1800)
                    break
            except Exception:
                pass

        if _login_required(page):
            raise DouyinBrowserError("login_required")

        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for payload in payloads:
            for row in _candidates(payload, limit=max(30, limit * 2)):
                key = (row.get("external_id") or row.get("url") or row.get("title") or "").casefold()
                if key and key not in seen:
                    seen.add(key); items.append(row)
                    if len(items) >= limit: break
            if len(items) >= limit: break
        source = "creator_activity_api"
        if not items:
            source = "creator_activity_dom"
            raw = page.evaluate(EVENTS_JS, max(1, min(limit, 50))) or []
            for row in raw:
                if not isinstance(row, dict):
                    continue
                title = str(row.get("title") or "").strip()[:240]
                if not title:
                    continue
                items.append({
                    "external_id": "",
                    "title": title,
                    "url": str(row.get("url") or "")[:2048],
                    "starts_at": "",
                    "ends_at": "",
                    "description": str(row.get("text") or "")[:3000],
                })
        return {"items": items[:limit], "source": source, "page_url": (page.url or "")[:2048]}
    finally:
        _close(p, context)


def run(action: str, directory: Path, params: dict[str, Any]) -> dict[str, Any]:
    if action != "events":
        raise DouyinBrowserError("unsupported_action")
    limit = max(1, min(int(params.get("limit") or 30), 50))
    return events(directory, limit, browser_channel=str(params.get("browser_channel") or "") or None)
