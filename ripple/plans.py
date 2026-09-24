"""不触发发布的内容计划，以及从未执行的定时任务返回计划。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import uuid

from pydantic import BaseModel, ConfigDict, Field

from .publishing import WorkflowError, normalize_time


class ContentPlanInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    scheduled_local: str = Field(min_length=16, max_length=40)
    timezone: str = Field(min_length=1, max_length=100)
    fold: int | None = Field(default=None, ge=0, le=1)
    source_id: str = Field(default="", max_length=32)
    variant_id: str = Field(default="", max_length=32)


class ContentPlanCreate(ContentPlanInput):
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


class ContentPlanRevision(ContentPlanInput):
    expected_version: int = Field(ge=1)


class ContentPlanService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store

    @staticmethod
    def _public(value: dict) -> dict:
        return {key: deepcopy(item) for key, item in value.items() if key != "idempotency_key"}

    @staticmethod
    def _validate_refs(state: dict, source_id: str, variant_id: str) -> tuple[str, str]:
        if variant_id:
            variant = state.get("variants", {}).get(variant_id)
            if not variant:
                raise WorkflowError("平台版本不存在。", 404)
            if source_id and variant["source_id"] != source_id:
                raise WorkflowError("平台版本不属于所选主稿。", 422)
            source_id = variant["source_id"]
            variant_version_id = variant["version_id"]
        else:
            variant_version_id = ""
        if source_id and source_id not in state.get("contents", {}):
            raise WorkflowError("主稿不存在。", 404)
        return source_id, variant_version_id

    def list(self) -> list[dict]:
        with self.store.transaction(write=False) as state:
            return [self._public(value) for value in sorted(state.get("content_plans", {}).values(), key=lambda row: row["scheduled_at"])]

    def get(self, plan_id: str) -> dict:
        with self.store.transaction(write=False) as state:
            plan = state.get("content_plans", {}).get(plan_id)
            if not plan:
                raise WorkflowError("内容计划不存在。", 404)
            return self._public(plan)

    def create(self, req: ContentPlanCreate) -> dict:
        scheduled_at = normalize_time(req.scheduled_local, req.timezone, req.fold)
        with self.store.transaction() as state:
            source_id, variant_version_id = self._validate_refs(state, req.source_id, req.variant_id)
            rows = state.setdefault("content_plans", {})
            for existing in rows.values():
                if existing["idempotency_key"] == req.idempotency_key:
                    same = (existing["title"] == req.title and existing["scheduled_at"] == scheduled_at
                            and existing["source_id"] == source_id and existing["variant_id"] == req.variant_id
                            and existing["variant_version_id"] == variant_version_id)
                    if not same:
                        raise WorkflowError("计划创建键已被不同内容使用。", 409)
                    return self._public(existing)
            if len(rows) >= 500:
                raise WorkflowError("内容计划已达到上限。", 422)
            at = datetime.now(timezone.utc).isoformat()
            plan = {"id": uuid.uuid4().hex, "version": 1, "title": req.title,
                    "scheduled_local": req.scheduled_local, "timezone": req.timezone, "fold": req.fold,
                    "scheduled_at": scheduled_at, "source_id": source_id, "variant_id": req.variant_id,
                    "variant_version_id": variant_version_id, "task_id": "", "status": "planned",
                    "idempotency_key": req.idempotency_key, "created_at": at, "updated_at": at}
            rows[plan["id"]] = plan
            return self._public(plan)

    def revise(self, plan_id: str, req: ContentPlanRevision) -> dict:
        scheduled_at = normalize_time(req.scheduled_local, req.timezone, req.fold)
        with self.store.transaction() as state:
            plan = state.get("content_plans", {}).get(plan_id)
            if not plan:
                raise WorkflowError("内容计划不存在。", 404)
            if plan["version"] != req.expected_version:
                raise WorkflowError("计划已更新，请刷新后重试。", 409)
            linked_task = state.get("tasks", {}).get(plan.get("task_id")) if plan.get("task_id") else None
            if linked_task and linked_task["status"] != "cancelled":
                raise WorkflowError("该计划已有发布任务。请先取消任务，再修改计划。", 409)
            source_id, variant_version_id = self._validate_refs(state, req.source_id, req.variant_id)
            plan.update(title=req.title, scheduled_local=req.scheduled_local, scheduled_at=scheduled_at,
                        timezone=req.timezone, fold=req.fold, source_id=source_id,
                        variant_id=req.variant_id, variant_version_id=variant_version_id,
                        task_id="",
                        version=plan["version"] + 1, updated_at=datetime.now(timezone.utc).isoformat())
            return self._public(plan)

    def return_to_plan(self, task_id: str, expected_version: str) -> dict:
        """单次事务保留计划与任务历史；执行意图已生成时拒绝撤回。"""
        with self.store.transaction() as state:
            task = state.get("tasks", {}).get(task_id)
            if not task:
                raise WorkflowError("发布任务不存在。", 404)
            if task["version_id"] != expected_version:
                raise WorkflowError("任务已更新，请刷新后重试。", 409)
            rows = state.setdefault("content_plans", {})
            if task["status"] == "cancelled" and task.get("plan_id") in rows:
                return {"task": deepcopy(task), "plan": self._public(rows[task["plan_id"]])}
            if task["status"] != "scheduled" or task.get("attempts") or task.get("operation_id"):
                raise WorkflowError("任务正在执行或结果待核对，不能直接撤回。", 409)
            at = datetime.now(timezone.utc).isoformat()
            plan = rows.get(task.get("plan_id"))
            if not plan:
                plan = {"id": uuid.uuid4().hex, "version": 1,
                        "title": task["content"]["title"], "scheduled_local": task["content"].get("scheduled_local") or "",
                        "timezone": task["content"].get("timezone") or "Asia/Shanghai",
                        "fold": task["content"].get("fold"), "scheduled_at": task["content"].get("scheduled_at"),
                        "source_id": task["content"].get("source_id") or "", "variant_id": task.get("variant_id") or "",
                        "variant_version_id": task.get("variant_version_id") or "", "task_id": "", "status": "planned",
                        "idempotency_key": f"return-{task_id}", "created_at": at, "updated_at": at}
                rows[plan["id"]] = plan
                task["plan_id"] = plan["id"]
            else:
                plan.update(task_id="", status="planned", scheduled_local=task["content"].get("scheduled_local") or plan["scheduled_local"],
                            scheduled_at=task["content"].get("scheduled_at"), version=plan["version"] + 1, updated_at=at)
            task["approval"] = None
            self.workspace._event(task, "cancelled", "定时已取消；创作计划与日期仍保留。")
            return {"task": deepcopy(task), "plan": self._public(plan)}

    def calendar_entries(self) -> list[dict]:
        tasks = self.workspace.calendar()
        with self.store.transaction(write=False) as state:
            plans = []
            pending_task_ids: set[str] = set()
            for row in state.get("content_plans", {}).values():
                task = state.get("tasks", {}).get(row.get("task_id"))
                if task and task["status"] not in {"draft", "review_ready", "cancelled"}:
                    continue
                if task and task["status"] in {"draft", "review_ready"}:
                    pending_task_ids.add(task["id"])
                plans.append({"kind": "plan", **self._public(row)})
        return [*plans, *({"kind": "publish_task", **task} for task in tasks if task["id"] not in pending_task_ids)]
