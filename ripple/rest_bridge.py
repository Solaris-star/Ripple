"""Configurable REST account connection and first real X publishing adapter.

Configuration, account authorization and per-content external-service consent are
independent gates. This module only preserves the legacy external REST bridge; native
X and WeChat Official Account adapters are implemented elsewhere. TikTok discovery
must not be advertised here as a native publishing implementation.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
import uuid

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from .secrets import protect
from .catalog import RECEIPT_HOSTS
from .publishing import WorkflowError, fingerprint
from .rest_client import RestClient, RestError, PLATFORM_MAP, identifier, api_base, host_name

ADAPTER = "aitoearn-rest"
X_QUEUE_SECONDS = 90


class RestConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    base_url: str = Field(min_length=8, max_length=1024)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), max_length=4096)
    upload_hosts: list[str] = Field(default_factory=list, max_length=8)
    confirmed: bool = False

    @field_validator("upload_hosts")
    @classmethod
    def valid_hosts(cls, value):
        return sorted({host_name(h) for h in value})


class RemoteImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    discovery_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    account_ids: list[str] = Field(min_length=1, max_length=20)
    confirmed: bool = False

    @field_validator("account_ids")
    @classmethod
    def valid_ids(cls, values):
        checked = [identifier(v) for v in values]
        if len(set(checked)) != len(checked):
            raise ValueError("重复账号")
        return checked


def at_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path: Path):
    if not path.exists():
        return {}
    try:
        if path.stat().st_size > 512000:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (OSError, ValueError):
        raise WorkflowError("私有服务配置或回执无法读取，已停止操作。", 503) from None


def atomic_write(path: Path, value: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def public_account(item):
    if not isinstance(item, dict) or item.get("type") not in PLATFORM_MAP:
        return None
    remote_id = identifier(item.get("id"))
    if type(item.get("status")) is not int:
        raise RestError("账号状态格式不匹配。")
    return {"remote_id": remote_id, "platform": PLATFORM_MAP[item["type"]], "remote_platform": item["type"],
            "name": str(item.get("nickname") or remote_id)[:80], "uid": str(item.get("uid") or "")[:128],
            "status": item["status"]}


def public_platform(item):
    if not isinstance(item, dict) or item.get("platform") not in PLATFORM_MAP:
        return None
    names = item.get("displayName")
    name = names.get("zh-CN", names.get("en-US", item["platform"])) if isinstance(names, dict) else item["platform"]
    raw = {"remote_platform": item["platform"], "platform": PLATFORM_MAP[item["platform"]], "name": str(name)[:80],
        "status": item.get("status"), "auth_type": item.get("authType"),
        "content_limits": item.get("contentLimits") or {}, "media_rules": item.get("mediaRules") or {},
        "publish_capabilities": (item.get("capabilities") or {}).get("publish") or {},
        "option_schema": item.get("optionSchema") or {}}
    if len(json.dumps(raw)) > 64000 or any(not isinstance(raw[k], dict) for k in ("content_limits", "media_rules", "publish_capabilities", "option_schema")):
        raise RestError("平台能力响应过大或结构不匹配。")
    return raw


class RestBridgeService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store
        self.path = workspace.private / "integrations" / "aitoearn-rest.json"
        self.client_factory = RestClient

    def config(self):
        return read_json(self.path)

    def status(self):
        cfg = self.config()
        return {"configured": bool(cfg.get("protected_key")), "base_url": cfg.get("base_url", ""),
            "upload_hosts": cfg.get("upload_hosts", []), "revision": cfg.get("revision"),
            "discovery": cfg.get("discovery"), "connected": bool(cfg.get("discovery")),
            "implemented_publish_platforms": ["x"], "remote_queue_seconds": X_QUEUE_SECONDS,
            "live_verified": False, "key_storage": "Windows DPAPI"}

    def _client(self, cfg):
        if not cfg.get("protected_key"):
            raise WorkflowError("请先配置 REST 服务连接。", 422)
        try:
            secret = protect(base64.b64decode(cfg["protected_key"]), decrypt=True).decode("utf-8")
        except (ValueError, UnicodeError):
            raise WorkflowError("系统无法解密当前服务配置，请重新保存密钥。", 503) from None
        return self.client_factory(cfg["base_url"], secret)

    def save(self, req: RestConfig):
        if not req.confirmed:
            raise WorkflowError("请确认受信任的服务地址及密钥保存授权。", 422)
        address = api_base(req.base_url)
        with self.store.transaction() as state:
            for t in state["tasks"].values():
                a = state.get("accounts", {}).get(t["content"].get("account_id"), {})
                if a.get("adapter") == ADAPTER and t["status"] in {"dispatching", "accepted", "unknown_result", "verification_required"} and t["attempts"] and not (t.get('receipt') or {}).get('not_submitted'):
                    raise WorkflowError("仍有远端提交待核对，先处理回执再更换服务配置。")
            cfg = self.config()
            raw = req.api_key.get_secret_value().strip()
            if raw:
                encrypted = base64.b64encode(protect(raw.encode())).decode()
            elif cfg.get("base_url") == address and cfg.get("protected_key"):
                encrypted = cfg["protected_key"]
            else:
                raise WorkflowError("新服务地址需要重新提供 API Key。", 422)
            revision = uuid.uuid4().hex
            atomic_write(self.path, {"base_url": address, "protected_key": encrypted, "revision": revision,
                                     "upload_hosts": req.upload_hosts})
            for a in state.get("accounts", {}).values():
                if a.get("adapter") == ADAPTER:
                    a.update(status="disconnected", auth_revision=a["auth_revision"] + 1,
                             message="服务配置已更新，请重新发现并连接账号。")
                    self.workspace.accounts.invalidate_tasks(state, a["id"], "外部服务配置变化，原审批失效。")
        return self.status()

    def discover(self, confirmed):
        if not confirmed:
            raise WorkflowError("请确认从服务读取账号与能力元数据。", 422)
        cfg = self.config()
        client = self._client(cfg)
        try:
            platforms = [p for raw in client.platforms() if (p := public_platform(raw))]
            result = client.accounts()
            accounts = [a for raw in result["list"] if (a := public_account(raw))]
            if len(accounts) > 100:
                raise WorkflowError("匹配账号超过 100 个，请在服务端缩小授权范围。", 422)
        finally:
            client.close()
        discovery = {"id": uuid.uuid4().hex, "at": at_now(), "platforms": platforms, "accounts": accounts,
            "remote_total": result["total"], "partial": result["total"] != len(result["list"])}
        with self.store.transaction(write=False):
            current = self.config()
            if current.get("revision") != cfg["revision"]:
                raise WorkflowError("服务配置已变化，本次检查结果未保存。")
            current["discovery"] = discovery
            atomic_write(self.path, current)
        return self.status()

    def import_accounts(self, req: RemoteImport):
        if not req.confirmed:
            raise WorkflowError("请确认将所选远端账号连接到 Ripple。", 422)
        cfg = self.config()
        discovery = cfg.get("discovery") or {}
        if discovery.get("id") != req.discovery_id:
            raise WorkflowError("账号发现结果已过期，请刷新。")
        discovered = {a["remote_id"]: a for a in discovery["accounts"]}
        if any(key not in discovered for key in req.account_ids):
            raise WorkflowError("只能连接当前已发现的账号。", 422)
        platform_by_id = {p["remote_platform"]: p for p in discovery["platforms"]}
        client = self._client(cfg)
        records = []
        try:
            for remote_id in req.account_ids:
                item, auth_status = client.account(remote_id)
                a = public_account(item)
                if not a or a["remote_platform"] != discovered[remote_id]["remote_platform"] or a["uid"] != discovered[remote_id]["uid"]:
                    raise WorkflowError("远端账号身份已变化，请重新发现。")
                records.append((a, auth_status))
        finally:
            client.close()
        with self.store.transaction() as state:
            if self.config().get("revision") != cfg["revision"]:
                raise WorkflowError("服务配置已变化，账号未导入。")
            accounts = state.setdefault("accounts", {})
            existing = {a["external_id"]: a for a in accounts.values() if a.get("adapter") == ADAPTER and a.get("bridge_base") == cfg["base_url"]}
            if len(accounts) + sum(r["remote_id"] not in existing for r, _ in records) > 100:
                raise WorkflowError("账号数量已达到 100 个上限。", 422)
            rows = []
            for remote, auth in records:
                account_id = existing.get(remote["remote_id"], {}).get("id") or uuid.uuid4().hex
                old = accounts.get(account_id)
                if old and (old.get("operation") or {}).get("state") in {"running", "recovery_required"}:
                    raise WorkflowError("所选账号仍在执行或等待恢复。")
                metadata = platform_by_id.get(remote["remote_platform"], {})
                connected = remote["status"] == 1 and auth == 1
                identity = {"name": remote["name"], "remote_id": remote["uid"] or remote["remote_id"], "logged_in": connected}
                a = {"id": account_id, "platform": remote["platform"], "label": old["label"] if old else remote["name"],
                    "status": "connected" if connected else "expired", "auth_revision": (old["auth_revision"] if old else 0) + 1,
                    "identity": identity, "checked_at": at_now(), "created_at": old["created_at"] if old else at_now(),
                    "operation": old.get("operation") if old else None, "adapter": ADAPTER,
                    "message": "已核验服务端授权；发布能力以当前实现范围为准。" if connected else "服务端授权不可用，请在服务端重新授权。",
                    "creation_key": "remote-" + account_id, "creation_digest": fingerprint(remote), "live_verified": False,
                    "external_id": remote["remote_id"], "external_platform": remote["remote_platform"],
                    "bridge_revision": cfg["revision"], "bridge_base": cfg["base_url"], "capabilities": deepcopy(metadata)}
                accounts[account_id] = a
                self.workspace.accounts.invalidate_tasks(state, account_id, "远端账号状态已刷新，请重新审核待发版本。")
                rows.append(deepcopy(a))
            return {"items": rows}

    def refresh_account(self, account_id, confirmed):
        if not confirmed:
            raise WorkflowError("请确认检查服务端账号授权。", 422)
        local = self.workspace.accounts.get(account_id)
        # Refresh metadata first, then validate the selected identity before import.
        status = self.discover(True)
        self.import_accounts(RemoteImport(discovery_id=status["discovery"]["id"], account_ids=[local["external_id"]], confirmed=True))
        return self.workspace.accounts.get(account_id)

    def problems(self, task, state):
        c = task["content"]
        a = state.get("accounts", {}).get(c["account_id"])
        cfg = self.config()
        errors = []
        if not a or a.get("adapter") != ADAPTER or a["platform"] != c["platform"]:
            return ["远端账号与渠道不匹配。"]
        if a.get("bridge_revision") != cfg.get("revision"):
            errors.append("服务配置已变化，请重新连接远端账号。")
        if a["status"] != "connected":
            errors.append("服务端账号授权尚未确认有效。")
        if (a.get("operation") or {}).get("state") in {"running", "recovery_required"}:
            errors.append("账号有未完成或等待恢复的操作。")
        try:
            if not a.get("checked_at") or (datetime.now(timezone.utc) - datetime.fromisoformat(a["checked_at"])).total_seconds() > 86400:
                errors.append("账号能力检查已超过 24 小时，请重新检查状态。")
        except ValueError:
            errors.append("账号检查时间无效，请重新连接。")
        if c["platform"] != "x":
            errors.append("此渠道已支持账号连接；发布选项、审核策略和媒体流程尚未全部接入。")
        p = a.get("capabilities") or {}
        if p.get("status") != "available" or p.get("auth_type") != "oauth2" or p.get("publish_capabilities", {}).get("supported") is not True:
            errors.append("远端平台未报告可用的 OAuth 发布能力。")
        if p.get("option_schema", {}).get("required"):
            errors.append("当前远端要求额外发布选项，需要继续适配后才能提交。")
        limits = p.get("content_limits", {})
        maximum = limits.get("maxBodyLength") or limits.get("maxTotalTextLength")
        if not isinstance(maximum, (int, float)) or maximum <= 0:
            errors.append("服务未返回可靠的正文长度限制，暂不提交。")
        elif len(c["body"]) > maximum:
            errors.append(f"正文超过服务当前报告的 {maximum} 字符限制。")
        if not c["body"].strip():
            errors.append("X 发布需要正文；标题仅作为 Ripple 的任务名称。")
        if c.get("tags"):
            errors.append("X 的话题请直接写入正文；独立话题字段应留空。")
        if limits.get("coverRequired"):
            errors.append("此平台要求专用封面，当前桥接尚不支持。")
        if c["media"] and not cfg.get("upload_hosts"):
            errors.append("需要先在设置中确认对象存储上传域名。")
        if any(Path(p).suffix.lower() in {".mp4", ".mov", ".webm"} for p in c["media"]):
            errors.append("第一版 X 桥接支持文字和图片；视频上传尚未验收。")
        max_images = limits.get("maxImages", 0)
        if not isinstance(max_images, (int, float)) or len(c["media"]) > max_images:
            errors.append("图片数量超过服务当前报告的限制。")
        for media in c["media_snapshot"]:
            try:
                actual = self.workspace.media_info(media["path"])
                if actual["sha256"] != media["sha256"]:
                    errors.append("素材已改变，请保存新版本。")
                maximum_size = p.get("media_rules", {}).get("maxImageSize")
                if maximum_size and actual["bytes"] > maximum_size:
                    errors.append("图片超过服务返回的大小限制。")
            except (WorkflowError, OSError):
                errors.append("素材无法读取或校验失败。")
        if c.get("scheduled_at") and datetime.fromisoformat(c["scheduled_at"]) <= self.workspace.clock():
            errors.append("排期已过期，请调整。")
        return errors

    def publish(self, task, account, paths):
        cfg = self.config()
        if cfg.get("revision") != account.get("bridge_revision") or not task["approval"].get("external_service_confirmed"):
            return {"state": "verification_required", "not_submitted": True, "message": "外部服务授权或配置已变化，未提交。"}
        directory = self.workspace.accounts.directory(account["id"]) / "operations" / task["operation_id"]
        client = self._client(cfg)
        submitted = False
        outcome = {"state": "verification_required", "not_submitted": True, "message": "远端准备未完成，未创建发布流程。"}
        flow_id = None
        try:
            remote, auth = client.account(account["external_id"])
            ident = public_account(remote)
            if not ident or auth != 1 or ident["status"] != 1 or ident["remote_platform"] != account["external_platform"] or (ident["uid"] or ident["remote_id"]) != account["identity"]["remote_id"]:
                raise WorkflowError("远端账号身份或授权已变化，停止提交。")
            current_platforms = [p for raw in client.platforms() if (p := public_platform(raw))]
            current = next((p for p in current_platforms if p['remote_platform'] == account['external_platform']), None)
            if current is None or fingerprint(current) != fingerprint(account.get('capabilities') or {}):
                raise WorkflowError('远端平台能力已变化，停止上传。请刷新账号状态并重新审核。')
            assets = [client.upload(Path(path), cfg["upload_hosts"]) for path in paths]
            payload = {"content": {"body": task["content"]["body"], "media": [{"url": a["url"]} for a in assets]},
                "publishAt": (datetime.now(timezone.utc) + timedelta(seconds=X_QUEUE_SECONDS)).isoformat(),
                "items": [{"platform": account["external_platform"], "accountId": account["external_id"]}]}
            atomic_write(directory / "remote-intent.json", {"task_id": task["id"], "version_id": task["version_id"],
                "operation_id": task["operation_id"], "payload_digest": fingerprint(payload), "at": at_now()})
            submitted = True
            result = client.request("POST", "/api/v2/channels/publish/flows", payload)
            if not isinstance(result, dict):
                raise RestError("发布流程回执结构不匹配。", outcome_unknown=True)
            flow_id = identifier(result.get("flowId"))
            # Persist the remote correlation before validating the remaining fields.
            outcome = {"state": "accepted", "message": "远端已创建发布流程，约 90 秒后进入平台队列。请核对执行回执。",
                       "flow_id": flow_id, "bridge_revision": cfg["revision"], "not_submitted": False}
            matches = [t for t in result.get("tasks", []) if t.get("accountId") == account["external_id"] and t.get("platform") == account["external_platform"]]
            if len(matches) == 1:
                outcome["remote_task_id"] = identifier(matches[0].get("id"))
                outcome["remote_status"] = matches[0].get("status")
            else:
                outcome["state"] = "unknown_result"
                outcome["message"] = "已取得远端流程标识，但任务关联尚未确认；请查询原流程，不能重发。"
        except Exception as exc:
            preparation_error = str(exc) if isinstance(exc, WorkflowError) else '远端账号或素材准备失败，请检查服务配置。'
            outcome = {"state": "unknown_result" if submitted else "verification_required", "not_submitted": not submitted,
                "message": "远端提交结果未知；未自动重发，请先核对原流程。" if submitted else '未创建发布流程：' + preparation_error,
                **({"flow_id": flow_id, "bridge_revision": cfg["revision"]} if flow_id else {})}
        finally:
            client.close()
        outcome.update(operation_id=task["operation_id"], task_id=task["id"], version_id=task["version_id"], evidence="aitoearn_rest")
        atomic_write(directory / "result.json", outcome)
        return outcome

    def query(self, task, account):
        cfg = self.config()
        receipt = task.get("receipt") or {}
        if not receipt.get("flow_id"):
            result = self.workspace.accounts.read_result(account["id"], task.get("operation_id", ""))
            if result and result.get("task_id") == task["id"] and result.get("version_id") == task["version_id"]:
                receipt = result
        if not receipt.get("flow_id"):
            return None  # No safe remote lookup key; do not issue another create.
        if cfg.get("revision") != receipt.get("bridge_revision"):
            raise WorkflowError("当前服务配置与原发布回执不一致，禁止查询其他服务。")
        client = self._client(cfg)
        try:
            result = client.request("GET", "/api/v2/channels/publish/flows/" + identifier(receipt["flow_id"]))
        finally:
            client.close()
        if not isinstance(result, dict) or result.get("flowId") != receipt["flow_id"]:
            raise WorkflowError("流程查询关联不匹配。", 502)
        matches = [t for t in result.get("tasks", []) if t.get("accountId") == account["external_id"] and t.get("platform") == account["external_platform"]
                   and (not receipt.get("remote_task_id") or t.get("id") == receipt["remote_task_id"])]
        if len(matches) != 1 or type(matches[0].get("status")) not in {int, float}:
            raise WorkflowError("流程内任务关联或状态未确认，保留原记录。", 502)
        remote = matches[0]
        state = "failed_terminal" if remote["status"] in {-1, 5, 9} else "verification_required" if remote["status"] == 8 else "accepted"
        outcome = {"state": state, "not_submitted": False, "flow_id": receipt["flow_id"], "bridge_revision": cfg["revision"],
            "remote_task_id": identifier(remote["id"]), "remote_status": remote["status"], "evidence": "aitoearn_rest_query",
            "message": f'已查询原远端流程，服务状态为 {remote["status"]}。公开可见状态仍需核对。',
            "operation_id": task["operation_id"], "task_id": task["id"], "version_id": task["version_id"]}
        try:
            link = remote.get("workLink", "")
            p = urlsplit(link)
            if p.scheme == "https" and p.hostname in RECEIPT_HOSTS.get(account["platform"], set()) and not p.username and not p.password:
                outcome["candidate_url"] = link[:2048]
        except ValueError:
            pass
        directory = self.workspace.accounts.directory(account["id"]) / "operations" / task["operation_id"]
        atomic_write(directory / "result.json", outcome)
        return outcome
