"""Version-bound publishing primitives and local adapters.

WorkspaceService adds explicitly authorized native accounts. This base remains
network-free for simulation/export and deterministic regression tests.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Literal
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import zipfile

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .store import JsonStore
from .tenancy import DEFAULT_WORKSPACE_ID

UTC = timezone.utc
PLATFORMS = {
    "x": "X", "xiaohongshu": "小红书", "wechat": "微信公众号",
    "weixin-channels": "微信视频号", "zhihu": "知乎", "bilibili": "Bilibili",
    "blog": "个人 Blog", "douyin": "抖音", "tiktok": "TikTok", "kuaishou": "快手",
}
TERMINAL = {"simulated", "exported", "cancelled", "failed_terminal"}
EDITABLE = {"draft", "review_ready", "approved", "scheduled", "failed_retryable", "verification_required"}
EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp4", ".mov", ".webm"}
MAX_MEDIA_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 25 * 1024 * 1024
MAX_TASKS = 500


class WorkflowError(ValueError):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class ContentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    project_id: str = Field(default="local", min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=100_000)
    platform: str = "blog"
    account_id: str = Field(default="local", min_length=1, max_length=100)
    mode: Literal["simulation", "blog", "real"] = "simulation"
    media: list[str] = Field(default_factory=list, max_length=12)
    scheduled_local: str | None = Field(default=None, max_length=40)
    timezone: str = Field(default="Asia/Shanghai", min_length=1, max_length=80)
    fold: Literal[0, 1] | None = None
    simulation_result: Literal["success", "retryable", "unknown", "verification", "accepted"] = "success"
    source_id: str = Field(default="", max_length=32, pattern=r"^([a-f0-9]{32})?$")
    source_version_id: str = Field(default="", max_length=64, pattern=r"^([a-f0-9]{64})?$")
    tags: str = Field(default="", max_length=1000)
    options: dict = Field(default_factory=dict)

    @field_validator("title")
    @classmethod
    def nonempty_title(cls, value):
        if not value.strip():
            raise ValueError("标题不能为空")
        return value

    @field_validator("platform")
    @classmethod
    def known_platform(cls, value):
        if value not in PLATFORMS:
            raise ValueError("未知渠道")
        return value

    @model_validator(mode="after")
    def valid_mode(self):
        if self.mode == "blog" and self.platform != "blog":
            raise ValueError("Blog 导出只能使用个人 Blog 渠道")
        if self.mode != "real" and self.account_id != "local":
            raise ValueError("本地模拟与导出的账号必须为 local，不使用社交平台账号")
        return self


class CreateInput(ContentInput):
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


class RevisionInput(ContentInput):
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")


class VersionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")


class ApprovalInput(VersionAction):
    confirmed: bool = False
    real_publish_confirmed: bool = False
    expected_account_revision: int | None = Field(default=None, ge=0)
    external_service_confirmed: bool = False


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def utc_now() -> datetime:
    return datetime.now(UTC)


def normalize_time(local: str | None, zone_name: str, fold: int | None) -> str | None:
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise WorkflowError("无效的 IANA 时区。", 422) from None
    if not local:
        return None
    try:
        naive = datetime.fromisoformat(local)
    except ValueError:
        raise WorkflowError("排期时间格式无效。", 422) from None
    if naive.tzinfo is not None:
        raise WorkflowError("请输入不带偏移的本地时间，并单独指定 IANA 时区。", 422)
    candidates = []
    for f in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=f)
        utc = aware.astimezone(UTC)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            candidates.append((f, utc))
    if not candidates:
        raise WorkflowError("该本地时间落在夏令时跳过区间，请选择其他时间。", 422)
    unique = {utc for _, utc in candidates}
    if len(unique) > 1 and fold is None:
        raise WorkflowError("该时间在夏令时切换时出现两次，请指定第一次或第二次。", 422)
    selected = next((utc for f, utc in candidates if f == (fold or 0)), candidates[0][1])
    return selected.isoformat()


def capabilities() -> list[dict]:
    return [{
        "id": key, "name": label, "connected": False, "direct_publish": False,
        "status": "export_only" if key == "blog" else "not_connected",
        "text": key == "blog", "images": key == "blog", "video": key == "blog",
        "draft": False, "native_schedule": False, "remote_status": False,
        "local_simulation": True, "local_export": key == "blog",
        "reason": "仅生成本地 Markdown / 素材包，未连接 CMS。" if key == "blog" else "真实适配器尚未启用；需核验接口并单独授权。",
    } for key, label in PLATFORMS.items()]


class PublishingService:
    def __init__(self, outputs: Path, *, clock=utc_now):
        self.outputs = outputs.resolve()
        self.store = JsonStore(self.outputs / "_ripple")
        self.clock = clock

    def _media_bytes(self, rel: str) -> bytes:
        p = PurePosixPath(rel)
        if not rel or "\\" in rel or ":" in rel or p.is_absolute() or len(p.parts) < 2:
            raise WorkflowError("素材必须是内容库内的项目相对路径。", 422)
        if any(part.startswith((".", "_")) or part == ".." for part in p.parts):
            raise WorkflowError("禁止读取隐藏文件、系统数据或凭据目录。", 422)
        if p.suffix.lower() not in EXTENSIONS:
            raise WorkflowError("仅支持 PNG/JPEG/WebP/GIF/MP4/MOV/WebM 素材。", 422)
        path = self.outputs.joinpath(*p.parts)
        current = self.outputs
        for part in p.parts:
            current /= part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise WorkflowError("素材不能通过符号链接或目录联接访问。", 422)
        resolved = path.resolve()
        if self.outputs not in resolved.parents or not resolved.is_file():
            raise WorkflowError("素材不存在或路径越界。", 422)
        with resolved.open("rb") as stream:
            data = stream.read(MAX_MEDIA_BYTES + 1)
        if not data or len(data) > MAX_MEDIA_BYTES:
            raise WorkflowError("每个素材必须非空且不超过 10 MiB。", 422)
        suffix = p.suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
            from PIL import Image
            try:
                with Image.open(io.BytesIO(data)) as image:
                    expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".gif": "GIF"}
                    if image.format != expected[suffix]:
                        raise ValueError("format mismatch")
                    image.verify()
            except Exception:
                raise WorkflowError("图片数据无效或扩展名不匹配。", 422) from None
        elif suffix in {".mp4", ".mov"} and b"ftyp" not in data[:32]:
            raise WorkflowError("视频容器与扩展名不匹配。", 422)
        elif suffix == ".webm" and not data.startswith(b"\x1a\x45\xdf\xa3"):
            raise WorkflowError("WebM 容器无效。", 422)
        return data

    def _snapshot(self, req: ContentInput) -> dict:
        value = ContentInput.model_validate(req.model_dump(include=set(ContentInput.model_fields))).model_dump()
        value["scheduled_at"] = normalize_time(req.scheduled_local, req.timezone, req.fold)
        media = []
        total = 0
        for rel in req.media:
            data = self._media_bytes(rel)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise WorkflowError("素材总量不能超过 25 MiB。", 422)
            media.append({"path": rel, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        value["media_snapshot"] = media
        return value

    def _task(self, state: dict, task_id: str, version: str | None = None) -> dict:
        task = state["tasks"].get(task_id)
        if task is None:
            raise WorkflowError("任务不存在。", 404)
        if version is not None and task["version_id"] != version:
            raise WorkflowError("内容版本已变化，请刷新后重新审核。")
        return task

    def _event(self, task: dict, state: str, note: str):
        task["status"] = state
        task["updated_at"] = self.clock().isoformat()
        task["events"].append({"at": task["updated_at"], "status": state, "note": note,
                               "version_id": task["version_id"], "attempt": task["attempts"]})
        task["events"] = task["events"][-100:]

    def list(self, limit: int = 200, offset: int = 0) -> dict:
        with self.store.transaction(write=False) as state:
            values = sorted(state["tasks"].values(), key=lambda t: t["created_at"], reverse=True)
            return {"items": [{k: deepcopy(v) for k, v in item.items() if k != "history"} for item in values[offset:offset + limit]],
                    "total": len(values), "counts": dict(Counter(item["status"] for item in values))}

    def get(self, task_id: str) -> dict:
        with self.store.transaction(write=False) as state:
            return deepcopy(self._task(state, task_id))

    def create(self, req: CreateInput) -> dict:
        snapshot = self._snapshot(req)
        initial = fingerprint(snapshot)
        with self.store.transaction() as state:
            for task in state["tasks"].values():
                if task["idempotency_key"] == req.idempotency_key:
                    if task["initial_digest"] != initial:
                        raise WorkflowError("幂等键已被其他内容使用。")
                    return deepcopy(task)
            if len(state["tasks"]) >= MAX_TASKS:
                raise WorkflowError("首版最多保存 500 个任务，请先备份归档。", 422)
            now = self.clock().isoformat()
            task = {"id": uuid.uuid4().hex, "workspace_id": DEFAULT_WORKSPACE_ID, "version": 1, "version_id": initial,
                    "content": snapshot, "history": [], "idempotency_key": req.idempotency_key,
                    "initial_digest": initial, "approval": None, "attempts": 0,
                    "receipt": None, "created_at": now, "updated_at": now, "events": []}
            self._event(task, "draft", "草稿已保存；尚未审核。")
            state["tasks"][task["id"]] = task
            return deepcopy(task)

    def revise(self, task_id: str, req: RevisionInput) -> dict:
        snapshot = self._snapshot(req)
        with self.store.transaction() as state:
            task = self._task(state, task_id, req.expected_version)
            if task["status"] not in EDITABLE:
                raise WorkflowError("已提交或结果未知的任务不能修改；请先核对回执。")
            if len(task["history"]) >= 30:
                raise WorkflowError("该任务已达到 30 次修订上限，请新建任务。", 422)
            task["history"].append({"version": task["version"], "version_id": task["version_id"], "content": task["content"],
                                    "approval": task["approval"], "attempts": task["attempts"], "receipt": task["receipt"]})
            task.update(content=snapshot, version=task["version"] + 1,
                        version_id=fingerprint({"revision": task["version"] + 1, "content": snapshot}),
                        approval=None, receipt=None, attempts=0, retry_after=0)
            self._event(task, "draft", "内容或目标已修改，原审批失效。")
            return deepcopy(task)

    def _problems(self, task: dict, state: dict | None = None) -> list[str]:
        c = task["content"]
        problems = []
        if c["mode"] == "real":
            problems.append("真实平台发布未启用，尚未进行账号授权与适配器验收。")
        if c["scheduled_at"] and datetime.fromisoformat(c["scheduled_at"]) <= self.clock():
            problems.append("排期已过期，请调整后重新审核。")
        for media in c["media_snapshot"]:
            try:
                if hashlib.sha256(self._media_bytes(media["path"])).hexdigest() != media["sha256"]:
                    problems.append("素材已变化，请保存新版本并重新审核。")
            except (WorkflowError, OSError) as exc:
                problems.append(str(exc) if isinstance(exc, WorkflowError) else "素材不可读取。")
        return problems

    def preflight(self, task_id: str, version: str) -> dict:
        with self.store.transaction() as state:
            task = self._task(state, task_id, version)
            problems = self._problems(task, state)
            if not problems and task["status"] == "draft":
                self._event(task, "review_ready", "预检通过，可进行人工审核。")
            return {"ok": not problems, "problems": problems, "task": deepcopy(task),
                    "notice": "审核后仍需明确执行；平台提交与公开可见状态分开记录。"}

    def approve(self, task_id: str, req: ApprovalInput) -> dict:
        if not req.confirmed:
            raise WorkflowError("需要明确确认当前内容、目标及排期。", 422)
        with self.store.transaction() as state:
            task = self._task(state, task_id, req.expected_version)
            if task["status"] not in {"review_ready", "approved", "scheduled"}:
                raise WorkflowError("请先对当前版本执行预检。")
            problems = self._problems(task, state)
            if problems:
                raise WorkflowError("；".join(problems), 422)
            task["approval"] = {"version_id": task["version_id"], "at": self.clock().isoformat(), "actor": "local-user"}
            self._event(task, "scheduled" if task["content"]["scheduled_at"] else "approved", "已确认当前不可变版本、目标和排期。")
            return deepcopy(task)

    def dispatch(self, task_id: str, version: str) -> dict:
        # Persist dispatch intent before doing any adapter work. Duplicate calls
        # observe this same intent and never issue a second submission.
        with self.store.transaction() as state:
            task = self._task(state, task_id, version)
            if task["status"] in {"simulated", "exported", "dispatching", "accepted"}:
                return deepcopy(task)
            if task["status"] not in {"approved", "scheduled", "failed_retryable"}:
                raise WorkflowError("任务当前不可执行；未知结果必须先核对，不能直接重发。")
            if not task["approval"] or task["approval"]["version_id"] != task["version_id"]:
                raise WorkflowError("当前版本没有有效审批。")
            c = task["content"]
            if c["mode"] not in {"simulation", "blog"}:
                raise WorkflowError("真实发布保持关闭。", 403)
            if c["scheduled_at"] and datetime.fromisoformat(c["scheduled_at"]) > self.clock():
                raise WorkflowError("尚未到计划执行时间。")
            if task["status"] == "failed_retryable":
                if task["attempts"] >= 3:
                    raise WorkflowError("已达到最多 3 次尝试。")
                if self.clock().timestamp() < task.get("retry_after", 0):
                    raise WorkflowError("重试冷却中，请稍后操作。", 429)
            # Validate media again, but do not reject a due schedule as expired.
            check = deepcopy(task)
            check["content"]["scheduled_at"] = None
            if self._problems(check, state):
                task["approval"] = None
                self._event(task, "draft", "素材校验失败，审批已失效；请保存新版本。")
                return deepcopy(task)
            task["attempts"] += 1
            self._event(task, "dispatching", "本地适配器执行意图已持久化。")
            work = deepcopy(task)
        try:
            if work["content"]["mode"] == "blog":
                receipt = self._export(work)
                result = "exported"
            else:
                scenario = work["content"]["simulation_result"]
                result = {"success": "simulated", "retryable": "failed_retryable", "unknown": "unknown_result",
                          "verification": "verification_required", "accepted": "accepted"}[scenario]
                if scenario == "retryable" and work["attempts"] > 1:
                    result = "simulated"
                receipt = {"adapter": "simulation", "simulated": True, "remote_id": None,
                           "local_receipt_id": "sim-" + task_id, "public_url": None, "result": result}
        except Exception:
            # Do not expose exception text (paths, credential-bearing URLs, etc.).
            result, receipt = "unknown_result", None
        with self.store.transaction() as state:
            task = self._task(state, task_id, version)
            task["receipt"] = receipt
            if result == "failed_retryable":
                task["retry_after"] = self.clock().timestamp() + 5
            self._event(task, result, {"simulated": "模拟完成，未向任何社交平台发送。", "exported": "本地导出完成，未上传至 Blog。",
                "unknown_result": "结果未知：禁止盲目重试，先核对本地回执。", "failed_retryable": "模拟明确的未提交失败，可在冷却后手动重试。",
                "verification_required": "模拟需要人工核对；不会绕过验证。", "accepted": "模拟已受理，尚未确认完成。"}[result])
            return deepcopy(task)

    def query(self, task_id: str, version: str) -> dict:
        with self.store.transaction() as state:
            task = self._task(state, task_id, version)
            if task["status"] in {"unknown_result", "accepted"}:
                c = task["content"]
                if c["mode"] == "simulation" and task["receipt"]:
                    self._event(task, "simulated", "已核对模拟适配器回执；未执行重发。")
                    task["receipt"]["result"] = "simulated"
                elif c["mode"] == "blog":
                    path = self.export_path(task)
                    if path.is_file():
                        try:
                            with zipfile.ZipFile(path) as archive:
                                metadata = json.loads(archive.read("manifest.json"))
                            if metadata["task_id"] == task_id and metadata["version_id"] == version:
                                task["receipt"] = self._export_receipt(task)
                                self._event(task, "exported", "已核对本地导出包，未重复执行。")
                        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                            pass
            return deepcopy(task)

    def cancel(self, task_id: str, version: str) -> dict:
        with self.store.transaction() as state:
            task = self._task(state, task_id, version)
            if task["status"] == "cancelled":
                return deepcopy(task)
            if task["status"] not in EDITABLE:
                raise WorkflowError("提交中、已受理或未知结果不能假定撤回成功。")
            task["approval"] = None
            self._event(task, "cancelled", "本地任务已取消。")
            return deepcopy(task)

    def recover(self) -> int:
        changed = 0
        with self.store.transaction() as state:
            for task in state["tasks"].values():
                if task["status"] == "dispatching":
                    self._event(task, "unknown_result", "进程中断留下执行意图，请核对回执；不会自动重发。")
                    changed += 1
                elif task["status"] == "scheduled" and datetime.fromisoformat(task["content"]["scheduled_at"]) <= self.clock():
                    task["approval"] = None
                    self._event(task, "verification_required", "重启后排期已过期，请调整时间并重新审批；未补发。")
                    changed += 1
        return changed

    def tick(self):
        with self.store.transaction() as state:
            due = []
            for task in state["tasks"].values():
                if task["status"] != "scheduled":
                    continue
                delay = (self.clock() - datetime.fromisoformat(task["content"]["scheduled_at"])).total_seconds()
                if delay > 300:
                    task["approval"] = None
                    self._event(task, "verification_required", "已错过 5 分钟执行窗口，暂停任务并等待重新审核。")
                elif delay >= 0:
                    due.append((task["id"], task["version_id"]))
        for task_id, version in due[:10]:
            try:
                self.dispatch(task_id, version)
            except WorkflowError:
                continue

    def export_path(self, task: dict) -> Path:
        return self.store.directory / "exports" / (task["id"] + "-" + task["version_id"] + ".zip")

    def _export_receipt(self, task: dict) -> dict:
        return {"adapter": "blog-markdown", "simulated": False, "public_url": None,
                "artifact_url": f'/api/ripple/tasks/{task["id"]}/export', "result": "exported"}

    def _export(self, task: dict) -> dict:
        c = task["content"]
        body = c["body"]
        media = []
        for index, item in enumerate(c["media_snapshot"], 1):
            data = self._media_bytes(item["path"])
            if hashlib.sha256(data).hexdigest() != item["sha256"]:
                raise WorkflowError("素材在执行前已变化。")
            name = f'media/{index:02d}{PurePosixPath(item["path"]).suffix.lower()}'
            body = body.replace(item["path"], name)
            media.append((name, data))
        frontmatter = "---\ntitle: " + json.dumps(c["title"], ensure_ascii=False) + "\ndraft: true\n---\n\n"
        metadata = {"task_id": task["id"], "version_id": task["version_id"], "export_only": True,
                    "timezone": c["timezone"], "media": [name for name, _ in media]}
        path = self.export_path(task)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        os.close(fd)
        try:
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("post.md", frontmatter + body)
                archive.writestr("manifest.json", json.dumps(metadata, ensure_ascii=False, indent=2))
                for name, data in media:
                    archive.writestr(name, data)
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return self._export_receipt(task)
