from __future__ import annotations

import asyncio
import json
import time

from web import app as upstream


def _campaign(**overrides):
    payload = {
        "title": "真实活动测试",
        "platform": "xiaohongshu",
        "organizer": "测试主办方",
        "activity_type": "征稿",
        "reward_type": "流量扶持",
        "reward_summary": "以活动原文为准",
        "summary": "面向用户的活动摘要",
        "prizes": ["奖金池 5 万元"],
        "winning_conditions": ["播放量达到活动门槛"],
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


def test_legacy_campaign_rows_are_normalized_for_new_detail_fields(tmp_path, monkeypatch):
    path = tmp_path / "campaigns.json"
    path.write_text(json.dumps([{
        "id": "legacy-1", "title": "旧活动", "platform": "bilibili",
        "created_at": 100, "updated_at": 120, "last_verified_at": 0,
    }], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", path)
    row = upstream._read_campaigns()[0]
    assert row["summary"] == ""
    assert row["prizes"] == []
    assert row["winning_conditions"] == []
    assert row["eligibility"] == []
    assert row["discovered_at"] == 100
    assert row["last_seen_at"] == 120


def test_running_agent_status_is_recovered_after_server_restart(tmp_path, monkeypatch):
    path = tmp_path / "campaigns.json"
    path.write_text(json.dumps([{
        "id": "stale-running", "title": "中断活动", "platform": "bilibili",
        "created_at": 100, "updated_at": 120, "enrichment_status": "running",
        "rule_evidence_fingerprint": "fp-new", "last_agent_fingerprint": "",
        "eligibility": [], "prizes": [], "winning_conditions": [],
    }], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", path)
    row = upstream._read_campaigns()[0]
    assert row["enrichment_status"] == "incomplete"
    assert row["missing_fields"]


def test_account_specific_campaign_qualification_is_projected_to_card_and_account_state(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    row = upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu", "external_id": "xhs:44697",
        "title": "光遇联动狂欢月创作征集", "organizer": "小红书创作服务平台",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page1", "source_type": "creator_events_api_v2",
        "source_status": "verified", "account_id": "xhs-account-1",
        "qualification_state": "eligible", "qualification_basis": "创作者后台已向当前账号返回可执行活动任务。",
        "content_requirements": ["发布#光遇 话题笔记"], "submission_spec": {"formats": ["image_text", "video"]},
        "evidence": {"kind": "creator_account_activity", "account_id": "xhs-account-1"},
    })
    assert row["qualification_state"] == "eligible"
    assert "可执行活动任务" in row["qualification_basis"]
    assert row["account_states"]["xhs-account-1"]["qualification_state"] == "eligible"
    assert "可执行活动任务" in row["account_states"]["xhs-account-1"]["qualification_basis"]


def test_fresh_xhs_v2_refresh_removes_only_unsaved_legacy_creator_event_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    legacy = {
        "id": "legacy-xhs", "title": "旧聚合活动", "platform": "xiaohongshu",
        "source_type": "creator_events_api", "source_url": "https://creator.xiaohongshu.com/new/events",
        "source_status": "verified", "saved": False, "created_at": 1, "updated_at": 1,
    }
    saved = {**legacy, "id": "saved-xhs", "title": "已收藏旧活动", "saved": True}
    upstream._write_campaigns([legacy, saved])
    payload = {"results": [{
        "platform": "xiaohongshu", "status": "fresh", "items": [{
            "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
            "external_id": "xhs:44697", "title": "光遇联动狂欢月创作征集",
            "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page1",
            "source_type": "creator_activity_center_api", "source_status": "verified",
            "evidence": {"kind": "creator_account_activity"},
        }],
    }]}
    result = upstream._merge_campaign_refresh(payload)
    rows = upstream._read_campaigns()
    assert result["merged"] == 1
    assert all(row["id"] != "legacy-xhs" for row in rows)
    assert any(row["id"] == "saved-xhs" for row in rows)
    assert any(row.get("title") == "光遇联动狂欢月创作征集" for row in rows)


def test_campaign_import_is_not_presented_as_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    created = asyncio.run(upstream.api_campaign_create(_campaign()))
    assert created["source_type"] == "user_import"
    assert created["source_status"] == "imported"
    assert created["last_verified_at"] == 0
    assert created["rule_version"] == 1
    assert created["qualification_state"] == "unknown"
    assert created["summary"] == "面向用户的活动摘要"
    assert created["prizes"] == ["奖金池 5 万元"]
    assert created["winning_conditions"] == ["播放量达到活动门槛"]

    rows = asyncio.run(upstream.api_campaign_list())
    assert rows[0]["id"] == created["id"]
    assert rows[0]["source_status"] == "imported"


def test_model_prompt_candidate_is_not_presented_as_verified(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    row = upstream._merge_campaign_candidate(items, {
        "provider_id": "x_model_prompt", "platform": "x", "external_id": "x:123",
        "title": "X Creator Challenge", "organizer": "@brand",
        "source_url": "https://x.com/brand/status/123", "source_type": "x_model_prompt",
        "source_status": "model_reported", "summary": "Model-reported activity",
        "evidence": {"kind": "model_prompt_x_post", "url": "https://x.com/brand/status/123"},
    })
    assert row["source_status"] == "model_reported"
    assert row["last_verified_at"] == 0
    assert row["source_evidence"][0]["status"] == "model_reported"

    upgraded = upstream._merge_campaign_candidate(items, {
        "provider_id": "x_developer_api", "platform": "x", "external_id": "x:123",
        "title": "X Creator Challenge", "organizer": "@brand",
        "source_url": "https://x.com/brand/status/123", "source_type": "x_developer_api",
        "source_status": "verified", "summary": "API verified",
        "evidence": {"kind": "x_post", "url": "https://x.com/brand/status/123"},
    })
    assert upgraded["source_status"] == "verified"
    assert upgraded["last_verified_at"] > 0


def test_x_model_prompt_completeness_uses_real_rule_fields(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    complete = upstream._merge_campaign_candidate(items, {
        "provider_id": "x_model_prompt", "platform": "x", "external_id": "x:complete",
        "title": "Vizard Agent Challenge", "organizer": "@vizard_ai",
        "source_url": "https://x.com/vizard_ai/status/456", "source_type": "x_model_prompt",
        "source_status": "model_reported", "summary": "活动规则中文摘要",
        "starts_at": "2026-09-18", "signup_deadline": "2026-09-20", "submit_deadline": "2026-10-06",
        "stats_deadline": "2026-10-10", "eligibility": ["免费参与"],
        "content_requirements": ["按赛道要求提交作品"], "prizes": ["3,000 美元现金"],
        "winning_conditions": ["各赛道冠军按活动规则评选"], "reward_rules": ["冠军各获得 500 美元"],
        "required_topics": ["#VizardAgentChallenge"], "submission_spec": {"formats": ["video"]},
        "ai_policy": "原帖未说明 AI 使用要求",
        "evidence": {"kind": "model_prompt_x_post", "url": "https://x.com/vizard_ai/status/456"},
    })
    assert complete["source_status"] == "model_reported"
    assert complete["missing_fields"] == []
    assert complete["enrichment_status"] == "complete"
    assert complete["signup_deadline"] == "2026-09-20"
    assert complete["stats_deadline"] == "2026-10-10"
    assert complete["ai_policy"] == "原帖未说明 AI 使用要求"
    assert complete["submission_spec"]["formats"] == ["video"]

    incomplete = upstream._merge_campaign_candidate(items, {
        "provider_id": "x_model_prompt", "platform": "x", "external_id": "x:incomplete",
        "title": "规则缺失活动", "organizer": "@brand",
        "source_url": "https://x.com/brand/status/789", "source_type": "x_model_prompt",
        "source_status": "model_reported", "reward_summary": "500 美元奖金",
        "evidence": {"kind": "model_prompt_x_post", "url": "https://x.com/brand/status/789"},
    })
    assert set(incomplete["missing_fields"]) == {"eligibility", "submission_spec", "winning_conditions"}
    assert incomplete["enrichment_status"] == "incomplete"
    assert incomplete["last_verified_at"] == 0


def test_existing_x_campaign_is_enriched_once_and_then_skipped(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    row = upstream._merge_campaign_candidate(items, {
        "provider_id": "x_model_prompt", "platform": "x", "external_id": "x:321",
        "title": "Vizard Agent Challenge", "organizer": "@vizard_ai",
        "source_url": "https://x.com/vizard_ai/status/321", "source_type": "x_model_prompt",
        "source_status": "model_reported", "summary": "Old English summary",
        "reward_summary": "$3,000 Cash", "submit_deadline": "Oct 6",
        "eligibility": ["Open to everyone"], "prizes": ["$3,000 Cash"],
        "winning_conditions": ["Top creators win"],
        "evidence": {"kind": "model_prompt_x_discovery", "url": "https://x.com/vizard_ai/status/321"},
    })
    upstream._write_campaigns(items)
    monkeypatch.setattr(upstream._AI_PROVIDERS, "resolved", lambda *args, **kwargs: {
        "provider_id": "gateway", "provider_name": "Gateway", "model": "grok-search",
    })
    calls = []
    def fake_enrich(current):
        calls.append(current["id"])
        return {
            "provider_id": "x_model_prompt", "platform": "x", "external_id": "x:321",
            "title": "Vizard Agent Challenge", "organizer": "@vizard_ai",
            "source_url": "https://x.com/vizard_ai/status/321", "source_type": "x_model_prompt",
            "source_status": "model_reported", "summary": "中文活动摘要",
            "reward_summary": "3,000 美元现金", "submit_deadline": "2026-10-06",
            "eligibility": ["免费参与"], "content_requirements": ["按赛道要求提交作品"],
            "prizes": ["3,000 美元现金"], "winning_conditions": ["按活动规则评选赛道冠军"],
            "reward_rules": [], "required_topics": [],
            "submission_spec": {"formats": ["video"]}, "ai_policy": "unknown",
            "evidence": {"kind": "model_prompt_x_rules", "url": "https://x.com/vizard_ai/status/321", "model": "grok-search"},
        }
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "enrich_x_prompt_campaign", fake_enrich)

    assert upstream._x_enrichment_candidates(limit=10)[0]["id"] == row["id"]
    first = upstream._x_enrich_one(row["id"])
    assert first["called"] is True
    assert first["item"]["x_enrichment_version"] == upstream.X_RULE_ENRICHMENT_VERSION
    assert first["item"]["x_enrichment_status"] == "complete"
    assert first["item"]["summary"] == "中文活动摘要"
    assert first["item"]["reward_summary"] == "3,000 美元现金"
    assert first["item"]["submit_deadline"] == "2026-10-06"
    assert first["item"]["eligibility"] == ["免费参与"]
    assert first["item"]["prizes"] == ["3,000 美元现金"]
    assert first["item"]["winning_conditions"] == ["按活动规则评选赛道冠军"]
    assert "Old English" not in first["item"]["summary"]
    assert "$3,000 Cash" not in first["item"]["reward_summary"]
    assert first["item"]["submission_spec"]["formats"] == ["video"]
    assert calls == [row["id"]]
    assert upstream._x_enrichment_candidates(limit=10) == []

    second = upstream._x_enrich_one(row["id"])
    assert second["called"] is False
    assert second["reason"] == "already_enriched"
    assert calls == [row["id"]]


def test_x_enrichment_failure_is_backed_off_and_does_not_loop(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    row = upstream._merge_campaign_candidate(items, {
        "provider_id": "x_model_prompt", "platform": "x", "external_id": "x:654",
        "title": "失败活动", "source_url": "https://x.com/brand/status/654",
        "source_type": "x_model_prompt", "source_status": "model_reported",
        "evidence": {"kind": "model_prompt_x_discovery", "url": "https://x.com/brand/status/654"},
    })
    upstream._write_campaigns(items)
    monkeypatch.setattr(upstream._AI_PROVIDERS, "resolved", lambda *args, **kwargs: {
        "provider_id": "gateway", "provider_name": "Gateway", "model": "grok-search",
    })
    monkeypatch.setattr(
        upstream._CAMPAIGN_SOURCES, "enrich_x_prompt_campaign",
        lambda current: (_ for _ in ()).throw(upstream.WorkflowError("timeout", 504)),
    )
    try:
        upstream._x_enrich_one(row["id"])
        assert False, "expected WorkflowError"
    except upstream.WorkflowError:
        pass
    stored = upstream._read_campaigns()[0]
    assert stored["x_enrichment_status"] == "failed"
    assert stored["x_enrichment_attempt_count"] == 1
    assert stored["x_enrichment_next_retry_at"] > int(time.time())
    assert upstream._x_enrichment_candidates(limit=10) == []



def test_scheduler_processes_existing_x_rule_queue_even_when_no_source_is_due(monkeypatch):
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "due_platforms", lambda: [])
    monkeypatch.setattr(upstream, "_x_enrichment_candidates", lambda limit=3: [{"id": "x1"}, {"id": "x2"}])
    started = []
    monkeypatch.setattr(upstream, "_start_x_enrichment_batch", lambda ids: started.append(ids) or True)
    monkeypatch.setattr(upstream, "_read_campaigns", lambda: [{"id": "x1"}, {"id": "x2"}])
    result = upstream._campaign_scheduler_tick()
    assert result["due"] == []
    assert started == [["x1", "x2"]]


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
    assert updated["prizes"] == ["奖金池 5 万元"]
    assert updated["winning_conditions"] == ["播放量达到活动门槛"]
    assert len(updated["rule_history"]) == 1
    assert updated["rule_history"][0]["version"] == 1
    assert updated["rule_history"][0]["content_requirements"] == ["原创图文"]


def test_campaign_source_capabilities_only_claim_bilibili_without_configuration(monkeypatch):
    state = {
        "items": [
            {"platform": "bilibili", "automatic": True, "status": "ready"},
            {"platform": "x", "automatic": False, "status": "needs_config"},
            {"platform": "xiaohongshu", "automatic": False, "status": "needs_config"},
            {"platform": "douyin", "automatic": False, "status": "needs_config"},
            {"platform": "wechat", "automatic": False, "status": "manual"},
            {"platform": "weixin-channels", "automatic": False, "status": "manual"},
        ],
        "automatic_count": 1,
    }
    monkeypatch.setattr(upstream, "_ensure_ai_provider_migration", lambda: None)
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "public_state", lambda: state)
    result = asyncio.run(upstream.api_campaign_sources())
    assert len(result["items"]) == 6
    assert result["automatic_count"] == 1
    assert [item["platform"] for item in result["items"] if item["automatic"]] == ["bilibili"]


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


def test_automatic_candidate_merges_evidence_without_overwriting_manual_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    manual = asyncio.run(upstream.api_campaign_create(_campaign(title="同一个活动", reward_summary="人工确认奖励")))
    items = upstream._read_campaigns()
    merged = upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events",
        "platform": "xiaohongshu",
        "external_id": "xhs:a1",
        "title": "同一个活动",
        "organizer": "平台",
        "reward_summary": "自动源摘要不应覆盖人工规则",
        "source_url": "https://creator.xiaohongshu.com/new/events",
        "source_type": "creator_events_api",
        "evidence": {"kind": "creator_account"},
        "account_id": "account-1",
        "summary": "自动来源摘要",
        "prizes": ["流量扶持"],
        "winning_conditions": ["官方评审入选"],
    })
    assert merged["id"] == manual["id"]
    assert merged["reward_summary"] == "人工确认奖励"
    assert merged["external_ids"]["xiaohongshu_creator_events"] == "xhs:a1"
    assert merged["source_evidence"][-1]["evidence"]["kind"] == "creator_account"
    assert merged["account_states"]["account-1"]["visible"] is True
    assert merged["summary"] == "面向用户的活动摘要"
    assert merged["prizes"] == ["奖金池 5 万元"]
    assert merged["winning_conditions"] == ["播放量达到活动门槛"]


def test_list_discovery_and_rule_verification_use_separate_timestamps(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    listed = upstream._merge_campaign_candidate(items, {
        "provider_id": "bilibili_public", "platform": "bilibili", "external_id": "bilibili:1",
        "title": "B站活动", "organizer": "B站", "source_url": "https://www.bilibili.com/blackboard/a.html",
        "source_type": "platform_public", "evidence": {"kind": "platform_public_list"},
    })
    assert listed["last_seen_at"] > 0
    assert listed["last_verified_at"] == 0
    first_version = listed["rule_version"]

    verified = upstream._merge_campaign_candidate(items, {
        "provider_id": "bilibili_public", "platform": "bilibili", "external_id": "bilibili:1",
        "title": "B站活动", "organizer": "B站", "source_url": "https://www.bilibili.com/blackboard/a.html",
        "source_type": "platform_public", "evidence": {"kind": "platform_public_detail"},
        "prizes": ["瓜分5000元"], "winning_conditions": ["总投币数≥30"],
    })
    assert verified["last_verified_at"] > 0
    assert verified["prizes"] == ["瓜分5000元"]
    assert verified["winning_conditions"] == ["总投币数≥30"]
    assert verified["rule_version"] == first_version + 1

    cleared = upstream._merge_campaign_candidate(items, {
        "provider_id": "bilibili_public", "platform": "bilibili", "external_id": "bilibili:1",
        "title": "B站活动", "organizer": "B站", "source_url": "https://www.bilibili.com/blackboard/a.html",
        "source_type": "platform_public", "evidence": {"kind": "platform_public_detail"},
    })
    assert cleared["prizes"] == []
    assert cleared["winning_conditions"] == []
    assert cleared["rule_version"] == first_version + 2


def test_evidence_fingerprint_change_alone_does_not_bump_rule_version(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    first = upstream._merge_campaign_candidate(items, {
        "provider_id": "bilibili_public", "platform": "bilibili", "external_id": "bilibili:fp",
        "title": "指纹测试", "organizer": "B站", "source_url": "https://www.bilibili.com/blackboard/fp.html",
        "source_type": "platform_public", "evidence": {"kind": "platform_public_detail"},
        "prizes": ["瓜分5000元"], "winning_conditions": ["播放量≥100"],
        "rule_evidence_fingerprint": "fp-1",
    })
    version = first["rule_version"]
    second = upstream._merge_campaign_candidate(items, {
        "provider_id": "bilibili_public", "platform": "bilibili", "external_id": "bilibili:fp",
        "title": "指纹测试", "organizer": "B站", "source_url": "https://www.bilibili.com/blackboard/fp.html",
        "source_type": "platform_public", "evidence": {"kind": "platform_public_detail"},
        "prizes": ["瓜分5000元"], "winning_conditions": ["播放量≥100"],
        "rule_evidence_fingerprint": "fp-2",
    })
    assert second["rule_evidence_fingerprint"] == "fp-2"
    assert second["rule_version"] == version


def test_campaign_agent_does_not_repeat_same_evidence_but_reacts_to_changed_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    item = upstream._merge_campaign_candidate(items, {
        "provider_id": "bilibili_public", "platform": "bilibili", "external_id": "bilibili:agent",
        "title": "Agent 测试", "organizer": "B站", "source_url": "https://www.bilibili.com/blackboard/agent.html",
        "source_type": "platform_public", "evidence": {"kind": "platform_public_detail"},
        "rule_evidence_fingerprint": "seed",
    })
    upstream._write_campaigns(items)
    monkeypatch.setattr(upstream, "_campaign_agent_model", lambda: "fake-model")
    evidence = {"value": {
        "platform": "bilibili", "source_url": item["source_url"],
        "evidence_text": "参加活动前必须报名。视频作品需带指定话题。",
        "evidence_fingerprint": "fp-a", "structured": {},
    }}
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "bilibili_page_evidence", lambda url, force=False: dict(evidence["value"]))
    calls = []
    def fake_agent(*args, **kwargs):
        calls.append((args, kwargs))
        return json.dumps({
            "summary": "", "eligibility": ["必须报名"], "content_requirements": [], "prizes": [],
            "winning_conditions": [], "reward_rules": [], "required_topics": [], "submission_spec": {},
            "field_evidence": {"eligibility": ["参加活动前必须报名"]},
        }, ensure_ascii=False)
    monkeypatch.setattr(upstream, "run_agent_sync", fake_agent)

    first = upstream._campaign_enrich_one(item["id"], force=False)
    assert first["called"] is True
    assert len(calls) == 1
    second = upstream._campaign_enrich_one(item["id"], force=False)
    assert second["called"] is False and second["reason"] == "already_analyzed"
    assert len(calls) == 1

    evidence["value"]["evidence_fingerprint"] = "fp-b"
    third = upstream._campaign_enrich_one(item["id"], force=False)
    assert third["called"] is True
    assert len(calls) == 2


def test_campaign_agent_fetch_failure_does_not_call_model(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    item = upstream._merge_campaign_candidate(items, {
        "provider_id": "bilibili_public", "platform": "bilibili", "external_id": "bilibili:offline",
        "title": "网络失败", "organizer": "B站", "source_url": "https://www.bilibili.com/blackboard/offline.html",
        "source_type": "platform_public", "evidence": {"kind": "platform_public_detail"},
    })
    upstream._write_campaigns(items)
    monkeypatch.setattr(upstream, "_campaign_agent_model", lambda: "fake-model")
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "bilibili_page_evidence", lambda *a, **k: (_ for _ in ()).throw(upstream.WorkflowError("network", 502)))
    calls = []
    monkeypatch.setattr(upstream, "run_agent_sync", lambda *a, **k: calls.append(1) or "{}")
    try:
        upstream._campaign_enrich_one(item["id"], force=False)
        assert False, "expected WorkflowError"
    except upstream.WorkflowError:
        pass
    assert calls == []


def test_bilibili_url_preview_does_not_create_campaign(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    monkeypatch.setattr(upstream, "_campaign_agent_model", lambda: "")
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "preview_bilibili_url", lambda url: {
        "title": "预览活动", "platform": "bilibili", "organizer": "B站", "activity_type": "创作活动",
        "source_url": url, "source_type": "user_import", "eligibility": [], "content_requirements": [],
        "prizes": [], "winning_conditions": [], "reward_rules": [], "required_topics": [], "submission_spec": {},
        "_agent_evidence": {"evidence_fingerprint": "fp", "evidence_text": "投稿活动"},
    })
    result = asyncio.run(upstream.api_campaign_import_preview(upstream.CampaignImportPreviewInput(url="https://www.bilibili.com/blackboard/import.html")))
    assert result["draft"]["title"] == "预览活动"
    assert result["agent_used"] is False
    assert upstream._read_campaigns() == []


def test_blank_manual_fields_are_not_locked_against_future_enrichment(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    created = asyncio.run(upstream.api_campaign_create(upstream.CampaignInput(
        title="空规则活动", platform="bilibili", source_url="https://www.bilibili.com/blackboard/manual.html",
    )))
    assert created["user_confirmed_fields"] == []
    assert "submission_spec" in created["missing_fields"]


def test_single_bilibili_rule_verify_enriches_existing_campaign(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    item = upstream._merge_campaign_candidate(items, {
        "provider_id": "bilibili_public", "platform": "bilibili", "external_id": "bilibili:9",
        "title": "待核验活动", "organizer": "B站", "source_url": "https://www.bilibili.com/blackboard/detail.html",
        "source_type": "platform_public", "evidence": {"kind": "platform_public_list"},
    })
    upstream._write_campaigns(items)
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "verify_bilibili_campaign", lambda current: {
        "provider_id": "bilibili_public", "platform": "bilibili", "external_id": "bilibili:9",
        "title": current["title"], "organizer": "B站", "source_url": current["source_url"],
        "source_type": "platform_public", "evidence": {"kind": "platform_public_detail"},
        "eligibility": ["需报名"], "prizes": ["瓜分5000元"], "winning_conditions": ["播放量≥10000"],
    })
    result = asyncio.run(upstream.api_campaign_verify(item["id"]))
    assert result["eligibility"] == ["需报名"]
    assert result["prizes"] == ["瓜分5000元"]
    assert result["winning_conditions"] == ["播放量≥10000"]
    assert result["last_verified_at"] > 0


def test_background_campaign_tick_merges_due_provider_results(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "due_platforms", lambda: ["bilibili"])
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "refresh", lambda platforms, force=False: {
        "results": [{"platform": "bilibili", "status": "fresh", "items": [{
            "provider_id": "bilibili_public", "platform": "bilibili", "external_id": "bilibili:bg",
            "title": "后台同步活动", "organizer": "B站", "source_url": "https://www.bilibili.com/blackboard/bg.html",
            "source_type": "platform_public", "evidence": {"kind": "platform_public_detail"},
            "prizes": ["奖金池"],
        }]}],
        "sources": {},
    })
    result = upstream._campaign_scheduler_tick()
    assert result["due"] == ["bilibili"]
    assert upstream._read_campaigns()[0]["title"] == "后台同步活动"


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
