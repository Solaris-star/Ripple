"""Mother content revisions and links to independent platform versions."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import uuid

from pydantic import BaseModel, ConfigDict, Field
from .publishing import WorkflowError, fingerprint
from .tenancy import DEFAULT_WORKSPACE_ID


class MotherInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=100000)
    media: list[str] = Field(default_factory=list, max_length=12)
    tags: str = Field(default="", max_length=1000)
    project_id: str = Field(default="local", min_length=1, max_length=100)


class MotherCreate(MotherInput):
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


class MotherRevision(MotherInput):
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")


def mother_snapshot(value: dict) -> dict:
    return MotherInput.model_validate({k: value[k] for k in MotherInput.model_fields if k in value}).model_dump()


def add_mother(state: dict, snapshot: dict, key: str) -> dict:
    records = state.setdefault("contents", {})
    digest = fingerprint(snapshot)
    for item in records.values():
        if item["creation_key"] == key:
            if item["initial_digest"] != digest:
                raise WorkflowError("内容创建键已被不同内容使用。")
            return item
    if len(records) >= 500:
        raise WorkflowError("最多保存 500 份内容，请先归档。", 422)
    at = datetime.now(timezone.utc).isoformat()
    item = {"id": uuid.uuid4().hex, "workspace_id": DEFAULT_WORKSPACE_ID, "version": 1, "version_id": digest, "content": snapshot,
            "history": [], "creation_key": key, "initial_digest": digest, "created_at": at, "updated_at": at}
    records[item["id"]] = item
    return item


class ContentLibrary:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store

    def _validate(self, req):
        snapshot = mother_snapshot(req.model_dump())
        for path in snapshot["media"]:
            self.workspace.media_info(path)
        return snapshot

    @staticmethod
    def _public(item):
        return {k: deepcopy(v) for k, v in item.items() if k not in {"creation_key", "initial_digest"}}

    def list(self):
        with self.store.transaction(write=False) as state:
            rows = []
            for item in sorted(state.get("contents", {}).values(), key=lambda i: i["updated_at"], reverse=True):
                row = self._public(item)
                row.pop("history", None)
                row["variants"] = [{"id": variant["id"], "platform": variant["platform"],
                    "target_id": variant["content"].get("target_id", ""), "delivery": variant["content"].get("delivery", "remote"),
                    "version": variant["version"], "version_id": variant["version_id"],
                    "stale": variant.get("source_version_id") != item["version_id"]}
                    for variant in state.get("variants", {}).values() if variant.get("source_id") == item["id"]]
                rows.append(row)
            return rows

    def get(self, source_id):
        with self.store.transaction(write=False) as state:
            item = state.get("contents", {}).get(source_id)
            if not item:
                raise WorkflowError("内容不存在。", 404)
            return self._public(item)

    def create(self, req: MotherCreate):
        snapshot = self._validate(req)
        with self.store.transaction() as state:
            return self._public(add_mother(state, snapshot, req.idempotency_key))

    def revise(self, source_id: str, req: MotherRevision):
        snapshot = self._validate(req)
        with self.store.transaction() as state:
            item = state.get("contents", {}).get(source_id)
            if not item:
                raise WorkflowError("内容不存在。", 404)
            if item["version_id"] != req.expected_version:
                raise WorkflowError("主稿已被修改，请刷新后重试。")
            if len(item["history"]) >= 30:
                raise WorkflowError("已达到 30 次版本上限。", 422)
            item["history"].append({"version": item["version"], "version_id": item["version_id"], "content": item["content"]})
            item["version"] += 1
            item["version_id"] = fingerprint({"revision": item["version"], "content": snapshot})
            item["content"] = snapshot
            item["updated_at"] = datetime.now(timezone.utc).isoformat()
            # Platform variants keep their own copy and simply become stale. Existing
            # publish tasks are immutable execution history and are never rewritten.
            return self._public(item)
