from __future__ import annotations

import asyncio
import json

import pytest
from PIL import Image
from fastapi import HTTPException

from ripple.library import MotherCreate, MotherRevision
from ripple.workspace import WorkspaceService
from web import app as webapp


def test_outputs_are_grouped_by_mother_and_legacy_is_not_guessed(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    private = tmp_path / "private"
    service = WorkspaceService(outputs, private=private)
    old_service = webapp.app.state.ripple
    monkeypatch.setattr(webapp, "OUTPUTS_DIR", outputs)
    webapp.app.state.ripple = service
    try:
        mother = service.library.create(MotherCreate(title="芝加哥旅行攻略", idempotency_key="output-mother-one"))
        linked = outputs / "travel-assets"
        linked.mkdir(parents=True)
        (linked / "cover.png").write_bytes(b"fixture")
        (linked / ".ripple.json").write_text(json.dumps({
            "title": "旧目录标题不参与归属判断", "content_id": mother["id"],
            "source_version_id": mother["version_id"], "deliverables": ["cover.png"], "cover": "cover.png",
        }), encoding="utf-8")
        legacy = outputs / "same-title-but-unlinked"
        legacy.mkdir()
        (legacy / "old.txt").write_text("legacy", encoding="utf-8")
        (legacy / ".ripple.json").write_text(json.dumps({"title": "芝加哥旅行攻略"}), encoding="utf-8")

        tree = webapp.get_output_tree()
        content_root = next(row for row in tree if row.get("content_id") == mother["id"])
        assert content_root["synthetic"] is True
        assert content_root["meta"]["title"] == "芝加哥旅行攻略"
        assert content_root["meta"]["contentId"] == mother["id"]
        assert any(row["name"] == "cover.png" for row in content_root["children"])
        history = next(row for row in tree if row.get("legacy_unlinked"))
        assert history["meta"]["title"] == "历史产物 / 未关联内容"
        assert [row["name"] for row in history["children"]] == ["same-title-but-unlinked"]
    finally:
        webapp.app.state.ripple = old_service
        service.close()


def test_system_output_namespaces_are_not_exposed_or_deletable(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    service = WorkspaceService(outputs, private=tmp_path / "private")
    old_service = webapp.app.state.ripple
    monkeypatch.setattr(webapp, "OUTPUTS_DIR", outputs)
    webapp.app.state.ripple = service
    try:
        assert service.store.path.is_file()
        with pytest.raises(HTTPException) as read_blocked:
            webapp._safe_output_path("_ripple/state.json")
        assert read_blocked.value.status_code == 403
        with pytest.raises(HTTPException) as delete_blocked:
            asyncio.run(webapp.api_output_delete("_ripple", webapp.OutputDeleteRequest(confirmed=True)))
        assert delete_blocked.value.status_code == 403
        assert service.store.path.is_file()
    finally:
        webapp.app.state.ripple = old_service
        service.close()


def test_output_delete_requires_confirmation_and_blocks_live_references(tmp_path, monkeypatch):
    outputs = tmp_path / "outputs"
    service = WorkspaceService(outputs, private=tmp_path / "private")
    old_service = webapp.app.state.ripple
    monkeypatch.setattr(webapp, "OUTPUTS_DIR", outputs)
    webapp.app.state.ripple = service
    try:
        folder = outputs / "article-assets"
        folder.mkdir(parents=True)
        media = folder / "cover.png"
        Image.new("RGB", (2, 2), "white").save(media, format="PNG")
        mother = service.library.create(MotherCreate(
            title="引用素材", media=["article-assets/cover.png"], idempotency_key="output-reference-mother",
        ))
        with pytest.raises(HTTPException) as unconfirmed:
            asyncio.run(webapp.api_output_delete("article-assets/cover.png", webapp.OutputDeleteRequest(confirmed=False)))
        assert unconfirmed.value.status_code == 422
        with pytest.raises(HTTPException) as referenced:
            asyncio.run(webapp.api_output_delete("article-assets/cover.png", webapp.OutputDeleteRequest(confirmed=True)))
        assert referenced.value.status_code == 409
        assert "母版内容" in str(referenced.value.detail)
        with pytest.raises(HTTPException) as aliased:
            asyncio.run(webapp.api_output_delete("article-assets//cover.png", webapp.OutputDeleteRequest(confirmed=True)))
        assert aliased.value.status_code == 409
        assert media.exists()
        service.library.revise(mother["id"], MotherRevision(
            title="引用素材", media=[], expected_version=mother["version_id"],
        ))
        deleted = asyncio.run(webapp.api_output_delete("article-assets/cover.png", webapp.OutputDeleteRequest(confirmed=True)))
        assert deleted["ok"] is True and not media.exists()
    finally:
        webapp.app.state.ripple = old_service
        service.close()
