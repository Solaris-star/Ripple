from __future__ import annotations

from pathlib import Path
import json

import pytest

from ripple.accounts import AccountInput
from ripple.ai_providers import AIProviderService
from ripple.campaign_sources import CampaignSourceService, _bili_reward_summary, _bilibili_detail_from_html
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
