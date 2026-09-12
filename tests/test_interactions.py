from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ripple.accounts import AccountInput
from ripple.api import install
from ripple.publishing import WorkflowError
from ripple.workspace import WorkspaceService


def service_with_account(tmp_path: Path, platform: str = "xiaohongshu", *, connected: bool = False) -> tuple[WorkspaceService, str]:
    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    account = service.accounts.create(AccountInput(
        platform=platform, label=f"{platform}-test", idempotency_key=f"interaction-{platform}-account",
    ))
    if connected:
        with service.store.transaction() as state:
            state["accounts"][account["id"]].update(
                status="connected", identity={"logged_in": True, "name": "tester", "remote_id": "u1"},
            )
    return service, account["id"]


def seed_xhs_locator(service: WorkspaceService, account_id: str) -> None:
    service.xhs_ops._save_locators(account_id, [{
        "note_id": "note123",
        "url": "https://www.xiaohongshu.com/explore/note123?xsec_token=PRIVATE_TOKEN",
    }])


def seed_remote_source(service: WorkspaceService, account_id: str, source_id: str = "d" * 32) -> dict:
    source = {
        "id": source_id, "kind": "remote", "platform": "xiaohongshu", "label": "测试作品",
        "account_id": account_id, "account_label": "xiaohongshu-test", "target_id": "note123",
        "target_url": "https://www.xiaohongshu.com/explore/note123",
        "comments": [
            {"id": "c1", "nickname": "甲", "content": "怎么开始？", "time": 1, "time_str": "刚刚", "like": "2", "parent": ""},
            {"id": "c2", "nickname": "乙", "content": "求完整教程", "time": 2, "time_str": "1小时前", "like": "5", "parent": ""},
        ],
        "count": 2, "created_at": "2026-09-10T00:00:00+00:00", "updated_at": "2026-09-10T00:00:00+00:00",
    }
    with service.store.transaction() as state:
        state["interaction_sources"] = {source_id: source}
    return source


def seed_legacy_import_source(service: WorkspaceService, source_id: str = "e" * 32, platform: str = "generic") -> dict:
    source = {
        "id": source_id, "kind": "import", "platform": platform, "label": "旧版导入记录",
        "account_id": "", "account_label": "", "target_id": "", "target_url": "",
        "comments": [
            {"id": "local-c1", "nickname": "旧用户", "content": "参数是多少？", "time": 0, "time_str": "", "like": "", "parent": ""},
            {"id": "local-c2", "nickname": "旧用户2", "content": "价格太贵了，踩雷", "time": 0, "time_str": "", "like": "", "parent": ""},
        ],
        "count": 2, "created_at": "2026-09-01T00:00:00+00:00", "updated_at": "2026-09-01T00:00:00+00:00",
    }
    with service.store.transaction() as state:
        state.setdefault("interaction_sources", {})[source_id] = source
    return source


def test_capabilities_expose_real_platforms_only_and_fail_closed(tmp_path):
    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    rows = {row["platform"]: row for row in service.interactions.capabilities()["items"]}
    assert "generic" not in rows
    assert all("import_comments" not in row for row in rows.values())
    assert rows["xiaohongshu"]["read_comments"] is True
    assert rows["xiaohongshu"]["reply"] is True
    assert rows["xiaohongshu"]["platform_verify"] is False
    assert rows["zhihu"]["read_comments"] is False
    assert rows["zhihu"]["reply"] is False
    assert "互动能力尚未接入" in rows["zhihu"]["note"]


def test_legacy_import_source_remains_readable_and_analyzable_only(tmp_path):
    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    source = seed_legacy_import_source(service)
    summaries = service.interactions.sources()["items"]
    assert summaries[0]["id"] == source["id"]
    assert "comments" not in summaries[0]
    detail = service.interactions.get_source(source["id"])
    assert detail["kind"] == "import" and len(detail["comments"]) == 2
    report = service.interactions.analyze_source(source["id"])
    assert report["total"] == 2
    assert "question" in report["comment_labels"]["local-c1"]
    assert "negative" in report["comment_labels"]["local-c2"]

    xhs_import = seed_legacy_import_source(service, "f" * 32, platform="xiaohongshu")
    with pytest.raises(WorkflowError, match="只读保留"):
        service.interactions.create_draft(
            platform="xiaohongshu", source_id=xhs_import["id"], kind="reply",
            items=[{"id": "local-c1", "nickname": "旧用户", "content": "参数是多少？", "reply": "回复"}],
            idempotency_key="legacy-import-new-draft",
        )


def test_existing_local_import_draft_is_read_only_and_never_executable(tmp_path):
    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    source = seed_legacy_import_source(service)
    row = {
        "id": "legacy-local-draft", "platform": "generic", "source_kind": "import", "source_id": source["id"],
        "delivery": "local", "account_id": "", "account_label": "", "target_id": "", "target_url": "",
        "kind": "reply", "payload": {"items": [{"id": "local-c1", "nickname": "旧用户", "content": "参数是多少？", "reply": "旧回复"}]},
        "status": "draft", "attempts": 0, "operation_id": None, "result": None,
        "created_at": "2026-09-01T00:00:00+00:00", "updated_at": "2026-09-01T00:00:00+00:00",
    }
    with service.store.transaction() as state:
        state["interactions"] = {row["id"]: row}
    listed = service.interactions.list(platform="generic")["items"]
    assert listed[0]["id"] == row["id"]
    with pytest.raises(WorkflowError, match="只读保留"):
        service.interactions.update_draft(
            row["id"], expected_updated_at=row["updated_at"],
            items=[{"id": "local-c1", "nickname": "旧用户", "content": "参数是多少？", "reply": "改回复"}],
        )
    with pytest.raises(WorkflowError, match="只读保留"):
        service.interactions.cancel_draft(row["id"], expected_updated_at=row["updated_at"])
    with pytest.raises(WorkflowError, match="只读保留"):
        service.interactions.execute(row["id"], True)
    assert service.interactions.list(platform="generic")["items"][0]["attempts"] == 0


def test_generic_new_drafts_are_rejected_even_without_source(tmp_path):
    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    with pytest.raises(WorkflowError, match="通用/未知来源不支持"):
        service.interactions.create_draft(
            platform="generic", kind="reply", items=[{"id": "c1", "content": "问题", "reply": "回答"}],
            idempotency_key="generic-rejected-001",
        )


def test_remote_source_binds_comment_and_allows_xhs_draft(tmp_path):
    service, account_id = service_with_account(tmp_path, connected=True)
    seed_xhs_locator(service, account_id)
    source = seed_remote_source(service, account_id)
    draft = service.interactions.create_draft(
        platform="xiaohongshu", source_id=source["id"], kind="reply",
        items=[{"id": "c1", "nickname": "伪造昵称", "content": "伪造正文", "reply": "先从第一步开始。"}],
        idempotency_key="remote-source-draft-001",
    )
    assert draft["delivery"] == "remote"
    assert draft["account_id"] == account_id
    assert draft["payload"]["items"][0]["nickname"] == "甲"
    assert draft["payload"]["items"][0]["content"] == "怎么开始？"


def test_legacy_xhs_interactions_migrate_without_replay_and_remain_visible_disconnected(tmp_path):
    service, account_id = service_with_account(tmp_path, connected=False)
    legacy = {
        "id": "legacy001", "creation_key": "legacy-key", "digest": "digest",
        "account_id": account_id, "account_label": "旧账号", "note_id": "note123",
        "note_url": "https://www.xiaohongshu.com/explore/note123", "kind": "reply",
        "payload": {"items": [{"id": "c1", "nickname": "A", "content": "原文", "reply": "已拟回复"}]},
        "status": "unknown_result", "attempts": 1, "operation_id": "a" * 32,
        "result": None, "created_at": "2026-09-01T00:00:00+00:00", "updated_at": "2026-09-01T00:00:01+00:00",
    }
    with service.store.transaction() as state:
        state["xhs_interactions"] = {legacy["id"]: legacy}
    row = service.interactions.list(account_id=account_id)["items"][0]
    assert row["id"] == "legacy001" and row["status"] == "unknown_result"
    assert row["attempts"] == 1 and row["operation_id"] == "a" * 32
    assert row["platform"] == "xiaohongshu" and row["legacy_origin"] == "xhs_interactions"
    with service.store.transaction(write=False) as state:
        assert "legacy001" in state["xhs_interactions"] and "legacy001" in state["interactions"]
    resolved = service.interactions.resolve_unknown("legacy001", result="not_submitted", confirmed=True)
    assert resolved["status"] == "not_submitted"
    assert service.accounts.delete(account_id, True)["deleted"] is True


def test_account_delete_blocks_unknown_interaction_but_history_does_not_require_connection(tmp_path):
    service, account_id = service_with_account(tmp_path, connected=False)
    with service.store.transaction() as state:
        state["interactions"] = {"i1": {
            "id": "i1", "platform": "xiaohongshu", "account_id": account_id,
            "status": "unknown_result", "created_at": "2026-09-01T00:00:00+00:00", "updated_at": "2026-09-01T00:00:00+00:00",
        }}
    assert service.interactions.list(account_id=account_id)["items"][0]["id"] == "i1"
    with pytest.raises(WorkflowError, match="真实互动待核对"):
        service.accounts.delete(account_id, True)


def test_manual_unknown_resolution_records_source_and_unblocks_account_delete(tmp_path):
    service, account_id = service_with_account(tmp_path, connected=False)
    with service.store.transaction() as state:
        state["interactions"] = {"i2": {
            "id": "i2", "platform": "xiaohongshu", "source_kind": "remote", "source_id": "",
            "delivery": "remote", "account_id": account_id, "account_label": "旧账号",
            "target_id": "note123", "target_url": "https://www.xiaohongshu.com/explore/note123",
            "kind": "comment", "payload": {"text": "曾尝试发送"}, "status": "unknown_result",
            "attempts": 1, "operation_id": "b" * 32, "result": None,
            "created_at": "2026-09-01T00:00:00+00:00", "updated_at": "2026-09-01T00:00:01+00:00",
        }}
    with pytest.raises(WorkflowError, match="已经在对应平台人工检查"):
        service.interactions.resolve_unknown("i2", result="not_submitted", confirmed=False)
    resolved = service.interactions.resolve_unknown(
        "i2", result="not_submitted", confirmed=True, note="已在平台人工检查，没有看到该评论",
    )
    assert resolved["status"] == "not_submitted"
    assert resolved["resolution"]["source"] == "manual_platform_check"
    assert service.accounts.delete(account_id, True)["deleted"] is True


def test_unsupported_remote_platform_operations_fail_before_worker(tmp_path, monkeypatch):
    service, account_id = service_with_account(tmp_path, platform="zhihu", connected=True)
    called = []
    monkeypatch.setattr(service.accounts, "run", lambda *args, **kwargs: called.append((args, kwargs)))
    with pytest.raises(WorkflowError, match="尚未接入"):
        service.interactions.remote_contents(account_id)
    with pytest.raises(WorkflowError, match="尚未接入"):
        service.interactions.remote_comments(account_id=account_id, target_id="123")
    with pytest.raises(WorkflowError, match="尚未接入"):
        service.interactions.create_draft(
            platform="zhihu", account_id=account_id, target_id="123", kind="reply",
            items=[{"id": "c1", "content": "评论", "reply": "回复"}], idempotency_key="zhihu-remote-001",
        )
    assert called == []


def test_xhs_unknown_is_not_replayed_and_refresh_is_not_platform_verify(tmp_path, monkeypatch):
    service, account_id = service_with_account(tmp_path, connected=True)
    seed_xhs_locator(service, account_id)
    draft = service.interactions.create_draft(
        platform="xiaohongshu", account_id=account_id, target_id="note123", kind="comment", text="测试评论",
        idempotency_key="xhs-unknown-001",
    )
    sends = []
    def uncertain(account, operation, operation_id, **extra):
        sends.append((operation, operation_id, extra.get("xhs_action")))
        return {"state": "unknown_result", "data": {"status": "unknown_result", "reason": ""}}
    monkeypatch.setattr(service.accounts, "run", uncertain)
    first = service.interactions.execute(draft["id"], True)
    second = service.interactions.execute(draft["id"], True)
    assert first["status"] == second["status"] == "unknown_result" and len(sends) == 1
    assert service.accounts.get(account_id)["operation"]["state"] == "recovery_required"

    monkeypatch.setattr(service.accounts, "read_result", lambda aid, oid: {
        "state": "verified", "data": {"status": "verified", "reason": ""}, "operation_id": oid,
    })
    refreshed = service.interactions.refresh_result(draft["id"])
    assert refreshed["interaction"]["status"] == "verified"
    assert refreshed["platform_verified"] is False and refreshed["source"] == "local_worker"
    assert "没有重新查询平台" in refreshed["note"]
    assert service.accounts.get(account_id)["operation"] is None
    with pytest.raises(WorkflowError, match="主动远端复核尚未接入"):
        service.interactions.verify_platform(draft["id"])


def test_recovery_turns_dispatching_interaction_unknown_without_replay(tmp_path):
    service, account_id = service_with_account(tmp_path, connected=True)
    interaction_id = "recover-interaction"
    operation_id = "c" * 32
    with service.store.transaction() as state:
        state["interactions"] = {interaction_id: {
            "id": interaction_id, "platform": "xiaohongshu", "source_kind": "remote", "source_id": "",
            "delivery": "remote", "account_id": account_id, "account_label": "恢复账号",
            "target_id": "note123", "target_url": "https://www.xiaohongshu.com/explore/note123",
            "kind": "reply", "payload": {"items": [{"id": "c1", "nickname": "A", "content": "问题", "reply": "回复"}]},
            "status": "dispatching", "attempts": 1, "operation_id": operation_id, "result": None,
            "created_at": "2026-09-01T00:00:00+00:00", "updated_at": "2026-09-01T00:00:01+00:00",
        }}
        state["accounts"][account_id]["operation"] = {
            "id": operation_id, "kind": "interaction", "state": "running",
            "interaction_id": interaction_id, "started_at": "2026-09-01T00:00:01+00:00",
        }
    service.recover()
    row = service.interactions.list(account_id=account_id)["items"][0]
    assert row["status"] == "unknown_result" and row["attempts"] == 1
    assert service.accounts.get(account_id)["operation"]["state"] == "recovery_required"
    assert service.interactions.resolve_unknown(interaction_id, result="not_submitted", confirmed=True)["status"] == "not_submitted"
    assert service.accounts.get(account_id)["operation"] is None


def test_disconnected_xhs_can_keep_remote_draft_but_cannot_execute(tmp_path):
    service, account_id = service_with_account(tmp_path, connected=False)
    seed_xhs_locator(service, account_id)
    draft = service.interactions.create_draft(
        platform="xiaohongshu", account_id=account_id, target_id="note123", kind="reply",
        items=[{"id": "c1", "nickname": "A", "content": "问题", "reply": "回复"}], idempotency_key="offline-xhs-draft",
    )
    assert draft["status"] == "draft"
    with pytest.raises(WorkflowError, match="未连接"):
        service.interactions.execute(draft["id"], True)
    assert service.interactions.list(account_id=account_id)["items"][0]["attempts"] == 0


def test_generic_api_has_no_import_route_and_old_xhs_routes_still_work(tmp_path, monkeypatch):
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    account = service.accounts.create(AccountInput(platform="xiaohongshu", label="API XHS", idempotency_key="api-interaction-account"))
    with service.store.transaction() as state:
        state["accounts"][account["id"]].update(status="connected", identity={"logged_in": True, "name": "api", "remote_id": "u2"})
    seed_xhs_locator(service, account["id"])
    with TestClient(app, base_url="http://localhost") as client:
        caps = client.get("/api/ripple/interactions/capabilities")
        assert caps.status_code == 200
        assert all(row["platform"] != "generic" for row in caps.json()["items"])
        removed = client.post("/api/ripple/interactions/import", json={"platform": "xiaohongshu", "label": "x", "raw": "hello", "format": "text", "idempotency_key": "removed-import"})
        assert removed.status_code in {404, 405}

        generic = client.post("/api/ripple/interactions", json={
            "platform": "xiaohongshu", "account_id": account["id"], "source_id": "", "target_id": "note123", "target_url": "",
            "kind": "reply", "items": [{"id": "c1", "nickname": "A", "content": "问题", "reply": "回复"}], "text": "",
            "idempotency_key": "generic-api-draft",
        })
        assert generic.status_code == 201 and generic.json()["platform"] == "xiaohongshu"
        legacy_list = client.get(f"/api/ripple/xiaohongshu/interactions?account_id={account['id']}")
        assert legacy_list.status_code == 200 and legacy_list.json()["items"][0]["id"] == generic.json()["id"]
        old_create = client.post("/api/ripple/xiaohongshu/interactions", json={
            "account_id": account["id"], "note_id": "note123", "url": "", "kind": "comment",
            "items": [], "text": "兼容评论", "idempotency_key": "legacy-api-draft",
        })
        assert old_create.status_code == 201 and old_create.json()["platform"] == "xiaohongshu"


def test_agent_interaction_tool_rejects_generic_or_legacy_import_and_accepts_real_xhs(tmp_path, monkeypatch):
    from web import app as webapp

    service, account_id = service_with_account(tmp_path, connected=True)
    seed_xhs_locator(service, account_id)
    remote = seed_remote_source(service, account_id)
    legacy = seed_legacy_import_source(service)
    monkeypatch.setattr(webapp.app.state, "ripple", service)
    headers = {"X-Ripple-Agent-Token": webapp._AGENT_RUNTIME.tool_token}
    with TestClient(webapp.app, base_url="http://localhost") as client:
        denied = client.post("/api/ripple-agent/tools/interactions/draft", json={
            "platform": "xiaohongshu", "source_id": remote["id"], "kind": "reply",
            "items": [{"id": "c1", "reply": "回复"}],
        })
        assert denied.status_code == 403

        generic = client.post("/api/ripple-agent/tools/interactions/draft", headers=headers, json={
            "platform": "generic", "source_id": legacy["id"], "kind": "reply",
            "items": [{"id": "local-c1", "reply": "回复"}],
        })
        assert generic.status_code in {409, 422}

        real = client.post("/api/ripple-agent/tools/interactions/draft", headers=headers, json={
            "platform": "xiaohongshu", "source_id": remote["id"], "kind": "reply",
            "items": [{"id": "c1", "reply": "先从第一步开始。"}],
        })
        assert real.status_code == 200
        body = real.json()
        assert body["status"] == "draft" and body["delivery"] == "remote"
        assert service.interactions.list(account_id=account_id)["items"][0]["attempts"] == 0
