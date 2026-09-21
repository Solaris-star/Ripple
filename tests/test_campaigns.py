from __future__ import annotations

import asyncio
import json

from web import app as upstream


def _campaign(**overrides):
    payload = {
        "title": "真实活动测试",
        "platform": "xiaohongshu",
        "organizer": "测试主办方",
        "activity_type": "征稿",
        "reward_type": "流量扶持",
        "reward_summary": "以活动原文为准",
        "qualification_state": "unknown",
        "content_requirements": ["原创图文"],
        "eligibility": ["账号状态正常"],
        "reward_rules": ["具体奖励以平台结算为准"],
        "required_topics": ["#测试活动"],
        "ai_policy": "未说明",
        "source_url": "https://example.com/activity/1",
        "status": "active",
    }
    payload.update(overrides)
    return upstream.CampaignInput(**payload)


def test_campaign_import_is_not_presented_as_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    created = asyncio.run(upstream.api_campaign_create(_campaign()))
    assert created["source_type"] == "user_import"
    assert created["source_status"] == "imported"
    assert created["last_verified_at"] == 0
    assert created["rule_version"] == 1
    assert created["qualification_state"] == "unknown"

    rows = asyncio.run(upstream.api_campaign_list())
    assert rows[0]["id"] == created["id"]
    assert rows[0]["source_status"] == "imported"


def test_campaign_edit_bumps_rule_version_and_keeps_import_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    created = asyncio.run(upstream.api_campaign_create(_campaign()))
    updated = asyncio.run(upstream.api_campaign_update(
        created["id"],
        _campaign(content_requirements=["原创视频", "需带指定话题"], qualification_state="eligible"),
    ))
    assert updated["rule_version"] == 2
    assert updated["source_status"] == "imported"
    assert updated["qualification_state"] == "eligible"
    assert updated["qualification_basis"] == "user_confirmed"
    assert updated["content_requirements"] == ["原创视频", "需带指定话题"]
    assert len(updated["rule_history"]) == 1
    assert updated["rule_history"][0]["version"] == 1
    assert updated["rule_history"][0]["content_requirements"] == ["原创图文"]


def test_campaign_source_capabilities_do_not_claim_unverified_automation():
    state = asyncio.run(upstream.api_campaign_sources())
    assert len(state["items"]) == 6
    assert state["automatic_count"] == 0
    assert all(item["automatic"] is False for item in state["items"])
    assert {item["platform"] for item in state["items"]} == {
        "x", "xiaohongshu", "douyin", "bilibili", "wechat", "weixin-channels",
    }


def test_structured_idea_keeps_campaign_and_trend_context(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "IDEAS_FILE", tmp_path / "ideas.json")
    idea = upstream.IdeaItem(
        title="活动选题",
        note="说明",
        source="AI推荐",
        campaign_id="campaign-1",
        campaign_rule_version=3,
        trend_refs=["热点 A"],
        target_platforms=["xiaohongshu"],
        requirements=["原创"],
        pending_checks=["确认报名状态"],
        angle="实测",
        reason="适配账号",
    )
    created = asyncio.run(upstream.api_ideas_create(idea))
    assert created["campaign_id"] == "campaign-1"
    assert created["campaign_rule_version"] == 3
    assert created["trend_refs"] == ["热点 A"]
    assert created["requirements"] == ["原创"]
    assert created["pending_checks"] == ["确认报名状态"]


    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    campaign = asyncio.run(upstream.api_campaign_create(_campaign()))
    linked = upstream.IdeaItem(
        title="带规则快照的选题",
        campaign_id=campaign["id"],
        campaign_rule_version=campaign["rule_version"],
    )
    linked_created = asyncio.run(upstream.api_ideas_create(linked))
    asyncio.run(upstream.api_campaign_update(
        campaign["id"], _campaign(content_requirements=["新版规则"]),
    ))
    detail = asyncio.run(upstream.api_idea_detail(linked_created["id"]))
    assert detail["campaign"]["rule_version"] == 2
    assert detail["campaign_rule_snapshot"]["version"] == 1
    assert detail["campaign_rule_snapshot"]["content_requirements"] == ["原创图文"]


def test_campaign_recommendation_uses_campaign_as_data_and_returns_rule_version(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    monkeypatch.setattr(upstream, "IDEAS_FILE", tmp_path / "ideas.json")
    created = asyncio.run(upstream.api_campaign_create(_campaign()))

    monkeypatch.setattr(upstream, "_recommendation_ai_backend", lambda: "direct")
    monkeypatch.setattr(upstream, "profile_exists", lambda name: True)
    monkeypatch.setattr(upstream, "load_profile_text", lambda name: "账号方向：效率工具")
    monkeypatch.setattr(upstream._TREND_SERVICE, "get_group", lambda platform, limit: {
        "platform": platform,
        "label": platform,
        "status": "fresh",
        "source": "fixture",
        "items": [{"title": "热点 A", "hot": "100", "url": ""}],
    })

    captured = {}

    def fake_llm(prompt, timeout):
        captured["prompt"] = prompt
        return json.dumps({
            "recommendations": [{
                "title": "活动规则驱动选题",
                "angle": "真实测试",
                "reason": "符合活动方向",
                "score": 91,
                "platforms": ["xiaohongshu"],
                "trend_refs": ["热点 A"],
                "requirements": ["原创图文"],
                "pending_checks": ["确认活动是否允许 AI 辅助"],
            }]
        }, ensure_ascii=False)

    monkeypatch.setattr(upstream, "_direct_llm_chat", fake_llm)

    result = asyncio.run(upstream.api_ideas_recommend(upstream.IdeaRecommendRequest(
        persona="测试画像",
        trend_sources=["xiaohongshu"],
        target_platforms=["xiaohongshu"],
        campaign_id=created["id"],
        limit=3,
    )))

    assert result["campaign"]["id"] == created["id"]
    assert result["campaign"]["rule_version"] == 1
    assert result["recommendations"][0]["campaign_id"] == created["id"]
    assert result["recommendations"][0]["campaign_rule_version"] == 1
    assert result["recommendations"][0]["requirements"] == ["原创图文"]
    assert '"campaign"' in captured["prompt"]
    assert "测试主办方" in captured["prompt"]
