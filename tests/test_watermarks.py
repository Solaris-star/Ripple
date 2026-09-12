"""Regression tests for Ripple implicit-watermark integration."""
from __future__ import annotations

import shutil
import urllib.error

import pytest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from ripple.api import install
from ripple.publishing import CreateInput
from ripple.workspace import WorkspaceService
from ripple.watermarks import WatermarkService


def _png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (8, 8), (120, 140, 160)).save(path)


def _fake_inspect(_source, relative):
    return {
        "path": relative,
        "status": "unknown",
        "provider": None,
        "signals": [],
        "supported": True,
        "inspection_ready": True,
        "deep_clean_ready": True,
        "reason": "",
        "note": "未检出可识别线索；这不代表图片不存在私有或未知的隐式水印。",
    }


def _fake_clean(source, relative):
    target = source.with_name(source.stem + ".implicit-clean-test" + source.suffix)
    shutil.copyfile(source, target)
    output = str(target.relative_to(source.parents[1])).replace("\\", "/")
    # source.parents[1] is the outputs root for the two-level fixture path.
    return {"source": relative, "output": output}


def test_manual_watermark_clean_preserves_original(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path)
    source = tmp_path / "article" / "cover.png"
    _png(source)
    monkeypatch.setattr(service.watermarks, "inspect", _fake_inspect)
    monkeypatch.setattr(service.watermarks, "clean", _fake_clean)

    result = service.clean_watermark("article/cover.png")

    assert source.is_file()
    assert result["output"] == "article/cover.implicit-clean-test.png"
    assert (tmp_path / result["output"]).is_file()
    assert result["inspection"]["status"] == "unknown"


def test_task_watermark_clean_creates_new_version_and_replaces_media(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path)
    source = tmp_path / "article" / "cover.png"
    _png(source)
    monkeypatch.setattr(service.watermarks, "inspect", _fake_inspect)
    monkeypatch.setattr(service.watermarks, "clean", _fake_clean)
    task = service.create(CreateInput(
        title="隐式水印测试",
        media=["article/cover.png"],
        idempotency_key="watermark-task-001",
    ))

    result = service.clean_task_watermarks(task["id"], task["version_id"], True)
    updated = result["task"]

    assert source.is_file()
    assert updated["version"] == task["version"] + 1
    assert updated["version_id"] != task["version_id"]
    assert updated["status"] == "draft"
    assert updated["approval"] is None
    assert updated["content"]["media"] == ["article/cover.implicit-clean-test.png"]
    assert result["cleaned"][0]["source"] == "article/cover.png"


def test_preflight_returns_watermark_reports_without_blocking_unknown(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path)
    source = tmp_path / "article" / "cover.png"
    _png(source)
    monkeypatch.setattr(service.watermarks, "inspect", _fake_inspect)
    task = service.create(CreateInput(
        title="预检测试",
        media=["article/cover.png"],
        idempotency_key="watermark-preflight-001",
    ))

    result = service.preflight(task["id"], task["version_id"])

    assert result["ok"] is True
    assert result["task"]["status"] == "review_ready"
    assert result["watermarks"][0]["path"] == "article/cover.png"
    assert result["watermarks"][0]["status"] == "unknown"


def test_watermark_api_requires_confirmation_and_rejects_escape(tmp_path, monkeypatch):
    application = FastAPI()
    service = install(application, tmp_path)
    source = tmp_path / "article" / "cover.png"
    _png(source)
    monkeypatch.setattr(service.watermarks, "inspect", _fake_inspect)
    monkeypatch.setattr(service.watermarks, "clean", _fake_clean)

    with TestClient(application, base_url="http://localhost") as client:
        inspected = client.post("/api/ripple/media/watermark/inspect", json={"path": "article/cover.png"})
        assert inspected.status_code == 200 and inspected.json()["status"] == "unknown"
        assert client.post("/api/ripple/media/watermark/clean", json={"path": "article/cover.png"}).status_code == 422
        cleaned = client.post("/api/ripple/media/watermark/clean", json={"path": "article/cover.png", "confirmed": True})
        assert cleaned.status_code == 201 and cleaned.json()["source"] == "article/cover.png"
        assert client.post("/api/ripple/media/watermark/inspect", json={"path": "../secret.png"}).status_code == 422


def test_watermark_model_source_fails_fast_when_uncached_and_offline(monkeypatch):
    service = WatermarkService()
    monkeypatch.setattr(service, "_model_cache_ready", lambda: False)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("HF_ENDPOINT", raising=False)
    monkeypatch.setattr(
        "ripple.watermarks.urllib.request.urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(urllib.error.URLError("offline")),
    )

    with pytest.raises(ValueError, match="Hugging Face"):
        service._ensure_model_source()


def test_task_watermark_clean_requires_explicit_confirmation(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path)
    source = tmp_path / "article" / "cover.png"
    _png(source)
    monkeypatch.setattr(service.watermarks, "clean", _fake_clean)
    task = service.create(CreateInput(
        title="确认测试",
        media=["article/cover.png"],
        idempotency_key="watermark-confirm-001",
    ))

    try:
        service.clean_task_watermarks(task["id"], task["version_id"], False)
    except ValueError as exc:
        assert "确认" in str(exc)
    else:
        raise AssertionError("unconfirmed deep cleaning must fail")
    assert list(source.parent.glob("*.implicit-clean-*")) == []
