import asyncio
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import ripple.content_profiles as content_profiles
import ripple.persona as persona
from ripple.content_profiles import ContentProfileService
from ripple.persona import _FILE_ORDER
from ripple.publishing import WorkflowError
from web import app as webapp


def _files(marker: str) -> dict[str, str]:
    return {name: f"# {name}\n\n{marker}\n" for name in _FILE_ORDER}


@pytest.fixture()
def isolated_profiles(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    db_path = tmp_path / "profiles.sqlite3"
    monkeypatch.setattr(content_profiles, "PROFILES_DIR", profiles)
    monkeypatch.setattr(persona, "PROFILES_DIR", profiles)
    monkeypatch.setattr(persona, "PROFILE_DB_PATH", db_path)
    return ContentProfileService(db_path), profiles, db_path


def _proposal_run(service: ContentProfileService, account_id: str, profile_id: str = ""):
    profile = service.get_profile(profile_id) if profile_id else None
    binding = service.binding(target_kind="account", account_id=account_id)
    run = service.create_analysis(
        target_kind="account",
        account_id=account_id,
        profile_id=profile_id,
        samples=[{"title": "fixture"}],
        model={
            "base_profile_revision": profile["current_revision"] if profile else 0,
            "base_files": dict(profile["files"]) if profile else {},
            "base_binding_revision": int((binding or {}).get("binding_revision") or 0),
            "base_binding_profile_id": str((binding or {}).get("profile_id") or ""),
            "base_overrides": dict((binding or {}).get("overrides") or {}),
        },
    )
    service.save_analysis_proposal(run["id"], {"files": _files("PROPOSED")})
    return service.get_analysis(run["id"])


def test_new_profile_apply_failure_never_deletes_profiles_root(monkeypatch, isolated_profiles):
    service, profiles, db_path = isolated_profiles
    keep_a = service.create_profile("Keep-A", _files("A"))
    keep_b = service.create_profile("Keep-B", _files("B"))
    run = _proposal_run(service, "a" * 32)

    with sqlite3.connect(db_path) as db:
        db.execute(
            "CREATE TRIGGER reject_agent_revision BEFORE INSERT ON profile_revisions "
            "WHEN NEW.source='agent_analysis' BEGIN SELECT RAISE(ABORT, 'blocked revision'); END;"
        )

    monkeypatch.setattr(webapp, "_CONTENT_PROFILES", service)
    monkeypatch.setattr(
        webapp,
        "_profile_target",
        lambda *_args, **_kwargs: {"id": "a" * 32, "platform": "xiaohongshu"},
    )

    with pytest.raises(sqlite3.IntegrityError, match="blocked revision"):
        asyncio.run(
            webapp.api_profile_analysis_apply(
                run["id"],
                webapp.ProfileAnalysisApplyRequest(display_name="New-profile", bind_target=True),
            )
        )

    assert profiles.is_dir()
    assert (profiles / "Keep-A").is_dir()
    assert (profiles / "Keep-B").is_dir()
    assert service.get_profile(keep_a["id"])["current_revision"] == 1
    assert service.get_profile(keep_b["id"])["current_revision"] == 1
    assert not (profiles / "New-profile").exists()


def test_sync_failure_preserves_external_drafts_per_profile(monkeypatch, isolated_profiles):
    service, profiles, db_path = isolated_profiles
    service.create_profile("A-profile", _files("A"))
    service.create_profile("B-profile", _files("B"))
    (profiles / "A-profile" / "identity.md").write_text("UNCONFIRMED-A", encoding="utf-8")
    (profiles / "B-profile" / "identity.md").write_text("UNCONFIRMED-B", encoding="utf-8")

    original = content_profiles._restore_confirmed_mirror

    def fail_b(name, files):
        if name == "B-profile":
            raise OSError("fixture lock")
        return original(name, files)

    monkeypatch.setattr(content_profiles, "_restore_confirmed_mirror", fail_b)
    service.sync_legacy_profiles()

    with sqlite3.connect(db_path) as db:
        rows = db.execute(
            "SELECT files_json FROM profile_revisions WHERE status='draft' ORDER BY rowid"
        ).fetchall()
    assert len(rows) == 2
    assert "UNCONFIRMED-A" in rows[0][0]
    assert "UNCONFIRMED-B" in rows[1][0]
    assert "UNCONFIRMED-A" not in (profiles / "A-profile" / "identity.md").read_text(encoding="utf-8")
    assert "UNCONFIRMED-B" in (profiles / "B-profile" / "identity.md").read_text(encoding="utf-8")


def test_cross_service_concurrent_create_preserves_winner_directory(monkeypatch, isolated_profiles):
    service_a, profiles, db_path = isolated_profiles
    service_b = ContentProfileService(db_path)
    barrier = threading.Barrier(2)
    outcomes = {}

    def create(service, marker):
        barrier.wait(timeout=5)
        try:
            outcomes[marker] = service.create_profile(
                "Contended", {"identity.md": f"# 身份定位\n\n{marker}\n"}
            )["id"]
        except WorkflowError as exc:
            outcomes[marker] = exc.status

    threads = [
        threading.Thread(target=create, args=(service_a, "A"), daemon=True),
        threading.Thread(target=create, args=(service_b, "B"), daemon=True),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(8)
        assert not thread.is_alive()

    winners = [marker for marker, value in outcomes.items() if isinstance(value, str)]
    losers = [marker for marker, value in outcomes.items() if value == 409]
    assert len(winners) == 1
    assert len(losers) == 1
    assert (profiles / "Contended").is_dir()
    assert winners[0] in (profiles / "Contended" / "identity.md").read_text(encoding="utf-8")
    assert len(service_a.list_profiles()) == 1


def test_extra_markdown_is_drafted_then_removed_from_confirmed_mirror(isolated_profiles):
    service, profiles, _db_path = isolated_profiles
    profile = service.create_profile("Extra", _files("CONFIRMED"))
    extra = profiles / "Extra" / "extra.md"
    extra.write_text("UNCONFIRMED EXTRA FILE", encoding="utf-8")

    service.sync_legacy_profiles()

    with sqlite3.connect(service.db_path) as db:
        draft = db.execute(
            "SELECT files_json,status FROM profile_revisions "
            "WHERE profile_id=? ORDER BY revision DESC LIMIT 1",
            (profile["id"],),
        ).fetchone()
    assert draft[1] == "draft"
    assert "UNCONFIRMED EXTRA FILE" in draft[0]
    assert not extra.exists()
    assert "UNCONFIRMED EXTRA FILE" not in persona.load_profile_text("Extra")


class _InlineThread:
    def __init__(self, *, target, args, daemon):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


def test_history_waiting_task_resumes_saved_manifest_without_recrawl(monkeypatch, isolated_profiles):
    service, _profiles, _db_path = isolated_profiles
    account_id = "h" * 32
    monkeypatch.setattr(webapp, "_CONTENT_PROFILES", service)
    monkeypatch.setattr(
        webapp,
        "_profile_target",
        lambda *_args, **_kwargs: {"id": account_id, "platform": "xiaohongshu"},
    )
    remote_calls = []

    def remote_contents(_account_id, _limit):
        remote_calls.append(True)
        return {
            "items": [
                {
                    "title": "历史作品",
                    "url": "https://example.invalid/history",
                    "metrics": {"views": len(remote_calls)},
                }
            ]
        }

    monkeypatch.setattr(
        webapp,
        "_RIPPLE_WORKSPACE",
        type("W", (), {"interactions": type("I", (), {"remote_contents": staticmethod(remote_contents)})()})(),
    )
    ready = {"value": False}
    worker_calls = []
    monkeypatch.setattr(webapp, "_recommendation_ai_backend", lambda: "fixture" if ready["value"] else "")
    monkeypatch.setattr(webapp, "threading", SimpleNamespace(Thread=_InlineThread))

    def worker(run_id, _target, samples, _profile_id):
        worker_calls.append(list(samples))
        service.save_analysis_proposal(run_id, {"files": _files("DONE")})

    monkeypatch.setattr(webapp, "_profile_analysis_worker", worker)

    payload = webapp.ProfileAnalysisCreateRequest(
        account_id=account_id,
        use_account_history=True,
        samples=[],
        history_limit=10,
        idempotency_key="history-resume",
        confirmed=True,
    )
    first = asyncio.run(webapp.api_profile_analysis_create(payload))
    assert first["status"] == "waiting_user"
    assert len(remote_calls) == 1
    assert first["samples"][0]["kind"] == "account_history_title_only"

    # Exact create replay dedupes before remote acquisition.
    replay = asyncio.run(webapp.api_profile_analysis_create(payload))
    assert replay["id"] == first["id"]
    assert len(remote_calls) == 1

    ready["value"] = True
    resumed = asyncio.run(webapp.api_profile_analysis_resume(first["id"]))
    assert resumed["id"] == first["id"]
    assert len(remote_calls) == 1
    assert len(worker_calls) == 1
    assert worker_calls[0] == first["samples"]


def test_stale_waiting_task_rejects_before_worker(monkeypatch, isolated_profiles):
    service, _profiles, _db_path = isolated_profiles
    account_id = "s" * 32
    profile = service.create_profile("Stale", _files("V1"))
    service.bind(target_kind="account", account_id=account_id, profile_id=profile["id"])
    monkeypatch.setattr(webapp, "_CONTENT_PROFILES", service)
    monkeypatch.setattr(
        webapp,
        "_profile_target",
        lambda *_args, **_kwargs: {"id": account_id, "platform": "xiaohongshu"},
    )
    monkeypatch.setattr(webapp, "_recommendation_ai_backend", lambda: "")
    worker_calls = []
    monkeypatch.setattr(webapp, "_profile_analysis_worker", lambda *args: worker_calls.append(args))
    monkeypatch.setattr(webapp, "threading", SimpleNamespace(Thread=_InlineThread))

    first = asyncio.run(
        webapp.api_profile_analysis_create(
            webapp.ProfileAnalysisCreateRequest(
                account_id=account_id,
                profile_id=profile["id"],
                expected_profile_revision=1,
                expected_binding_revision=1,
                expected_binding_profile_id=profile["id"],
                samples=[{"title": "fixture"}],
                idempotency_key="stale-run",
                confirmed=True,
            )
        )
    )
    assert first["status"] == "waiting_user"

    service.save_revision(profile["id"], _files("V2"), source="manual", expected_revision=1)
    monkeypatch.setattr(webapp, "_recommendation_ai_backend", lambda: "fixture")

    with pytest.raises(HTTPException) as exc:
        asyncio.run(webapp.api_profile_analysis_resume(first["id"]))
    assert exc.value.status_code == 409
    assert worker_calls == []
