"""人工确认后的逐条发送记录；所有重入只返回原任务，不重放提交。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import uuid
from typing import Any

from .publishing import WorkflowError, fingerprint


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InteractionExecution:
    @staticmethod
    def _execution_status(row: dict) -> str:
        states = [item.get("status") for item in row.get("item_results", [])]
        if not states or any(value in {"submitting", "unknown_result"} for value in states):
            return "unknown_result"
        if all(value == "verified" for value in states):
            return "verified"
        if all(value == "not_submitted" for value in states):
            return "not_submitted"
        return "partial"

    def _settle_execution(self, state: dict, row: dict) -> None:
        row.update(status=self._execution_status(row), result={"results": deepcopy(row.get("item_results", []))}, updated_at=_now())
        account = state.get("accounts", {}).get(row.get("account_id"))
        operation = (account or {}).get("operation") or {}
        if account and operation.get("id") == row.get("operation_id"):
            if row["status"] == "unknown_result":
                operation["state"] = "recovery_required"
            else:
                account["operation"] = None

    def _normalize_item_result(self, row: dict, item: dict, response: dict) -> dict:
        response = response if isinstance(response, dict) else {}
        data = response.get("data") if isinstance(response.get("data"), dict) else {}
        results = data.get("results")
        matches = [value for value in results if isinstance(value, dict) and str(value.get("id") or "") == item["id"]] if isinstance(results, list) else []
        detail = matches[0] if len(matches) == 1 else data if not results and data.get("status") else {}
        status = detail.get("status")
        if status not in {"verified", "not_submitted", "unknown_result"}:
            status = "not_submitted" if response.get("not_submitted") is True or response.get("state") == "not_submitted" else "unknown_result"
        evidence = detail.get("evidence") if isinstance(detail.get("evidence"), dict) else {}
        # 相同文字、输入框内容及无目标绑定的成功标志不能作为回复回执。
        if status == "verified" and row["kind"] in {"reply", "comment"}:
            valid = (evidence.get("kind") in {"platform_receipt", "new_reply"}
                     and evidence.get("target_comment_id") == (item["id"] if row["kind"] == "reply" else "")
                     and evidence.get("target_id") == row["target_id"]
                     and evidence.get("account_remote_id") == row["execution_snapshot"]["account_remote_id"]
                     and bool(evidence.get("account_remote_id")) and bool(evidence.get("reply_id"))
                     and evidence.get("text") == item["reply"])
            if not valid:
                status = "unknown_result"
        return {"id": item["id"], "status": status, "reason": str(detail.get("reason") or response.get("message") or "")[:500],
                "evidence": deepcopy(evidence), "finished_at": _now(),
                "stop_batch": bool(detail.get("stop_batch")) or response.get("state") in {"login_required", "verification_required"}}

    def execute(self, interaction_id: str, confirmed: bool, expected_updated_at: str = "") -> dict[str, Any]:
        if not expected_updated_at:
            raise WorkflowError("缺少草稿版本，请刷新并重新审阅后发送。", 422)
        if not confirmed:
            raise WorkflowError("请先审阅并确认完整回复。", 422)
        with self.store.transaction() as state:
            row = self._migrate(state).get(interaction_id)
            if not row:
                raise WorkflowError("互动任务不存在。", 404)
            if row.get("execution_snapshot") or row.get("status") in {"dispatching", "unknown_result", "verified", "partial", "not_submitted"}:
                return self._project(row)
            if row.get("status") != "draft":
                raise WorkflowError("当前互动任务不能执行。", 409)
            if row.get("delivery") != "remote" or row.get("source_kind") == "import":
                raise WorkflowError("历史本地导入草稿只读保留，不能执行平台写入。", 409)
            if expected_updated_at != row.get("updated_at"):
                raise WorkflowError("草稿已在其他页面修改，请刷新并重新审阅完整回复。", 409)
            account = state.get("accounts", {}).get(row.get("account_id"))
            if not account or account.get("platform") != row.get("platform") or account.get("status") != "connected":
                raise WorkflowError("目标账号当前未连接或平台不匹配，请重新连接。", 409)
            reason = self._connection_reason(account)
            if reason:
                raise WorkflowError(reason, 409)
            reason = self._identity_reason(account)
            if reason:
                raise WorkflowError(reason, 409)
            if (account.get("operation") or {}).get("state") in {"running", "recovery_required"}:
                raise WorkflowError("该账号有执行中或结果待核对的操作，请先处理。", 409)
            payload = self._clean_payload(row["kind"], (row.get("payload") or {}).get("items", []), (row.get("payload") or {}).get("text", ""), platform=row["platform"])
            self._validate_platform_payload(row["platform"], payload)
            entries = payload.get("items") or [{"id": "comment", "reply": payload.get("text", "")}]
            if row["kind"] in {"reply", "delete"} or row.get("retry_of"):
                self._check_reply_duplicates(state, row, entries)
            operation_id = uuid.uuid4().hex
            row.update(status="dispatching", operation_id=operation_id, attempts=1, updated_at=_now(),
                       execution_snapshot={"payload": deepcopy(payload), "expected_updated_at": expected_updated_at,
                                           "account_remote_id": str((account.get("identity") or {}).get("remote_id") or ""),
                                           "account_auth_revision": account.get("auth_revision"), "at": _now()},
                       item_results=[{"id": item["id"], "operation_id": uuid.uuid4().hex, "status": "pending"} for item in entries])
            account["operation"] = {"id": operation_id, "kind": "interaction", "state": "running", "interaction_id": interaction_id, "started_at": _now()}
            work = deepcopy(row)
        for index, item in enumerate(entries):
            with self.store.transaction() as state:
                current = self._migrate(state)[interaction_id]
                record = current["item_results"][index]
                if current["status"] != "dispatching" or record["status"] != "pending":
                    return self._project(current)
                # 此记录落盘后才启动 Worker。崩溃时按未知处理，绝不自动重发。
                record.update(status="submitting", started_at=_now())
                item_operation = record["operation_id"]
            try:
                response = self._send_item(work, item, item_operation)
            except Exception:
                response = {"state": "unknown_result", "message": "执行中断，尚不能确认是否发送，请人工核对。"}
            outcome = self._normalize_item_result(work, item, response)
            with self.store.transaction() as state:
                current = self._migrate(state)[interaction_id]
                current["item_results"][index].update(outcome)
                current["updated_at"] = _now()
                if outcome["status"] == "unknown_result" or outcome["stop_batch"]:
                    for pending in current["item_results"][index + 1:]:
                        if pending["status"] == "pending":
                            pending.update(status="not_submitted", reason="前序条目需要核对或登录验证，本条尚未开始发送。")
                    break
        with self.store.transaction() as state:
            row = self._migrate(state)[interaction_id]
            self._settle_execution(state, row)
            return self._project(row)

    def _reply_conflicts(self, state: dict, row: dict) -> dict[str, dict]:
        conflicts = {}
        rows = self._migrate(state)
        def root_id(record):
            seen = set()
            while record.get('retry_of') in rows and record['id'] not in seen:
                seen.add(record['id'])
                record = rows[record['retry_of']]
            return record['id']
        for other in rows.values():
            if other["id"] == row["id"] or other.get("kind") != row.get("kind") or other.get("platform") != row.get("platform") or other.get("account_id") != row["account_id"] or other.get("target_id") != row["target_id"]:
                continue
            # 顶层评论仅约束同一重试链，独立创建的新评论不互相阻止。
            if row.get('kind') == 'comment' and root_id(other) != root_id(row):
                continue
            if other.get("status") in {"draft", "cancelled"}:
                continue
            outcomes = {item.get("id"): item.get("status") for item in other.get("item_results", [])}
            originals = (other.get("payload") or {}).get("items") or ([{"id": "comment"}] if other.get('kind') == 'comment' else [])
            for original in originals:
                item_id = original.get("id")
                if item_id and outcomes.get(item_id) != "not_submitted":
                    conflicts[item_id] = {"id": item_id, "interaction_id": other["id"], "status": outcomes.get(item_id) or "unknown_result"}
        return conflicts

    def _retry_availability(self, state: dict, row: dict) -> dict:
        candidates = [item["id"] for item in row.get("item_results", []) if item.get("status") == "not_submitted"]
        conflicts = self._reply_conflicts(state, row) if candidates else {}
        return {"retryable_item_ids": [key for key in candidates if key not in conflicts],
                "retry_exclusions": [conflicts[key] for key in candidates if key in conflicts]}

    def _check_reply_duplicates(self, state: dict, row: dict, entries: list[dict]) -> None:
        conflicts = self._reply_conflicts(state, row)
        if any(item["id"] in conflicts for item in entries):
            raise WorkflowError("该评论已有发送记录或待核对结果，不能重新发送。请逐条核对原任务。", 409)

    def _send_item(self, row: dict, item: dict, operation_id: str) -> dict:
        account = self.accounts.get(row["account_id"])
        snapshot = row["execution_snapshot"]
        if (account.get("identity") or {}).get("remote_id", "") != snapshot["account_remote_id"] or account.get("auth_revision") != snapshot["account_auth_revision"]:
            return {"state": "verification_required", "not_submitted": True, "message": "账号身份已变化，本条未发送。"}
        payload = {"items": [item]} if row["kind"] != "comment" else {"text": snapshot["payload"]["text"]}
        payload.update(expected_account_remote_id=snapshot["account_remote_id"], target_id=row["target_id"])
        if row["platform"] == "xiaohongshu":
            _, locator = self.workspace.xhs_ops._locator(row["account_id"], note_id=row["target_id"])
            return self.workspace.xhs_ops.execute_interaction_action(row["account_id"], row["kind"], locator, payload, operation_id)
        return self._x_worker(account, "reply", payload, operation_id=operation_id)

    def refresh_result(self, interaction_id: str) -> dict:
        row = self._get_row(interaction_id)
        # 正在运行的循环负责自己的回执；这里不能把尚在发送的条目标为未发送。
        if row.get("status") != "unknown_result" or not row.get("execution_snapshot"):
            return {"source": "local_ledger", "platform_verified": False, "interaction": self._project(row)}
        entries = row["execution_snapshot"]["payload"].get("items") or [{"id": "comment", "reply": row["execution_snapshot"]["payload"].get("text", "")}]
        for item, record in zip(entries, row.get("item_results", [])):
            if record.get("status") != "unknown_result" or record.get("resolution"):
                continue
            response = self.accounts.read_result(row["account_id"], record["operation_id"])
            if not response:
                continue
            outcome = self._normalize_item_result(row, item, response)
            with self.store.transaction() as state:
                current = self._migrate(state)[interaction_id]
                target = next(value for value in current["item_results"] if value["id"] == item["id"])
                if target.get("status") == "unknown_result" and not target.get("resolution"):
                    target.update(outcome)
                self._settle_execution(state, current)
        with self.store.transaction() as state:
            current = self._migrate(state)[interaction_id]
            if current.get("status") == "unknown_result":
                self._settle_execution(state, current)
            projected = self._project(current)
        return {"source": "local_worker", "platform_verified": False, "interaction": projected,
                "note": "已刷新本地逐条回执；未重新查询平台。"}

    def resolve_unknown(self, interaction_id: str, *, item_id: str = "", result: str, confirmed: bool, expected_updated_at: str = "", note: str = "") -> dict:
        if not confirmed or result not in {"verified", "not_submitted"} or not item_id:
            raise WorkflowError("请指定评论并确认已在平台逐条核对结果。", 422)
        with self.store.transaction() as state:
            row = self._migrate(state).get(interaction_id)
            if not row:
                raise WorkflowError("互动任务不存在。", 404)
            if row.get("status") in {"draft", "cancelled", "dispatching"}:
                raise WorkflowError("当前任务不能记录人工核对。", 409)
            if row.get("updated_at") != expected_updated_at:
                raise WorkflowError("执行记录已更新，请刷新后重新核对。", 409)
            if not row.get("item_results"):
                row["item_results"] = self._project(row)["item_results"]
            target = next((item for item in row["item_results"] if item["id"] == item_id), None)
            if not target or target.get("status") != "unknown_result":
                raise WorkflowError("只有结果待核对的条目可以记录人工核对结果。", 409)
            target.update(status=result, resolution={"source": "manual_platform_check", "result": result, "note": note.strip()[:500], "at": _now()})
            self._settle_execution(state, row)
            return self._project(row)

    def retry_draft(self, interaction_id: str, *, expected_updated_at: str, item_ids: list[str]) -> dict:
        with self.store.transaction() as state:
            rows = self._migrate(state)
            row = rows.get(interaction_id)
            if not row:
                raise WorkflowError("互动任务不存在。", 404)
            if row.get("updated_at") != expected_updated_at:
                raise WorkflowError("执行记录已更新，请刷新后再建立重试草稿。", 409)
            if row.get("status") in {"draft", "dispatching", "cancelled"} or not 1 <= len(item_ids) <= 20 or len(set(item_ids)) != len(item_ids):
                raise WorkflowError("请选择确认未发送的条目。", 422)
            results = {item["id"]: item for item in row.get("item_results", [])}
            if any(results.get(key, {}).get("status") != "not_submitted" for key in item_ids):
                raise WorkflowError("只能为确认未发送的条目建立草稿；未知条目须先逐条核对。", 409)
            # 原记录保存历史事实；重试范围必须参考同账号、同作品的所有后续记录。
            available = set(self._retry_availability(state, row)["retryable_item_ids"])
            item_ids = [key for key in item_ids if key in available]
            if not item_ids:
                raise WorkflowError("所选评论已有发送记录或待核对结果，没有可重试的条目。请刷新历史记录。", 409)
            key = "retry-" + fingerprint({"task": interaction_id, "items": sorted(item_ids)})
            existing = next((item for item in rows.values() if item.get("creation_key") == key and item.get("status") != "cancelled"), None)
            if existing:
                return self._project(existing)
            if len(rows) >= 500:
                raise WorkflowError("互动任务已达到上限，无法建立新草稿。", 422)
            # 从不可变的原任务复制，评论样本被刷新或清理后仍可重试已确认未发送项。
            current = deepcopy(row)
            payload = deepcopy((row.get("execution_snapshot") or {}).get("payload") or row["payload"])
            if row["kind"] != "comment":
                payload["items"] = [item for item in payload.get("items", []) if item.get("id") in item_ids]
            current.update(id=uuid.uuid4().hex, creation_key=key, digest=fingerprint({"key": key, "payload": payload}),
                           payload=payload, status="draft", attempts=0, operation_id=None, result=None, retry_of=interaction_id,
                           created_at=_now(), updated_at=_now())
            for field in ("execution_snapshot", "item_results", "resolution"):
                current.pop(field, None)
            rows[current["id"]] = current
            return self._project(current)
