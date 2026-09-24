from pathlib import Path
import shutil

import pytest

import ripple.content_profiles as content_profiles
from ripple.content_profiles import ContentProfileService
from ripple.publishing import WorkflowError


def _legacy_profile(root: Path, name: str = "科技工具") -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "identity.md").write_text("# 身份定位\n\n科技工具实测\n", encoding="utf-8")
    (directory / "style.md").write_text("# 内容风格\n\n清晰、直接\n", encoding="utf-8")
    return directory


def test_legacy_profile_gets_stable_id_and_revision(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    directory = _legacy_profile(profiles)

    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    first = service.list_profiles()[0]
    assert first["id"].startswith("cp_")
    assert first["current_revision"] == 1

    (directory / "identity.md").write_text("# 身份定位\n\n开发者工具实测\n", encoding="utf-8")
    service.sync_legacy_profiles()
    second = service.list_profiles()[0]
    assert second["id"] == first["id"]
    assert second["current_revision"] == 2
    assert "开发者工具实测" in service.get_profile(first["id"])["files"]["identity.md"]


def test_binding_resolves_profile_and_blocks_archive(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    _legacy_profile(profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    profile = service.list_profiles()[0]

    binding = service.bind(
        target_kind="account",
        account_id="xhs-tech",
        profile_id=profile["id"],
        overrides={"tone": "收藏型教程"},
    )
    resolved = service.resolve(target_kind="account", account_id="xhs-tech")
    assert resolved["profile"]["id"] == profile["id"]
    assert resolved["binding"]["binding_revision"] == binding["binding_revision"]
    assert resolved["overrides"] == {"tone": "收藏型教程"}

    with pytest.raises(WorkflowError, match="仍有关联账号"):
        service.archive_profile(profile["id"])


def test_draft_revision_does_not_collide_with_confirmed_revision(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    _legacy_profile(profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    profile = service.get_profile(service.list_profiles()[0]["id"])

    draft_files = dict(profile["files"])
    draft_files["identity.md"] = "# 身份定位\n\n草稿定位\n"
    draft = service.save_revision(
        profile["id"], draft_files, source="test", expected_revision=1, confirm=False
    )
    assert draft["current_revision"] == 1

    confirmed_files = dict(profile["files"])
    confirmed_files["identity.md"] = "# 身份定位\n\n确认定位\n"
    confirmed = service.save_revision(
        profile["id"], confirmed_files, source="test", expected_revision=1, confirm=True
    )
    assert confirmed["current_revision"] == 3
    assert "确认定位" in confirmed["files"]["identity.md"]


def test_deleted_name_can_be_recreated_without_changing_stable_id(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    directory = _legacy_profile(profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    first = service.list_profiles()[0]

    shutil.rmtree(directory)
    service.archive_profile(first["id"])
    recreated = service.create_profile(
        "科技工具",
        {"identity.md": "# 身份定位\n\n重新建档\n", "style.md": "# 内容风格\n\n新版\n"},
    )
    assert recreated["id"] == first["id"]
    assert recreated["current_revision"] == 2
    assert recreated["state"] == "confirmed"


def test_history_capability_is_explicit_per_platform(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")

    xhs = service.analysis_capability(target_kind="account", account_id="xhs-one", platform="xiaohongshu")
    assert xhs["automatic_history_supported"] is True
    assert xhs["automatic_history_fields"] == ["title", "url", "metrics"]
    assert "最多 30 条" in xhs["note"]

    douyin = service.analysis_capability(target_kind="account", account_id="dy-one", platform="douyin")
    assert douyin["automatic_history_supported"] is False
    assert douyin["automatic_history_planned"] is True
    assert douyin["import_samples_supported"] is True


def test_analysis_proposal_requires_explicit_apply(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    _legacy_profile(profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    profile = service.list_profiles()[0]

    run = service.create_analysis(
        target_kind="account",
        account_id="douyin-tech",
        profile_id=profile["id"],
        capability={"automatic_history_supported": False},
        samples=[{"title": "样本", "body": "正文"}],
    )
    assert run["status"] == "waiting_user"
    before = service.get_profile(profile["id"])["current_revision"]

    proposed = service.save_analysis_proposal(
        run["id"],
        {
            "files": {"identity.md": "# 身份定位\n\nAgent 提案\n"},
            "observations": ["样本以教程为主"],
            "assumptions": ["受众可能偏效率工具用户"],
        },
    )
    assert proposed["status"] == "succeeded"
    assert service.get_profile(profile["id"])["current_revision"] == before
