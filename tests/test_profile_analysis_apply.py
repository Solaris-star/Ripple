import asyncio
import sqlite3

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
    assert result["binding"]["profile_revision"] == result["profile"]["current_revision"]

    repeated = asyncio.run(webapp.api_profile_analysis_apply(
        run["id"],
        webapp.ProfileAnalysisApplyRequest(
            profile_id=profile_a["id"],
            expected_revision=profile_a["current_revision"],
            bind_target=True,
        ),
    ))
    assert repeated == result
    assert service.get_profile(profile_a["id"])["current_revision"] == 2


def test_analysis_apply_validates_target_before_creating_profile(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    run = service.create_analysis(
        target_kind="account", account_id="b" * 32, profile_id="",
        model={"base_profile_revision": 0, "base_binding_revision": 0, "base_binding_profile_id": "", "base_overrides": {}},
        samples=[{"title": "fixture"}],
    )
    service.save_analysis_proposal(run["id"], {"files": _files("NEW")})
    monkeypatch.setattr(webapp, "_CONTENT_PROFILES", service)
    monkeypatch.setattr(webapp, "_profile_target", lambda *_args, **_kwargs: (_ for _ in ()).throw(webapp.WorkflowError("账号不存在", 404)))

    with pytest.raises(HTTPException) as exc:
        asyncio.run(webapp.api_profile_analysis_apply(
            run["id"], webapp.ProfileAnalysisApplyRequest(display_name="不会残留", bind_target=True),
        ))
    assert exc.value.status_code == 404
    assert service.list_profiles() == []
    assert not (profiles / "不会残留").exists()


def test_analysis_apply_rolls_back_profile_when_binding_insert_fails(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    db_path = tmp_path / "profiles.sqlite3"
    service = ContentProfileService(db_path)
    profile = service.create_profile("原画像", _files("BASE"))
    run = service.create_analysis(
        target_kind="account", account_id="c" * 32, profile_id=profile["id"],
        model={"base_profile_revision": 1, "base_binding_revision": 0, "base_binding_profile_id": "", "base_overrides": {}},
        samples=[{"title": "fixture"}],
    )
    service.save_analysis_proposal(run["id"], {"files": _files("AGENT")})
    monkeypatch.setattr(webapp, "_CONTENT_PROFILES", service)
    monkeypatch.setattr(webapp, "_profile_target", lambda *_args, **_kwargs: {"id": "c" * 32, "platform": "xiaohongshu"})
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TRIGGER block_binding BEFORE INSERT ON account_profile_bindings BEGIN SELECT RAISE(ABORT, 'blocked binding'); END;")

    with pytest.raises(sqlite3.IntegrityError, match="blocked binding"):
        asyncio.run(webapp.api_profile_analysis_apply(
            run["id"], webapp.ProfileAnalysisApplyRequest(profile_id=profile["id"], expected_revision=1, bind_target=True),
        ))

    current = service.get_profile(profile["id"])
    assert current["current_revision"] == 1
    assert current["files"]["identity.md"] == _files("BASE")["identity.md"]
    assert (profiles / "原画像" / "identity.md").read_text(encoding="utf-8") == _files("BASE")["identity.md"]
    assert service.binding(target_kind="account", account_id="c" * 32) is None
