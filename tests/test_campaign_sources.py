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


def test_campaign_scheduler_intervals_and_failed_attempt_backoff(source_service):
    _, service = source_service
    assert service.sync_interval("bilibili") == 30 * 60
    assert service.sync_interval("x") == 2 * 60 * 60
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


def test_x_source_accepts_verified_x_search_capability_from_compatible_gateway(source_service, monkeypatch):
    _, service = source_service
    service.configure("x", {"method": "xai"})
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
    service.configure("x", {"method": "xai", "fallback_method": "x_api", "fallback_enabled": False})
    calls = []

    def fake_method(method, state):
        calls.append(method)
        if method == "xai":
            raise WorkflowError("primary failed", 502)
        return []

    monkeypatch.setattr(service, "_x_method", fake_method)
    first = service.refresh(["x"], force=True)
    assert calls == ["xai"]
    assert first["results"][0]["status"] == "error"

    service.configure("x", {"method": "xai", "fallback_method": "x_api", "fallback_enabled": True})
    calls.clear()
    second = service.refresh(["x"], force=True)
    assert calls == ["xai", "x_api"]
    assert second["results"][0]["status"] == "fresh"
    assert second["results"][0]["fallback_used"] is True


def test_tikhub_fallback_is_opt_in_even_when_creator_portal_fails(source_service, monkeypatch):
    _, service = source_service
    calls = []
    monkeypatch.setattr(service, "_douyin_portal", lambda state: (_ for _ in ()).throw(WorkflowError("login required", 409)))
    monkeypatch.setattr(service, "_tikhub", lambda: calls.append("tikhub") or [])

    first = service.refresh(["douyin"], force=True)
    assert calls == []
    assert first["results"][0]["status"] == "needs_login"

    service.configure("douyin", {"tikhub_enabled": True})
    monkeypatch.setattr(service, "_tikhub_key", lambda: "configured")
    second = service.refresh(["douyin"], force=True)
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
    monkeypatch.setattr(workspace.xhs_ops, "events", lambda account_id, limit: {
        "source": "creator_events_api",
        "items": [{"external_id": "x1", "title": "小红书创作活动", "url": "https://creator.xiaohongshu.com/new/events",
                   "starts_at": "", "ends_at": "", "description": "官方创作服务平台活动"}],
    })
    rows = service._xiaohongshu(service._state())
    assert rows[0]["account_id"] == account["id"]
    assert rows[0]["evidence"]["kind"] == "creator_account"
    assert rows[0]["source_type"] == "creator_events_api"
