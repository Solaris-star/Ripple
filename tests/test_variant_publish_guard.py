import pytest

from ripple.library import MotherCreate, MotherRevision
from ripple.publishing import WorkflowError
from ripple.variants import VariantBatchInput, VariantRevision, VariantTaskCreate
from ripple.workspace import WorkspaceService


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("RIPPLE_ENABLE_AI", raising=False)
    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    with work.store.transaction() as state:
        state.setdefault("accounts", {})["test-xhs"] = {"id": "test-xhs", "platform": "xiaohongshu"}
    yield work
    work.close()


def draft_and_task(service):
    mother = service.library.create(MotherCreate(title="测试图文", body="正文", idempotency_key="guard-source"))
    variant = service.variants.create_many(mother["id"], VariantBatchInput(
        expected_source_version=mother["version_id"], idempotency_key="guard-variant",
        targets=[{"platform": "xiaohongshu", "account_id": "test-xhs"}],
    ))["items"][0]
    task = service.variants.create_task(variant["id"], VariantTaskCreate(
        expected_version=variant["version_id"], idempotency_key="guard-first-task"))
    return variant, task


def revise(service, variant):
    return service.variants.revise(variant["id"], VariantRevision(
        **{**variant["content"], "body": "修订后的正文"},
        expected_version=variant["version_id"], source_version_id=variant["source_version_id"],
    ))


@pytest.mark.parametrize("status", ["dispatching", "accepted", "unknown_result", "verification_required"])
def test_new_revision_cannot_bypass_unresolved_submission(service, status):
    variant, task = draft_and_task(service)
    with service.store.transaction() as state:
        state["tasks"][task["id"]].update(status=status, attempts=1)
    updated = revise(service, variant)
    with pytest.raises(WorkflowError, match="发布结果待核对"):
        service.variants.create_task(updated["id"], VariantTaskCreate(
            expected_version=updated["version_id"], idempotency_key="guard-second-task"))
    assert len(service.variants.tasks(variant["id"])) == 1


def test_unchanged_draft_reuses_task_after_failed_check(service):
    variant, task = draft_and_task(service)
    replay = service.variants.create_task(variant["id"], VariantTaskCreate(
        expected_version=variant["version_id"], idempotency_key="guard-repeat-check"))
    assert replay["id"] == task["id"]
    assert len(service.variants.tasks(variant["id"])) == 1


def test_old_schedule_must_be_cancelled_before_new_revision(service):
    variant, task = draft_and_task(service)
    with service.store.transaction() as state:
        state["tasks"][task["id"]]["status"] = "scheduled"
    updated = revise(service, variant)
    with pytest.raises(WorkflowError, match="取消原定时"):
        service.variants.create_task(updated["id"], VariantTaskCreate(
            expected_version=updated["version_id"], idempotency_key="guard-second-task"))


def test_confirmed_not_submitted_can_prepare_revision(service):
    variant, task = draft_and_task(service)
    with service.store.transaction() as state:
        state["tasks"][task["id"]].update(status="verification_required", attempts=1,
                                        receipt={"not_submitted": True})
    updated = revise(service, variant)
    next_task = service.variants.create_task(updated["id"], VariantTaskCreate(
        expected_version=updated["version_id"], idempotency_key="guard-second-task"))
    assert next_task["id"] != task["id"]
    assert next_task["status"] == "draft"


def test_mother_update_keeps_platform_rewrite_and_original_source_snapshot(service):
    mother = service.library.create(MotherCreate(title="原主稿", body="原正文", idempotency_key="keep-mother-source"))
    variant = service.variants.create_many(mother['id'], VariantBatchInput(
        expected_source_version=mother['version_id'], idempotency_key='keep-platform-variant',
        targets=[{'platform': 'xiaohongshu', 'account_id': 'test-xhs'}],
    ))['items'][0]
    rewritten = service.variants.revise(variant['id'], VariantRevision(
        **{**variant['content'], 'title': '平台标题', 'body': '平台改写正文'},
        expected_version=variant['version_id'], source_version_id=mother['version_id'],
    ))
    service.library.revise(mother['id'], MotherRevision(title='主稿已更新', body='新的主稿正文', expected_version=mother['version_id']))
    task = service.variants.create_task(variant['id'], VariantTaskCreate(expected_version=rewritten['version_id'], idempotency_key='keep-rewrite-publish'))
    assert task['content']['body'] == '平台改写正文'
    assert task['content']['title'] == '平台标题'
    assert task['content']['source_version_id'] == mother['version_id']
    with service.store.transaction(write=False) as state:
        assert service._lineage_problems(state['tasks'][task['id']], state) == []
