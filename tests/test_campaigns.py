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

def test_campaign_page_boundaries_and_legacy_list_compatibility(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")

    def rows(count):
        return [{
            "id": f"bili-{index}", "title": f"活动 {index}", "platform": "bilibili",
            "platform_label": "B站", "status": "active", "qualification_state": "unknown",
            "activity_type": "创作活动", "reward_type": "", "submit_deadline": "",
            "saved": False, "created_at": index, "updated_at": index,
            "external_ids": {"bilibili_public": f"bilibili:{index}"},
        } for index in range(1, count + 1)]

    for count, expected_pages, first_page_count in ((0, 1, 0), (1, 1, 1), (10, 1, 10), (11, 2, 10), (250, 25, 10)):
        upstream._write_campaigns(rows(count))
        result = upstream._campaign_page_result(platform="bilibili", sort="new", page=1)
        assert result["total"] == count
        assert result["total_pages"] == expected_pages
        assert len(result["items"]) == first_page_count
        assert result["page_size"] == 10
        if count:
            assert result["range_start"] == 1 and result["range_end"] == min(10, count)
        else:
            assert result["range_start"] == 0 and result["range_end"] == 0

    upstream._write_campaigns(rows(11))
    second = upstream._campaign_page_result(platform="bilibili", sort="new", page=2)
    assert [row["id"] for row in second["items"]] == ["bili-1"]
    assert second["range_start"] == 11 and second["range_end"] == 11

    dated = rows(1)
    dated[0]["submit_deadline"] = "2099-01-01"
    upstream._write_campaigns(dated)
    dated_page = upstream._campaign_page_result(platform="bilibili", sort="deadline", page=1)
    assert dated_page["items"][0]["submit_deadline"] == "2099-01-01"

    upstream._write_campaigns(rows(11))
    legacy = asyncio.run(upstream.api_campaign_list())
    assert isinstance(legacy, list) and len(legacy) == 11
    paths = [getattr(route, "path", "") for route in upstream.app.routes]
    assert paths.index("/api/campaigns/page") < paths.index("/api/campaigns/{cid}")


def test_xhs_campaign_page_uses_exact_official_order_and_filters_before_pagination(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    rows = []
    for index in range(1, 13):
        rows.append({
            "id": f"xhs-{index}",
            "title": "同名活动" if index in {5, 6} else f"小红书活动 {index}",
            "platform": "xiaohongshu", "platform_label": "小红书",
            "status": "active", "qualification_state": "unknown",
            "activity_type": "精选" if index % 2 == 0 else "普通",
            "reward_type": "现金" if index % 3 == 0 else "",
            "submit_deadline": "", "saved": False,
            "account_id": "account-1",
            "account_states": {"account-1": {"visible": True}},
            "created_at": index, "updated_at": 9999 - index,
            "external_ids": {"xiaohongshu_creator_events": f"xhs:{index}"},
        })
    upstream._write_campaigns(rows)
    default_ids = [f"xhs:{index}" for index in range(12, 0, -1)]
    latest_ids = [f"xhs:{index}" for index in range(1, 13)]

    def snapshot(account_id="", sort_name="default"):
        ids = default_ids if sort_name == "default" else latest_ids
        return {
            "account_id": account_id or "account-1", "sort": sort_name,
            "status": "fresh", "snapshot_id": f"snap-{sort_name}",
            "activity_ids": ids, "source_count": len(ids), "fetched_at": "2026-09-22T09:00:00+00:00",
            "truncated": False,
        }
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "xiaohongshu_official_snapshot", snapshot)

    first = upstream._campaign_page_result(
        platform="xiaohongshu", account_id="account-1", sort="default", page=1,
    )
    assert [row["external_ids"]["xiaohongshu_creator_events"] for row in first["items"]] == default_ids[:10]
    assert first["total"] == 12 and first["total_pages"] == 2
    assert first["snapshot_id"] == "snap-default"
    assert first["source_total"] == 12
    assert first["missing_source_items"] == 0

    latest = upstream._campaign_page_result(
        platform="xiaohongshu", account_id="account-1", sort="latest", page=1,
    )
    assert [row["external_ids"]["xiaohongshu_creator_events"] for row in latest["items"]] == latest_ids[:10]

    filtered = upstream._campaign_page_result(
        platform="xiaohongshu", account_id="account-1", sort="default", page=1, activity_type="精选",
    )
    assert filtered["total"] == 6
    assert [row["external_ids"]["xiaohongshu_creator_events"] for row in filtered["items"]] == [
        "xhs:12", "xhs:10", "xhs:8", "xhs:6", "xhs:4", "xhs:2",
    ]
    # Same title never collapses official IDs.
    assert {row["external_ids"]["xiaohongshu_creator_events"] for row in first["items"] if row["title"] == "同名活动"} == {"xhs:6", "xhs:5"}


def test_xhs_campaign_page_rejects_missing_or_changed_official_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    upstream._write_campaigns([])
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "xiaohongshu_official_snapshot", lambda *args, **kwargs: {
        "account_id": "account-1", "status": "missing", "snapshot_id": "", "activity_ids": [],
        "source_count": 0, "fetched_at": "", "truncated": False,
    })
    try:
        upstream._campaign_page_result(platform="xiaohongshu", account_id="account-1", sort="default")
        assert False, "expected WorkflowError"
    except upstream.WorkflowError as exc:
        assert exc.status == 409

    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "xiaohongshu_official_snapshot", lambda *args, **kwargs: {
        "account_id": "account-1", "status": "fresh", "snapshot_id": "new-snapshot",
        "activity_ids": ["xhs:1"], "source_count": 1, "fetched_at": "2026-09-22T09:00:00+00:00",
        "truncated": False,
    })
    try:
        upstream._campaign_page_result(
            platform="xiaohongshu", account_id="account-1", sort="default", snapshot_id="old-snapshot",
        )
        assert False, "expected WorkflowError"
    except upstream.WorkflowError as exc:
        assert exc.status == 409



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


def test_xhs_authoritative_detail_replaces_old_program_inference_and_promo_reward(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    row = upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
        "external_id": "xhs:44728", "title": "去阿秋店里坐坐",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page1",
        "source_type": "creator_activity_center_api", "source_status": "verified",
        "account_id": "xhs-account-1",
        "reward_summary": "秋天开了店喊你来坐坐",
        "qualification_state": "eligible", "qualification_basis": "旧解析器推断",
        "submission_spec": {"formats": ["image_text", "video"], "submission_method": "通过活动页发布"},
        "evidence": {"kind": "creator_account_activity", "account_id": "xhs-account-1"},
    })
    assert row["qualification_state"] == "eligible"
    updated = upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
        "external_id": "xhs:44728", "title": "去阿秋店里坐坐",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page1",
        "source_type": "creator_event_detail", "source_status": "verified",
        "account_id": "xhs-account-1",
        "reward_summary": "2000流量券、定制便携餐具包、定制阿秋薯公仔",
        "prizes": ["2000流量券", "定制便携餐具包", "定制阿秋薯公仔"],
        "winning_conditions": ["活动进度达到 10（进度单位以活动页说明为准）"],
        "qualification_state": "unknown",
        "qualification_basis": "任务可见，不代表满足全部参赛或领奖条件。",
        "submission_spec": {"formats": [], "submission_method": "通过活动页带指定话题发布笔记"},
        "field_evidence": {"prizes": {"source": "xhs_milestone_api", "originals": ["2000流量券"]}},
        "xhs_detail_status": "parsed", "xhs_detail_version": 3, "xhs_detail_fetched_at": 20,
        "evidence": {"kind": "platform_public_detail", "account_id": "xhs-account-1", "detail_version": 3},
    })
    assert updated["qualification_state"] == "unknown"
    assert updated["submission_spec"]["formats"] == []
    assert updated["reward_summary"].startswith("2000流量券")
    assert updated["prizes"] == ["2000流量券", "定制便携餐具包", "定制阿秋薯公仔"]
    assert updated["winning_conditions"] == ["活动进度达到 10（进度单位以活动页说明为准）"]
    assert updated["field_evidence"]["prizes"]["source"] == "xhs_milestone_api"
    assert updated["xhs_detail_status"] == "parsed"
    assert updated["last_verified_at"] > 0


def test_xhs_list_refresh_does_not_downgrade_existing_detail_status(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    detailed = upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
        "external_id": "xhs:44728", "title": "去阿秋店里坐坐",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page1",
        "source_type": "creator_event_detail", "source_status": "verified",
        "account_id": "xhs-account-1",
        "prizes": ["2000流量券"], "reward_summary": "2000流量券",
        "qualification_state": "unknown",
        "xhs_detail_status": "parsed", "xhs_detail_version": 3, "xhs_detail_fetched_at": 20,
        "evidence": {"kind": "platform_public_detail", "account_id": "xhs-account-1", "detail_version": 3},
    })
    assert detailed["xhs_detail_status"] == "parsed"
    refreshed = upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
        "external_id": "xhs:44728", "title": "去阿秋店里坐坐",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page1",
        "source_type": "creator_activity_center_api", "source_status": "verified",
        "account_id": "xhs-account-1",
        "summary": "秋天开了店喊你来坐坐",
        "qualification_state": "unknown",
        "xhs_detail_status": "not_fetched", "xhs_detail_version": 0,
        "evidence": {"kind": "creator_account_activity", "account_id": "xhs-account-1"},
    })
    assert refreshed["xhs_detail_status"] == "parsed"
    assert refreshed["xhs_detail_version"] == 3
    assert refreshed["xhs_detail_fetched_at"] == 20
    assert refreshed["prizes"] == ["2000流量券"]


def test_xhs_visual_review_detail_clears_old_program_inference_without_claiming_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
        "external_id": "xhs:43010", "title": "Pick你的每周时刻",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page-pick",
        "source_type": "creator_activity_center_api", "source_status": "verified",
        "account_id": "xhs-account-1",
        "qualification_state": "eligible", "qualification_basis": "旧解析器推断",
        "submission_spec": {"formats": ["image_text", "video"], "submission_method": "旧推断"},
        "evidence": {"kind": "creator_account_activity", "account_id": "xhs-account-1"},
    })
    updated = upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
        "external_id": "xhs:43010", "title": "Pick你的每周时刻",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page-pick",
        "source_type": "creator_event_detail", "source_status": "verified",
        "account_id": "xhs-account-1",
        "qualification_state": "unknown",
        "qualification_basis": "详情页已读取，但未返回明确资格结论。",
        "submission_spec": {"formats": []},
        "eligibility": [], "content_requirements": [], "prizes": [], "winning_conditions": [], "reward_rules": [],
        "xhs_detail_status": "needs_visual_review", "xhs_detail_version": 3,
        "evidence": {"kind": "platform_public_detail", "account_id": "xhs-account-1", "detail_version": 3},
    })
    assert updated["xhs_detail_status"] == "needs_visual_review"
    assert updated["qualification_state"] == "unknown"
    assert updated["submission_spec"]["formats"] == []
    assert updated["eligibility"] == []
    assert updated["winning_conditions"] == []


def test_xhs_detail_refresh_preserves_user_confirmed_qualification_and_submission_spec(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    row = upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
        "external_id": "xhs:1", "title": "人工确认活动",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page1",
        "source_type": "creator_activity_center_api", "source_status": "verified",
        "account_id": "xhs-account-1", "qualification_state": "unknown",
        "evidence": {"kind": "creator_account_activity", "account_id": "xhs-account-1"},
    })
    row["qualification_state"] = "eligible"
    row["qualification_basis"] = "user_confirmed"
    row["submission_spec"] = upstream.normalize_submission_spec({"formats": ["video"], "submission_method": "人工确认投稿方式"})
    row["user_confirmed_fields"] = ["qualification_state", "submission_spec"]
    updated = upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
        "external_id": "xhs:1", "title": "人工确认活动",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page1",
        "source_type": "creator_event_detail", "source_status": "verified",
        "account_id": "xhs-account-1", "qualification_state": "unknown",
        "qualification_basis": "平台未返回完整资格结论。",
        "submission_spec": {"formats": []},
        "xhs_detail_status": "no_structured_rules", "xhs_detail_version": 3,
        "evidence": {"kind": "platform_public_detail", "account_id": "xhs-account-1", "detail_version": 3},
    })
    assert updated["qualification_state"] == "eligible"
    assert updated["qualification_basis"] == "user_confirmed"
    assert updated["submission_spec"]["formats"] == ["video"]
    assert updated["submission_spec"]["submission_method"] == "人工确认投稿方式"


def test_xhs_detail_api_merges_single_campaign_refresh(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    item = {
        "id": "xhs-detail-1", "title": "去阿秋店里坐坐", "platform": "xiaohongshu",
        "platform_label": "小红书", "source_type": "creator_activity_center_api", "source_status": "verified",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page1", "account_id": "a1",
        "qualification_state": "unknown", "created_at": 1, "updated_at": 1,
        "external_ids": {"xiaohongshu_creator_events": "xhs:44728"},
    }
    upstream._write_campaigns([item])
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "verify_xiaohongshu_campaign", lambda current: {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
        "external_id": "xhs:44728", "title": current["title"],
        "source_url": current["source_url"], "source_type": "creator_event_detail",
        "source_status": "verified", "account_id": "a1",
        "prizes": ["2000流量券"], "reward_summary": "2000流量券",
        "winning_conditions": ["活动进度达到 10（进度单位以活动页说明为准）"],
        "qualification_state": "unknown", "submission_spec": {"formats": []},
        "xhs_detail_status": "parsed", "xhs_detail_version": 3,
        "evidence": {"kind": "platform_public_detail", "account_id": "a1"},
    })
    updated = asyncio.run(upstream.api_campaign_xhs_detail("xhs-detail-1"))
    assert updated["prizes"] == ["2000流量券"]
    assert updated["xhs_detail_status"] == "parsed"
    assert upstream._read_campaigns()[0]["winning_conditions"]



def test_xhs_official_activity_id_prevents_same_title_campaigns_from_merging(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    items = []
    first = upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
        "external_id": "xhs:44405", "title": "原神创作者激励计划",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page-a",
        "source_type": "creator_activity_center_api", "source_status": "verified",
        "evidence": {"kind": "creator_account_activity"},
    })
    second = upstream._merge_campaign_candidate(items, {
        "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
        "external_id": "xhs:43521", "title": "原神创作者激励计划",
        "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page-b",
        "source_type": "creator_activity_center_api", "source_status": "verified",
        "evidence": {"kind": "creator_account_activity"},
    })
    assert first["id"] != second["id"]
    assert len(items) == 2
    assert {row["external_ids"]["xiaohongshu_creator_events"] for row in items} == {"xhs:44405", "xhs:43521"}


def test_fresh_xhs_v2_refresh_removes_only_confirmed_unsaved_legacy_topic_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    legacy_topic = {
        "id": "legacy-topic", "title": "光遇", "platform": "xiaohongshu",
        "source_type": "creator_events_api", "source_url": "https://creator.xiaohongshu.com/new/events",
        "source_status": "verified", "saved": False, "external_ids": {"xiaohongshu_creator_events": "xhs:t1"},
        "created_at": 1, "updated_at": 1,
    }
    legacy_activity = {
        **legacy_topic, "id": "legacy-activity", "title": "历史真实活动",
        "external_ids": {"xiaohongshu_creator_events": "xhs:44000"},
    }
    saved_topic = {**legacy_topic, "id": "saved-topic", "title": "已收藏旧话题", "saved": True}
    upstream._write_campaigns([legacy_topic, legacy_activity, saved_topic])
    payload = {"results": [{
        "platform": "xiaohongshu", "status": "fresh", "items": [{
            "provider_id": "xiaohongshu_creator_events", "platform": "xiaohongshu",
            "external_id": "xhs:44697", "title": "光遇联动狂欢月创作征集",
            "source_url": "https://fe.xiaohongshu.com/ditto/vincent/page1",
            "source_type": "creator_activity_center_api", "source_status": "verified",
            "evidence": {"kind": "creator_account_activity", "topic_ids": ["t1"]},
        }],
    }]}
    result = upstream._merge_campaign_refresh(payload)
    rows = upstream._read_campaigns()
    assert result["merged"] == 1
    assert all(row["id"] != "legacy-topic" for row in rows)
    assert any(row["id"] == "legacy-activity" for row in rows)
    assert any(row["id"] == "saved-topic" for row in rows)
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
