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

    def _with_variants(self, item, state):
        row = self._public(item)
        row["variants"] = [{"id": variant["id"], "platform": variant["platform"],
            "target_id": variant["content"].get("target_id", ""), "delivery": variant["content"].get("delivery", "remote"),
            "version": variant["version"], "version_id": variant["version_id"],
            "stale": variant.get("source_version_id") != item["version_id"]}
            for variant in state.get("variants", {}).values() if variant.get("source_id") == item["id"]]
        return row

    def list(self):
        with self.store.transaction(write=False) as state:
            rows = []
            for item in sorted(state.get("contents", {}).values(), key=lambda i: i["updated_at"], reverse=True):
                row = self._with_variants(item, state)
                row.pop("history", None)
                rows.append(row)
            return rows

    def get(self, source_id):
        with self.store.transaction(write=False) as state:
            item = state.get("contents", {}).get(source_id)
            if not item:
                raise WorkflowError("内容不存在。", 404)
            return self._with_variants(item, state)

    def create(self, req: MotherCreate):
        snapshot = self._validate(req)
        with self.store.transaction() as state:
            return self._with_variants(add_mother(state, snapshot, req.idempotency_key), state)

    def revise(self, source_id: str, req: MotherRevision):
        snapshot = self._validate(req)
        with self.store.transaction() as state:
            item = state.get("contents", {}).get(source_id)
            if not item:
                raise WorkflowError("内容不存在。", 404)
            if item["version_id"] != req.expected_version:
                raise WorkflowError("主稿已被修改，请刷新后重试。")
            self._revise_locked(item, snapshot)
            # Platform variants keep their own copy and simply become stale. Existing
            # publish tasks are immutable execution history and are never rewritten.
            return self._with_variants(item, state)

    @staticmethod
    def _revise_locked(item: dict, snapshot: dict) -> None:
        if len(item["history"]) >= 30:
            raise WorkflowError("已达到 30 次版本上限。", 422)
        item["history"].append({"version": item["version"], "version_id": item["version_id"], "content": deepcopy(item["content"])})
        item["version"] += 1
        item["version_id"] = fingerprint({"revision": item["version"], "content": snapshot})
        item["content"] = snapshot
        item["updated_at"] = datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _proposal_public(item: dict) -> dict:
        return {k: deepcopy(v) for k, v in item.items() if k not in {"idempotency_key", "initial_digest"}}

    def propose(self, source_id: str, req: MotherRevision, idempotency_key: str) -> dict:
        """对已有主稿只保存修改建议；应用动作由用户另行发起。"""
        snapshot = self._validate(req)
        with self.store.transaction() as state:
            proposals = state.setdefault("content_proposals", {})
            digest = fingerprint({"source_id": source_id, "base_version_id": req.expected_version, "after": snapshot})
            for row in proposals.values():
                if row["idempotency_key"] == idempotency_key:
                    if row["initial_digest"] != digest:
                        raise WorkflowError("建议创建键已被其他修改使用。", 409)
                    return self._proposal_public(row)
            source = state.get("contents", {}).get(source_id)
            if not source:
                raise WorkflowError("内容不存在。", 404)
            if source["version_id"] != req.expected_version:
                raise WorkflowError("主稿已被修改，请重新读取后提出建议。", 409)
            if len(proposals) >= 500:
                raise WorkflowError("建议记录已达到上限。", 422)
            at = datetime.now(timezone.utc).isoformat()
            row = {"id": uuid.uuid4().hex, "content_id": source_id, "base_version_id": req.expected_version,
                   "before": deepcopy(source["content"]), "after": snapshot, "status": "pending",
                   "idempotency_key": idempotency_key, "initial_digest": digest,
                   "created_at": at, "updated_at": at, "applied_version_id": None, "undone_version_id": None}
            proposals[row["id"]] = row
            return self._proposal_public(row)

    def get_proposal(self, proposal_id: str) -> dict:
        with self.store.transaction(write=False) as state:
            row = state.get("content_proposals", {}).get(proposal_id)
            if not row:
                raise WorkflowError("修改建议不存在。", 404)
            return self._proposal_public(row)

    def list_proposals(self, source_id: str) -> list[dict]:
        with self.store.transaction(write=False) as state:
            if source_id not in state.get("contents", {}):
                raise WorkflowError("内容不存在。", 404)
            rows = [self._proposal_public(row) for row in state.get("content_proposals", {}).values() if row["content_id"] == source_id]
            return sorted(rows, key=lambda row: row["created_at"], reverse=True)

    def apply_proposal(self, proposal_id: str, expected_version: str) -> dict:
        with self.store.transaction() as state:
            row = state.get("content_proposals", {}).get(proposal_id)
            if not row:
                raise WorkflowError("修改建议不存在。", 404)
            if row["status"] == "applied":
                return self._proposal_public(row)
            if row["status"] != "pending":
                raise WorkflowError("建议已被放弃，不能应用。", 409)
            source = state.get("contents", {}).get(row["content_id"])
            if not source:
                raise WorkflowError("内容不存在。", 404)
            if expected_version != row["base_version_id"] or source["version_id"] != expected_version:
                raise WorkflowError("主稿已被修改，请比较当前版本后重新提出建议。", 409)
            self._revise_locked(source, deepcopy(row["after"]))
            row["status"] = "applied"
            row["applied_version_id"] = source["version_id"]
            row["updated_at"] = datetime.now(timezone.utc).isoformat()
            return self._proposal_public(row)

    def dismiss_proposal(self, proposal_id: str) -> dict:
        with self.store.transaction() as state:
            row = state.get("content_proposals", {}).get(proposal_id)
            if not row:
                raise WorkflowError("修改建议不存在。", 404)
            if row["status"] == "applied":
                raise WorkflowError("已应用的建议不能放弃；请使用版本化撤销。", 409)
            if row["status"] == "pending":
                row["status"] = "dismissed"
                row["updated_at"] = datetime.now(timezone.utc).isoformat()
            return self._proposal_public(row)

    def undo_proposal(self, proposal_id: str, expected_version: str) -> dict:
        with self.store.transaction() as state:
            row = state.get("content_proposals", {}).get(proposal_id)
            if not row:
                raise WorkflowError("修改建议不存在。", 404)
            if row["status"] == "undone":
                return self._proposal_public(row)
            if row["status"] != "applied":
                raise WorkflowError("只有已应用的建议可以撤销。", 409)
            source = state.get("contents", {}).get(row["content_id"])
            if not source:
                raise WorkflowError("内容不存在。", 404)
            if expected_version != row["applied_version_id"] or source["version_id"] != expected_version:
                raise WorkflowError("主稿已有新编辑，请先比较当前版本。", 409)
            self._revise_locked(source, deepcopy(row["before"]))
            row["status"] = "undone"
            row["undone_version_id"] = source["version_id"]
            row["updated_at"] = datetime.now(timezone.utc).isoformat()
            return self._proposal_public(row)
