"""Browser checks against a running Ripple. Never starts a real login or refresh.

Activity checks read the previously synchronized list. QR lifecycle uses a fake
account and synthetic image; all non-GET API calls are intercepted. Real platform
QR capture is separately validated in an anonymous, temporary browser profile.
"""
from __future__ import annotations

import asyncio
from io import BytesIO
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

import segno
from playwright.async_api import async_playwright, expect

BASE = "http://127.0.0.1:7860"
OUT = Path(__file__).resolve().parents[1] / "artifacts/douyin-official-20260923"


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    report = {"checks": [], "page_errors": []}
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="msedge", headless=True)
        context = await browser.new_context(viewport={"width": 1440, "height": 1100}, locale="zh-CN")
        blocked = []

        async def readonly(route):
            if re.fullmatch(r"/api/agent/sessions/[^/]+/config", urlsplit(route.request.url).path) and route.request.method != "GET":
                return await route.fulfill(json={})  # Unrelated bootstrap write is mocked too.
            if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                blocked.append(urlsplit(route.request.url).path)
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/api/**", readonly)
        page = await context.new_page()
        page.on("pageerror", lambda e: report["page_errors"].append(str(e)))
        await page.goto(BASE, wait_until="domcontentloaded")
        await page.get_by_role("button", name="选题中心", exact=True).click()
        await page.get_by_role("button", name="活动广场", exact=True).click()
        await page.locator(".campaign-filters select").first.select_option("douyin")
        total = page.locator(".campaign-sort > span")
        await expect(total).to_contain_text("共 13 个活动")
        await expect(page.locator(".campaign-card")).to_have_count(10)
        await expect(page.locator(".campaign-list")).to_contain_text("展示期，投稿截止未核实")
        assert await page.locator(".campaign-card h3").filter(has_text="摄影摄像").count() == 0
        await expect(page.get_by_role("button", name="刷新抖音活动", exact=True)).to_be_enabled()
        await expect(page.locator(".campaign-source-status").filter(has_text="抖音")).to_contain_text("2026-09-01～2026-09-30")
        report["checks"].append("real calendar scope, 13 distinct activities, 10-row first page, no taxonomy card")
        await page.get_by_role("button", name="下一页", exact=True).click()
        await expect(total).to_contain_text("2/2 页")
        await expect(page.locator(".campaign-card")).to_have_count(3)
        report["checks"].append("second page contains the remaining three records")
        search = page.get_by_role("searchbox", name="搜索活动", exact=True)
        await search.fill("2026抖音创作者大会")
        await search.press("Enter")
        await expect(total).to_contain_text("共 2 个活动")
        await expect(page.locator(".campaign-card")).to_have_count(2)
        await expect(total).to_contain_text("1/1 页")
        await page.locator(".campaign-list").screenshot(path=str(OUT / "douyin-same-title-records.png"))
        report["checks"].append("same-title distinct official IDs survive search and reset pagination")
        await page.locator(".campaign-card").first.get_by_role("button", name="查看详情", exact=True).click()
        drawer = page.locator(".campaign-drawer")
        await expect(drawer).to_contain_text("展示日期不等同报名或投稿截止日期")
        await expect(drawer).to_contain_text("详情含图片，规则待核实")
        await expect(drawer.locator(".campaign-source-link")).to_have_attribute("href", re.compile(r"^https://api\.amemv\.com/magic/eco/runtime/release/"))
        await drawer.locator("header > button").click()
        await page.get_by_role("button", name="清空活动搜索", exact=True).click()
        await expect(page.locator(".campaign-card")).to_have_count(10)
        app_card = page.locator(".campaign-card").filter(has_text="请在抖音 App 内查看").first
        await app_card.get_by_role("button", name="查看详情", exact=True).click()
        await expect(drawer).to_contain_text("官方列表未提供可打开的网页详情")
        assert await drawer.get_by_role("link", name="打开原活动页", exact=True).count() == 0
        await drawer.screenshot(path=str(OUT / "douyin-app-only-detail.png"))
        await drawer.locator("header > button").click()
        report["checks"].append("public detail links are real; App-only records never link to a fake homepage")
        await page.set_viewport_size({"width": 480, "height": 900})
        await page.locator(".campaign-search").scroll_into_view_if_needed()
        assert await page.locator(".campaign-search").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        assert not blocked, blocked
        await context.close()

        # Account operations below are browser-only mocks, never real credentials.
        context = await browser.new_context(viewport={"width": 1280, "height": 1000}, locale="zh-CN")
        state = {"login_state": "starting", "qr_available": False, "qr_revision": "", "message": "正在打开抖音登录窗口并读取二维码…"}
        account = {"id": "1" * 32, "platform": "douyin", "label": "二维码回归测试账号", "status": "connected",
                   "auth_revision": 1, "identity": {"logged_in": True, "name": "测试账号", "remote_id": "12345"},
                   "checked_at": None, "message": "测试账号", "live_verified": False, "operation": None,
                   "execution_node_id": "local", "profile_id": "1" * 32, "browser_channel": "msedge", "adapter": "native",
                   "login_url": "https://creator.douyin.com/"}
        operation = {"id": "2" * 32, "kind": "login", "state": "running", "started_at": "2026-09-23T10:00:00Z", "browser_channel": "msedge"}
        active = False
        mock_posts, qr_requests, unsafe = [], [], []
        image = BytesIO()
        segno.make("Ripple synthetic QR test - not a login", micro=False).save(image, kind="png", scale=5)

        async def mock_api(route):
            nonlocal active
            path = urlsplit(route.request.url).path
            if re.fullmatch(r"/api/agent/sessions/[^/]+/config", path) and route.request.method != "GET":
                return await route.fulfill(json={})
            if path == "/api/ripple/accounts" and route.request.method == "GET":
                row = {**account}
                if active:
                    row.update(state)
                    row["status"] = "connected" if state["login_state"] == "success" else "connecting"
                    row["operation"] = {**operation, "state": "finished" if state["login_state"] == "success" else "running"}
                return await route.fulfill(json=[row])
            if path == f"/api/ripple/accounts/{account['id']}/login" and route.request.method == "POST":
                active = True
                mock_posts.append(path)
                return await route.fulfill(json={**account, **state, "operation": operation})
            if path.startswith(f"/api/ripple/accounts/{account['id']}/qr/"):
                qr_requests.append(route.request.url)
                return await route.fulfill(status=200, content_type="image/png", body=image.getvalue(), headers={"Cache-Control": "no-store"})
            if route.request.method not in {"GET", "HEAD", "OPTIONS"}:
                unsafe.append(path)
                return await route.abort()
            await route.continue_()

        await context.route("**/api/**", mock_api)
        page = await context.new_page()
        page.on("pageerror", lambda e: report["page_errors"].append(str(e)))
        page.on("dialog", lambda dialog: dialog.accept())
        await page.goto(BASE, wait_until="domcontentloaded")
        await page.get_by_role("button", name="账号与平台", exact=True).click()
        await page.locator('.r2-channel[data-platform="douyin"]').get_by_role("button", name="重新登录", exact=True).click()
        dialog = page.get_by_role("dialog", name="登录 · 二维码回归测试账号", exact=True)
        await expect(dialog).to_contain_text("正在读取平台二维码")
        await expect(dialog.locator(".r2-login-qr")).to_have_count(0)
        state.update(login_state="qr_ready", qr_available=True, qr_revision="a" * 16, message="请使用抖音 App 扫码，并在手机上确认登录。")
        qr = dialog.locator(".r2-login-qr")
        await expect(qr).to_be_visible(timeout=7000)
        await page.wait_for_function("document.querySelector('.r2-login-qr')?.naturalWidth > 0")
        await expect(qr).to_have_attribute("src", re.compile(r"\?v=a{16}$"))
        await dialog.screenshot(path=str(OUT / "douyin-qr-dialog-synthetic.png"))
        state.update(qr_revision="b" * 16)
        await expect(qr).to_have_attribute("src", re.compile(r"\?v=b{16}$"), timeout=7000)
        state.update(login_state="scanned", qr_available=False, qr_revision="", message="已扫码，请在手机上完成登录确认。")
        await expect(qr).to_have_count(0, timeout=7000)
        await expect(dialog).to_contain_text("无需再次扫码")
        state.update(login_state="expired", message="二维码或登录等待已超时，请重新打开登录。")
        await expect(dialog).to_contain_text("已超时", timeout=7000)
        await expect(qr).to_have_count(0)
        state.update(login_state="success", message="抖音登录成功，账号身份已核验。")
        await expect(dialog.locator(".r2-connection-success")).to_contain_text("已连接", timeout=7000)
        assert len(mock_posts) == 1 and len(qr_requests) >= 2 and not unsafe
        report["checks"].append("mock login shows startup, QR, rotation, scanned, expired and success states without touching the real account")
        assert not report["page_errors"], report["page_errors"]
        report.update(passed=True, real_mutations=0, mocked_login_requests=len(mock_posts), qr_image_requests=len(qr_requests))
        (OUT / "browser-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
