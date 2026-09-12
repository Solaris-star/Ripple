"""Integrated content, account and publishing application service.

Live subprocesses are created only after explicit version-bound approval. A
publisher's successful UI signal is 'accepted', never proof of public visibility.
"""
from __future__ import annotations

from copy import deepcopy
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import threading
import time
from urllib.parse import urlsplit
import uuid

from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field
from .accounts import AccountService
from .bridge import BridgeService
from .rest_bridge import RestBridgeService, ADAPTER as REST_ADAPTER
from .x_adapter import XService, ADAPTER as X_ADAPTER
from .wechat_adapter import WeChatService, ADAPTER as WECHAT_ADAPTER
from .catalog import CONNECTION_METHODS, NATIVE, NAMES, RECEIPT_HOSTS, X_BROWSER_ADAPTER
from .library import ContentLibrary, add_mother, mother_snapshot
from .variants import VariantService
from .blog_connector import BlogConnectorService
from .execution_nodes import LOCAL_NODE_ID, ExecutionNodeService
from .auth import AuthService
from .publishing import (
    ApprovalInput, ContentInput, CreateInput, PublishingService, RevisionInput,
    WorkflowError, EDITABLE, EXTENSIONS, MAX_TASKS, fingerprint, normalize_time,
)
from .watermarks import SUPPORTED_IMAGE_EXTENSIONS, WatermarkService
from .xhs_ops import XhsOpsService
from .interactions import InteractionService
from .operations import OperationService

MAX_VIDEO_BYTES = 512 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_BATCH_BYTES = 1024 * 1024 * 1024
ACTIVE = {"running", "recovery_required"}


class ReceiptInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    confirmed: bool = False
    public_url: str = Field(min_length=8, max_length=2048)
    note: str = Field(default="", max_length=300)


class WorkspaceService(PublishingService):
    def __init__(self, outputs: Path, *, private: Path | None = None, clock=None):
        super().__init__(outputs, **({"clock": clock} if clock else {}))
        # Outside outputs: legacy file/media endpoints cannot serve credentials.
        self.private = (private or (self.outputs.parent / ".ripple-private" / self.outputs.name)).resolve()
        self.auth = AuthService(self.store)
        self.execution_nodes = ExecutionNodeService(self.store, self.private)
        self.accounts = AccountService(self.store, self.private, self.execution_nodes)
        self.bridge = BridgeService(self.private)
        self.rest = RestBridgeService(self)
        self.x = XService(self)
        self.wechat = WeChatService(self)
        self.library = ContentLibrary(self)
        self.variants = VariantService(self)
        self.blogs = BlogConnectorService(self)
        self.watermarks = WatermarkService()
        self.xhs_ops = XhsOpsService(self)
        self.interactions = InteractionService(self)
        self.operations = OperationService(self)
        self._workers: dict[str, threading.Thread] = {}
        self._guard = threading.Lock()
        self._closed = False

    def list(self, limit=200, offset=0):
        with self.store.transaction(write=False) as state:
            values = sorted(state['tasks'].values(), key=lambda t: t['created_at'], reverse=True)
            return {'items': [{k: deepcopy(v) for k, v in t.items() if k != 'history'} for t in values[offset:offset + limit]],
                    'total': len(values), 'counts': dict(Counter(t['status'] for t in values)),
                    'real_counts': dict(Counter(t['status'] for t in values if t['content']['mode'] == 'real'))}

    def channels(self):
        accounts = self.accounts.list()
        env = self.accounts.environment
        rows = []
        blog_targets = self.blogs.list()
        for key, name in NAMES.items():
            method_options = deepcopy(CONNECTION_METHODS.get(key, []))
            if key == "blog":
                connected_blogs = [target for target in blog_targets if target.get("status") == "connected"]
                rows.append({"id": key, "name": name, "connected": bool(connected_blogs),
                    "direct_publish": bool(connected_blogs), "adapter_available": True, "adapter": "blog-connector",
                    "status": "connected" if connected_blogs else "export_only", "formats": ["markdown", "media"],
                    "native_schedule": False, "local_schedule": True, "remote_status": False, "local_export": True,
                    "local_simulation": True, "account_count": len(blog_targets), "target_count": len(blog_targets),
                    "environment_ready": True, "live_verified": False,
                    "reason": "可连接兼容 Blog OpenAPI 进行文章更新，也可保留 Markdown / 素材导出。",
                    "scope_note": "Blog 连接由 OpenAPI 能力声明决定；Token 只保存在本机私有配置。"})
                rows[-1]["connection_methods"] = [item["id"] for item in method_options]
                rows[-1]["connection_options"] = method_options
                continue
            if key == "wechat":
                linked = [a for a in accounts if a["platform"] == "wechat" and a.get("adapter") == WECHAT_ADAPTER]
                connected = [a for a in linked if a.get("status") == "connected"]
                freepublish = [a for a in connected if "freepublish" in set(a.get("capabilities") or [])]
                rows.append({"id": key, "name": name, "connected": bool(connected),
                    "direct_publish": bool(freepublish), "adapter_available": True, "adapter": WECHAT_ADAPTER,
                    "connection_methods": [item["id"] for item in method_options], "connection_options": method_options,
                    "status": "connected" if connected else "not_connected", "formats": ["text", "images"],
                    "native_schedule": False, "local_schedule": True, "remote_status": True, "local_export": False,
                    "local_simulation": True, "account_count": len(linked), "environment_ready": True,
                    "live_verified": any(a.get("live_verified") for a in linked),
                    "reason": "使用微信公众号官方 API 写入草稿箱；账号具备 freepublish 权限时可提交发布。",
                    "scope_note": "AppSecret 加密保存在 Ripple 私有目录；服务器出口 IP 需加入公众号 IP 白名单。"})
                continue
            native = NATIVE.get(key)
            linked = [a for a in accounts if a["platform"] == key]
            connected = [a for a in linked if a["status"] == "connected"]
            is_x = key == "x"
            ready = bool(env["biliup"] if key == "bilibili" else env["browser"])
            if is_x:
                api_accounts = [a for a in linked if a.get("adapter") == X_ADAPTER]
                browser_accounts = [a for a in linked if a.get("adapter") == X_BROWSER_ADAPTER]
                api_connected = [a for a in api_accounts if a["status"] == "connected"]
                browser_connected = [a for a in browser_accounts if a["status"] == "connected"]
                x_configured = self.x.status()["configured"]
                connected_x = [*api_connected, *browser_connected]
                rows.append({"id": key, "name": name, "connected": bool(connected_x),
                    "direct_publish": bool((api_connected and x_configured) or (browser_connected and env["browser"])),
                    "adapter_available": True, "adapter": "x-multi",
                    "connection_methods": [item["id"] for item in method_options], "connection_options": method_options,
                    "status": "connected" if connected_x else "not_connected",
                    "formats": ["text", "images"], "native_schedule": False, "local_schedule": True,
                    "remote_status": False, "local_export": False, "local_simulation": True,
                    "account_count": len(linked), "environment_ready": bool(env["browser"] or x_configured),
                    "live_verified": any(a.get("live_verified") for a in [*api_accounts, *browser_accounts]),
                    "reason": "可选浏览器登录（无需 Developer API，实验性）或 X 官方 API。",
                    "scope_note": "浏览器模式使用独立 Edge/Chromium Profile，不复制 auth_token/ct0；非官方网页自动化存在账号限制风险。官方 API 模式的权限和费用取决于 X Developer 计划。"})
                continue
            rows.append({"id": key, "name": name, "connected": bool(connected),
                "direct_publish": bool(native and connected and ready), "adapter_available": bool(native),
                "adapter": "biliup" if key == "bilibili" else "browser" if native else "export" if key == "blog" else "unavailable",
                "status": "connected" if connected else "not_connected" if native else "export_only" if key == "blog" else "not_available",
                "formats": native["formats"] if native else ["markdown", "media"] if key == "blog" else [],
                "native_schedule": False, "local_schedule": bool(native or key == "blog"), "remote_status": False,
                "local_export": key == "blog", "local_simulation": True, "account_count": len(linked),
                "environment_ready": ready if native else True, "live_verified": any(a.get("live_verified") for a in linked),
                "reason": "可创建独立账号并扫码登录；发布前需审核具体内容。" if native else "通用 Markdown 与素材导出；没有连接 CMS。" if key == "blog" else "该渠道的 Ripple 本地适配器尚未接入。",
                "scope_note": "能力范围来自本地适配器；账号权限与网页兼容性需实际登录后核验。"})
            rows[-1]["connection_methods"] = [item["id"] for item in method_options]
            rows[-1]["connection_options"] = method_options
        for row in rows:
            remote = [a for a in accounts if a['platform'] == row['id'] and a.get('adapter') == REST_ADAPTER]
            if remote:
                row['legacy_remote_accounts'] = len(remote)
                # Preserve old local data without making the relay a new-connection product path.
                if row['id'] != 'x' and any(a['status'] == 'connected' for a in remote):
                    row['connected'] = True
        return rows

    def account_action(self, account_id, kind, req):
        account = self.accounts.get(account_id)
        if account.get('status') == 'deleting':
            raise WorkflowError('账号正在删除，请完成删除后重新连接。', 409)
        if account.get('adapter') == X_ADAPTER:
            if kind == 'login':
                return self.x.reconnect(account_id, req.confirmed)
            return self.x.probe(account_id, req.confirmed)
        if account.get('adapter') == WECHAT_ADAPTER:
            if kind == 'login':
                raise WorkflowError('微信公众号使用 AppID/AppSecret 连接；请点击“重新连接”并重新填写凭据。', 422)
            return self.wechat.probe(account_id, req.confirmed)
        if account.get('adapter') == REST_ADAPTER:
            if kind == 'login':
                raise WorkflowError('这是旧版外部渠道账号；请改用 Ripple Native X 或在原服务端维护授权。', 422)
            return self.rest.refresh_account(account_id, req.confirmed)
        return self.accounts.start(account_id, kind, req)

    def disconnect_account(self, account_id, confirmed):
        account = self.accounts.get(account_id)
        if account.get('status') == 'deleting':
            raise WorkflowError('账号正在删除，请再次执行删除清理。', 409)
        if account.get('adapter') == X_ADAPTER:
            return self.x.disconnect(account_id, confirmed)
        if account.get('adapter') == WECHAT_ADAPTER:
            return self.wechat.disconnect(account_id, confirmed)
        return self.accounts.disconnect(account_id, confirmed)

    def delete_account(self, account_id, confirmed):
        account = self.accounts.get(account_id)
        result = self.accounts.delete(account_id, confirmed)
        if account.get('adapter') == X_ADAPTER:
            result['pending_oauth_removed'] = self.x.purge_pending(account_id)
        if account.get('adapter') == WECHAT_ADAPTER:
            self.wechat.purge(account_id)
        return result

    def safe_media_path(self, rel: str) -> Path:
        p = PurePosixPath(rel)
        if not rel or "\\" in rel or ":" in rel or p.is_absolute() or len(p.parts) < 2:
            raise WorkflowError("素材路径无效。", 422)
        if any(part.startswith((".", "_")) for part in p.parts) or p.suffix.lower() not in EXTENSIONS:
            raise WorkflowError("素材类型或路径不允许。", 422)
        path = self.outputs
        for part in p.parts:
            path /= part
            if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
                raise WorkflowError("素材不能使用符号链接或目录联接。", 422)
        resolved = path.resolve()
        if self.outputs not in resolved.parents or not resolved.is_file():
            raise WorkflowError("素材不存在或路径越界。", 422)
        return resolved

    def media_info(self, rel: str) -> dict:
        path = self.safe_media_path(rel)
        is_video = path.suffix.lower() in {".mp4", ".mov", ".webm"}
        maximum = MAX_VIDEO_BYTES if is_video else MAX_IMAGE_BYTES
        digest = hashlib.sha256()
        count = 0
        with path.open("rb") as stream:
            first = stream.read(64)
            if is_video:
                if path.suffix.lower() == ".webm":
                    valid = first.startswith(b"\x1a\x45\xdf\xa3")
                else:
                    valid = b"ftyp" in first[:32]
                if not valid:
                    raise WorkflowError("视频容器与文件扩展名不匹配。", 422)
            stream.seek(0)
            while chunk := stream.read(1024 * 1024):
                count += len(chunk)
                if count > maximum:
                    raise WorkflowError("图片不能超过 20 MiB，视频不能超过 512 MiB。", 422)
                digest.update(chunk)
        if count == 0:
            raise WorkflowError("素材文件为空。", 422)
        if not is_video:
            from PIL import Image
            try:
                with Image.open(path) as image:
                    expected = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP", ".gif": "GIF"}
                    if image.format != expected[path.suffix.lower()]:
                        raise ValueError("format mismatch")
                    image.verify()
            except Exception:
                raise WorkflowError("图片数据无效。", 422) from None
        return {"path": rel, "bytes": count, "sha256": digest.hexdigest(), "kind": "video" if is_video else "image"}

    def inspect_watermark(self, rel: str) -> dict:
        """Inspect one user-visible image without treating missing evidence as clean."""
        path = self.safe_media_path(rel)
        return self.watermarks.inspect(path, rel)

    def clean_watermark(self, rel: str) -> dict:
        """Regenerate one image into a new sibling file; the source is never modified."""
        source = self.safe_media_path(rel)
        result = self.watermarks.clean(source, rel)
        output_path = None
        try:
            info = self.media_info(result["output"])
            output_path = self.safe_media_path(result["output"])
            return {**result, "media": info, "inspection": self.inspect_watermark(result["output"])}
        except Exception:
            if output_path is None:
                candidate = self.outputs.joinpath(*PurePosixPath(result["output"]).parts)
                if self.outputs in candidate.resolve().parents:
                    output_path = candidate
            if output_path is not None:
                output_path.unlink(missing_ok=True)
            raise

    def clean_task_watermarks(self, task_id: str, version: str, confirmed: bool) -> dict:
        """Clean all supported task images, then save them as a new task version."""
        if not confirmed:
            raise WorkflowError("请确认深度清理会重建图片像素，并为原图生成新的派生副本。", 422)
        with self.store.transaction(write=False) as state:
            task = deepcopy(self._task(state, task_id, version))
        if task["status"] not in EDITABLE:
            raise WorkflowError("该任务已进入执行或回执阶段，不能替换素材。")
        targets = [
            rel for rel in task["content"].get("media", [])
            if PurePosixPath(rel).suffix.lower() in SUPPORTED_IMAGE_EXTENSIONS
        ]
        if not targets:
            raise WorkflowError("当前版本没有可执行隐式水印清理的 PNG/JPEG/WebP 图片。", 422)

        cleaned: list[dict] = []
        replacements: dict[str, str] = {}
        try:
            for rel in dict.fromkeys(targets):
                result = self.clean_watermark(rel)
                cleaned.append(result)
                replacements[rel] = result["output"]
            payload = {name: task["content"].get(name) for name in ContentInput.model_fields}
            payload["media"] = [replacements.get(rel, rel) for rel in task["content"].get("media", [])]
            payload["expected_version"] = version
            updated = self.revise(task_id, RevisionInput.model_validate(payload))
        except Exception:
            for item in cleaned:
                try:
                    self.safe_media_path(item["output"]).unlink(missing_ok=True)
                except Exception:
                    pass
            raise
        return {"task": updated, "cleaned": cleaned}

    def _snapshot(self, req: ContentInput):
        if req.mode != "real":
            return super()._snapshot(req)
        value = ContentInput.model_validate(req.model_dump(include=set(ContentInput.model_fields))).model_dump()
        value["scheduled_at"] = normalize_time(req.scheduled_local, req.timezone, req.fold)
        value["media_snapshot"] = [self.media_info(p) for p in req.media]
        if sum(p["bytes"] for p in value["media_snapshot"]) > MAX_BATCH_BYTES:
            raise WorkflowError("单个任务的素材总量不能超过 1 GiB。", 422)
        return value

    def create(self, req: CreateInput):
        snapshot = self._snapshot(req)
        request_digest = fingerprint(snapshot)
        with self.store.transaction() as state:
            for task in state["tasks"].values():
                if task["idempotency_key"] == req.idempotency_key:
                    if task.get("request_digest", task["initial_digest"]) != request_digest:
                        raise WorkflowError("创建键已用于其他内容。")
                    return deepcopy(task)
            if len(state["tasks"]) >= MAX_TASKS:
                raise WorkflowError("最多保存 500 个发布任务。", 422)
            if snapshot["source_id"]:
                mother = state.get("contents", {}).get(snapshot["source_id"])
                if not mother or snapshot["source_version_id"] != mother["version_id"]:
                    raise WorkflowError("主稿版本不存在或已经更新。")
            else:
                mother = add_mother(state, mother_snapshot(snapshot), "task-" + req.idempotency_key)
                snapshot.update(source_id=mother["id"], source_version_id=mother["version_id"])
            version_id = fingerprint(snapshot)
            now = self.clock().isoformat()
            task = {"id": uuid.uuid4().hex, "version": 1, "version_id": version_id,
                "content": snapshot, "history": [], "idempotency_key": req.idempotency_key,
                "initial_digest": request_digest, "request_digest": request_digest, "approval": None,
                "attempts": 0, "receipt": None, "created_at": now, "updated_at": now, "events": []}
            self._event(task, "draft", "平台版本已保存，与主稿建立关联。")
            state["tasks"][task["id"]] = task
            return deepcopy(task)

    def revise(self, task_id, req: RevisionInput):
        with self.store.transaction(write=False) as state:
            task = self._task(state, task_id, req.expected_version)
            if task.get("variant_id"):
                raise WorkflowError("发布任务是平台版本的不可变执行快照；请回到平台版本继续编辑并创建新的发布任务。")
            if task["content"]["mode"] == "real" and task["attempts"] > 0:
                if not (task.get("receipt") or {}).get("not_submitted"):
                    raise WorkflowError("此任务曾进入真实提交，必须先核对回执；不能通过修改绕过防重复保护。")
            source_id = req.source_id or task["content"].get("source_id", "")
            source_version = req.source_version_id or task["content"].get("source_version_id", "")
            if source_id:
                mother = state.get("contents", {}).get(source_id)
                if not mother or mother["version_id"] != source_version:
                    raise WorkflowError("主稿已更新，请先同步来源版本。")
        return super().revise(task_id, req.model_copy(update={"source_id": source_id, "source_version_id": source_version}))

    def _lineage_problems(self, task, state):
        if task.get("variant_id"):
            variant = state.get("variants", {}).get(task["variant_id"])
            if not variant:
                return ["来源平台版本不存在。"]
            if variant.get("version_id") != task.get("variant_version_id"):
                return ["平台版本已更新，请从最新平台版本创建新的发布任务。"]
            return []
        source_id = task["content"].get("source_id")
        if source_id:
            mother = state.get("contents", {}).get(source_id)
            if not mother or mother["version_id"] != task["content"].get("source_version_id"):
                return ["主稿已更新，当前历史任务的来源已过期。"]
        return []

    def _problems(self, task, state=None):
        if state is None:
            raise RuntimeError("Workspace validation requires the current transaction")
        c = task["content"]
        if c["mode"] == "real":
            problems = []
            if c.get("platform") == "blog":
                try:
                    connector = self.blogs.get(c.get("account_id") or "")
                    if connector.get("status") != "connected":
                        problems.append("目标 Blog 当前未连接。")
                    variant = state.get("variants", {}).get(task.get("variant_id"), {})
                    content_type = str((variant.get("content", {}).get("options") or {}).get("content_type") or "article")
                    capabilities = set(connector.get("capabilities") or [])
                    required = {f"{content_type}.create", f"{content_type}.read", f"{content_type}.update", f"{content_type}.publish"}
                    if content_type not in {"article", "thought"} or not required.issubset(capabilities):
                        problems.append("目标 Blog 不支持当前内容类型的完整发布能力。")
                    if c.get("media") and "media.upload" not in capabilities:
                        problems.append("当前内容包含素材，但目标 Blog 未声明媒体上传能力。")
                    if content_type == "thought" and c.get("media"):
                        problems.append("当前 Blog Connector 的想法类型暂不接受媒体，请改用文章版本或移除素材。")
                except WorkflowError:
                    problems.append("请选择已连接的 Blog 发布目标。")
                for media in c.get("media_snapshot") or []:
                    try:
                        if self.media_info(media["path"])["sha256"] != media["sha256"]:
                            problems.append("素材已变化，需要从平台版本创建新的发布任务。")
                    except (WorkflowError, OSError):
                        problems.append("素材无法读取或已失效。")
                if c.get("scheduled_at") and datetime.fromisoformat(c["scheduled_at"]) <= self.clock():
                    problems.append("排期已过期，请调整时间。")
                problems.extend(self._lineage_problems(task, state))
                return problems
            candidate = state.get('accounts', {}).get(c.get('account_id'), {})
            if candidate.get('adapter') == X_ADAPTER:
                problems = self.x.problems(task, state)
                problems.extend(self._lineage_problems(task, state))
                return problems
            if candidate.get('adapter') == WECHAT_ADAPTER:
                problems = self.wechat.problems(task, state)
                problems.extend(self._lineage_problems(task, state))
                return problems
            if candidate.get('adapter') == REST_ADAPTER:
                problems = self.rest.problems(task, state)
                problems.extend(self._lineage_problems(task, state))
                return problems
            if candidate.get('adapter') == X_BROWSER_ADAPTER:
                if candidate.get('execution_node_id', LOCAL_NODE_ID) != LOCAL_NODE_ID:
                    problems.append('远程 Browser Node 的 X 发布传输尚未启用；请在绑定该 Profile 的执行设备上完成发布，或暂时改用本机节点 / X 官方 API。')
                if c.get('tags'):
                    problems.append('X 浏览器模式的话题请直接写入正文；独立话题字段应留空。')
                if len(c.get('media') or []) > 4:
                    problems.append('X 浏览器模式首版最多支持 4 张图片。')
                for media in c.get('media_snapshot') or []:
                    suffix = PurePosixPath(media['path']).suffix.lower()
                    if suffix not in {'.png', '.jpg', '.jpeg', '.webp'}:
                        problems.append('X 浏览器模式首版只支持 PNG/JPEG/WebP 图片，不支持 GIF 或视频。')
                    if int(media.get('bytes') or 0) > 5 * 1024 * 1024:
                        problems.append('X 浏览器模式首版单张图片不能超过 5 MiB。')
            native = NATIVE.get(c["platform"])
            a = state.get("accounts", {}).get(c["account_id"])
            if not native:
                problems.append("该渠道的真实发布适配器尚未接入。")
            if not a or a["platform"] != c["platform"]:
                problems.append("请绑定并选择属于该平台的具体账号。")
            elif a["status"] != "connected":
                problems.append("目标账号尚未登录或登录已失效。")
            elif a.get("operation") and a["operation"]["state"] in ACTIVE:
                problems.append("目标账号有未结束或待核对的操作。")
            if native:
                env = self.accounts.environment
                if c["platform"] == "bilibili" and not env["biliup"]:
                    problems.append("未检测到项目环境中的 biliup。")
                if c["platform"] != "bilibili" and not env["browser"]:
                    problems.append("未检测到可用浏览器。")
                length = len(c["title"])
                if c["platform"] == "xiaohongshu":
                    units = c["title"].encode("utf-16-le")
                    length = (sum(2 if int.from_bytes(units[i:i+2], "little") > 127 else 1 for i in range(0, len(units), 2)) + 1) // 2
                if length > native["title_limit"] or len(c["body"]) > native["body_limit"]:
                    problems.append(f'当前适配器限制：标题 {native["title_limit"]}、正文 {native["body_limit"]} 字符。')
                video = [p for p in c["media"] if PurePosixPath(p).suffix.lower() in {".mp4", ".mov", ".webm"}]
                images = [p for p in c["media"] if p not in video]
                if video and (images or len(video) > 1):
                    problems.append("每条内容只允许一个视频，不能混用图片与视频。")
                if (video and "video" not in native["formats"]) or (images and ("images" not in native["formats"] or len(images) > native["max_images"])):
                    problems.append("所选媒体不在当前适配器支持范围内。")
                if not c["media"] and "text" not in native["formats"]:
                    problems.append("此平台需要附带媒体文件。")
            for media in c["media_snapshot"]:
                try:
                    if self.media_info(media["path"])["sha256"] != media["sha256"]:
                        problems.append("素材已变化，需要保存新版本。")
                except (WorkflowError, OSError):
                    problems.append("素材无法读取或已失效。")
            if c.get("scheduled_at") and datetime.fromisoformat(c["scheduled_at"]) <= self.clock():
                problems.append("排期已过期，请调整时间。")
        else:
            problems = super()._problems(task, state)
        problems.extend(self._lineage_problems(task, state))
        return problems

    def preflight(self, task_id, version):
        with self.store.transaction() as state:
            task = self._task(state, task_id, version)
            problems = self._problems(task, state)
            if not problems and task["content"]["mode"] == "real":
                if task["content"].get("platform") == "blog":
                    connector = self.blogs.get(task["content"]["account_id"])
                    task["review_context"] = {"connector_id": connector["id"], "label": connector["label"], "adapter": "blog-openapi"}
                else:
                    a = state["accounts"][task["content"]["account_id"]]
                    task["review_context"] = {"account_id": a["id"], "auth_revision": a["auth_revision"], "label": a["label"], "identity": deepcopy(a["identity"]), "adapter": a.get('adapter', 'native')}
            if not problems and task["status"] == "draft":
                self._event(task, "review_ready", "当前内容、素材和账号预检通过。")
            response = {"ok": not problems, "problems": problems, "task": deepcopy(task)}
        reports = []
        for rel in response["task"]["content"].get("media", []):
            if PurePosixPath(rel).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                continue
            try:
                reports.append(self.inspect_watermark(rel))
            except (WorkflowError, OSError):
                capability = self.watermarks.capability()
                reports.append({"path": rel, "status": "error", "provider": None, "signals": [],
                                "supported": False, **capability, "note": "该图片无法完成隐式水印检查。"})
        response["watermarks"] = reports
        response["watermark_capability"] = self.watermarks.capability()
        return response

    def approve(self, task_id, req: ApprovalInput):
        with self.store.transaction() as state:
            task = self._task(state, task_id, req.expected_version)
            if task["status"] not in {"review_ready", "approved", "scheduled"}:
                raise WorkflowError("请先对当前版本完成预检。")
            if not req.confirmed:
                raise WorkflowError("需要确认此版本。", 422)
            real = task["content"]["mode"] == "real"
            if real and task['attempts'] > 0 and not (task.get('receipt') or {}).get('not_submitted'):
                raise WorkflowError('此任务已进入真实提交，重新登录或修改账号不能解除回执核对要求。')
            if real and not req.real_publish_confirmed:
                wechat_draft = task["content"].get("platform") == "wechat" and (task["content"].get("options") or {}).get("wechat_action", "draft") != "publish"
                raise WorkflowError("请明确确认把该内容写入所选微信公众号草稿箱。" if wechat_draft else "请明确确认该内容将发布到选中的真实账号。", 422)
            problems = self._problems(task, state)
            if problems:
                raise WorkflowError("；".join(problems), 422)
            approval = {"version_id": task["version_id"], "at": self.clock().isoformat(), "actor": "local-user", "real_publish_confirmed": real and req.real_publish_confirmed}
            if real:
                if task["content"].get("platform") == "blog":
                    connector = self.blogs.get(task["content"]["account_id"])
                    if (task.get("review_context") or {}).get("connector_id") != connector["id"]:
                        raise WorkflowError("Blog 连接在审核期间发生变化，请重新预检。")
                    approval.update(connector_id=connector["id"], label=connector["label"], adapter="blog-openapi")
                else:
                    a = state["accounts"][task["content"]["account_id"]]
                    if req.expected_account_revision != a["auth_revision"] or (task.get("review_context") or {}).get("auth_revision") != a["auth_revision"]:
                        raise WorkflowError("账号在审核期间发生变化，请重新预检。")
                    approval.update(account_id=a["id"], auth_revision=a["auth_revision"], identity=deepcopy(a["identity"]))
                    if a.get('adapter') == REST_ADAPTER:
                        if not req.external_service_confirmed:
                            raise WorkflowError('请单独确认外部服务传输和可能的额度/费用消耗。', 422)
                        approval.update(external_service_confirmed=True, bridge_revision=a['bridge_revision'])
            task["approval"] = approval
            wechat_draft = real and task["content"].get("platform") == "wechat" and (task["content"].get("options") or {}).get("wechat_action", "draft") != "publish"
            self._event(task, "scheduled" if task["content"]["scheduled_at"] else "approved", "已确认内容、账号及排期。" + ("此授权允许写入公众号草稿箱，不允许公开发布。" if wechat_draft else "此授权允许真实发布。" if real else "仅执行本地任务。"))
            return deepcopy(task)

    def dispatch(self, task_id, version):
        task = self.get(task_id)
        if task["content"]["mode"] != "real":
            return super().dispatch(task_id, version)
        with self.store.transaction() as state:
            task = self._task(state, task_id, version)
            if task["status"] in {"dispatching", "accepted", "published"}:
                return deepcopy(task)
            if task["status"] not in {"approved", "scheduled"} or self._closed:
                raise WorkflowError("当前任务不能提交；未知结果必须先核对。")
            if task['attempts'] > 0 and not (task.get('receipt') or {}).get('not_submitted'):
                raise WorkflowError('原提交尚未排除重复风险，禁止再次执行此任务。')
            approval = task.get("approval") or {}
            if approval.get("version_id") != version or approval.get("real_publish_confirmed") is not True:
                raise WorkflowError("此版本没有有效的真实发布授权。")
            c = task["content"]
            is_blog = c.get("platform") == "blog"
            account = None
            if is_blog:
                connector = self.blogs.get(c["account_id"])
                if approval.get("connector_id") != connector["id"]:
                    raise WorkflowError("Blog 连接授权状态已变化，请重新审核。")
            else:
                a = state.get("accounts", {}).get(c["account_id"])
                if not a or approval.get("account_id") != a["id"] or approval.get("auth_revision") != a["auth_revision"]:
                    raise WorkflowError("账号授权状态已变化，请重新审核。")
                if a.get('adapter') == REST_ADAPTER and (not approval.get('external_service_confirmed') or approval.get('bridge_revision') != a.get('bridge_revision')):
                    raise WorkflowError('当前版本缺少有效的外部服务传输授权。')
                account = deepcopy(a)
            if c["scheduled_at"] and datetime.fromisoformat(c["scheduled_at"]) > self.clock():
                raise WorkflowError("尚未到计划时间。")
            check = deepcopy(task)
            check["content"]["scheduled_at"] = None
            problems = self._problems(check, state)
            if problems:
                raise WorkflowError("；".join(problems), 422)
            if not is_blog and sum((a.get('operation') or {}).get('state') == 'running' for a in state.get('accounts', {}).values()) >= 3:
                raise WorkflowError("已有三个账号操作运行中，请稍后执行。", 429)
            operation_id = uuid.uuid4().hex
            task["attempts"] += 1
            task["operation_id"] = operation_id
            if not is_blog:
                state["accounts"][c["account_id"]]["operation"] = {"id": operation_id, "kind": "publish", "state": "running", "task_id": task_id, "started_at": self.clock().isoformat()}
            self._event(task, "dispatching", "执行意图已保存，正在启动 Blog 发布器。" if is_blog else "执行意图已保存，正在准备审核版本的素材并启动发布器。")
            work = deepcopy(task)
        target = self._publish_blog if is_blog else self._publish
        args = (work,) if is_blog else (work, account)
        worker = threading.Thread(target=target, args=args, daemon=True, name="ripple-blog-publish" if is_blog else "ripple-publish")
        with self._guard:
            self._workers[task_id] = worker
        worker.start()
        return self.get(task_id)

    def _publish_blog(self, task):
        result = {"state": "unknown_result", "message": "Blog 发布结果未确认；禁止盲目重试，请先核对远端状态。"}
        try:
            variant = self.variants.get(task["variant_id"])
            result = self.blogs.publish(task, variant)
        except WorkflowError as exc:
            if exc.status in {401, 403, 409, 422}:
                result = {"state": "verification_required", "not_submitted": True, "message": str(exc)[:300]}
        except Exception:
            pass
        finally:
            try:
                self._record_blog_result(task["id"], task["operation_id"], result)
            finally:
                with self._guard:
                    self._workers.pop(task["id"], None)

    def _record_blog_result(self, task_id, operation_id, result):
        with self.store.transaction() as state:
            task = self._task(state, task_id)
            if task.get("operation_id") != operation_id or task["status"] == "published":
                return deepcopy(task)
            status = result.get("state", "unknown_result")
            if status not in {"published", "unknown_result", "verification_required", "failed_terminal"}:
                status = "unknown_result"
            receipt = {"adapter": result.get("adapter", "blog-openapi"), "simulated": False,
                "result": status, "operation_id": operation_id, "not_submitted": result.get("not_submitted") is True,
                "evidence": "remote_api" if status == "published" else "unconfirmed", "public_url": result.get("public_url") or None}
            for key in ("remote_id", "remote_revision", "slug", "media"):
                if key in result:
                    receipt[key] = deepcopy(result[key])
            task["receipt"] = receipt
            self._event(task, status, result.get("message", "Blog 发布结果尚未确认。"))
            return deepcopy(task)

    def _staged_media(self, task, account):
        target = self.accounts.directory(account["id"]) / "operations" / task["operation_id"] / "media"
        target.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, media in enumerate(task["content"]["media_snapshot"]):
            source = self.safe_media_path(media["path"])
            dest = target / (str(i) + source.suffix.lower())
            h = hashlib.sha256()
            count = 0
            with source.open("rb") as source_file, dest.open("xb") as destination:
                while chunk := source_file.read(1024 * 1024):
                    count += len(chunk)
                    if count > MAX_VIDEO_BYTES:
                        raise WorkflowError("素材超过安全上限。")
                    h.update(chunk)
                    destination.write(chunk)
            if count != media["bytes"] or h.hexdigest() != media["sha256"]:
                raise WorkflowError("素材在审核后发生变化，停止提交。")
            paths.append(str(dest))
        return paths

    def _publish(self, task, account):
        result = {"state": "verification_required", "not_submitted": True, "message": "素材准备失败，未执行上传；请重新保存版本。"}
        try:
            paths = self._staged_media(task, account)
            result = {"state": "unknown_result", "message": "发布执行未返回可确认结果，请先核对平台。"}
            if account.get('adapter') == X_ADAPTER:
                result = self.x.publish(task, account, paths)
            elif account.get('adapter') == WECHAT_ADAPTER:
                result = self.wechat.publish(task, account, paths)
            elif account.get('adapter') == REST_ADAPTER:
                result = self.rest.publish(task, account, paths)
            else:
                result = self.accounts.run(account, "publish", task["operation_id"], confirmed=True,
                    task_id=task["id"], version_id=task["version_id"], content=task["content"],
                    identity=task["approval"]["identity"], media_paths=paths)
            if result.get("operation_id") not in {None, task["operation_id"]} or result.get("task_id") not in {None, task["id"]} or result.get("version_id") not in {None, task["version_id"]}:
                result = {"state": "unknown_result", "message": "发布回执关联不匹配，请人工核对。"}
        except Exception:
            pass
        finally:
            try:
                self._record_result(task["id"], task["operation_id"], result)
            finally:
                with self._guard:
                    self._workers.pop(task["id"], None)

    def _record_result(self, task_id, operation_id, result):
        with self.store.transaction() as state:
            task = self._task(state, task_id)
            if task.get("operation_id") != operation_id or task["status"] == "published":
                return deepcopy(task)
            status = result.get("state", "unknown_result")
            if status not in {"accepted", "published", "unknown_result", "verification_required", "failed_terminal"}:
                status = "unknown_result"
            remote_account = state.get('accounts', {}).get(task['content']['account_id'], {})
            task["receipt"] = {"adapter": remote_account.get('adapter', 'native'), "simulated": False, "public_url": None,
                "result": status, "operation_id": operation_id, "not_submitted": result.get("not_submitted") is True,
                "evidence": result.get("evidence", "unconfirmed")}
            if remote_account.get('adapter') == REST_ADAPTER:
                for key in ('flow_id', 'bridge_revision', 'remote_task_id', 'remote_status', 'candidate_url'):
                    if key in result:
                        task['receipt'][key] = result[key]
            if remote_account.get('adapter') == X_ADAPTER:
                for key in ('post_id', 'candidate_url'):
                    if key in result:
                        task['receipt'][key] = result[key]
            if remote_account.get('adapter') == WECHAT_ADAPTER:
                for key in ('draft_media_id', 'publish_id', 'publish_status', 'draft_only', 'candidate_url'):
                    if key in result and result[key] is not None:
                        task['receipt'][key] = result[key]
                if result.get('public_url'):
                    task['receipt']['public_url'] = result['public_url']
            self._event(task, status, result.get("message", "发布结果尚未确认。"))
            a = state.get("accounts", {}).get(task["content"]["account_id"])
            if a and (a.get("operation") or {}).get("id") == operation_id:
                a["operation"]["state"] = "finished"
            return deepcopy(task)

    def query(self, task_id, version):
        task = self.get(task_id)
        if task["version_id"] != version:
            raise WorkflowError("版本已变化。")
        if task["content"]["mode"] != "real":
            return super().query(task_id, version)
        if task["status"] not in {"accepted", "unknown_result", "verification_required", "dispatching"}:
            return task
        if task["content"].get("platform") == "blog":
            if not task.get("variant_id"):
                return task
            try:
                variant = self.variants.get(task["variant_id"])
                result = self.blogs.query(task, variant)
            except WorkflowError:
                return task
            return self._record_blog_result(task_id, task.get("operation_id") or "", result) if result else task
        if task["status"] == "verification_required" and (task.get("receipt") or {}).get("not_submitted") is True:
            return task
        account = self.accounts.get(task['content']['account_id'])
        if account.get('adapter') == X_ADAPTER:
            if task['status'] not in {'accepted', 'unknown_result', 'verification_required'}:
                return task
            result = self.x.query(task, account)
            return self._record_result(task_id, task['operation_id'], result) if result else task
        if account.get('adapter') == WECHAT_ADAPTER:
            if task['status'] not in {'accepted', 'unknown_result', 'verification_required'}:
                return task
            result = self.wechat.query(task, account)
            return self._record_result(task_id, task['operation_id'], result) if result else task
        if account.get('adapter') == REST_ADAPTER:
            if task['status'] not in {'accepted', 'unknown_result', 'verification_required'}:
                return task
            result = self.rest.query(task, account)
            return self._record_result(task_id, task['operation_id'], result) if result else task
        op = task.get("operation_id")
        if op and task["status"] in {"unknown_result", "accepted", "dispatching"}:
            result = self.accounts.read_result(task["content"]["account_id"], op)
            if result and result.get("task_id") == task_id and result.get("version_id") == version:
                return self._record_result(task_id, op, result)
        return task

    def confirm_receipt(self, task_id, req: ReceiptInput):
        try:
            parsed = urlsplit(req.public_url)
            loopback_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            valid = (parsed.scheme == "https" or loopback_http) and not parsed.username and not parsed.password and not parsed.fragment
        except ValueError:
            valid = False
        if not req.confirmed or not valid:
            raise WorkflowError("请确认核对结果并填写有效作品链接；公网必须使用 HTTPS。", 422)
        with self.store.transaction() as state:
            task = self._task(state, task_id, req.expected_version)
            if task["content"]["mode"] != "real" or task["status"] not in {"accepted", "unknown_result", "verification_required"}:
                raise WorkflowError("当前任务不需要人工确认发布回执。")
            is_blog = task["content"].get("platform") == "blog"
            if not is_blog and (parsed.hostname not in RECEIPT_HOSTS.get(task["content"]["platform"], set()) or parsed.path in {"", "/"}):
                raise WorkflowError("链接必须指向当前平台的具体作品。", 422)
            a = None
            if not is_blog:
                a = self.accounts._account(state, task["content"]["account_id"])
                try:
                    with FileLock(str(self.accounts.directory(a["id"]) / "browser-operation.lock"), timeout=0):
                        pass
                except Timeout:
                    raise WorkflowError("发布进程仍在运行，请等待后再核对。") from None
            task["receipt"] = {**(task.get("receipt") or {}), "result": "published", "public_url": req.public_url,
                "verification": "manual_user_confirmation", "verified_at": self.clock().isoformat()}
            self._event(task, "published", "用户已在平台人工核对作品链接。" + (" 备注：" + req.note if req.note else ""))
            if a and (a.get("operation") or {}).get("id") == task.get("operation_id"):
                a["operation"]["state"] = "finished"
            # Do not claim automated live end-to-end verification from a pasted URL.
            return deepcopy(task)

    def cancel(self, task_id, version):
        task = self.get(task_id)
        if task["content"]["mode"] == "real" and task["attempts"] > 0 and not (task.get("receipt") or {}).get("not_submitted"):
            raise WorkflowError("已进入真实提交阶段，不能假定远程取消成功。")
        return super().cancel(task_id, version)

    def recover(self):
        changed = super().recover()
        self.accounts.recover()
        self.interactions.recover()
        with self.store.transaction() as state:
            for a in state.get("accounts", {}).values():
                op = a.get("operation") or {}
                if op.get("kind") == "publish" and op.get("state") == "running":
                    op["state"] = "recovery_required"
        return changed

    def calendar(self):
        with self.store.transaction(write=False) as state:
            accounts = state.get("accounts", {})
            blogs = {item["id"]: item for item in self.blogs.list()}
            return [{"id": t["id"], "title": t["content"]["title"], "platform": t["content"]["platform"],
                "account": (blogs.get(t["content"]["account_id"], {}).get("label") if t["content"].get("platform") == "blog" else accounts.get(t["content"]["account_id"], {}).get("label")) or "本地",
                "scheduled_at": t["content"].get("scheduled_at"), "timezone": t["content"].get("timezone"),
                "status": t["status"], "source_id": t["content"].get("source_id", ""), "mode": t["content"]["mode"]}
                for t in state["tasks"].values() if t["content"].get("scheduled_at")]

    def close(self):
        self._closed = True
        self.accounts.close()
        with self._guard:
            workers = list(self._workers.values())
        for worker in workers:
            worker.join(timeout=12)
