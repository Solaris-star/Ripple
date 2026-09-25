import asyncio

import pytest
from fastapi import HTTPException

import ripple.content_profiles as content_profiles
from ripple.content_profiles import ContentProfileService
from web import app as webapp


class _InlineThread:
    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


def test_analysis_waiting_task_resumes_and_same_key_rejects_changed_input(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    monkeypatch.setattr(webapp, "_CONTENT_PROFILES", service)
    monkeypatch.setattr(
        webapp,
        "_profile_target",
        lambda target_kind, account_id: {"id": account_id, "platform": "xiaohongshu"},
    )

    ready = {"value": False}
    calls = []

    monkeypatch.setattr(webapp, "_recommendation_ai_backend", lambda: "fixture" if ready["value"] else "")
    monkeypatch.setattr(webapp.threading, "Thread", _InlineThread)

    def worker(run_id, target, samples, profile_id):
        calls.append((run_id, list(samples), profile_id))
        service.save_analysis_proposal(run_id, {"files": {"identity.md": "# 身份定位\n\n恢复成功\n"}})

    monkeypatch.setattr(webapp, "_profile_analysis_worker", worker)

    payload = dict(
        target_kind="account",
        account_id="a" * 32,
        display_name="恢复画像",
        samples=[{"title": "FIRST SAMPLE"}],
        idempotency_key="resume-same-key",
        confirmed=True,
    )
    first = asyncio.run(webapp.api_profile_analysis_create(webapp.ProfileAnalysisCreateRequest(**payload)))
    assert first["status"] == "waiting_user"
    assert "模型尚未配置" in first["error"]

    ready["value"] = True
    second = asyncio.run(webapp.api_profile_analysis_create(webapp.ProfileAnalysisCreateRequest(**payload)))
    assert second["id"] == first["id"]
    assert len(calls) == 1
    assert service.get_analysis(first["id"])["status"] == "succeeded"

    latest = asyncio.run(webapp.api_profile_analysis_latest("account", "a" * 32))
    assert latest["id"] == first["id"]
    assert latest["model"]["request_key"] == "resume-same-key"

    changed = dict(payload)
    changed["samples"] = [{"title": "CHANGED SAMPLE"}]
    with pytest.raises(HTTPException) as exc:
        asyncio.run(webapp.api_profile_analysis_create(webapp.ProfileAnalysisCreateRequest(**changed)))
    assert exc.value.status_code == 409
    assert "同一分析请求键" in str(exc.value.detail)
