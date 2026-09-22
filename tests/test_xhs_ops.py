from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ripple.accounts import AccountInput
from ripple.api import install
from ripple.publishing import WorkflowError
from ripple.workspace import WorkspaceService
from ripple.xhs_browser import _creator_activity_rows, _creator_task_detail, _metrics, parse_note_url, public_note_url


def connected_service(tmp_path: Path) -> tuple[WorkspaceService, str]:
    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    account = service.accounts.create(AccountInput(
        platform="xiaohongshu", label="测试小红书", idempotency_key="xhs-test-account",
    ))
    with service.store.transaction() as state:
        row = state["accounts"][account["id"]]
        row.update(status="connected", identity={"logged_in": True, "name": "tester", "remote_id": "u1"})
    return service, account["id"]


def fake_read_result(action: str) -> dict:
    locator = "https://www.xiaohongshu.com/explore/note123?xsec_token=PRIVATE_TOKEN&xsec_source=pc"
    if action == "feed":
        return {"state": "success", "data": {"items": [{
            "note_id": "note123", "title": "推荐样本", "author": "作者", "url": "https://www.xiaohongshu.com/explore/note123",
            "scope": "recommended_feed", "metrics": {"views": None, "likes": 10, "collects": 3, "comments": 2, "shares": None},
        }], "_locators": [{"note_id": "note123", "url": locator}]}}
    if action == "search":
        return {"state": "success", "data": {"items": [{"note_id": "note123", "title": "搜索结果", "url": "https://www.xiaohongshu.com/explore/note123", "scope": "keyword_search", "metrics": {}}], "_locators": [{"note_id": "note123", "url": locator}]}}
    if action == "notes":
        return {"state": "success", "data": {"items": [{"note_id": "note123", "title": "我的作品", "url": "https://www.xiaohongshu.com/explore/note123", "scope": "account_notes", "metrics": {"likes": 12}}], "_locators": [{"note_id": "note123", "url": locator}]}}
    if action == "events":
        return {"state": "success", "data": {"source": "creator_events_api", "items": [{
            "external_id": "event1", "title": "创作活动", "url": "https://creator.xiaohongshu.com/new/events",
            "starts_at": "", "ends_at": "", "description": "活动详情",
        }]}}
    if action == "note":
        return {"state": "success", "data": {"note": {"note_id": "note123", "title": "详情", "body": "正文", "author": "作者", "url": "https://www.xiaohongshu.com/explore/note123", "scope": "note_detail", "metrics": {"likes": 12}, "images": []}, "_locators": [{"note_id": "note123", "url": locator}]}}
    if action == "comments":
        return {"state": "success", "data": {"note_id": "note123", "count": 1, "comments": [{"id": "c1", "nickname": "读者", "content": "怎么做？", "time": 1, "time_str": "", "like": "2", "parent": ""}], "_locators": [{"note_id": "note123", "url": locator}]}}
    raise AssertionError(action)


def test_creator_activity_rows_use_activity_list_only_and_keep_topics_nested():
    payload = {"data": {"activity_list": [{
        "activity_id": 44697, "activity_name": "光遇联动狂欢月创作征集",
        "page_id": "page1", "instance_id": 327001,
        "start_time": 1790000000000, "end_time": 1791000000000,
        "activity_link": "https://fe.xiaohongshu.com/ditto/vincent/page1?resource_instance_id=327001",
        "activity_reward": "参与投稿瓜分10W现金",
        "topic_infos": [{"id": "topic1", "name": "光遇", "link": "xhsdiscover://topic/v2/topic1"}],
    }]}}
    rows = _creator_activity_rows(payload, limit=20)
    assert len(rows) == 1
    assert rows[0]["external_id"] == "44697"
    assert rows[0]["title"] == "光遇联动狂欢月创作征集"
    assert rows[0]["reward_summary"] == "参与投稿瓜分10W现金"
    assert rows[0]["topics"] == [{"id": "topic1", "name": "光遇", "link": "xhsdiscover://topic/v2/topic1"}]


def test_creator_activity_rows_keep_full_activity_center_result_set():
    payload = {"data": {"activity_list": [
        {"activity_id": index, "activity_name": f"活动{index}"}
        for index in range(1, 251)
    ]}}
    rows = _creator_activity_rows(payload)
    assert len(rows) == 250
    assert rows[0]["external_id"] == "1"
    assert rows[-1]["external_id"] == "250"


def test_creator_task_detail_maps_publish_topic_rewards_and_account_eligibility():
    payload = {"data": {"enabled": 1, "status": 1, "score_name": "积分", "tasks": [
        {"event_type": "follow_user", "name": "关注@闪耀暖暖 官方账号", "description": "可获得10积分", "finished": False,
         "status": {"button_name": "去关注"}, "progress_info": {"current": 0, "total": 1}, "extend_field": {}},
        {"event_type": "post_note", "name": "发布#闪耀暖暖进组了 话题笔记", "description": "可获得10积分, 已完成 0/3", "finished": False,
         "status": {"button_name": "去发布"}, "progress_info": {"current": 0, "total": 3},
         "extend_field": {"publish_note_topic": '[{"name":"闪耀暖暖进组了"}]'}},
        {"event_type": "booster", "name": "邀请好友助力", "description": "可获得10积分", "finished": False,
         "status": {"button_name": "去邀请"}, "progress_info": {"current": 0, "total": 3},
         "extend_field": {"booster_invite_card": '{"title":"参与投稿瓜分10W现金！","desc":"闪耀暖暖有奖主题活动开启！参与活动赢丰厚好礼！"}'}},
    ]}}
    detail = _creator_task_detail(payload, "参与投稿瓜分10W现金")
    assert detail["qualification_state"] == "eligible"
    assert detail["required_topics"] == ["闪耀暖暖进组了"]
    assert detail["content_requirements"] == ["发布#闪耀暖暖进组了 话题笔记"]
    assert detail["submission_spec"]["formats"] == ["image_text", "video"]
    assert detail["submission_spec"]["submission_method"] == "通过活动页带指定话题发布笔记"
    assert any("关注@闪耀暖暖" in row for row in detail["reward_rules"])
    assert any("瓜分10W现金" in row for row in detail["prizes"])


def test_note_url_and_metrics_are_public_safe():
    note_id, token, original = parse_note_url(
        "https://www.xiaohongshu.com/explore/abc123?xsec_token=SECRET&xsec_source=pc"
    )
    assert note_id == "abc123" and token == "SECRET"
    assert "SECRET" in original
    assert public_note_url(original) == "https://www.xiaohongshu.com/explore/abc123"
    assert _metrics("点赞 1.2万 收藏 88 评论 9") == {
        "views": None, "likes": 12000, "collects": 88, "comments": 9, "shares": None,
    }
    with pytest.raises(Exception):
        parse_note_url("https://evil.example/explore/abc123?xsec_token=SECRET")


def test_reads_store_private_locator_but_never_project_token(tmp_path, monkeypatch):
    service, account_id = connected_service(tmp_path)
    calls = []

    def run(account, operation, operation_id, **extra):
        calls.append((operation, extra["xhs_action"]))
        return fake_read_result(extra["xhs_action"])

    monkeypatch.setattr(service.accounts, "run", run)
    feed = service.xhs_ops.feed(account_id, 12)
    assert feed["source"] == "recommended_feed"
    assert feed["items"][0]["title"] == "推荐样本"
    assert "PRIVATE_TOKEN" not in json.dumps(feed, ensure_ascii=False)

    locators = (service.accounts.directory(account_id) / "xhs-note-locators.json").read_text(encoding="utf-8")
    assert "PRIVATE_TOKEN" in locators

    note = service.xhs_ops.note(account_id, note_id="note123")
    comments = service.xhs_ops.comments(account_id, note_id="note123")
    assert note["note"]["body"] == "正文"
    assert comments["comments"][0]["id"] == "c1"
    snapshots = service.xhs_ops.snapshots(account_id)["items"]
    assert len(snapshots) == 3
    assert "PRIVATE_TOKEN" not in json.dumps(snapshots, ensure_ascii=False)
    assert calls == [("xhs_read", "feed"), ("xhs_read", "note"), ("xhs_read", "comments")]


def test_search_and_account_notes_have_explicit_sample_scope(tmp_path, monkeypatch):
    service, account_id = connected_service(tmp_path)
    monkeypatch.setattr(service.accounts, "run", lambda account, operation, operation_id, **extra: fake_read_result(extra["xhs_action"]))
    search = service.xhs_ops.search(account_id, "旅行", 8)
    notes = service.xhs_ops.notes(account_id, 8)
    assert search["sample_scope"] == "query:旅行"
    assert notes["sample_scope"] == "selected_account_creator_notes"
    assert search["items"][0]["scope"] == "keyword_search"


def test_creator_events_use_same_connected_xhs_profile_boundary(tmp_path, monkeypatch):
    service, account_id = connected_service(tmp_path)
    calls = []
    def run(account, operation, operation_id, **extra):
        calls.append((operation, extra["xhs_action"], extra["xhs_params"]))
        return fake_read_result(extra["xhs_action"])
    monkeypatch.setattr(service.accounts, "run", run)
    result = service.xhs_ops.events(account_id, 250, detail_limit=12)
    assert result["source"] == "creator_events_api"
    assert result["sample_scope"] == "selected_account_creator_events"
    assert result["items"][0]["external_id"] == "event1"
    assert calls == [("xhs_read", "events", {"limit": 250, "detail_limit": 12})]


def test_interaction_draft_idempotency_guard_and_execute_once(tmp_path, monkeypatch):
    service, account_id = connected_service(tmp_path)
    monkeypatch.setattr(service.accounts, "run", lambda account, operation, operation_id, **extra: fake_read_result(extra["xhs_action"]))
    service.xhs_ops.feed(account_id, 1)

    draft = service.xhs_ops.draft_interaction(
        account_id=account_id, note_id="note123", kind="reply",
        items=[{"id": "c1", "nickname": "读者", "content": "怎么做？", "reply": "可以先从第一步开始。"}],
        idempotency_key="reply-draft-001",
    )
    again = service.xhs_ops.draft_interaction(
        account_id=account_id, note_id="note123", kind="reply",
        items=[{"id": "c1", "nickname": "读者", "content": "怎么做？", "reply": "可以先从第一步开始。"}],
        idempotency_key="reply-draft-001",
    )
    assert again["id"] == draft["id"]
    assert "xsec_token" not in json.dumps(draft)
    with pytest.raises(WorkflowError, match="不同内容"):
        service.xhs_ops.draft_interaction(
            account_id=account_id, note_id="note123", kind="reply",
            items=[{"id": "c1", "nickname": "读者", "content": "怎么做？", "reply": "另一条回复"}],
            idempotency_key="reply-draft-001",
        )
    with pytest.raises(WorkflowError, match="疑似凭据"):
        service.xhs_ops.draft_interaction(
            account_id=account_id, note_id="note123", kind="comment", text="api_key=abcdefghijk",
            idempotency_key="bad-secret-001",
        )

    executions = []
    def execute_run(account, operation, operation_id, **extra):
        executions.append((operation, operation_id, extra["xhs_action"]))
        return {"state": "verified", "data": {"results": [{"id": "c1", "status": "verified", "reason": ""}]}}
    monkeypatch.setattr(service.accounts, "run", execute_run)
    result = service.xhs_ops.execute_interaction(draft["id"], True)
    duplicate = service.xhs_ops.execute_interaction(draft["id"], True)
    assert result["status"] == "verified"
    assert duplicate["status"] == "verified"
    assert len(executions) == 1
    assert executions[0][0] == "xhs_interact"


def test_unknown_interaction_requires_query_not_resubmit(tmp_path, monkeypatch):
    service, account_id = connected_service(tmp_path)
    locator = "https://www.xiaohongshu.com/explore/note123?xsec_token=PRIVATE_TOKEN"
    service.xhs_ops._save_locators(account_id, [{"note_id": "note123", "url": locator}])
    draft = service.xhs_ops.draft_interaction(
        account_id=account_id, note_id="note123", kind="comment", text="测试互动",
        idempotency_key="comment-draft-01",
    )
    sends = []
    def uncertain(account, operation, operation_id, **extra):
        sends.append(operation_id)
        return {"state": "unknown_result", "data": {"status": "unknown_result", "reason": ""}}
    monkeypatch.setattr(service.accounts, "run", uncertain)
    result = service.xhs_ops.execute_interaction(draft["id"], True)
    assert result["status"] == "unknown_result"
    same = service.xhs_ops.execute_interaction(draft["id"], True)
    assert same["status"] == "unknown_result" and len(sends) == 1

    monkeypatch.setattr(service.accounts, "read_result", lambda account_id, operation_id: {
        "state": "verified", "data": {"status": "verified", "reason": ""}, "operation_id": operation_id,
    })
    queried = service.xhs_ops.query_interaction(draft["id"])
    assert queried["status"] == "verified"
    assert service.accounts.get(account_id)["operation"] is None


def test_api_routes_wrap_same_xhs_service(tmp_path, monkeypatch):
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    account = service.accounts.create(AccountInput(platform="xiaohongshu", label="API 测试", idempotency_key="xhs-api-account"))
    with service.store.transaction() as state:
        state["accounts"][account["id"]].update(status="connected", identity={"logged_in": True, "name": "api", "remote_id": "u2"})
    monkeypatch.setattr(service.accounts, "run", lambda account, operation, operation_id, **extra: fake_read_result(extra["xhs_action"]))

    with TestClient(app, base_url="http://localhost") as client:
        feed = client.get(f"/api/ripple/xiaohongshu/accounts/{account['id']}/feed?limit=1")
        assert feed.status_code == 200 and feed.json()["items"][0]["note_id"] == "note123"
        note = client.post(f"/api/ripple/xiaohongshu/accounts/{account['id']}/note", json={"note_id": "note123", "url": ""})
        assert note.status_code == 200 and note.json()["note"]["title"] == "详情"
        interaction = client.post("/api/ripple/xiaohongshu/interactions", json={
            "account_id": account["id"], "note_id": "note123", "url": "", "kind": "reply",
            "items": [{"id": "c1", "nickname": "读者", "content": "怎么做？", "reply": "先做第一步"}],
            "text": "", "idempotency_key": "api-reply-draft",
        })
        assert interaction.status_code == 201 and interaction.json()["status"] == "draft"


def test_agent_xhs_tools_are_authenticated_read_only_and_draft_only(tmp_path, monkeypatch):
    from web import app as webapp

    service, account_id = connected_service(tmp_path)
    calls = []

    def run(account, operation, operation_id, **extra):
        calls.append((operation, extra["xhs_action"]))
        return fake_read_result(extra["xhs_action"])

    monkeypatch.setattr(service.accounts, "run", run)
    monkeypatch.setattr(webapp.app.state, "ripple", service)
    headers = {"X-Ripple-Agent-Token": webapp._AGENT_RUNTIME.tool_token}

    with TestClient(webapp.app, base_url="http://localhost") as client:
        denied = client.post("/api/ripple-agent/tools/xiaohongshu/read", json={"operation": "accounts"})
        assert denied.status_code == 403

        accounts = client.post("/api/ripple-agent/tools/xiaohongshu/read", headers=headers, json={"operation": "accounts"})
        assert accounts.status_code == 200
        assert accounts.json()["items"] == [{
            "id": account_id, "label": "测试小红书",
            "identity": {"logged_in": True, "name": "tester", "remote_id": "u1"},
        }]

        feed = client.post("/api/ripple-agent/tools/xiaohongshu/read", headers=headers, json={
            "operation": "feed", "account_id": account_id, "limit": 1,
        })
        assert feed.status_code == 200
        assert feed.json()["sample_scope"] == "selected_account_personalized_feed"
        assert "PRIVATE_TOKEN" not in feed.text

        draft = client.post("/api/ripple-agent/tools/xiaohongshu/interaction-draft", headers=headers, json={
            "account_id": account_id, "note_id": "note123", "kind": "reply",
            "items": [{"id": "c1", "nickname": "读者", "content": "怎么做？", "reply": "先从第一步开始。"}],
        })
        assert draft.status_code == 200
        body = draft.json()
        assert body["status"] == "draft"
        assert "未发送" in body["notice"]
        assert calls == [("xhs_read", "feed")]
