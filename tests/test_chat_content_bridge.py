from __future__ import annotations

import base64
import pytest

from fastapi.testclient import TestClient

from ripple.library import MotherCreate, MotherRevision
from ripple.workspace import WorkspaceService


def test_agent_content_tool_updates_existing_content_without_duplication(tmp_path, monkeypatch):
    from web import app as webapp

    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    original = service.library.create(MotherCreate(
        title="原稿", body="第一版正文", media=[], tags="原标签", project_id="local",
        idempotency_key="chat-bridge-original",
    ))
    monkeypatch.setattr(webapp.app.state, "ripple", service)
    headers = {"X-Ripple-Agent-Token": webapp._AGENT_RUNTIME.tool_token}

    with TestClient(webapp.app, base_url="http://localhost") as client:
        updated = client.post("/api/ripple-agent/tools/content/draft", headers=headers, json={
            "title": "原稿（AI 修改）",
            "body": "第二版正文",
            "tags": "新标签",
            "idempotency_key": "chat-bridge-update",
            "content_id": original["id"],
            "expected_version": original["version_id"],
        })
        assert updated.status_code == 200
        payload = updated.json()
        assert payload["id"] == original["id"]
        assert payload["updated"] is False
        assert payload["status"] == "proposal"
        assert payload["version_id"] == original["version_id"]
        assert len(service.library.list()) == 1
        current = service.library.get(original["id"])
        assert current["content"]["body"] == "第一版正文"
        applied = service.library.apply_proposal(payload["proposal_id"], original["version_id"])
        assert applied["status"] == "applied"
        assert service.library.apply_proposal(payload["proposal_id"], original["version_id"])["applied_version_id"] == applied["applied_version_id"]
        current = service.library.get(original["id"])
        assert current["version"] == original["version"] + 1
        assert current["content"]["title"] == "原稿（AI 修改）"
        assert current["content"]["body"] == "第二版正文"
        assert current["content"]["tags"] == "新标签"

        media_dir = tmp_path / "outputs" / "AI媒体生成"
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "generated.png").write_bytes(base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        ))
        media_only = client.post("/api/ripple-agent/tools/content/draft", headers=headers, json={
            "media": ["AI媒体生成/generated.png"],
            "idempotency_key": "chat-bridge-media-update",
            "content_id": original["id"],
            "expected_version": current["version_id"],
        })
        assert media_only.status_code == 200
        media_payload = media_only.json()
        assert service.library.get(original["id"])["content"]["media"] == []
        applied_media = service.library.apply_proposal(media_payload["proposal_id"], current["version_id"])
        assert applied_media["status"] == "applied"
        current = service.library.get(original["id"])
        assert current["content"]["title"] == "原稿（AI 修改）"
        assert current["content"]["body"] == "第二版正文"
        assert current["content"]["tags"] == "新标签"
        assert current["content"]["media"] == ["AI媒体生成/generated.png"]
        assert media_payload["id"] == original["id"]
        assert len(service.library.list()) == 1

        discarded = client.post("/api/ripple-agent/tools/content/draft", headers=headers, json={
            "body": "应当放弃", "idempotency_key": "chat-bridge-discard",
            "content_id": original["id"], "expected_version": current["version_id"],
        })
        assert discarded.status_code == 200
        proposal_id = discarded.json()["proposal_id"]
        assert service.library.dismiss_proposal(proposal_id)["status"] == "dismissed"
        with pytest.raises(ValueError):
            service.library.apply_proposal(proposal_id, current["version_id"])
        assert service.library.get(original["id"])["content"]["body"] == "第二版正文"

        stale = client.post("/api/ripple-agent/tools/content/draft", headers=headers, json={
            "title": "不应覆盖",
            "body": "过期修改",
            "tags": "",
            "idempotency_key": "chat-bridge-stale",
            "content_id": original["id"],
            "expected_version": original["version_id"],
        })
        assert stale.status_code == 409
        assert service.library.get(original["id"])["content"]["body"] == "第二版正文"

        before_undo = service.library.get(original["id"])
        undone = service.library.revise(original["id"], MotherRevision(
            title="原稿（AI 修改）", body="第二版正文", tags="新标签", media=[], project_id="local",
            expected_version=before_undo["version_id"],
        ))
        assert undone["id"] == original["id"]
        assert undone["version"] == before_undo["version"] + 1
        assert undone["content"]["media"] == []


def test_agent_content_tool_create_and_update_arguments_are_mutually_consistent(tmp_path, monkeypatch):
    from web import app as webapp

    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    monkeypatch.setattr(webapp.app.state, "ripple", service)
    headers = {"X-Ripple-Agent-Token": webapp._AGENT_RUNTIME.tool_token}

    with TestClient(webapp.app, base_url="http://localhost") as client:
        bad_update = client.post("/api/ripple-agent/tools/content/draft", headers=headers, json={
            "title": "缺版本",
            "body": "x",
            "tags": "",
            "idempotency_key": "chat-bridge-bad-update",
            "content_id": "a" * 32,
        })
        assert bad_update.status_code == 422

        bad_create = client.post("/api/ripple-agent/tools/content/draft", headers=headers, json={
            "title": "新稿",
            "body": "x",
            "tags": "",
            "idempotency_key": "chat-bridge-bad-create",
            "expected_version": "b" * 64,
        })
        assert bad_create.status_code == 422

        no_title = client.post("/api/ripple-agent/tools/content/draft", headers=headers, json={
            "body": "只有正文",
            "idempotency_key": "chat-bridge-no-title",
        })
        assert no_title.status_code == 422
        assert service.library.list() == []
