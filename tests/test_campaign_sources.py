from __future__ import annotations

from pathlib import Path
import json
import time

import pytest

from ripple.accounts import AccountInput
from ripple.ai_providers import AIProviderService
from ripple.campaign_sources import (
    CampaignSourceService, _bili_reward_summary, _bilibili_detail_from_html,
    _bilibili_rule_fingerprint_text,
)
from ripple.publishing import WorkflowError
from ripple.workspace import WorkspaceService
from ripple.xhs_browser import _campaign_candidates as xhs_candidates
from ripple.douyin_browser import _candidates as douyin_candidates


@pytest.fixture
def source_service(tmp_path):
    workspace = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    service = CampaignSourceService(workspace, AIProviderService(workspace.private))
    yield workspace, service
    workspace.close()


def test_public_source_state_has_bilibili_ready_without_configuration(source_service):
    _, service = source_service
    state = service.public_state()
    bili = next(row for row in state["items"] if row["platform"] == "bilibili")
    assert bili["status"] == "ready"
    assert bili["automatic"] is True
    assert bili["billing"] == "free"
    assert state["automatic_count"] == 1

    assert bili["sync_interval_seconds"] == 30 * 60
    assert bili["next_sync_at"] == 0


def test_campaign_scheduler_intervals_and_failed_attempt_backoff(source_service, monkeypatch):
    _, service = source_service
    monkeypatch.setattr(time, "time", lambda: 1790042460)  # Outside a paid window.
    assert service.sync_interval("bilibili") == 30 * 60
    assert service.sync_interval("x") == 0  # Paid sources use daily slots.
    assert service.sync_interval("xiaohongshu") == 60 * 60
    assert service.sync_interval("douyin") == 60 * 60

    state = service._state()
    now = int(time.time())
    state["last_sync"]["bilibili"] = {"at": now, "status": "error", "count": 0, "error": "fixture"}
    service._write(state)
    assert "bilibili" not in service.due_platforms()
    bili = next(row for row in service.public_state()["items"] if row["platform"] == "bilibili")
    assert bili["next_sync_at"] == now + 30 * 60

    state = service._state()
    state["last_sync"]["bilibili"]["at"] = now - 31 * 60
    service._write(state)
    assert "bilibili" in service.due_platforms()


def test_sync_state_preserves_last_success_time_and_count_after_failure(source_service):
    _, service = source_service
    service._record_sync("bilibili", status="fresh", count=15, provider="fixture")
    first = next(row for row in service.public_state()["items"] if row["platform"] == "bilibili")["last_sync"]
    assert first["last_success_at"] > 0
    assert first["last_success_count"] == 15

    service._record_sync("bilibili", status="error", count=0, error="temporary", provider="fixture")
    second = next(row for row in service.public_state()["items"] if row["platform"] == "bilibili")["last_sync"]
    assert second["status"] == "error"
    assert second["count"] == 0
    assert second["last_success_at"] == first["last_success_at"]
    assert second["last_success_count"] == 15
    assert second["last_attempt_at"] >= first["last_attempt_at"]
    assert second["next_run_at"] == second["last_attempt_at"] + 30 * 60


def test_rule_fingerprint_text_ignores_dynamic_metrics():
    first = _bilibili_rule_fingerprint_text(["投稿要求：原创视频至少30秒", "浏览量：1000", "热度 20万"])
    second = _bilibili_rule_fingerprint_text(["投稿要求：原创视频至少30秒", "浏览量：9999", "热度 99万"])
    assert first == second
    assert "浏览量" not in first and "热度" not in first


def test_bilibili_priority_detail_rules_cover_saved_linked_and_near_deadline(source_service):
    workspace, service = source_service
    campaigns = [
        {"id": "saved", "platform": "bilibili", "saved": True, "source_url": "https://www.bilibili.com/blackboard/saved.html"},
        {"id": "linked", "platform": "bilibili", "saved": False, "source_url": "https://www.bilibili.com/blackboard/linked.html"},
        {"id": "plain", "platform": "bilibili", "saved": False, "source_url": "https://www.bilibili.com/blackboard/plain.html"},
    ]
    (workspace.outputs / "_campaigns.json").write_text(json.dumps(campaigns, ensure_ascii=False), encoding="utf-8")
    (workspace.outputs / "_ideas.json").write_text(json.dumps([{"campaign_id": "linked"}]), encoding="utf-8")
    urls = service._bilibili_priority_urls()
    assert "https://www.bilibili.com/blackboard/saved.html" in urls
    assert "https://www.bilibili.com/blackboard/linked.html" in urls
    assert "https://www.bilibili.com/blackboard/plain.html" not in urls
    assert service._bilibili_deadline_soon(time.time() + 6 * 24 * 60 * 60) is True
    assert service._bilibili_deadline_soon(time.time() + 8 * 24 * 60 * 60) is False


def test_bilibili_detail_parser_separates_prizes_winning_conditions_and_entry_requirements():
    payload = {
        "layerTree": [
            {
                "name": "EraVideoSourcePc",
                "props": {"config": {
                    "topic_name": "九月创作激励",
                    "poolList": [
                        {"bonus": "瓜分5000元", "label": "起始粉丝量＜1w，累计投币≥30个", "rule": "总投币数≥30,起始粉丝量＜10000"},
                        {"bonus": "瓜分1元", "label": "瓜分奖", "rule": ""},
                        {"bonus": "瓜分0元", "label": "筛选用", "rule": "单稿播放量≥1"},
                    ],
                }},
            },
            {"name": "EvaLinkButton", "alias": "报名", "props": {"jumpAddress": "https://example.invalid/form"}},
        ]
    }
    html = '<script>window.__BILIACT_EVAPAGEDATA__ = ' + json.dumps(payload, ensure_ascii=False) + ';</script>'
    result = _bilibili_detail_from_html(html)
    assert result["prizes"] == ["瓜分5000元"]
    assert result["winning_conditions"] == ["总投币数≥30、起始粉丝量＜10000"]
    assert result["reward_rules"] == ["瓜分5000元：总投币数≥30、起始粉丝量＜10000"]
    assert result["eligibility"] == ["需通过活动页面完成报名"]
    assert result["required_topics"] == ["九月创作激励"]
    assert result["content_requirements"] == ["投稿需关联活动话题：#九月创作激励"]
    assert "页面明确的奖励包括：瓜分5000元" in result["summary_hint"]


def test_bilibili_task_templates_extract_creation_tasks_rewards_and_live_specs():
    payload = {"layerTree": [
        {"name": "EraTasklistPc", "props": {"tasklist": [{
            "taskName": "每日投稿#王者万象棋正式上线 活动话题视频",
            "awardName": "抽奖次数",
            "checkpoints": [{"alias": "每日投稿#王者万象棋正式上线 活动话题视频"}],
        }]}},
        {"name": "EvaTaskButton", "props": {"taskItem": {
            "taskName": "当日专区开播≥60分钟（直播）",
            "awardName": "线索*50",
            "checkpoints": [{"alias": "当日专区开播≥60分钟（直播）"}],
        }}},
        {"name": "EvaText", "props": {"content": "参赛作品必须原创视频"}},
    ]}
    html = '<script>window.__BILIACT_EVAPAGEDATA__ = ' + json.dumps(payload, ensure_ascii=False) + ';</script>'
    result = _bilibili_detail_from_html(html)
    assert "每日投稿#王者万象棋正式上线 活动话题视频" in result["content_requirements"]
    assert "当日专区开播≥60分钟（直播）" in result["content_requirements"]
    assert "参赛作品必须原创视频" in result["content_requirements"]
    assert result["required_topics"] == ["王者万象棋正式上线"]
    assert "抽奖次数" in result["prizes"] and "线索*50" in result["prizes"]
    assert "当日专区开播≥60分钟（直播）" in result["winning_conditions"]
    assert "live" in result["submission_spec"]["formats"]
    assert result["submission_spec"]["duration_seconds"]["min"] == 3600
    assert result["submission_spec"]["original_required"] is True


def test_x_prompt_source_is_ready_with_only_bound_model(source_service, monkeypatch):
    _, service = source_service
    service.configure("x", {"method": "prompt"})
    monkeypatch.setattr(service.ai, "resolved", lambda *args, **kwargs: {
        "provider_id": "gateway", "provider_name": "Grok Gateway", "kind": "openai-compatible",
        "model": "grok-4.5-search", "capabilities": {"x_search": "unknown"},
    })
    state = service.public_state()
    x = next(row for row in state["items"] if row["platform"] == "x")
    assert x["status"] == "ready"
    assert x["automatic"] is True
    assert x["mode"] == "prompt"
    assert "Prompt" in x["detail"]


def test_legacy_xai_source_alias_migrates_to_prompt(source_service):
    _, service = source_service
    service.configure("x", {"method": "xai"})
    state = service._state()
    assert state["x"]["method"] == "prompt"


def test_x_prompt_discovery_only_returns_verified_status_identity(source_service, monkeypatch):
    _, service = source_service
    seen = []

    def fake_prompt_route(purpose, prompt, **kwargs):
        seen.append((purpose, prompt, kwargs))
        return {
            "provider_id": "gateway", "model": "grok-search",
            "text": json.dumps({"search_available": True, "campaigns": [
                {"title": "Vizard Agent Challenge", "organizer": "@vizard_ai",
                 "source_url": "https://x.com/vizard_ai/status/123", "external_id": "123"},
                {"title": "Bad Link", "source_url": "https://x.com/brand"},
            ]}, ensure_ascii=False),
        }

    monkeypatch.setattr(service.ai, "prompt_route", fake_prompt_route)
    rows = service._x_prompt()
    assert len(rows) == 1
    assert seen[0][0] == "x_campaign_discovery"
    assert "只做活动发现" in seen[0][1]
    assert "不要分析完整规则" in seen[0][1]
    assert seen[0][2]["max_tokens"] == 1200
    row = rows[0]
    assert row["external_id"] == "x:123"
    assert row["source_type"] == "x_model_prompt"
    assert row["source_status"] == "model_reported"
    assert row["summary"] == ""
    assert row["eligibility"] == []
    assert row["winning_conditions"] == []
    assert row["evidence"]["kind"] == "model_prompt_x_discovery"


def test_x_prompt_single_campaign_enrichment_returns_chinese_rules(source_service, monkeypatch):
    _, service = source_service
    seen = []

    def fake_prompt_route(purpose, prompt, **kwargs):
        seen.append((purpose, prompt, kwargs))
        return {
            "provider_id": "gateway", "model": "grok-search",
            "text": json.dumps({
                "title": "Vizard Agent Challenge", "organizer": "@vizard_ai",
                "summary": "Vizard Agent Challenge 已开启，共设 6 个赛道。",
                "starts_at": "2026-09-18", "signup_deadline": "", "submit_deadline": "2026-10-06", "stats_deadline": "",
                "eligibility": ["免费参与，按活动要求选择赛道并提交作品"],
                "content_requirements": ["围绕所选赛道提交符合要求的创作项目"],
                "prizes": ["3,000 美元现金", "100,000 积分"],
                "reward_summary": "3,000 美元现金 + 100,000 积分",
                "winning_conditions": ["各赛道冠军及其他获奖作品按活动规则评选"],
                "reward_rules": ["6 名赛道冠军各获得 500 美元现金"],
                "required_topics": ["#VizardAgentChallenge"],
                "submission_spec": {
                    "formats": ["video"], "content_directions": ["按赛道要求完成创作"], "style_requirements": [],
                    "duration_seconds": {"min": None, "max": None}, "aspect_ratios": [], "resolutions": [],
                    "orientation": None, "image_count": {"min": None, "max": None},
                    "text_length": {"min": None, "max": None},
                    "live": {"min_duration_seconds": None, "required_category": "", "title_keywords": []},
                    "original_required": None, "first_publish_required": None, "exclusive_required": None,
                    "min_entries": 1, "max_entries": None, "submission_method": "通过活动要求提交作品",
                    "required_mentions": [], "required_music": [],
                },
                "ai_policy": "原帖未说明 AI 使用要求",
            }, ensure_ascii=False),
        }

    monkeypatch.setattr(service.ai, "prompt_route", fake_prompt_route)
    row = service.enrich_x_prompt_campaign({
        "platform": "x", "title": "Vizard Agent Challenge", "organizer": "@vizard_ai",
        "source_url": "https://x.com/vizard_ai/status/123",
    })
    assert "所有面向用户展示的文字必须使用简体中文" in seen[0][1]
    assert "只处理下面这一条已发现的 X 活动" in seen[0][1]
    assert row["summary"] == "Vizard Agent Challenge 已开启，共设 6 个赛道。"
    assert row["eligibility"] == ["免费参与，按活动要求选择赛道并提交作品"]
    assert row["prizes"] == ["3,000 美元现金", "100,000 积分"]
    assert row["winning_conditions"] == ["各赛道冠军及其他获奖作品按活动规则评选"]
    assert row["submission_spec"]["formats"] == ["video"]
    assert row["submission_spec"]["min_entries"] == 1
    assert row["source_status"] == "model_reported"
    assert row["evidence"]["kind"] == "model_prompt_x_rules"


def test_x_prompt_enrichment_drops_english_user_copy_and_invalid_dates(source_service, monkeypatch):
    _, service = source_service
    monkeypatch.setattr(service.ai, "prompt_route", lambda *args, **kwargs: {
        "provider_id": "gateway", "model": "grok-search",
        "text": json.dumps({
            "title": "English Title", "organizer": "@brand",
            "summary": "Join our creator challenge now",
            "starts_at": "September 18", "submit_deadline": "Oct 6",
            "eligibility": ["Open to everyone"], "content_requirements": ["Make a video"],
            "prizes": ["$3,000 cash"], "reward_summary": "$3,000 cash",
            "winning_conditions": ["Six champions win"], "reward_rules": ["$500 each"],
            "required_topics": ["#CreatorChallenge"],
            "submission_spec": {"formats": ["video"], "content_directions": ["Create anything"], "submission_method": "Post on X"},
            "ai_policy": "No AI restrictions stated",
        }),
    })
    row = service.enrich_x_prompt_campaign({
        "platform": "x", "title": "Original Activity", "organizer": "@brand",
        "source_url": "https://x.com/brand/status/999",
    })
    assert row["summary"] == ""
    assert row["starts_at"] == "" and row["submit_deadline"] == ""
    assert row["eligibility"] == [] and row["content_requirements"] == []
    assert row["prizes"] == [] and row["reward_summary"] == ""
    assert row["winning_conditions"] == [] and row["reward_rules"] == []
    assert row["submission_spec"]["formats"] == ["video"]
    assert row["submission_spec"]["content_directions"] == []
    assert row["submission_spec"]["submission_method"] == ""
    assert row["required_topics"] == ["#CreatorChallenge"]
    assert row["ai_policy"] == "unknown"


def test_x_source_accepts_verified_x_search_capability_from_compatible_gateway(source_service, monkeypatch):
    _, service = source_service
    service.configure("x", {"method": "x_search"})
    monkeypatch.setattr(service.ai, "resolved", lambda *args, **kwargs: {
        "provider_id": "gateway", "provider_name": "Grok Gateway", "kind": "openai-compatible",
        "model": "grok-4.5-search", "capabilities": {"x_search": "verified"},
        "capability_evidence": {"x_search": {"method": "responses_required_tool_v2", "model_id": "grok-4.5-search"}},
    })
    state = service.public_state()
    x = next(row for row in state["items"] if row["platform"] == "x")
    assert x["status"] == "ready"
    assert x["automatic"] is True
    assert "grok-4.5-search" in x["detail"]


def test_bilibili_detail_parser_does_not_invent_image_only_rules():
    payload = {"layerTree": [{"name": "EvaModal", "alias": "活动规则弹窗", "props": {
        "modalContentLayoutContainerProps": {"background": {"src": "//i0.hdslb.com/rules.png"}}
    }}]}
    html = '<script>window.__BILIACT_EVAPAGEDATA__ = ' + json.dumps(payload, ensure_ascii=False) + ';</script>'
    result = _bilibili_detail_from_html(html)
    assert result["eligibility"] == []
    assert result["prizes"] == []
    assert result["winning_conditions"] == []
    assert result["reward_rules"] == []
    assert result["image_only_rule_panels"] == 1


def test_bilibili_detail_rejects_off_domain_redirect(source_service):
    _, service = source_service

    class Response:
        status_code = 302
        headers = {"location": "https://example.com/steal"}
        content = b""
        text = ""
        def raise_for_status(self):
            return None

    class Client:
        def get(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(WorkflowError, match="非 B站"):
        service._bilibili_detail(Client(), "https://www.bilibili.com/blackboard/a.html", {}, force=True, strict=True, ttl=0)


def test_bilibili_list_description_is_only_used_as_reward_when_it_is_reward_like():
    assert _bili_reward_summary("32万奖励等你瓜分") == "32万奖励等你瓜分"
    assert _bili_reward_summary("参与活动可获流量扶持") == "参与活动可获流量扶持"
    assert _bili_reward_summary("从零开始的 bilibili only 特辑") == ""


def test_x_fallback_is_never_used_until_explicitly_enabled(source_service, monkeypatch):
    _, service = source_service
    service.configure("x", {"method": "prompt", "fallback_method": "x_api", "fallback_enabled": False})
    monkeypatch.setattr(service, "_x_method_state", lambda *args: ("ready", "test source"))
    calls = []

    def fake_method(method, state):
        calls.append(method)
        if method == "prompt":
            raise WorkflowError("primary failed", 502)
        return []

    monkeypatch.setattr(service, "_x_method", fake_method)
    first = service.refresh(["x"], force=True)
    assert calls == ["prompt"]
    assert first["results"][0]["status"] == "error"

    service.configure("x", {"method": "prompt", "fallback_method": "x_api", "fallback_enabled": True})
    calls.clear()
    second = service.refresh(["x"], force=True)
    assert calls == ["prompt", "x_api"]
    assert second["results"][0]["status"] == "fresh"
    assert second["results"][0]["fallback_used"] is True


def test_tikhub_fallback_is_opt_in_even_when_creator_portal_fails(source_service, monkeypatch):
    _, service = source_service
    calls = []
    monkeypatch.setattr(service, "_douyin_portal", lambda state: (_ for _ in ()).throw(WorkflowError("login required", 409)))
    monkeypatch.setattr(service, "_tikhub", lambda: calls.append("tikhub") or [])

    first = service.refresh(["douyin"], force=True)
    assert calls == []
    assert first["results"][0]["status"] == "needs_config"

    service.configure("douyin", {"tikhub_enabled": True})
    monkeypatch.setattr(service, "_tikhub_key", lambda: "configured")
    blocked = service.refresh(["douyin"], force=True)
    assert calls == []
    assert blocked["results"][0]["status"] == "error"
    second = service.refresh(["douyin"], force=True, allow_paid=True)
    assert calls == ["tikhub"]
    assert second["results"][0]["status"] == "fresh"
    assert second["results"][0]["fallback_used"] is True


def test_xhs_and_douyin_candidate_parsers_accept_creator_api_shapes_without_dom():
    fixture = {
        "data": {
            "list": [{
                "activityId": "a1",
                "activityName": "创作激励计划",
                "startTime": 1789000000,
                "endTime": 1791000000,
                "jumpUrl": "https://creator.example/events/a1",
                "description": "投稿参与",
            }]
        }
    }
    xhs = xhs_candidates(fixture)
    douyin = douyin_candidates(fixture)
    assert xhs[0]["external_id"] == "a1" and xhs[0]["title"] == "创作激励计划"
    assert douyin[0]["external_id"] == "a1" and douyin[0]["title"] == "创作激励计划"


def test_xiaohongshu_source_uses_connected_creator_account_and_marks_account_evidence(source_service, monkeypatch):
    workspace, service = source_service
    account = workspace.accounts.create(AccountInput(
        platform="xiaohongshu", label="创作者号", idempotency_key="xhs-campaign-fixture",
    ))
    with workspace.store.transaction() as state:
        state["accounts"][account["id"]].update(status="connected", identity={"logged_in": True, "name": "creator", "remote_id": "u1"})
    calls = []
    def fake_events(account_id, limit, detail_limit=5):
        calls.append((account_id, limit, detail_limit))
        return {
            "source": "creator_events_api_v2", "raw_count": 250, "detail_count": 1,
            "fetched_at": "2026-09-22T09:00:00+00:00",
            "orders": {
                "default": {"observed": True, "activity_ids": ["x1", "x2"], "raw_count": 2, "unique_count": 2,
                            "truncated": False, "observed_at": 1790067600,
                            "query": {"sort": "1", "type": "1", "source": "3", "topic_activity": "0"}},
                "latest": {"observed": True, "activity_ids": ["x2", "x1"], "raw_count": 2, "unique_count": 2,
                           "truncated": False, "observed_at": 1790067600,
                           "query": {"sort": "2", "type": "1", "source": "3", "topic_activity": "0"}},
            },
            "items": [{
                "external_id": "x1", "title": "小红书创作活动",
                "url": "https://fe.xiaohongshu.com/ditto/vincent/page1?resource_instance_id=1",
                "starts_at": 1790000000000, "ends_at": 1791000000000,
                "description": "秋天开了店喊你来坐坐", "promotion_summary": "秋天开了店喊你来坐坐",
                "reward_summary": "",
                "topics": [{"id": "t1", "name": "测试话题", "link": "xhsdiscover://topic/v2/t1"}],
                "content_requirements": ["发布#测试话题 话题笔记"],
                "prizes": ["2000流量券", "定制便携餐具包"],
                "winning_conditions": ["活动进度达到 10（进度单位以活动页说明为准）"],
                "reward_rules": ["活动进度达到 10（进度单位以活动页说明为准）：对应奖励「2000流量券」"],
                "required_topics": ["测试话题"],
                "submission_spec": {"formats": [], "submission_method": "通过活动页带指定话题发布笔记"},
                "qualification_state": "unknown",
                "qualification_basis": "任务可见，不代表满足全部参赛或领奖条件。",
                "xhs_detail_status": "parsed", "xhs_detail_version": 3, "xhs_detail_fetched_at": 1790067600,
                "field_evidence": {"prizes": {"source": "xhs_milestone_api", "originals": ["2000流量券", "定制便携餐具包"]}},
                "page_id": "page1", "instance_id": "1",
            }],
        }
    monkeypatch.setattr(workspace.xhs_ops, "events", fake_events)
    rows = service._xiaohongshu(service._state())
    assert calls == [(account["id"], 500, 12)]
    assert rows[0]["account_id"] == account["id"]
    assert rows[0]["evidence"]["kind"] == "platform_public_detail"
    assert rows[0]["evidence"]["topic_ids"] == ["t1"]
    assert rows[0]["source_type"] == "creator_events_api_v2"
    assert rows[0]["required_topics"] == ["测试话题"]
    assert rows[0]["content_requirements"] == ["发布#测试话题 话题笔记"]
    assert rows[0]["prizes"] == ["2000流量券", "定制便携餐具包"]
    assert rows[0]["reward_summary"] == "2000流量券、定制便携餐具包"
    assert rows[0]["winning_conditions"] == ["活动进度达到 10（进度单位以活动页说明为准）"]
    assert rows[0]["submission_spec"]["formats"] == []
    assert rows[0]["qualification_state"] == "unknown"
    assert rows[0]["xhs_detail_status"] == "parsed"
    assert rows[0]["field_evidence"]["prizes"]["source"] == "xhs_milestone_api"

    default = service.xiaohongshu_official_snapshot(account["id"], "default")
    latest = service.xiaohongshu_official_snapshot(account["id"], "latest")
    assert default["status"] == "fresh" and default["activity_ids"] == ["xhs:x1", "xhs:x2"]
    assert latest["status"] == "fresh" and latest["activity_ids"] == ["xhs:x2", "xhs:x1"]
    assert default["snapshot_id"] and latest["snapshot_id"]

    first_default_snapshot_id = default["snapshot_id"]
    same_order = {
        "fetched_at": "2026-09-22T10:00:00+00:00",
        "orders": {
            "default": {"observed": True, "activity_ids": ["x1", "x2"], "raw_count": 2, "unique_count": 2,
                        "truncated": False, "observed_at": 1790071200,
                        "query": {"sort": "1", "type": "1", "source": "3", "topic_activity": "0"}},
            "latest": {"observed": True, "activity_ids": ["x2", "x1"], "raw_count": 2, "unique_count": 2,
                       "truncated": False, "observed_at": 1790071200,
                       "query": {"sort": "2", "type": "1", "source": "3", "topic_activity": "0"}},
        },
    }
    service._record_xhs_official_snapshots(account["id"], same_order)
    refreshed_default = service.xiaohongshu_official_snapshot(account["id"], "default")
    assert refreshed_default["snapshot_id"] == first_default_snapshot_id
    assert refreshed_default["fetched_at"] == "2026-09-22T10:00:00+00:00"


    truncated_refresh = {
        "fetched_at": "2026-09-22T11:00:00+00:00",
        "orders": {
            "default": {"observed": True, "activity_ids": ["x1"], "raw_count": 249, "unique_count": 249,
                        "truncated": True, "observed_at": 1790074800,
                        "query": {"sort": "1", "type": "1", "source": "3", "topic_activity": "0"}},
            "latest": {"observed": True, "activity_ids": ["x2"], "raw_count": 249, "unique_count": 249,
                       "truncated": True, "observed_at": 1790074800,
                       "query": {"sort": "2", "type": "1", "source": "3", "topic_activity": "0"}},
        },
    }
    service._record_xhs_official_snapshots(account["id"], truncated_refresh)
    preserved_default = service.xiaohongshu_official_snapshot(account["id"], "default")
    preserved_latest = service.xiaohongshu_official_snapshot(account["id"], "latest")
    assert preserved_default["activity_ids"] == ["xhs:x1", "xhs:x2"]
    assert preserved_latest["activity_ids"] == ["xhs:x2", "xhs:x1"]
    assert preserved_default["status"] == preserved_latest["status"] == "stale"
    assert preserved_default["last_observed_source_count"] == 249
    assert "截断" in preserved_default["error"]


def test_xiaohongshu_list_only_activity_uses_publish_topic_without_guessing_formats_or_reward(source_service, monkeypatch):
    workspace, service = source_service
    account = workspace.accounts.create(AccountInput(
        platform="xiaohongshu", label="列表号", idempotency_key="xhs-list-only",
    ))
    with workspace.store.transaction() as state:
        state["accounts"][account["id"]].update(status="connected", identity={"logged_in": True, "name": "creator", "remote_id": "u-list"})
    monkeypatch.setattr(workspace.xhs_ops, "events", lambda account_id, limit, detail_limit=12: {
        "source": "creator_activity_center_api", "api_observed": True, "raw_count": 1,
        "orders": {
            "default": {"observed": True, "activity_ids": ["43010"], "raw_count": 1, "unique_count": 1, "truncated": False},
            "latest": {"observed": True, "activity_ids": ["43010"], "raw_count": 1, "unique_count": 1, "truncated": False},
        },
        "items": [{
            "external_id": "43010", "title": "Pick你的每周时刻",
            "url": "https://fe.xiaohongshu.com/ditto/vincent/page-pick",
            "description": "带话题发笔记薯来送流量", "promotion_summary": "带话题发笔记薯来送流量",
            "reward_summary": "带话题发笔记薯来送流量",
            "topics": [{"id": "t-weekly", "name": "WeeklyPick", "link": "xhsdiscover://topic/v2/t-weekly"}],
            "publish_url": "https://creator.xiaohongshu.com/publish/publish?from=activity_center&activity_id=43010",
        }],
    })
    row = service._xiaohongshu(service._state())[0]
    assert row["required_topics"] == ["WeeklyPick"]
    assert row["content_requirements"] == ["发布时带 #WeeklyPick 话题"]
    assert row["submission_spec"]["formats"] == []
    assert row["submission_spec"]["submission_method"] == "通过活动中心指定投稿入口发布并带指定话题"
    assert row["qualification_state"] == "unknown"
    assert row["evidence"]["kind"] == "creator_account_activity"
    assert row["xhs_detail_status"] == "not_fetched"




def test_xiaohongshu_single_campaign_detail_refresh_uses_official_detail_reader(source_service, monkeypatch):
    workspace, service = source_service
    account = workspace.accounts.create(AccountInput(
        platform="xiaohongshu", label="详情刷新号", idempotency_key="xhs-detail-refresh",
    ))
    with workspace.store.transaction() as state:
        state["accounts"][account["id"]].update(status="connected", identity={"logged_in": True, "name": "creator", "remote_id": "u4"})
    calls = []
    monkeypatch.setattr(workspace.xhs_ops, "event_detail", lambda account_id, **kwargs: (
        calls.append((account_id, kwargs)) or {
            "source": "creator_event_detail", "external_id": "44728",
            "url": "https://fe.xiaohongshu.com/ditto/vincent/page1",
            "xhs_detail_status": "parsed", "xhs_detail_version": 3, "xhs_detail_fetched_at": 20,
            "prizes": ["2000流量券"], "winning_conditions": ["活动进度达到 10（进度单位以活动页说明为准）"],
            "reward_rules": ["活动进度达到 10（进度单位以活动页说明为准）：对应奖励「2000流量券」"],
            "eligibility": [], "content_requirements": ["连更打卡任务"], "required_topics": ["去阿秋店里坐坐"],
            "submission_spec": {"formats": [], "submission_method": "通过活动页带指定话题发布笔记"},
            "qualification_state": "unknown", "qualification_basis": "任务可见，不代表满足全部参赛或领奖条件。",
            "field_evidence": {"prizes": {"source": "xhs_milestone_api", "originals": ["2000流量券"]}},
        }
    ))
    candidate = service.verify_xiaohongshu_campaign({
        "title": "去阿秋店里坐坐", "platform": "xiaohongshu", "account_id": account["id"],
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page1",
        "summary": "秋天开了店喊你来坐坐", "required_topics": ["去阿秋店里坐坐"],
        "external_ids": {"xiaohongshu_creator_events": "xhs:44728"},
    })
    assert calls == [(account["id"], {
        "url": "https://fe.xiaohongshu.com/ditto/vincent/page1", "activity_id": "44728", "force": True,
    })]
    assert candidate["evidence"]["kind"] == "platform_public_detail"
    assert candidate["prizes"] == ["2000流量券"]
    assert candidate["reward_summary"] == "2000流量券"
    assert candidate["qualification_state"] == "unknown"
    assert candidate["submission_spec"]["formats"] == []



def test_xiaohongshu_official_snapshot_keeps_last_good_sort_when_one_sort_fails(source_service, monkeypatch):
    workspace, service = source_service
    account = workspace.accounts.create(AccountInput(
        platform="xiaohongshu", label="排序快照号", idempotency_key="xhs-sort-snapshot",
    ))
    with workspace.store.transaction() as state:
        state["accounts"][account["id"]].update(status="connected", identity={"logged_in": True, "name": "creator", "remote_id": "u3"})

    payloads = [
        {
            "source": "creator_activity_center_api", "api_observed": True, "raw_count": 2,
            "fetched_at": "2026-09-22T09:00:00+00:00",
            "orders": {
                "default": {"observed": True, "activity_ids": ["1", "2"], "raw_count": 2, "unique_count": 2, "truncated": False, "observed_at": 10},
                "latest": {"observed": True, "activity_ids": ["2", "1"], "raw_count": 2, "unique_count": 2, "truncated": False, "observed_at": 10},
            },
            "items": [{"external_id": "1", "title": "A"}, {"external_id": "2", "title": "B"}],
        },
        {
            "source": "creator_activity_center_api", "api_observed": True, "raw_count": 2,
            "fetched_at": "2026-09-22T10:00:00+00:00",
            "orders": {
                "default": {"observed": True, "activity_ids": ["2", "1"], "raw_count": 2, "unique_count": 2, "truncated": False, "observed_at": 20},
                "latest": {"observed": False, "activity_ids": [], "raw_count": 0, "unique_count": 0, "truncated": False, "observed_at": 20, "error": "latest_sort_option_missing"},
            },
            "items": [{"external_id": "2", "title": "B"}, {"external_id": "1", "title": "A"}],
        },
    ]
    monkeypatch.setattr(workspace.xhs_ops, "events", lambda *args, **kwargs: payloads.pop(0))
    service._xiaohongshu(service._state())
    old_latest = service.xiaohongshu_official_snapshot(account["id"], "latest")
    service._xiaohongshu(service._state())
    new_default = service.xiaohongshu_official_snapshot(account["id"], "default")
    new_latest = service.xiaohongshu_official_snapshot(account["id"], "latest")
    assert new_default["activity_ids"] == ["xhs:2", "xhs:1"] and new_default["status"] == "fresh"
    assert new_latest["activity_ids"] == old_latest["activity_ids"] == ["xhs:2", "xhs:1"]
    assert new_latest["status"] == "stale"
    assert new_latest["error"] == "latest_sort_option_missing"



def test_xiaohongshu_source_treats_missing_api_and_empty_dom_as_transient_failure(source_service, monkeypatch):
    workspace, service = source_service
    account = workspace.accounts.create(AccountInput(
        platform="xiaohongshu", label="创作者号", idempotency_key="xhs-empty-fixture",
    ))
    with workspace.store.transaction() as state:
        state["accounts"][account["id"]].update(status="connected", identity={"logged_in": True, "name": "creator", "remote_id": "u2"})
    monkeypatch.setattr(workspace.xhs_ops, "events", lambda account_id, limit, detail_limit=8: {
        "source": "creator_events_dom", "api_observed": False, "raw_count": 0, "items": [],
    })
    with pytest.raises(WorkflowError, match="活动列表接口本次未返回数据"):
        service._xiaohongshu(service._state())

    monkeypatch.setattr(workspace.xhs_ops, "events", lambda account_id, limit, detail_limit=8: {
        "source": "creator_activity_center_api", "api_observed": True, "raw_count": 0, "items": [],
    })
    assert service._xiaohongshu(service._state()) == []
