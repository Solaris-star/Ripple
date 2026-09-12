"""Trend provider contracts. No test here uses a real social login or browser profile."""
from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from ripple import trends
from ripple.trends import Provider, TrendService, XhsContext, _generic_items


def service(tmp_path):
    return TrendService(tmp_path / "cache.json", tmp_path / "private")


def test_generic_parser_accepts_string_arrays_for_bilibili():
    rows = _generic_items({"code": 200, "data": ["热点一", "热点二"]}, platform="bilibili")
    assert [r["title"] for r in rows] == ["热点一", "热点二"]
    assert rows[0]["url"].startswith("https://search.bilibili.com/all?keyword=")


def test_generic_parser_accepts_common_object_shapes():
    payload = {"data": {"list": [{"word": "主题", "hot_value": 123456, "link": "https://example.com/item"}]}}
    assert _generic_items(payload)[0] == {"title": "主题", "hot": "12万", "url": "https://example.com/item"}


def test_xhs_46la_public_table_parser(monkeypatch):
    html = '''<table><tbody id="hot-list"><tr><td>1</td><td><a href="https://www.xiaohongshu.com/search_result?keyword=topic&amp;type=51">旅行拍照</a></td><td><span>918.6万</span></td></tr></tbody></table>'''
    monkeypatch.setattr(trends, "_read_text", lambda url, **kwargs: html)
    assert trends._xhs_46la() == [{"title": "旅行拍照", "hot": "918.6万", "url": "https://www.xiaohongshu.com/search_result?keyword=topic&type=51"}]


def test_xhs_newrank_public_table_parser(monkeypatch):
    html = '''<div>小红书 · 热点</div><table><tbody><tr><td>1</td><td>古诗词里的中国</td><td>934.8w</td><td></td></tr></tbody></table>'''
    monkeypatch.setattr(trends, "_read_text", lambda url, **kwargs: html)
    assert trends._xhs_newrank() == [{"title": "古诗词里的中国", "hot": "934.8w", "url": ""}]


def test_direct_zhihu_mapping(monkeypatch):
    monkeypatch.setattr(trends, "_read_json", lambda *a, **k: {"data": [{
        "target": {"title": "知乎测试", "url": "https://api.zhihu.com/questions/123"},
        "detail_text": "956 万热度",
    }]})
    assert trends._direct_zhihu() == [{"title": "知乎测试", "hot": "956万", "url": "https://www.zhihu.com/question/123"}]


def test_direct_toutiao_mapping(monkeypatch):
    monkeypatch.setattr(trends, "_read_json", lambda *a, **k: {"data": [{
        "Title": "头条测试", "ClusterIdStr": "987", "HotValue": "123456", "Url": "https://www.toutiao.com/trending/987/",
    }]})
    assert trends._direct_toutiao()[0]["title"] == "头条测试"
    assert trends._direct_toutiao()[0]["hot"] == "12万"


def test_direct_bilibili_mapping(monkeypatch):
    monkeypatch.setattr(trends, "_read_json", lambda *a, **k: {"code": 0, "data": {"list": [{
        "title": "B站测试", "bvid": "BV1fixture", "stat": {"view": 1404190},
    }]}})
    item = trends._direct_bilibili()[0]
    assert item["title"] == "B站测试" and item["hot"] == "140万"
    assert item["url"].endswith("BV1fixture")


def test_provider_fallback_records_failed_sources(tmp_path, monkeypatch):
    s = service(tmp_path)
    def fail():
        raise ValueError("fixture")
    monkeypatch.setattr(s, "_providers", lambda platform: [
        Provider("one", "源一", fail), Provider("two", "源二", lambda: []),
        Provider("three", "源三", lambda: [{"title": "成功", "hot": "1", "url": ""}]),
    ])
    group = s.get_group("zhihu", force=True)
    assert group["status"] == "fresh" and group["source"] == "源三"
    assert group["attempts"] == [{"provider": "源一", "status": "parse_error"}, {"provider": "源二", "status": "empty"}]


def test_failed_refresh_uses_bounded_stale_cache(tmp_path, monkeypatch):
    s = service(tmp_path)
    s._cache["zhihu:public"] = {"fetched_at": time.time() - 600, "source": "上次成功源", "items": [{"title": "旧但可见", "hot": "", "url": ""}]}
    monkeypatch.setattr(s, "_providers", lambda platform: [Provider("dead", "失效源", lambda: [])])
    group = s.get_group("zhihu", force=True)
    assert group["status"] == "stale" and group["items"][0]["title"] == "旧但可见"
    assert "缓存" in group["error"]


def test_too_old_cache_is_not_presented_as_current(tmp_path, monkeypatch):
    s = service(tmp_path)
    s._cache["toutiao:public"] = {"fetched_at": time.time() - trends.MAX_STALE - 1, "source": "old", "items": [{"title": "too old", "hot": "", "url": ""}]}
    monkeypatch.setattr(s, "_providers", lambda platform: [Provider("dead", "失效源", lambda: [])])
    group = s.get_group("toutiao", force=True)
    assert group["status"] == "error" and group["items"] == []


def test_fresh_cache_avoids_network_until_forced(tmp_path, monkeypatch):
    s = service(tmp_path)
    calls = []
    monkeypatch.setattr(s, "_providers", lambda platform: [Provider("p", "实时源", lambda: calls.append(1) or [{"title": "A", "hot": "", "url": ""}])])
    assert s.get_group("zhihu")["status"] == "fresh"
    assert s.get_group("zhihu")["cached"] is True
    assert len(calls) == 1
    s.get_group("zhihu", force=True)
    assert len(calls) == 2


def test_cache_persists_across_service_restart(tmp_path, monkeypatch):
    first = service(tmp_path)
    monkeypatch.setattr(first, "_providers", lambda platform: [Provider("p", "provider", lambda: [{"title": "persist", "hot": "", "url": ""}])])
    first.get_group("zhihu", force=True)
    second = service(tmp_path)
    monkeypatch.setattr(second, "_providers", lambda platform: [Provider("dead", "dead", lambda: [])])
    assert second.get_group("zhihu")["items"][0]["title"] == "persist"


def test_xhs_default_uses_public_provider_without_launching_browser(tmp_path, monkeypatch):
    s = service(tmp_path)
    browser_calls = []
    monkeypatch.setattr(s, "_providers", lambda platform: [Provider("public", "公开聚合源", lambda: [{"title": "公开热点", "hot": "1", "url": ""}])])
    monkeypatch.setattr(s, "_xhs_fetch", lambda context: browser_calls.append(context) or [])
    group = s.get_group("xiaohongshu")
    assert group["status"] == "fresh" and group["source"] == "公开聚合源"
    assert group["items"][0]["title"] == "公开热点"
    assert browser_calls == []


def test_xhs_public_provider_fallback_and_failure_stay_browser_free(tmp_path, monkeypatch):
    s = service(tmp_path)
    browser_calls = []
    def fail():
        raise ValueError("fixture")
    monkeypatch.setattr(s, "_providers", lambda platform: [Provider("one", "聚合源一", fail), Provider("two", "聚合源二", lambda: [])])
    monkeypatch.setattr(s, "_xhs_fetch", lambda context: browser_calls.append(context) or [])
    group = s.get_group("xiaohongshu", force=True)
    assert group["status"] == "error"
    assert "公开小红书热点源" in group["error"]
    assert group["attempts"] == [{"provider": "聚合源一", "status": "parse_error"}, {"provider": "聚合源二", "status": "empty"}]
    assert browser_calls == []


def test_xhs_connected_account_cache_is_isolated_per_account(tmp_path, monkeypatch):
    s = service(tmp_path)
    calls = []
    monkeypatch.setattr(s, "_xhs_fetch", lambda context: calls.append(context.account_id) or [{"title": context.account_id, "hot": "", "url": ""}])
    one = XhsContext(profile=tmp_path / "p1", account_id="a" * 32)
    two = XhsContext(profile=tmp_path / "p2", account_id="b" * 32)
    assert s.get_group("xiaohongshu", xhs_context=one)["items"][0]["title"] == "a" * 32
    assert s.get_group("xiaohongshu", xhs_context=two)["items"][0]["title"] == "b" * 32
    assert calls == ["a" * 32, "b" * 32]


def test_xhs_error_message_for_busy_connected_account(tmp_path, monkeypatch):
    s = service(tmp_path)
    monkeypatch.setattr(s, "_xhs_fetch", lambda context: (_ for _ in ()).throw(RuntimeError("account_busy")))
    ctx = XhsContext(profile=tmp_path / "profile", account_id="c" * 32)
    group = s.get_group("xiaohongshu", force=True, xhs_context=ctx)
    assert group["status"] == "error" and "正在登录或发布" in group["error"]


def test_optional_dailyhot_provider_requires_operator_configuration(tmp_path, monkeypatch):
    s = service(tmp_path)
    monkeypatch.delenv("RIPPLE_TRENDS_DAILYHOT_BASE", raising=False)
    assert s._dailyhot("zhihu") == []
    monkeypatch.setenv("RIPPLE_TRENDS_DAILYHOT_BASE", "http://127.0.0.1:6688")
    providers = s._dailyhot("zhihu")
    assert len(providers) == 1 and providers[0].label == "自托管 DailyHot"


def test_recent_failure_has_short_cooldown(tmp_path, monkeypatch):
    s = service(tmp_path)
    calls = []
    def broken():
        calls.append(1)
        raise ValueError('fixture')
    monkeypatch.setattr(s, '_providers', lambda platform: [Provider('bad', 'bad', broken)])
    assert s.get_group('zhihu')['status'] == 'error'
    assert s.get_group('zhihu')['status'] == 'error'
    assert len(calls) == 1
    s.get_group('zhihu', force=True)
    assert len(calls) == 2


def test_untrusted_trend_links_are_removed(tmp_path, monkeypatch):
    s = service(tmp_path)
    monkeypatch.setattr(s, '_providers', lambda platform: [Provider('p', 'p', lambda: [
        {'title': 'safe', 'hot': '', 'url': 'https://www.zhihu.com/question/1'},
        {'title': 'evil', 'hot': '', 'url': 'javascript:alert(1)'},
        {'title': 'phish', 'hot': '', 'url': 'https://zhihu.com.evil.example/question/1'},
    ])])
    rows = s.get_group('zhihu', force=True)['items']
    assert rows[0]['url'] == 'https://www.zhihu.com/question/1'
    assert rows[1]['url'] == '' and rows[2]['url'] == ''


def test_api_global_refresh_does_not_probe_xhs_unless_requested(monkeypatch):
    import asyncio
    from web import app as upstream
    observed = {}
    def fake_group(platform, limit, *, force=False, xhs_context=None):
        observed[platform] = {"force": force, "context": xhs_context}
        return {"platform": platform, "label": platform, "items": [], "status": "error", "source": "", "fetched_at": 0,
                "error": "fixture", "attempts": [], "cached": False}
    monkeypatch.setattr(upstream._TREND_SERVICE, "get_group", fake_group)
    asyncio.run(upstream.api_trends(platforms="zhihu,xiaohongshu", limit=2, refresh=True))
    assert observed["zhihu"]["force"] is True
    assert observed["xiaohongshu"]["force"] is False
    observed.clear()
    asyncio.run(upstream.api_trends(platforms="xiaohongshu", limit=2, refresh=True, xiaohongshu_probe=True))
    assert observed["xiaohongshu"]["force"] is True


def test_api_uses_selected_connected_xhs_profile_only(tmp_path, monkeypatch):
    import asyncio
    from web import app as upstream
    account_id = 'd' * 32
    captured = {}
    monkeypatch.setattr(upstream.app.state.ripple.accounts, 'get', lambda value: {
        'id': value, 'platform': 'xiaohongshu', 'adapter': 'native', 'status': 'connected', 'label': '测试只读账号',
    })
    monkeypatch.setattr(upstream.app.state.ripple.accounts, 'directory', lambda value: tmp_path / value)
    def fake_group(platform, limit, *, force=False, xhs_context=None):
        captured['context'] = xhs_context
        return {'platform': platform, 'label': '小红书', 'items': [], 'status': 'error', 'source': '', 'fetched_at': 0,
                'error': 'fixture', 'attempts': [], 'cached': False}
    monkeypatch.setattr(upstream._TREND_SERVICE, 'get_group', fake_group)
    result = asyncio.run(upstream.api_trends(platforms='xiaohongshu', limit=5, xiaohongshu_account_id=account_id))
    ctx = captured['context']
    assert result['trends'][0]['platform'] == 'xiaohongshu'
    assert ctx.account_id == account_id
    assert ctx.profile == tmp_path / account_id / 'browser' / 'XiaohongshuProfile'
    assert ctx.lock_path == tmp_path / account_id / 'browser-operation.lock'


def test_api_rejects_unconnected_xhs_profile(monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from web import app as upstream
    monkeypatch.setattr(upstream.app.state.ripple.accounts, 'get', lambda value: {
        'id': value, 'platform': 'xiaohongshu', 'adapter': 'native', 'status': 'expired', 'label': 'expired',
    })
    with pytest.raises(HTTPException) as exc:
        asyncio.run(upstream.api_trends(platforms='xiaohongshu', xiaohongshu_account_id='e' * 32))
    assert exc.value.status_code == 409


def test_cache_file_contains_only_public_trend_payload(tmp_path, monkeypatch):
    s = service(tmp_path)
    monkeypatch.setattr(s, "_providers", lambda platform: [Provider("p", "provider", lambda: [{"title": "热点", "hot": "1", "url": "https://example.com"}])])
    s.get_group("zhihu", force=True)
    text = s.cache_path.read_text(encoding="utf-8")
    assert "热点" in text and "password" not in text.lower() and "cookie" not in text.lower()
