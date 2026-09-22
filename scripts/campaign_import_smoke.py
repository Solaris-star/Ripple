"""Browser smoke for generic campaign import. All writes are intercepted."""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

from playwright.async_api import async_playwright, expect


async def main():
    submitted = []
    blocked = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        context = await browser.new_context(viewport={"width": 1600, "height": 1000})

        async def guard(route):
            request = route.request
            parsed = urlsplit(request.url)
            if parsed.hostname not in {"127.0.0.1", "localhost"}:
                blocked.append((request.method, parsed.netloc, parsed.path))
                return await route.abort()
            if parsed.path == "/api/campaigns/import/preview" and request.method == "POST":
                payload = request.post_data_json
                submitted.append(payload)
                platform = payload["target_platform"]
                return await route.fulfill(json={
                    "draft": {
                        "title": "导入预览",
                        "platform": platform,
                        "organizer": "测试主办方",
                        "organizer_type": "platform",
                        "activity_type": "长期创作变现计划" if platform in {"wechat", "weixin-channels"} else "创作活动",
                        "reward_type": "", "reward_summary": "", "summary": "",
                        "starts_at": "", "signup_deadline": "", "submit_deadline": "", "stats_deadline": "",
                        "timezone": "", "eligibility": [], "qualification_state": "unknown",
                        "content_requirements": [], "reward_rules": [], "prizes": [], "winning_conditions": [],
                        "required_topics": [], "submission_spec": {
                            "formats": [], "content_directions": [], "style_requirements": [],
                            "duration_seconds": {"min": None, "max": None}, "aspect_ratios": [], "resolutions": [],
                            "orientation": None, "image_count": {"min": None, "max": None},
                            "text_length": {"min": None, "max": None},
                            "live": {"min_duration_seconds": None, "required_category": "", "title_keywords": []},
                            "original_required": None, "first_publish_required": None, "exclusive_required": None,
                            "min_entries": None, "max_entries": None, "submission_method": "",
                            "required_mentions": [], "required_music": [],
                        }, "ai_policy": "unknown",
                        "source_url": payload.get("url") or "", "note": payload.get("text") or "",
                        "status": "unknown", "account_id": "",
                    },
                    "agent_used": False, "model": "", "warning": "smoke preview",
                    "evidence_fingerprint": "fixture", "detected_platform": platform, "field_evidence": {},
                })
            if request.method not in {"GET", "HEAD"}:
                blocked.append((request.method, parsed.netloc, parsed.path))
                return await route.abort()
            await route.continue_()

        await context.route("**/*", guard)
        page = await context.new_page()
        await page.goto("http://127.0.0.1:5173", wait_until="domcontentloaded")
        await page.get_by_role("button", name="选题中心", exact=False).first.click()
        await page.get_by_role("button", name="活动广场", exact=False).first.click()
        await expect(page.locator(".campaign-source-status")).to_have_count(6)

        platform = page.locator(".campaign-filters select").first
        await platform.select_option("wechat")
        await page.get_by_role("button", name="补充导入", exact=True).first.click()
        modal = page.locator(".campaign-import-url-modal")
        await expect(modal).to_be_visible()
        await expect(modal.get_by_text("B站活动 URL", exact=True)).to_have_count(0)
        selects = modal.locator("select")
        await expect(selects.nth(0)).to_have_value("wechat")
        await expect(selects.nth(1)).to_have_value("url")
        await expect(modal.get_by_text("使用默认 Agent", exact=False)).to_be_visible()
        checkbox = modal.locator('input[type="checkbox"]')
        assert not await checkbox.is_checked()
        await modal.locator('input[type="url"]').fill("https://ad.weixin.qq.com/docs/45")
        await modal.get_by_role("button", name="生成草稿").click()
        await expect(page.locator(".campaign-import-modal")).to_be_visible()
        form_platform = page.locator(".campaign-import-modal select").first
        await expect(form_platform).to_have_value("wechat")
        await page.locator(".campaign-import-modal .campaign-modal-head button").click()

        await platform.select_option("weixin-channels")
        await page.get_by_role("button", name="补充导入", exact=True).first.click()
        modal = page.locator(".campaign-import-url-modal")
        await modal.locator("select").nth(1).select_option("text")
        await expect(modal.locator("select").nth(0)).to_have_value("weixin-channels")
        await modal.locator("textarea").fill("视频号创作分成计划\n公开规则原文")
        await modal.get_by_role("button", name="生成草稿").click()
        await expect(page.locator(".campaign-import-modal select").first).to_have_value("weixin-channels")

        assert submitted[0] == {
            "target_platform": "wechat", "input_kind": "url",
            "url": "https://ad.weixin.qq.com/docs/45", "text": "", "allow_agent": False,
        }
        assert submitted[1] == {
            "target_platform": "weixin-channels", "input_kind": "text",
            "url": "", "text": "视频号创作分成计划\n公开规则原文", "allow_agent": False,
        }
        assert not blocked, blocked
        print(json.dumps({
            "status": "passed",
            "checks": [
                "current platform inherited", "Bilibili-only label removed", "URL and pasted-text modes",
                "Agent default off", "preview does not create campaign", "returned draft keeps platform",
            ],
            "requests": submitted,
        }, ensure_ascii=False))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
