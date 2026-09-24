import pytest
from pydantic import ValidationError

from ripple.library import MotherCreate
from ripple.publishing import WorkflowError
from ripple.variants import VariantBatchInput, VariantTaskCreate
from ripple.workspace import WorkspaceService


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.delenv("RIPPLE_ENABLE_AI", raising=False)
    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    yield service
    service.close()


def _source(workspace):
    return workspace.library.create(
        MotherCreate(title="同平台多账号", body="账号隔离测试", idempotency_key="variant-account-source")
    )


def test_same_platform_different_accounts_create_independent_variants(workspace):
    mother = _source(workspace)
    request = VariantBatchInput(
        expected_source_version=mother["version_id"],
        idempotency_key="variant-account-batch",
        targets=[
            {"platform": "xiaohongshu", "account_id": "xhs-tech"},
            {"platform": "xiaohongshu", "account_id": "xhs-life"},
        ],
    )
    result = workspace.variants.create_many(mother["id"], request)

    assert len(result["items"]) == 2
    assert {row["content"]["target_id"] for row in result["items"]} == {"xhs-tech", "xhs-life"}
    assert {row["platform"] for row in result["items"]} == {"xiaohongshu"}


def test_same_platform_same_account_is_rejected_in_one_batch(workspace):
    mother = _source(workspace)
    with pytest.raises(ValidationError, match="同一平台的同一账号"):
        VariantBatchInput(
            expected_source_version=mother["version_id"],
            idempotency_key="variant-account-duplicate",
            targets=[
                {"platform": "xiaohongshu", "account_id": "xhs-tech"},
                {"platform": "xiaohongshu", "account_id": "xhs-tech"},
            ],
        )


def test_existing_variant_only_blocks_same_platform_account_pair(workspace):
    # Exact duplicate detection is pair-scoped; another account on the same platform remains valid.
    mother = _source(workspace)
    first = VariantBatchInput(
        expected_source_version=mother["version_id"],
        idempotency_key="variant-account-first",
        targets=[{"platform": "douyin", "account_id": "douyin-a"}],
    )
    workspace.variants.create_many(mother["id"], first)

    second = VariantBatchInput(
        expected_source_version=mother["version_id"],
        idempotency_key="variant-account-second",
        targets=[{"platform": "douyin", "account_id": "douyin-b"}],
    )
    assert workspace.variants.create_many(mother["id"], second)["items"][0]["content"]["target_id"] == "douyin-b"

    duplicate = VariantBatchInput(
        expected_source_version=mother["version_id"],
        idempotency_key="variant-account-third",
        targets=[{"platform": "douyin", "account_id": "douyin-a"}],
    )
    with pytest.raises(WorkflowError, match="账号版本已经存在"):
        workspace.variants.create_many(mother["id"], duplicate)


def test_publish_task_freezes_profile_binding_context(workspace):
    class FakeProfiles:
        revision = 7

        def binding(self, *, target_kind, account_id):
            assert target_kind == "account"
            assert account_id == "xhs-tech"
            return {
                "profile_id": "cp_profile",
                "binding_revision": 3,
                "overrides": {"tone": "教程"},
            }

        def get_profile(self, profile_id):
            assert profile_id == "cp_profile"
            return {
                "id": "cp_profile",
                "current_revision": self.revision,
                "display_name": "科技工具",
                "legacy_name": "科技工具",
            }

    workspace.content_profiles = FakeProfiles()
    with workspace.store.transaction() as state:
        state.setdefault("accounts", {})["xhs-tech"] = {"id": "xhs-tech", "platform": "xiaohongshu"}

    mother = _source(workspace)
    batch = workspace.variants.create_many(
        mother["id"],
        VariantBatchInput(
            expected_source_version=mother["version_id"],
            idempotency_key="variant-profile-context",
            targets=[{"platform": "xiaohongshu", "account_id": "xhs-tech"}],
        ),
    )
    variant = batch["items"][0]
    task = workspace.variants.create_task(
        variant["id"],
        VariantTaskCreate(expected_version=variant["version_id"], idempotency_key="task-profile-context"),
    )
    frozen = task["content_profile_context"]
    assert frozen == {
        "profile_id": "cp_profile",
        "profile_revision": 7,
        "profile_name": "科技工具",
        "legacy_name": "科技工具",
        "binding_revision": 3,
        "target_kind": "account",
        "account_id": "xhs-tech",
        "overrides": {"tone": "教程"},
    }

    workspace.content_profiles.revision = 8
    assert workspace.get(task["id"])["content_profile_context"]["profile_revision"] == 7
