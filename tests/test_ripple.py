"""Ripple local-only regression suite. All data is written to pytest tmp_path."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import zipfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from ripple.api import install
from ripple.publishing import (
    ApprovalInput, CreateInput, PublishingService, RevisionInput, WorkflowError,
    capabilities, normalize_time,
)
from ripple.store import StoreError

@pytest.fixture(autouse=True)
def no_ai(monkeypatch):
    monkeypatch.delenv("RIPPLE_ENABLE_AI", raising=False)


@pytest.fixture
def service(tmp_path):
    return PublishingService(tmp_path)


def create(service, **kwargs):
    import uuid
    return service.create(CreateInput(title="第一篇内容", body="手动创作正文", idempotency_key=uuid.uuid4().hex, **kwargs))


def ready(service, task):
    check = service.preflight(task["id"], task["version_id"])
    assert check["ok"]
    return service.approve(task["id"], ApprovalInput(expected_version=task["version_id"], confirmed=True))


def png(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (2, 2))
    image.save(path)


def test_no_credentials_registry_is_honest():
    channels = capabilities()
    assert len(channels) == 10  # Nine target channels plus inherited Kuaishou.
    assert any(c['id'] == 'kuaishou' for c in channels)
    assert not any(c["connected"] or c["direct_publish"] for c in channels)
    assert [c["id"] for c in channels if c["local_export"]] == ["blog"]


def test_draft_requires_preflight_and_confirmation(service):
    task = create(service)
    with pytest.raises(WorkflowError):
        service.dispatch(task["id"], task["version_id"])
    with pytest.raises(WorkflowError):
        service.approve(task["id"], ApprovalInput(expected_version=task["version_id"], confirmed=True))
    service.preflight(task["id"], task["version_id"])
    with pytest.raises(WorkflowError):
        service.approve(task["id"], ApprovalInput(expected_version=task["version_id"]))


def test_real_mode_preflight_blocked(service):
    task = create(service, mode="real", platform="x", account_id="unconnected")
    assert not service.preflight(task["id"], task["version_id"])["ok"]
    with pytest.raises(WorkflowError):
        service.dispatch(task["id"], task["version_id"])


def test_idempotent_creation_and_payload_conflict(service):
    req = CreateInput(title="同一草稿", idempotency_key="creation-key-001")
    first = service.create(req)
    assert service.create(req)["id"] == first["id"]
    with pytest.raises(WorkflowError):
        service.create(req.model_copy(update={"title": "不同内容"}))
    assert service.list()["total"] == 1


def test_content_edit_invalidates_approval_and_preserves_version(service):
    task = ready(service, create(service))
    revision = RevisionInput(title="修订内容", expected_version=task["version_id"])
    updated = service.revise(task["id"], revision)
    assert updated["approval"] is None and updated["status"] == "draft"
    assert updated["version"] == 2
    assert updated["history"][0]["content"]["title"] == "第一篇内容"
    with pytest.raises(WorkflowError):
        service.dispatch(task["id"], task["version_id"])
    with pytest.raises(WorkflowError):
        service.dispatch(task["id"], updated["version_id"])


def test_target_edit_invalidates_approval(service):
    task = ready(service, create(service))
    updated = service.revise(task["id"], RevisionInput(title="第一篇内容", platform="x", expected_version=task["version_id"]))
    assert updated["status"] == "draft" and updated["approval"] is None


def test_double_dispatch_is_one_attempt(service):
    task = ready(service, create(service))
    with ThreadPoolExecutor(8) as pool:
        results = list(pool.map(lambda _: service.dispatch(task["id"], task["version_id"]), range(8)))
    final = service.get(task["id"])
    assert final["status"] == "simulated" and final["attempts"] == 1
    assert final["receipt"]["simulated"] and final["receipt"]["public_url"] is None
    assert all(t["id"] == final["id"] for t in results)


def test_unknown_cannot_retry_but_can_reconcile(service):
    task = ready(service, create(service, simulation_result="unknown"))
    task = service.dispatch(task["id"], task["version_id"])
    assert task["status"] == "unknown_result"
    with pytest.raises(WorkflowError):
        service.dispatch(task["id"], task["version_id"])
    with pytest.raises(WorkflowError):
        service.cancel(task["id"], task["version_id"])
    task = service.query(task["id"], task["version_id"])
    assert task["status"] == "simulated" and task["attempts"] == 1


def test_accepted_is_not_success(service):
    task = ready(service, create(service, simulation_result="accepted"))
    task = service.dispatch(task["id"], task["version_id"])
    assert task["status"] == "accepted"
    assert service.query(task["id"], task["version_id"])["status"] == "simulated"


def test_manual_verification_not_automatically_retried(service):
    task = ready(service, create(service, simulation_result="verification"))
    task = service.dispatch(task["id"], task["version_id"])
    assert task["status"] == "verification_required"
    service.tick()
    assert service.get(task["id"])["attempts"] == 1


def test_retry_requires_cooldown(service):
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    service.clock = lambda: now
    task = ready(service, create(service, simulation_result="retryable"))
    assert service.dispatch(task["id"], task["version_id"])["status"] == "failed_retryable"
    with pytest.raises(WorkflowError) as exc:
        service.dispatch(task["id"], task["version_id"])
    assert exc.value.status == 429
    now += timedelta(seconds=6)
    final = service.dispatch(task["id"], task["version_id"])
    assert final["status"] == "simulated" and final["attempts"] == 2


def test_independent_platform_tasks(service):
    a = ready(service, create(service, platform="x"))
    b = ready(service, create(service, platform="douyin", simulation_result="unknown"))
    service.dispatch(a["id"], a["version_id"])
    service.dispatch(b["id"], b["version_id"])
    assert service.get(a["id"])["status"] == "simulated"
    assert service.get(b["id"])["status"] == "unknown_result"


def test_persistence_after_new_service(service):
    task = create(service)
    reloaded = PublishingService(service.outputs)
    assert reloaded.get(task["id"])["version_id"] == task["version_id"]


def test_scheduled_task_executes_only_when_due(service):
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    service.clock = lambda: now
    task = ready(service, create(service, scheduled_local="2026-09-08T00:01", timezone="UTC"))
    assert task["content"]["scheduled_at"].endswith("+00:00")
    with pytest.raises(WorkflowError):
        service.dispatch(task["id"], task["version_id"])
    service.tick()
    assert service.get(task["id"])["status"] == "scheduled"
    now += timedelta(minutes=2)
    service.tick()
    assert service.get(task["id"])["status"] == "simulated"


def test_restart_does_not_catch_up_overdue_tasks(service):
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    service.clock = lambda: now
    task = ready(service, create(service, scheduled_local="2026-09-08T00:01", timezone="UTC"))
    now += timedelta(minutes=3)
    assert service.recover() == 1
    service.tick()
    task = service.get(task["id"])
    assert task["status"] == "verification_required" and task["approval"] is None
    assert task["attempts"] == 0


def test_dispatch_crash_recovers_unknown_not_retry(service):
    task = ready(service, create(service))
    with service.store.transaction() as state:
        state["tasks"][task["id"]]["status"] = "dispatching"
    assert service.recover() == 1
    assert service.get(task["id"])["status"] == "unknown_result"
    assert service.query(task["id"], task["version_id"])["status"] == "unknown_result"


@pytest.mark.parametrize("local", ["2026-03-08T02:30", "2026-11-01T01:30"])
def test_dst_gap_or_unspecified_fold_rejected(local):
    with pytest.raises(WorkflowError):
        normalize_time(local, "America/Los_Angeles", None)


def test_dst_fold_distinguishes_two_instants():
    first = normalize_time("2026-11-01T01:30", "America/Los_Angeles", 0)
    second = normalize_time("2026-11-01T01:30", "America/Los_Angeles", 1)
    assert datetime.fromisoformat(second) - datetime.fromisoformat(first) == timedelta(hours=1)


@pytest.mark.parametrize("zone", ["../../etc/passwd", "NoSuch/Zone", ""])
def test_invalid_time_zone_rejected(zone):
    with pytest.raises(WorkflowError):
        normalize_time(None, zone, None)


@pytest.mark.parametrize("path", ["../secret.png", "C:/secret.png", "_login/cookies.png", "project/.env", "project/cookies.json", "project/a.svg", "project/../a.png", "project\\a.png"])
def test_media_escape_and_secret_paths_rejected(service, path):
    with pytest.raises(WorkflowError):
        create(service, media=[path])


def test_fake_image_extension_rejected(service):
    path = service.outputs / "article" / "image.png"
    path.parent.mkdir()
    path.write_bytes(b"this is not an image")
    with pytest.raises(WorkflowError):
        create(service, media=["article/image.png"])


def test_media_changed_after_approval_invalidates(service):
    path = service.outputs / "article" / "cover.png"
    png(path)
    task = ready(service, create(service, media=["article/cover.png"]))
    Image.new("RGB", (3, 3)).save(path)
    task = service.dispatch(task["id"], task["version_id"])
    assert task["status"] == "draft" and task["approval"] is None
    assert task["attempts"] == 0


def test_blog_export_real_archive_no_network(service, monkeypatch):
    def network_forbidden(*_a, **_k):
        raise AssertionError("No network should be used")
    import socket
    monkeypatch.setattr(socket, "create_connection", network_forbidden)
    png(service.outputs / "article" / "cover.png")
    task = ready(service, create(service, mode="blog", media=["article/cover.png"]))
    task = service.dispatch(task["id"], task["version_id"])
    assert task["status"] == "exported" and task["receipt"]["public_url"] is None
    with zipfile.ZipFile(service.export_path(task)) as archive:
        assert set(archive.namelist()) == {"post.md", "manifest.json", "media/01.png"}
        assert "draft: true" in archive.read("post.md").decode()
        assert json.loads(archive.read("manifest.json"))["export_only"] is True
    assert service.dispatch(task["id"], task["version_id"])["attempts"] == 1


def test_cancel_before_dispatch(service):
    task = ready(service, create(service))
    assert service.cancel(task["id"], task["version_id"])["status"] == "cancelled"
    with pytest.raises(WorkflowError):
        service.dispatch(task["id"], task["version_id"])


def test_corrupt_store_fails_closed(service):
    create(service)
    service.store.path.write_text("{broken", encoding="utf-8")
    with pytest.raises(StoreError):
        service.list()
    assert service.store.path.read_text() == "{broken"


def test_concurrent_writes_preserve_all_tasks(service):
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda _: create(service), range(20)))
    assert service.list()["total"] == 20


def test_api_workflow_and_security(tmp_path):
    application = FastAPI()
    install(application, tmp_path)
    with TestClient(application, base_url="http://localhost") as client:
        assert client.get("/api/ripple/status").json()["live_publishing"] is False
        assert client.post("/api/ripple/tasks", json={"title": "API 草稿", "idempotency_key": "test-http-001"}, headers={"origin": "https://evil.invalid"}).status_code == 403
        assert client.get("/api/ripple/tasks", headers={"host": "evil.invalid"}).status_code == 400
        assert client.post("/api/publish/x", json={}).status_code == 403
        # The isolated publishing API does not expose the product Agent/chat surface.
        assert client.post("/api/chat/stream", json={}).status_code == 404
        assert client.get("/api/accounts").status_code == 503
        assert client.get("/api/accounts/xiaohongshu/whoami").status_code == 503
        assert client.get("/api/analytics/platforms").status_code == 503
        assert client.post("/api/login/xiaohongshu").status_code == 503
        assert client.post("/api/logout/xiaohongshu").status_code == 503
        response = client.post("/api/ripple/tasks", json={"title": "API 草稿", "mode": "blog", "idempotency_key": "test-http-001"})
        assert response.status_code == 201
        task = response.json()
        path = "/api/ripple/tasks/" + task["id"]
        action = {"expected_version": task["version_id"]}
        assert client.post(path + "/dispatch", json=action).status_code == 409
        assert client.post(path + "/preflight", json=action).json()["ok"]
        assert client.post(path + "/approve", json={**action, "confirmed": True}).status_code == 200
        assert client.post(path + "/dispatch", json=action).json()["status"] == "exported"
        exported = client.get(path + "/export")
        assert exported.status_code == 200
        assert zipfile.ZipFile(io.BytesIO(exported.content)).testzip() is None
        assert client.get("/api/ripple/tasks?limit=201").status_code == 422


def test_long_sleep_does_not_auto_catch_up(service):
    now = datetime(2026, 9, 8, tzinfo=timezone.utc)
    service.clock = lambda: now
    task = ready(service, create(service, scheduled_local="2026-09-08T00:01", timezone="UTC"))
    now += timedelta(minutes=10)
    service.tick()
    recovered = service.get(task["id"])
    assert recovered["status"] == "verification_required"
    assert recovered["attempts"] == 0 and recovered["approval"] is None


def test_all_task_counts_independent_of_pagination(service):
    create(service)
    create(service)
    result = service.list(limit=1)
    assert len(result["items"]) == 1 and result["total"] == 2
    assert result["counts"]["draft"] == 2
    assert "history" not in result["items"][0]


def test_idle_scheduler_does_not_rewrite_store(service):
    create(service)
    before = service.store.path.stat().st_mtime_ns
    service.tick()
    assert service.store.path.stat().st_mtime_ns == before


def test_retry_limit(service):
    task = ready(service, create(service))
    with service.store.transaction() as state:
        stored = state["tasks"][task["id"]]
        stored.update(status="failed_retryable", attempts=3)
    with pytest.raises(WorkflowError):
        service.dispatch(task["id"], task["version_id"])


def test_event_records_exact_version(service):
    task = ready(service, create(service))
    assert all(event["version_id"] == task["version_id"] for event in task["events"])


def test_concurrent_processes_preserve_tasks(service):
    import subprocess
    import sys
    code = (
        "from pathlib import Path; import sys; "
        "from ripple.publishing import PublishingService, CreateInput; "
        "s=PublishingService(Path(sys.argv[1])); "
        "[s.create(CreateInput(title='process test', idempotency_key='worker-'+sys.argv[2]+'-'+str(i))) for i in range(4)]"
    )
    processes = [subprocess.Popen([sys.executable, "-X", "utf8", "-c", code, str(service.outputs), str(i)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE) for i in range(3)]
    try:
        for process in processes:
            _stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr.decode(errors="replace")
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert service.list()["total"] == 12


def test_manual_profile_does_not_start_ai(tmp_path, monkeypatch):
    import asyncio
    from web import app as upstream
    monkeypatch.setattr(upstream, "PROFILES_DIR", tmp_path / "profiles")
    monkeypatch.setattr(upstream, "PROFILE_BUILD_DIR", tmp_path / "profile-status")
    def forbidden(*args, **kwargs):
        raise AssertionError("AI must not be invoked")
    monkeypatch.setattr(upstream, "run_agent_sync", forbidden)
    result = asyncio.run(upstream.api_profile_build(upstream.ProfileBuildRequest(
        name="本地画像", form={"direction": "知识分享", "platforms": ["个人 Blog"]})))
    assert result["created"] is True and result["async"] is False
    assert (tmp_path / "profiles" / "本地画像" / "identity.md").is_file()
    assert not (tmp_path / "profile-status").exists()
