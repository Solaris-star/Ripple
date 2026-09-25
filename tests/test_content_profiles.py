from pathlib import Path
import shutil
import json
import sqlite3
import threading

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


def test_legacy_external_change_is_draft_not_auto_confirmed(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    directory = _legacy_profile(profiles)

    db_path = tmp_path / "profiles.sqlite3"
    service = ContentProfileService(db_path)
    first = service.list_profiles()[0]
    assert first["id"].startswith("cp_")
    assert first["current_revision"] == 1

    (directory / "identity.md").write_text("# 身份定位\n\n开发者工具实测\n", encoding="utf-8")
    service.sync_legacy_profiles()
    second = service.list_profiles()[0]
    assert second["id"] == first["id"]
    assert second["current_revision"] == 1
    assert "科技工具实测" in service.get_profile(first["id"])["files"]["identity.md"]

    with sqlite3.connect(db_path) as db:
        row = db.execute(
            "SELECT revision,files_json,source,status FROM profile_revisions WHERE profile_id=? ORDER BY revision DESC LIMIT 1",
            (first["id"],),
        ).fetchone()
    assert row[0] == 2
    assert row[2:] == ("legacy_sync", "draft")
    assert "开发者工具实测" in json.loads(row[1])["identity.md"]
    assert "科技工具实测" in (directory / "identity.md").read_text(encoding="utf-8")


def test_confirmed_save_rolls_back_database_when_legacy_write_fails(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    _legacy_profile(profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    profile = service.get_profile(service.list_profiles()[0]["id"])
    changed = dict(profile["files"])
    changed["identity.md"] = "# 身份定位\n\n不能半保存\n"

    monkeypatch.setattr(content_profiles, "_write_legacy_files", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        service.save_revision(profile["id"], changed, source="test", expected_revision=1, confirm=True)

    after = service.get_profile(profile["id"])
    assert after["current_revision"] == 1
    assert "科技工具实测" in after["files"]["identity.md"]


def test_profile_create_rolls_back_database_when_legacy_write_fails(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    monkeypatch.setattr(content_profiles, "_write_legacy_files", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    with pytest.raises(OSError, match="disk full"):
        service.create_profile("事务画像", {"identity.md": "# 身份定位\n\n不能半创建\n"})

    assert service.list_profiles() == []
    assert not (profiles / "事务画像").exists()


def test_concurrent_confirmed_save_keeps_db_and_legacy_mirror_consistent(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    _legacy_profile(profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    profile = service.get_profile(service.list_profiles()[0]["id"])
    barrier = threading.Barrier(2)
    outcomes = {}

    def save(marker: str):
        changed = dict(profile["files"])
        changed["identity.md"] = f"# 身份定位\n\n{marker}\n"
        barrier.wait(timeout=5)
        try:
            outcomes[marker] = service.save_revision(
                profile["id"], changed, source="concurrent-test", expected_revision=1, confirm=True,
            )["current_revision"]
        except WorkflowError as exc:
            outcomes[marker] = exc.status

    threads = [threading.Thread(target=save, args=(marker,), daemon=True) for marker in ("并发A", "并发B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(8)
        assert not thread.is_alive()

    winners = [marker for marker, result in outcomes.items() if result == 2]
    losers = [marker for marker, result in outcomes.items() if result == 409]
    assert len(winners) == 1 and len(losers) == 1
    winner = winners[0]
    current = service.get_profile(profile["id"])
    disk = (profiles / "科技工具" / "identity.md").read_text(encoding="utf-8")
    assert winner in current["files"]["identity.md"]
    assert winner in disk


def test_concurrent_create_does_not_delete_winner_directory(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    barrier = threading.Barrier(2)
    outcomes = {}

    def create(marker: str):
        barrier.wait(timeout=5)
        try:
            outcomes[marker] = service.create_profile(
                "并发画像", {"identity.md": f"# 身份定位\n\n{marker}\n"}, source="concurrent-test",
            )["id"]
        except WorkflowError as exc:
            outcomes[marker] = exc.status

    threads = [threading.Thread(target=create, args=(marker,), daemon=True) for marker in ("创建A", "创建B")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(8)
        assert not thread.is_alive()

    winners = [marker for marker, result in outcomes.items() if isinstance(result, str) and result.startswith("cp_")]
    losers = [marker for marker, result in outcomes.items() if result == 409]
    assert len(winners) == 1 and len(losers) == 1
    directory = profiles / "并发画像"
    assert directory.is_dir()
    assert winners[0] in (directory / "identity.md").read_text(encoding="utf-8")
    assert len(service.list_profiles()) == 1


def test_profile_storage_name_rejects_windows_path_forms(monkeypatch, tmp_path):
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", tmp_path / "profiles")
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    files = {"identity.md": "# 身份定位\n\n安全名称\n"}
    for name in ("C:review-profile", "CON", "name.", "..", "_internal"):
        with pytest.raises(WorkflowError, match="画像名称无效"):
            service.create_profile(name, files)


def test_analysis_request_is_idempotent_and_terminal_status_does_not_regress(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    _legacy_profile(profiles)
    service = ContentProfileService(tmp_path / "profiles.sqlite3")
    profile = service.list_profiles()[0]

    kwargs = dict(
        target_kind="account", account_id="douyin-tech", profile_id=profile["id"],
        samples=[{"title": "样本"}], request_digest="idem-001",
    )
    first = service.create_analysis(**kwargs)
    second = service.create_analysis(**kwargs)
    assert second["id"] == first["id"]
    service.set_analysis_status(first["id"], "running")
    succeeded = service.save_analysis_proposal(first["id"], {"files": {"identity.md": "提案"}})
    assert succeeded["status"] == "succeeded"
    regressed = service.set_analysis_status(first["id"], "running")
    assert regressed["status"] == "succeeded"


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
