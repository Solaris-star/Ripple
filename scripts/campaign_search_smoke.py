"""Read-only live/browser regressions for activity search and platform switching.

Run against an already running local Ripple service. No refresh/enrichment/publish
endpoint is called; controlled delays and failed requests are browser-route mocks.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect

BASE = "http://127.0.0.1:7860"
OUT = Path(__file__).resolve().parents[1] / "artifacts" / "campaign-search-switch"
PLATFORMS = ["x", "xiaohongshu", "douyin", "bilibili", "wechat", "weixin-channels"]


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    report: dict = {"checks": [], "switches": []}
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN")
        page = await context.new_page()
        page_errors: list[str] = []
        requests: list[dict] = []
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        def record(request):
            url = urlsplit(request.url)
            if url.path.startswith("/api/campaigns") or url.path == "/api/status":
                requests.append({"path": url.path, "query": parse_qs(url.query), "method": request.method})
        page.on("request", record)
        await page.goto(BASE, wait_until="domcontentloaded")
        await page.get_by_role("button", name="选题中心", exact=True).click()
        await page.get_by_role("button", name="活动广场", exact=True).wait_for()

        # A source-status response may be arbitrarily slow: activity rows must render first.
        source_release = asyncio.Event()
        source_started = asyncio.Event()
        async def hold_sources(route):
            source_started.set()
            await source_release.wait()
            await route.continue_()
        await page.route("**/api/campaigns/sources", hold_sources)
        start = time.perf_counter()
        await page.get_by_role("button", name="活动广场", exact=True).click()
        await expect(page.locator(".campaign-card").first).to_be_visible(timeout=4000)
        report["first_list_ms_with_sources_held"] = round((time.perf_counter() - start) * 1000, 1)
        assert source_started.is_set() and not source_release.is_set()
        report["checks"].append("activity list renders while source metadata is still blocked")
        source_release.set()
        await expect(page.locator(".campaign-source-hit")).to_have_count(6, timeout=5000)
        await page.unroute("**/api/campaigns/sources", hold_sources)

        platform_select = page.locator(".campaign-filters select").first
        summary = page.locator(".campaign-sort > span")
        search = page.get_by_role("searchbox", name="搜索活动", exact=True)

        async def settled(platform: str) -> None:
            await expect(platform_select).to_have_value(platform)
            await expect(page.locator(".campaign-list")).to_have_attribute("aria-busy", "false", timeout=5000)
            await expect(summary).to_contain_text(re.compile(r"共 \d+ 个活动"))
            assert await page.locator(".notice-error").count() == 0
            if platform != "all":
                wrong = page.locator(f".campaign-card .campaign-platform:not(.platform-{platform})")
                assert await wrong.count() == 0, f"stale cards from another platform under {platform}"

        baseline = len(requests)
        for pass_name in ["first_visit", "cached_return"]:
            for platform in PLATFORMS:
                before = len(requests)
                start = time.perf_counter()
                await platform_select.select_option(platform)
                await settled(platform)
                elapsed = round((time.perf_counter() - start) * 1000, 1)
                new_requests = requests[before:]
                list_count = sum(row["path"] == "/api/campaigns/page" for row in new_requests)
                report["switches"].append({"platform": platform, "pass": pass_name, "elapsed_ms": elapsed, "list_requests": list_count})
                if pass_name == "cached_return":
                    assert list_count == 0, (platform, new_requests)
        switch_requests = requests[baseline:]
        assert all(row["path"] == "/api/campaigns/page" and row["method"] == "GET" for row in switch_requests)
        report["checks"].append("six platform switches request only their list; six cached returns make zero requests")

        # Search a real official XHS row from page 2, not present on page 1.
        result = await context.request.get(BASE + "/api/campaigns/page?platform=xiaohongshu&sort=default&page=2")
        assert result.status == 200
        second_page = await result.json()
        target = second_page["items"][0]["title"]
        await platform_select.select_option("xiaohongshu")
        await settled("xiaohongshu")
        await page.get_by_role("button", name="下一页", exact=True).click()
        await expect(summary).to_contain_text(f"2/{second_page['total_pages']} 页")
        await search.fill(target)
        await search.press("Enter")
        await expect(summary).to_contain_text(target)
        await settled("xiaohongshu")
        await expect(page.locator(".campaign-card h3").filter(has_text=target).first).to_be_visible()
        await expect(summary).to_contain_text("1/")
        report["checks"].append("XHS keyword finds a real page-2 activity and resets search pagination to page 1")
        report["searched_off_page_title"] = target
        await page.screenshot(path=str(OUT / "search-result-desktop.png"), full_page=False)

        # Search follows platform scope and clear restores the full platform list.
        await platform_select.select_option("bilibili")
        await settled("bilibili")
        await expect(search).to_have_value(target)
        assert requests[-1]["query"].get("platform") == ["bilibili"]
        assert requests[-1]["query"].get("q") == [target]
        await page.get_by_role("button", name="清空活动搜索", exact=True).click()
        await settled("bilibili")
        await expect(search).to_have_value("")
        report["checks"].append("keyword stays in input but is scoped to the selected platform; clear restores all local rows")

        for keyword in ["all", "__没有匹配活动_测试__"]:
            async with page.expect_request(lambda req: urlsplit(req.url).path == "/api/campaigns/page" and parse_qs(urlsplit(req.url).query).get("q") == [keyword]):
                await search.fill(keyword)
                await search.press("Enter")
            await settled("bilibili")
        await expect(summary).to_contain_text("共 0 个活动")
        await expect(page.locator(".campaign-empty strong")).to_contain_text("未找到")
        assert await page.locator(".campaign-pagination").count() == 0
        report["checks"].append("literal all is sent as a keyword; empty results have zero count and no stale pagination")

        # Chinese IME should never send its intermediate composing value.
        await page.get_by_role("button", name="清空活动搜索", exact=True).click()
        await settled("bilibili")
        await search.dispatch_event("compositionstart")
        before = len(requests)
        await search.fill("科技")
        await page.wait_for_timeout(400)
        assert len(requests) == before
        async with page.expect_request(lambda req: parse_qs(urlsplit(req.url).query).get("q") == ["科技"]):
            await search.dispatch_event("compositionend")
        await settled("bilibili")
        report["checks"].append("Chinese composition does not search until compositionend")

        # Keep one list in flight, change platform and release it late.
        await page.get_by_role("button", name="清空活动搜索", exact=True).click()
        await settled("bilibili")
        pending = asyncio.Event()
        release = asyncio.Event()
        async def hold_old_list(route):
            query = parse_qs(urlsplit(route.request.url).query)
            if query.get("platform") == ["xiaohongshu"] and query.get("q") == ["__race_scope__"]:
                pending.set()
                await release.wait()
                try:
                    await route.continue_()
                except Exception:
                    pass  # An aborted browser request is the intended cancellation path.
            else:
                await route.continue_()
        await page.route("**/api/campaigns/page?*", hold_old_list)
        await search.fill("__race_scope__")
        await search.press("Enter")
        await settled("bilibili")
        await platform_select.select_option("xiaohongshu")
        await asyncio.wait_for(pending.wait(), timeout=5)
        assert await page.locator(".campaign-card").count() == 0
        assert "共 0 个活动" not in await summary.inner_text(), "loading was presented as an empty result"
        await platform_select.select_option("wechat")
        await settled("wechat")
        release.set()
        await page.wait_for_timeout(250)
        await settled("wechat")
        await page.unroute("**/api/campaigns/page?*", hold_old_list)
        report["checks"].append("rapid switching cancels stale requests; a late XHS response cannot overwrite WeChat")

        # Server/network failure is not a successful zero-result response.
        async def fail_search(route):
            if parse_qs(urlsplit(route.request.url).query).get("q") == ["__error_probe__"]:
                await route.fulfill(status=503, content_type="application/json", body=json.dumps({"detail": "搜索服务临时不可用"}, ensure_ascii=False))
            else:
                await route.continue_()
        await page.route("**/api/campaigns/page?*", fail_search)
        await search.fill("__error_probe__")
        await search.press("Enter")
        await expect(page.locator(".notice-error")).to_contain_text("搜索服务临时不可用")
        assert "共 0 个活动" not in await summary.inner_text()
        await page.unroute("**/api/campaigns/page?*", fail_search)
        await page.get_by_role("button", name="搜索", exact=True).click()
        await settled("wechat")
        await expect(summary).to_contain_text("共 0 个活动")
        report["checks"].append("the search button can retry an unchanged keyword after a temporary server failure")
        await page.get_by_role("button", name="清空活动搜索", exact=True).click()
        await settled("wechat")
        report["checks"].append("failed search displays the real error rather than a false zero count")

        await platform_select.select_option("xiaohongshu")
        await settled("xiaohongshu")
        await page.screenshot(path=str(OUT / "platform-desktop.png"), full_page=False)
        await page.set_viewport_size({"width": 480, "height": 900})
        await page.locator(".campaign-search").scroll_into_view_if_needed()
        await page.screenshot(path=str(OUT / "search-mobile.png"), full_page=False)
        assert await page.locator(".campaign-search").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        assert not page_errors, page_errors
        assert all(row["method"] == "GET" for row in requests), "Browsing unexpectedly made a campaign write request"
        report["checks"].append("responsive search form has no horizontal overflow; no page exceptions or campaign write requests")
        report["page_errors"] = page_errors
        report["campaign_requests"] = len([row for row in requests if row["path"].startswith("/api/campaigns")])
        report["passed"] = True
        (OUT / "browser-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
