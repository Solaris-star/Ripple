from fastapi import FastAPI
from fastapi.testclient import TestClient

from ripple.api import install
from ripple.library import MotherCreate, MotherRevision


def test_proposal_routes_require_explicit_apply_and_keep_history(tmp_path):
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    source = service.library.create(MotherCreate(title="第一版", body="原文", idempotency_key="proposal-original"))
    proposal = service.library.propose(source["id"], MotherRevision(
        title="新版", body="建议正文", expected_version=source["version_id"]), "proposal-change")
    assert service.library.get(source["id"])["content"]["body"] == "原文"

    with TestClient(app, base_url="http://localhost") as client:
        read = client.get(f'/api/ripple/content-proposals/{proposal["id"]}')
        assert read.status_code == 200
        assert read.json()["status"] == "pending"
        assert len(client.get(f'/api/ripple/contents/{source["id"]}/proposals').json()) == 1
        endpoint = f'/api/ripple/content-proposals/{proposal["id"]}/apply'
        applied = client.post(endpoint, json={"expected_version": source["version_id"]})
        assert applied.status_code == 200
        assert applied.json()["status"] == "applied"
        assert client.post(endpoint, json={"expected_version": source["version_id"]}).json()["applied_version_id"] == applied.json()["applied_version_id"]
    current = service.library.get(source["id"])
    assert current["content"]["body"] == "建议正文"
    assert len(current["history"]) == 1
    with TestClient(app, base_url="http://localhost") as client:
        undo_url = f'/api/ripple/content-proposals/{proposal["id"]}/undo'
        undone = client.post(undo_url, json={"expected_version": current["version_id"]})
        assert undone.status_code == 200
        assert undone.json()["status"] == "undone"
        assert client.post(undo_url, json={"expected_version": current["version_id"]}).json()["undone_version_id"] == undone.json()["undone_version_id"]
    restored = service.library.get(source["id"])
    assert restored["content"]["body"] == "原文"
    assert len(restored["history"]) == 2


def test_proposal_refuses_stale_base_version(tmp_path):
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    source = service.library.create(MotherCreate(title="原稿", body="第一句", idempotency_key="proposal-stale-original"))
    proposal = service.library.propose(source["id"], MotherRevision(
        title="原稿", body="AI 建议", expected_version=source["version_id"]), "proposal-stale-update")
    service.library.revise(source["id"], MotherRevision(
        title="原稿", body="手工改稿", expected_version=source["version_id"]))
    with TestClient(app, base_url="http://localhost") as client:
        conflict = client.post(f'/api/ripple/content-proposals/{proposal["id"]}/apply', json={"expected_version": source["version_id"]})
        assert conflict.status_code == 409
    assert service.library.get(source["id"])["content"]["body"] == "手工改稿"


def test_undo_refuses_later_manual_edit(tmp_path):
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    source = service.library.create(MotherCreate(title="标题", body="原文", idempotency_key="undo-base"))
    proposal = service.library.propose(source["id"], MotherRevision(
        title="标题", body="建议", expected_version=source["version_id"]), "undo-proposal")
    applied = service.library.apply_proposal(proposal["id"], source["version_id"])
    service.library.revise(source["id"], MotherRevision(
        title="标题", body="后来的手工编辑", expected_version=applied["applied_version_id"]))
    with TestClient(app, base_url="http://localhost") as client:
        result = client.post(f'/api/ripple/content-proposals/{proposal["id"]}/undo', json={"expected_version": applied["applied_version_id"]})
        assert result.status_code == 409
    assert service.library.get(source["id"])["content"]["body"] == "后来的手工编辑"
