from __future__ import annotations

import asyncio
import threading

import pytest

from web import app as upstream


def _row(index=1, **changes):
    row = {
        "id": f"activity-{index}", "title": "普通活动", "platform": "bilibili",
        "status": "active", "qualification_state": "eligible", "activity_type": "征稿",
        "reward_type": "现金", "summary": "", "organizer": "", "saved": False,
        "updated_at": index, "created_at": index, "account_id": "",
    }
    row.update(changes)
    return row


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    return upstream._write_campaigns


def test_keyword_searches_all_rows_before_pagination(store):
    rows = [_row(i, title=f"创作活动 {i}") for i in range(1, 28)]
    rows[0]["summary"] = "分页外的目标活动"
    store(rows)
    first = upstream._campaign_page_result(platform="bilibili", sort="new")
    assert "activity-1" not in {row["id"] for row in first["items"]}
    found = upstream._campaign_page_result(platform="bilibili", sort="new", q="目标活动", page=99)
    assert [row["id"] for row in found["items"]] == ["activity-1"]
    assert (found["total"], found["page"], found["total_pages"]) == (1, 1, 1)
    last = upstream._campaign_page_result(platform="bilibili", sort="new", q="创作活动", page=3)
    assert (last["total"], last["range_start"], last["range_end"]) == (27, 21, 27)
    assert len(last["items"]) == 7


@pytest.mark.parametrize("platform", ["all", "x", "xiaohongshu", "douyin", "bilibili", "wechat", "weixin-channels"])
def test_keyword_respects_each_platform(store, monkeypatch, platform):
    platforms = list(upstream.CAMPAIGN_PLATFORM_LABELS)
    rows = [_row(i, platform=p, title="AI 创作活动", external_ids={"xiaohongshu_creator_events": "xhs:1"})
            for i, p in enumerate(platforms, 1)]
    store(rows)
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "xiaohongshu_official_snapshot", lambda *args: {
        "account_id": "", "status": "fresh", "snapshot_id": "snapshot-1",
        "activity_ids": ["xhs:1"], "source_count": 1,
    })
    result = asyncio.run(upstream.api_campaign_page(
        platform=platform, sort="default" if platform == "xiaohongshu" else "recommend", q="AI"))
    assert result["total"] == (6 if platform == "all" else 1)
    assert all(platform == "all" or row["platform"] == platform for row in result["items"])


@pytest.mark.parametrize("field", ["title", "organizer", "summary", "activity_type", "reward_type", "reward_summary",
                                    "required_topics", "content_requirements", "eligibility", "prizes",
                                    "winning_conditions", "reward_rules"])
def test_searchable_display_fields(store, field):
    scalar = {"title", "organizer", "summary", "activity_type", "reward_type", "reward_summary"}
    value = "独特关键词" if field in scalar else ["独特关键词"]
    store([_row(**{field: value}), _row(2)])
    result = upstream._campaign_page_result(q="独特关键词")
    assert [row["id"] for row in result["items"]] == ["activity-1"]


def test_search_normalizes_case_width_whitespace_and_matches_all_terms(store):
    store([_row(title="AI 影像", organizer="Adobe", summary="校园创作"), _row(2, title="AI 影像")])
    result = upstream._campaign_page_result(q="  ＡＩ　ADOBE  校园  ")
    assert result["query"] == "ai adobe 校园"
    assert [row["id"] for row in result["items"]] == ["activity-1"]
    assert upstream._campaign_page_result(q="AI 不存在")["total"] == 0
    assert upstream._campaign_page_result(q=" \t\n ")["total"] == 2


@pytest.mark.parametrize("keyword", [".*", "%", "[x]", "+", "all"])
def test_search_metacharacters_and_all_are_literal(store, keyword):
    store([_row(title=f"创作 {keyword}"), _row(2)])
    result = asyncio.run(upstream.api_campaign_page(q=keyword))
    assert [row["id"] for row in result["items"]] == ["activity-1"]


def test_internal_evidence_and_account_ids_are_not_search_fields(store):
    store([_row(source_evidence=[{"text": "内部证据"}], account_id="隐藏账号", external_ids={"private": "内部标识"})])
    for keyword in ["内部证据", "隐藏账号", "内部标识"]:
        assert upstream._campaign_page_result(q=keyword)["total"] == 0


def test_search_combines_existing_filters_and_saved_sort(store):
    store([
        _row(1, title="科技征稿", account_id="a", saved=True),
        _row(2, title="科技征稿", account_id="b", saved=True),
        _row(3, title="科技征稿", account_id="a", saved=False),
        _row(4, title="科技征稿", account_id="a", saved=True, qualification_state="ineligible"),
        _row(5, title="科技征稿", account_id="a", saved=True, reward_type="流量"),
    ])
    result = upstream._campaign_page_result(platform="bilibili", q="科技", account_id="a", sort="saved",
                                            qualification="eligible", activity_type="征稿", reward_type="现金", deadline="none")
    assert [row["id"] for row in result["items"]] == ["activity-1"]


def test_search_preserves_xhs_official_order_and_snapshot_guard(store, monkeypatch):
    store([_row(i, platform="xiaohongshu", title="目标活动" if i % 2 == 0 else "其他活动",
                external_ids={"xiaohongshu_creator_events": f"xhs:{i}"}) for i in range(1, 14)])
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "xiaohongshu_official_snapshot", lambda account, sort: {
        "snapshot_id": "snap-1", "status": "fresh", "source_count": 13,
        "activity_ids": [f"xhs:{i}" for i in (range(13, 0, -1) if sort == "default" else range(1, 14))],
    })
    for sort, ids in [("default", [12, 10, 8, 6, 4, 2]), ("latest", [2, 4, 6, 8, 10, 12])]:
        result = upstream._campaign_page_result(platform="xiaohongshu", sort=sort, q="目标", snapshot_id="snap-1")
        assert [row["id"] for row in result["items"]] == [f"activity-{i}" for i in ids]
        assert result["source_total"] == 13 and result["total"] == 6
    with pytest.raises(upstream.WorkflowError) as exc:
        upstream._campaign_page_result(platform="xiaohongshu", sort="default", q="目标", snapshot_id="outdated")
    assert exc.value.status == 409


def test_overlong_keyword_rejected_and_no_results_have_correct_paging(store):
    store([_row()])
    with pytest.raises(upstream.WorkflowError) as exc:
        upstream._campaign_page_result(q="字" * 121)
    assert exc.value.status == 422
    result = upstream._campaign_page_result(q="不存在的活动", page=12)
    assert (result["total"], result["page"], result["total_pages"], result["range_start"], result["range_end"]) == (0, 1, 1, 0, 0)


def test_activity_page_work_does_not_block_event_loop(monkeypatch):
    main_thread = threading.get_ident()
    def result(**kwargs):
        assert threading.get_ident() != main_thread
        assert kwargs["q"] == "活动"
        return {"items": []}
    monkeypatch.setattr(upstream, "_campaign_page_result", result)
    assert asyncio.run(upstream.api_campaign_page(q="活动")) == {"items": []}


def test_source_inspection_does_not_block_activity_requests(monkeypatch):
    main_thread = threading.get_ident()
    def inspect():
        assert threading.get_ident() != main_thread
        return {"items": [], "automatic_count": 0}
    monkeypatch.setattr(upstream, "_ensure_ai_provider_migration", lambda: None)
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, "public_state", inspect)
    assert asyncio.run(upstream.api_campaign_sources())["automatic_count"] == 0
