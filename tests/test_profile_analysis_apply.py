import asyncio

import pytest
from fastapi import HTTPException

import ripple.content_profiles as content_profiles
from ripple.content_profiles import ContentProfileService
from ripple.persona import _FILE_ORDER
from web import app as webapp


def _files(prefix: str) -> dict[str, str]:
    return {name: f"# {name}\n\n{prefix}-{name}\n" for name in _FILE_ORDER}


def test_analysis_apply_is_profile_locked_and_preserves_overrides_and_redlines(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")

    profile_a = service.create_profile("画像A", _files("A"))
    profile_b = service.create_profile("画像B", _files("B"))
    binding = service.bind(
        target_kind="account",
        account_id="a" * 32,
        profile_id=profile_a["id"],
        overrides={"tone": "用户保留口吻", "formats": "图文教程"},
    )

    model = {
        "base_profile_revision": profile_a["current_revision"],
        "base_binding_revision": binding["binding_revision"],
        "base_binding_profile_id": profile_a["id"],
        "base_overrides": dict(binding["overrides"]),
    }
    run = service.create_analysis(
        target_kind="account",
        account_id="a" * 32,
        profile_id=profile_a["id"],
        model=model,
        samples=[{"title": "fixture"}],
    )
    proposal_files = _files("AGENT")
    proposal_files["preferences.md"] = "# 偏好与红线\n\nAgent 不得覆盖的值\n"
    service.save_analysis_proposal(run["id"], {"files": proposal_files})

    monkeypatch.setattr(webapp, "_CONTENT_PROFILES", service)
    monkeypatch.setattr(
        webapp,
        "_profile_target",
        lambda target_kind, account_id: {"id": account_id, "platform": "xiaohongshu"},
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(webapp.api_profile_analysis_apply(
            run["id"],
            webapp.ProfileAnalysisApplyRequest(
                profile_id=profile_b["id"],
                expected_revision=profile_b["current_revision"],
                bind_target=True,
            ),
        ))
    assert exc.value.status_code == 409
    assert "只能应用到分析时选定的画像" in str(exc.value.detail)

    result = asyncio.run(webapp.api_profile_analysis_apply(
        run["id"],
        webapp.ProfileAnalysisApplyRequest(
            profile_id=profile_a["id"],
            expected_revision=profile_a["current_revision"],
            bind_target=True,
        ),
    ))
    assert result["profile"]["id"] == profile_a["id"]
    assert result["profile"]["files"]["preferences.md"] == _files("A")["preferences.md"]
    assert result["binding"]["overrides"] == {"tone": "用户保留口吻", "formats": "图文教程"}
