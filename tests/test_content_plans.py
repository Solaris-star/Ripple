from fastapi import FastAPI
from fastapi.testclient import TestClient

from ripple.api import install
from ripple.library import MotherCreate
from ripple.plans import ContentPlanCreate
from ripple.publishing import ApprovalInput, CreateInput
from ripple.variants import VariantBatchInput, VariantRevision, VariantTarget, VariantTaskCreate


def test_plan_is_persistent_and_never_creates_a_publish_task(tmp_path):
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    payload = {"title": "周末城市漫步", "scheduled_local": "2032-09-23T18:30",
               "timezone": "Asia/Shanghai", "idempotency_key": "content-plan-first"}
    with TestClient(app, base_url="http://localhost") as client:
        created = client.post("/api/ripple/plans", json=payload)
        assert created.status_code == 201
        plan = created.json()
        assert plan["status"] == "planned"
        assert plan["task_id"] == ""
        assert client.post("/api/ripple/plans", json=payload).json()["id"] == plan["id"]
        assert len(client.get("/api/ripple/plans").json()) == 1
        assert any(row["kind"] == "plan" and row["id"] == plan["id"] for row in client.get("/api/ripple/calendar/entries").json())
        assert client.put(f'/api/ripple/plans/{plan["id"]}', json={
            **{key: value for key, value in payload.items() if key != "idempotency_key"},
            "scheduled_local": "2032-09-24T20:00", "expected_version": plan["version"],
        }).status_code == 200
    assert service.list()["total"] == 0


def test_unschedule_is_atomic_and_keeps_original_task_history(tmp_path):
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    task = service.create(CreateInput(
        title="排期中的测试内容", body="内容", platform="x", mode="simulation",
        scheduled_local="2032-09-23T18:30", timezone="Asia/Shanghai",
        idempotency_key="planned-task-original"))
    service.preflight(task["id"], task["version_id"])
    task = service.approve(task["id"], ApprovalInput(expected_version=task["version_id"], confirmed=True))
    with TestClient(app, base_url="http://localhost") as client:
        result = client.post(f'/api/ripple/tasks/{task["id"]}/return-to-plan', json={"expected_version": task["version_id"]})
        assert result.status_code == 200
        assert result.json()["task"]["status"] == "cancelled"
        assert result.json()["plan"]["status"] == "planned"
        assert result.json()["plan"]["scheduled_local"] == "2032-09-23T18:30"
        again = client.post(f'/api/ripple/tasks/{task["id"]}/return-to-plan', json={"expected_version": task["version_id"]})
        assert again.status_code == 200
        assert again.json()["plan"]["id"] == result.json()["plan"]["id"]
    assert service.get(task["id"])["status"] == "cancelled"
    assert len(service.plans.list()) == 1


def test_platform_plan_links_exact_variant_snapshot_without_approving(tmp_path):
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    source = service.library.create(MotherCreate(title="平台稿", body="正文", idempotency_key="plan-source"))
    variant = service.variants.create_many(source["id"], VariantBatchInput(
        expected_source_version=source["version_id"], idempotency_key="plan-variant",
        targets=[VariantTarget(platform="blog")]))["items"][0]
    variant = service.variants.revise(variant["id"], VariantRevision(
        **{**variant["content"], "delivery": "export", "scheduled_local": "2032-09-23T18:30"},
        expected_version=variant["version_id"], source_version_id=source["version_id"]))
    request = ContentPlanCreate(title="平台稿", scheduled_local="2032-09-23T18:30",
                                timezone="Asia/Shanghai", variant_id=variant["id"], idempotency_key="linked-plan")
    plan = service.plans.create(request)
    assert service.plans.create(request)["id"] == plan["id"]
    assert plan["source_id"] == source["id"]
    assert service.list()["total"] == 0
    task = service.variants.create_task(variant["id"], VariantTaskCreate(
        expected_version=variant["version_id"], idempotency_key="plan-task", plan_id=plan["id"]))
    assert task["plan_id"] == plan["id"]
    assert task["content"]["scheduled_at"] == plan["scheduled_at"]
    assert task["status"] == "draft"
    assert task["approval"] is None
    assert service.plans.get(plan["id"])["task_id"] == task["id"]
