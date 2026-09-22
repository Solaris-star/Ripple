"""Browser regression against local Ripple; all mutating requests are intercepted.

Run from the repository root: python scripts/campaign_refresh_smoke.py
A running frontend/backend is required. No platform requests, account profiles,
paid model calls or user-state writes are made by this smoke test.
"""
from __future__ import annotations

import asyncio
import json
import urllib.request
from copy import deepcopy
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright, expect

BASE = 'http://127.0.0.1:7860'
FRONTEND = 'http://127.0.0.1:5173'


def read_json(path: str):
    with urllib.request.urlopen(BASE + path, timeout=10) as response:
        return json.loads(response.read().decode('utf-8'))


async def main():
    source_state = read_json('/api/campaigns/sources')
    campaigns = read_json('/api/campaigns')
    for source in source_state['items']:
        paid = source['platform'] == 'x'
        source['schedule'] = {
            'mode': 'daily_slots' if paid else 'interval' if source['automatic'] else 'manual',
            'timezone': 'Asia/Shanghai', 'times': ['09:00', '14:00', '20:00'],
            'interval_seconds': 0 if paid else source.get('sync_interval_seconds', 3600),
            'cost': 'paid' if paid else 'free_primary', 'next_run_at': source.get('next_sync_at', 0),
        }
    submitted = []
    rejected = []
    page_reads = []
    state = {'empty_xhs': False, 'hold_refresh': False}
    release = asyncio.Event()
    started = asyncio.Event()
    errors = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='msedge', headless=True)
        context = await browser.new_context(viewport={'width': 1600, 'height': 1000})

        async def guard(route):
            request = route.request
            url = urlsplit(request.url)
            if url.hostname not in {'127.0.0.1', 'localhost'}:
                rejected.append((request.method, url.path))
                return await route.abort()
            if url.path == '/api/campaigns/refresh' and request.method == 'POST':
                data = request.post_data_json
                submitted.append(data)
                started.set()
                if state['hold_refresh']:
                    await release.wait()
                return await route.fulfill(json={
                    'results': [{'platform': platform, 'status': 'fresh', 'count': 0} for platform in data['platforms']],
                    'sources': source_state, 'merged': 0, 'total': len(campaigns),
                    'stale_platforms': [], 'campaigns': campaigns,
                })
            if request.method not in {'GET', 'HEAD'}:
                rejected.append((request.method, url.path))
                return await route.abort()
            if url.path == '/api/campaigns/sources':
                return await route.fulfill(json=source_state)
            if url.path == '/api/campaigns/page':
                page_reads.append(parse_qs(url.query))
                if state['empty_xhs'] and parse_qs(url.query).get('platform') == ['xiaohongshu']:
                    return await route.fulfill(json={
                        'items': [], 'total': 0, 'source_total': 0, 'page': 1, 'page_size': 10,
                        'total_pages': 1, 'range_start': 0, 'range_end': 0,
                        'sort': 'default', 'platform': 'xiaohongshu', 'account_id': '',
                        'snapshot_id': 'smoke-empty', 'fetched_at': '', 'source_status': 'fresh',
                        'truncated': False, 'missing_source_items': 0,
                        'stats': {'active': 0, 'soon': 0, 'saved': 0},
                        'filter_options': {'activity_types': [], 'reward_types': []},
                    })
            await route.continue_()

        await context.route('**/*', guard)
        page = await context.new_page()
        page.on('pageerror', lambda error: errors.append(str(error)))
        try:
            await page.goto(FRONTEND, wait_until='domcontentloaded')
            await page.get_by_role('button', name='选题中心', exact=False).first.click()
            await page.get_by_role('button', name='活动广场', exact=False).first.click()
            await expect(page.locator('.campaign-source-status')).to_have_count(6)
            await expect(page.get_by_text('Agent 补全缺失规则', exact=False)).to_have_count(0)
            await expect(page.locator('.campaign-source-status').filter(has=page.get_by_text('X', exact=True))).to_contain_text('09:00 / 14:00 / 20:00')
            platform_select = page.locator('.campaign-filters select').first

            await platform_select.select_option('douyin')
            await expect(page.locator('.campaign-empty')).to_contain_text('抖音活动源尚未就绪')
            await expect(page.get_by_role('button', name='刷新抖音活动', exact=True)).to_be_disabled()
            await expect(page.locator('.campaign-empty').get_by_role('button', name='配置抖音活动源')).to_be_visible()
            assert not submitted

            await platform_select.select_option('xiaohongshu')
            await expect(page.locator('.campaign-card')).to_have_count(10)
            await page.get_by_role('button', name='刷新小红书活动', exact=True).click()
            await expect(page.get_by_role('button', name='刷新小红书活动', exact=True)).to_be_enabled()
            assert submitted[-1] == {'platforms': ['xiaohongshu'], 'force': True, 'allow_paid': False}

            state['empty_xhs'] = True
            await platform_select.select_option('bilibili')
            await platform_select.select_option('xiaohongshu')
            await expect(page.locator('.campaign-empty')).to_be_visible()
            await page.locator('.campaign-empty').get_by_role('button', name='刷新小红书活动', exact=True).click()
            await expect(page.locator('.campaign-empty').get_by_role('button', name='刷新小红书活动', exact=True)).to_be_enabled()
            assert submitted[-1]['platforms'] == ['xiaohongshu']

            state['empty_xhs'] = False
            await platform_select.select_option('bilibili')
            await platform_select.select_option('xiaohongshu')
            await expect(page.locator('.campaign-card')).to_have_count(10)
            state['hold_refresh'] = True
            started.clear()
            await page.get_by_role('button', name='刷新小红书活动', exact=True).click()
            await asyncio.wait_for(started.wait(), 5)
            await platform_select.select_option('bilibili')
            await expect(page.get_by_role('button', name='刷新B站活动', exact=True)).to_be_enabled()
            release.set()
            await page.wait_for_timeout(600)
            await expect(platform_select).to_have_value('bilibili')
            state['hold_refresh'] = False

            await platform_select.select_option('x')
            button = page.get_by_role('button', name='刷新X活动', exact=True)
            await expect(button).to_have_attribute('title', '当前范围包含收费来源，手动刷新会额外消耗 Token / 接口额度。')
            await button.click()
            await expect(button).to_be_enabled()
            assert submitted[-1] == {'platforms': ['x'], 'force': True, 'allow_paid': True}

            await platform_select.select_option('all')
            await page.get_by_role('button', name='刷新全部活动', exact=True).click()
            await expect(page.get_by_role('button', name='刷新全部活动', exact=True)).to_be_enabled()
            assert set(submitted[-1]['platforms']) == {s['platform'] for s in source_state['items'] if s['automatic']}
            assert not rejected, rejected
            assert not errors, errors
            print(json.dumps({'status': 'passed', 'checks': [
                'global Agent entry removed', 'paid schedule shown', 'unconfigured platform has no refresh',
                'header refresh scoped', 'empty-state refresh scoped', 'platform switch ignores stale response',
                'paid manual warning', 'explicit all-platform scope', '10-card page preserved',
            ], 'intercepted_refresh_requests': submitted, 'external_or_unexpected_writes': rejected}, ensure_ascii=False))
        finally:
            release.set()
            await browser.close()


if __name__ == '__main__':
    asyncio.run(main())
