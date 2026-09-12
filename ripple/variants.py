"""Durable platform variants, separate from immutable publish attempts.

A Mother is the canonical content source. A PlatformVariant is a long-lived,
editable platform-specific derivative. PublishTask records one execution
attempt/snapshot of one PlatformVariant version.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .publishing import ContentInput, PLATFORMS, WorkflowError, fingerprint, MAX_TASKS
from .tenancy import DEFAULT_WORKSPACE_ID

MAX_VARIANTS = 1000
EDITABLE_LEGACY_TASKS = {"draft", "review_ready", "approved", "scheduled", "failed_retryable", "verification_required"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class VariantTarget(BaseModel):
    """One platform selection. Account/remote target is intentionally optional."""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    platform: str

    @field_validator("platform")
    @classmethod
    def known_platform(cls, value: str) -> str:
        if value not in PLATFORMS:
            raise ValueError("未知渠道")
        return value


class VariantBatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_source_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    targets: list[VariantTarget] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def unique_platforms(self):
        keys = [item.platform for item in self.targets]
        if len(set(keys)) != len(keys):
            raise ValueError("同一批次不能重复选择平台")
        return self


class VariantContent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project_id: str = Field(default="local", min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=100_000)
    media: list[str] = Field(default_factory=list, max_length=12)
    tags: str = Field(default="", max_length=1000)
    target_id: str = Field(default="", max_length=100)
    delivery: Literal["remote", "export"] = "remote"
    scheduled_local: str | None = Field(default=None, max_length=40)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    fold: Literal[0, 1] | None = None
    options: dict = Field(default_factory=dict)


class VariantRevision(VariantContent):
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_version_id: str = Field(pattern=r"^[a-f0-9]{64}$")


class VariantTaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


class VariantService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store
        self._migrate_legacy_editable_tasks()

    @staticmethod
    def _public(item: dict) -> dict:
        row = {k: deepcopy(v) for k, v in item.items() if k != "history"}
        return row

    def _validate_content(self, value: VariantContent) -> dict:
        snapshot = value.model_dump()
        for path in snapshot["media"]:
            self.workspace.media_info(path)
        return snapshot

    def _variant(self, state: dict, variant_id: str, version: str | None = None) -> dict:
        item = state.setdefault("variants", {}).get(variant_id)
        if not item:
            raise WorkflowError("平台版本不存在。", 404)
        if version is not None and item["version_id"] != version:
            raise WorkflowError("平台版本已变化，请刷新后重试。")
        return item

    def _new_variant(self, source: dict, platform: str, *, content: dict | None = None, legacy_task_id: str = "") -> dict:
        source_content = source["content"]
        body = content or {
            "project_id": source_content.get("project_id", "local"),
            "title": source_content["title"],
            "body": source_content.get("body", ""),
            "media": list(source_content.get("media") or []),
            "tags": source_content.get("tags", ""),
            "target_id": "",
            "delivery": "remote",
            "scheduled_local": None,
            "timezone": "Asia/Shanghai",
            "fold": None,
            "options": {},
        }
        body = VariantContent.model_validate(body).model_dump()
        variant_id = uuid.uuid4().hex
        version_id = fingerprint({"source_id": source["id"], "source_version_id": source["version_id"], "platform": platform, "revision": 1, "content": body})
        at = utc_now()
        return {
            "id": variant_id,
            "workspace_id": source.get("workspace_id") or DEFAULT_WORKSPACE_ID,
            "source_id": source["id"],
            "source_version_id": source["version_id"],
            "platform": platform,
            "version": 1,
            "version_id": version_id,
            "content": body,
            "history": [],
            "created_at": at,
            "updated_at": at,
            **({"legacy_task_id": legacy_task_id} if legacy_task_id else {}),
        }

    def _migrate_legacy_editable_tasks(self) -> None:
        """Promote only unexecuted editable legacy task-as-variant records.

        Historical/submitted tasks stay untouched as execution history. Migration
        is deterministic and marks promoted tasks with variant_id so it never
        duplicates work across restarts.
        """
        with self.store.transaction() as state:
            variants = state.setdefault("variants", {})
            for task in state.get("tasks", {}).values():
                if task.get("variant_id") or task.get("attempts", 0) != 0 or task.get("status") not in EDITABLE_LEGACY_TASKS:
                    continue
                content = task.get("content") or {}
                source_id = content.get("source_id") or ""
                platform = content.get("platform") or ""
                source = state.get("contents", {}).get(source_id)
                if not source or platform not in PLATFORMS:
                    continue
                variant = next((row for row in variants.values() if row.get("source_id") == source_id and row.get("platform") == platform), None)
                if variant is None:
                    variant_content = {
                        "project_id": content.get("project_id", "local"),
                        "title": content.get("title") or source["content"]["title"],
                        "body": content.get("body", ""),
                        "media": list(content.get("media") or []),
                        "tags": content.get("tags", ""),
                        "target_id": "" if content.get("account_id") in {None, "", "local", "unselected"} else content.get("account_id", ""),
                        "delivery": "export" if content.get("mode") == "blog" else "remote",
                        "scheduled_local": content.get("scheduled_local"),
                        "timezone": content.get("timezone") or "Asia/Shanghai",
                        "fold": content.get("fold"),
                        "options": {},
                    }
                    variant = self._new_variant(source, platform, content=variant_content, legacy_task_id=task["id"])
                    variant["source_version_id"] = content.get("source_version_id") or source["version_id"]
                    variant["version_id"] = fingerprint({"source_id": source_id, "source_version_id": variant["source_version_id"], "platform": platform, "revision": 1, "content": variant["content"]})
                    variants[variant["id"]] = variant
                task["variant_id"] = variant["id"]
                task["variant_version_id"] = variant["version_id"]

    def list_for_source(self, source_id: str) -> list[dict]:
        with self.store.transaction(write=False) as state:
            rows = [self._public(row) for row in state.get("variants", {}).values() if row.get("source_id") == source_id]
            return sorted(rows, key=lambda row: row["created_at"])

    def get(self, variant_id: str) -> dict:
        with self.store.transaction(write=False) as state:
            return self._public(self._variant(state, variant_id))

    def tasks(self, variant_id: str) -> list[dict]:
        with self.store.transaction(write=False) as state:
            self._variant(state, variant_id)
            rows = [deepcopy(task) for task in state.get("tasks", {}).values() if task.get("variant_id") == variant_id]
            return sorted(rows, key=lambda row: row["created_at"], reverse=True)

    def create_many(self, source_id: str, req: VariantBatchInput) -> dict:
        digest = fingerprint({"source_id": source_id, "expected_source_version": req.expected_source_version, "platforms": [x.platform for x in req.targets]})
        with self.store.transaction() as state:
            source = state.get("contents", {}).get(source_id)
            if not source or source["version_id"] != req.expected_source_version:
                raise WorkflowError("母稿已更新，请刷新后重新选择平台。")
            variants = state.setdefault("variants", {})
            batches = state.setdefault("variant_batches", {})
            previous = batches.get(req.idempotency_key)
            if previous:
                if previous["digest"] != digest:
                    raise WorkflowError("平台版本创建键已用于不同平台或母稿版本。")
                return {"items": [self._public(variants[item_id]) for item_id in previous["variant_ids"]], "replayed": True}
            if len(variants) + len(req.targets) > MAX_VARIANTS:
                raise WorkflowError("平台版本数量已达到上限，请先归档。", 422)
            existing = {row["platform"] for row in variants.values() if row.get("source_id") == source_id}
            duplicate = [item.platform for item in req.targets if item.platform in existing]
            if duplicate:
                raise WorkflowError("以下平台版本已经存在：" + "、".join(PLATFORMS[key] for key in duplicate), 409)
            created = []
            for target in req.targets:
                row = self._new_variant(source, target.platform)
                variants[row["id"]] = row
                created.append(row)
            batches[req.idempotency_key] = {"workspace_id": source.get("workspace_id") or DEFAULT_WORKSPACE_ID, "digest": digest, "variant_ids": [row["id"] for row in created], "created_at": utc_now()}
            return {"items": [self._public(row) for row in created], "replayed": False}

    def revise(self, variant_id: str, req: VariantRevision) -> dict:
        content = self._validate_content(req)
        content.pop("expected_version", None)
        source_version_id = content.pop("source_version_id")
        with self.store.transaction() as state:
            variant = self._variant(state, variant_id, req.expected_version)
            source = state.get("contents", {}).get(variant["source_id"])
            if not source or source_version_id not in {variant["source_version_id"], source["version_id"]}:
                raise WorkflowError("母稿来源版本不匹配，请刷新后重试。")
            if len(variant["history"]) >= 50:
                raise WorkflowError("平台版本已达到 50 次修订上限。", 422)
            variant["history"].append({"version": variant["version"], "version_id": variant["version_id"], "source_version_id": variant["source_version_id"], "content": deepcopy(variant["content"])})
            variant["version"] += 1
            variant["source_version_id"] = source_version_id
            variant["content"] = content
            variant["version_id"] = fingerprint({"source_id": variant["source_id"], "source_version_id": source_version_id, "platform": variant["platform"], "revision": variant["version"], "content": content})
            variant["updated_at"] = utc_now()
            return self._public(variant)

    def delete(self, variant_id: str, expected_version: str) -> dict:
        with self.store.transaction() as state:
            self._variant(state, variant_id, expected_version)
            if any(task.get("variant_id") == variant_id for task in state.get("tasks", {}).values()):
                raise WorkflowError("该平台版本已有发布任务记录，不能删除；可继续编辑并创建新的任务快照。")
            del state["variants"][variant_id]
            return {"id": variant_id, "deleted": True}

    def create_task(self, variant_id: str, req: VariantTaskCreate) -> dict:
        with self.store.transaction() as state:
            variant = self._variant(state, variant_id, req.expected_version)
            source = state.get("contents", {}).get(variant["source_id"])
            if not source:
                raise WorkflowError("来源母稿不存在。", 404)
            for task in state.get("tasks", {}).values():
                if task.get("idempotency_key") == req.idempotency_key:
                    if task.get("variant_id") != variant_id or task.get("variant_version_id") != variant["version_id"]:
                        raise WorkflowError("发布任务创建键已用于其他平台版本。")
                    return deepcopy(task)
            existing = [task for task in state.get("tasks", {}).values() if task.get("variant_id") == variant_id and task.get("variant_version_id") == variant["version_id"] and task.get("status") not in {"cancelled", "failed_terminal"}]
            if existing:
                return deepcopy(sorted(existing, key=lambda row: row["created_at"], reverse=True)[0])
            content = variant["content"]
            if content["delivery"] == "export":
                if variant["platform"] != "blog":
                    raise WorkflowError("只有 Blog 平台版本支持 Markdown 导出。", 422)
                account_id, mode = "local", "blog"
            else:
                account_id, mode = content.get("target_id") or "", "real"
                if not account_id:
                    raise WorkflowError("请先为平台版本选择发布目标。", 422)
                if variant["platform"] != "blog":
                    account = state.get("accounts", {}).get(account_id)
                    if not account or account.get("platform") != variant["platform"]:
                        raise WorkflowError("所选发布目标不属于当前平台。", 422)
            task_input = ContentInput(
                project_id=content["project_id"], title=content["title"], body=content["body"],
                platform=variant["platform"], account_id=account_id, mode=mode,
                media=content["media"], scheduled_local=content.get("scheduled_local"),
                timezone=content.get("timezone") or "Asia/Shanghai", fold=content.get("fold"),
                source_id=variant["source_id"], source_version_id=variant["source_version_id"], tags=content.get("tags", ""),
                options=content.get("options") or {},
            )
            snapshot = self.workspace._snapshot(task_input)
            if len(state.get("tasks", {})) >= MAX_TASKS:
                raise WorkflowError("最多保存 500 个发布任务。", 422)
            task_id = uuid.uuid4().hex
            version_id = fingerprint({"variant_id": variant_id, "variant_version_id": variant["version_id"], "content": snapshot})
            now = self.workspace.clock().isoformat()
            task = {
                "id": task_id, "version": 1, "version_id": version_id,
                "workspace_id": variant.get("workspace_id") or DEFAULT_WORKSPACE_ID,
                "variant_id": variant_id, "variant_version_id": variant["version_id"],
                "content": snapshot, "history": [], "idempotency_key": req.idempotency_key,
                "initial_digest": version_id, "request_digest": version_id, "approval": None,
                "attempts": 0, "receipt": None, "created_at": now, "updated_at": now, "events": [],
            }
            self.workspace._event(task, "draft", "已从平台版本创建发布任务快照；尚未审核或执行。")
            state.setdefault("tasks", {})[task_id] = task
            return deepcopy(task)
