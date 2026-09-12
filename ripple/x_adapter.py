"""Ripple's direct X OAuth 2.0 PKCE account and publishing adapter.

The adapter is local-first: the X Developer App Client ID is private local
configuration, OAuth tokens and PKCE verifiers use Ripple's machine-local secret
store, and no third-party relay is required. Remote mutation is never retried implicitly.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode
import uuid

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .secrets import protect
from .tenancy import DEFAULT_WORKSPACE_ID
from .auth import server_security_config
from .publishing import WorkflowError, fingerprint

ADAPTER = "x-api"
AUTHORIZE_URL = "https://x.com/i/oauth2/authorize"
API_BASE = "https://api.x.com"
SCOPES = ("tweet.read", "tweet.write", "users.read", "media.write", "offline.access")
PENDING_TTL = 600
MAX_RESPONSE = 2 * 1024 * 1024
MAX_IMAGES = 4
MAX_IMAGE_BYTES = 5 * 1024 * 1024
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
CLIENT_ID = re.compile(r"^[\x21-\x7e]{6,256}$")


class XConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_id: str = Field(min_length=6, max_length=256)
    confirmed: bool = False

    @field_validator("client_id")
    @classmethod
    def valid_client_id(cls, value: str) -> str:
        if not CLIENT_ID.fullmatch(value):
            raise ValueError("invalid client id")
        return value


class XConnectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    confirmed: bool = False


class XApiError(WorkflowError):
    def __init__(self, message: str, *, status: int = 502, outcome_unknown: bool = False):
        super().__init__(message, status)
        self.outcome_unknown = outcome_unknown


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read(path: Path, limit: int = 512_000) -> dict:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def callback_url() -> str:
    security = server_security_config()
    if security.get("mode") == "server" and security.get("public_origin_valid"):
        return str(security["public_origin"]).rstrip("/") + "/api/ripple/x/oauth/callback"
    raw = os.environ.get("RIPPLE_PORT", os.environ.get("RIPPLE_PORT", "7860"))
    try:
        port = int(raw)
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        port = 7860
    return f"http://127.0.0.1:{port}/api/ripple/x/oauth/callback"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class XClient:
    def __init__(self, *, transport=None):
        self.http = httpx.Client(
            base_url=API_BASE,
            timeout=httpx.Timeout(30, connect=8),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self.http.close()

    @staticmethod
    def _decode(response: httpx.Response, *, mutation=False, ambiguous_5xx=False) -> dict:
        if not 200 <= response.status_code < 300:
            unknown = bool(mutation and ambiguous_5xx and response.status_code >= 500)
            status = 422 if response.status_code in {400, 401, 403, 404, 409, 422, 429} else 502
            raise XApiError(f"X API 返回 HTTP {response.status_code}。请检查应用权限、额度和账号状态。",
                            status=status, outcome_unknown=unknown)
        raw = response.content
        if len(raw) > MAX_RESPONSE:
            raise XApiError("X API 响应超过安全上限。", outcome_unknown=mutation)
        try:
            value = response.json()
        except (ValueError, TypeError):
            raise XApiError("X API 返回了无法解析的响应。", outcome_unknown=mutation) from None
        if not isinstance(value, dict):
            raise XApiError("X API 响应结构不匹配。", outcome_unknown=mutation)
        return value

    def _request(self, method: str, path: str, *, token: str | None = None, json_body=None,
                 form=None, mutation=False, ambiguous_5xx=False) -> dict:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            response = self.http.request(method, path, headers=headers, json=json_body, data=form)
        except httpx.HTTPError:
            raise XApiError("X API 连接中断；未自动重试。", outcome_unknown=mutation) from None
        return self._decode(response, mutation=mutation, ambiguous_5xx=ambiguous_5xx)

    def exchange(self, code: str, client_id: str, redirect_uri: str, verifier: str) -> dict:
        return self._request("POST", "/2/oauth2/token", form={
            "code": code, "grant_type": "authorization_code", "client_id": client_id,
            "redirect_uri": redirect_uri, "code_verifier": verifier,
        })

    def refresh(self, refresh_token: str, client_id: str) -> dict:
        return self._request("POST", "/2/oauth2/token", form={
            "refresh_token": refresh_token, "grant_type": "refresh_token", "client_id": client_id,
        })

    def me(self, token: str) -> dict:
        value = self._request("GET", "/2/users/me", token=token)
        data = value.get("data")
        if not isinstance(data, dict) or not str(data.get("id", "")) or not str(data.get("username", "")):
            raise XApiError("X 没有返回可确认的账号身份。")
        return {"id": str(data["id"])[:128], "username": str(data["username"])[:80],
                "name": str(data.get("name") or data["username"])[:120]}

    def upload_image(self, token: str, path: Path) -> str:
        size = path.stat().st_size
        if not 0 < size <= MAX_IMAGE_BYTES:
            raise XApiError("X 图片需非空且不超过 5 MiB。", status=422)
        media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        raw = path.read_bytes()
        value = self._request("POST", "/2/media/upload", token=token, mutation=True, json_body={
            "media": base64.b64encode(raw).decode("ascii"),
            "media_category": "tweet_image", "media_type": media_type,
        })
        data = value.get("data")
        media_id = str((data or {}).get("id") or "") if isinstance(data, dict) else ""
        if not media_id or len(media_id) > 128:
            raise XApiError("X 媒体上传回执缺少有效 ID。")
        return media_id

    def create_post(self, token: str, text: str, media_ids: list[str]) -> str:
        body: dict = {"text": text}
        if media_ids:
            body["media"] = {"media_ids": media_ids}
        value = self._request("POST", "/2/tweets", token=token, json_body=body,
                              mutation=True, ambiguous_5xx=True)
        data = value.get("data")
        post_id = str((data or {}).get("id") or "") if isinstance(data, dict) else ""
        if not post_id or len(post_id) > 128:
            # The remote mutation returned an unusable success body; submission may still exist.
            raise XApiError("X 创建作品后的回执无法关联，请人工核对。", outcome_unknown=True)
        return post_id


class XService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store
        self.private = workspace.private
        self.config_path = self.private / "integrations" / "x.json"
        self.pending_dir = self.private / "integrations" / "x-pending"
        self.client_factory = XClient

    def config(self) -> dict:
        return _read(self.config_path)

    def status(self) -> dict:
        cfg = self.config()
        return {"configured": bool(cfg.get("client_id")), "client_id": str(cfg.get("client_id") or ""),
                "callback_url": callback_url(), "scopes": list(SCOPES), "adapter": ADAPTER}

    def save(self, req: XConfigInput) -> dict:
        if not req.confirmed:
            raise WorkflowError("请确认保存 X Developer App Client ID。", 422)
        old = self.config().get("client_id")
        _atomic(self.config_path, {"client_id": req.client_id, "updated_at": _now()})
        invalidated: list[str] = []
        if old and old != req.client_id:
            with self.store.transaction() as state:
                for account in state.get("accounts", {}).values():
                    if account.get("adapter") == ADAPTER:
                        account.update(status="disconnected", identity=None, checked_at=None,
                                       auth_revision=account.get("auth_revision", 0) + 1,
                                       message="X Client ID 已变化，请重新连接账号。")
                        self.workspace.accounts.invalidate_tasks(state, account["id"], "X 应用配置变化，原审批失效。")
                        invalidated.append(account["id"])
            for account_id in invalidated:
                self._credentials_path(account_id).unlink(missing_ok=True)
        return self.status()

    def _credentials_path(self, account_id: str) -> Path:
        return self.workspace.accounts.directory(account_id) / "x-credentials.json"

    def _save_credentials(self, account_id: str, value: dict) -> None:
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        protected = base64.b64encode(protect(raw)).decode("ascii")
        _atomic(self._credentials_path(account_id), {"protected": protected, "updated_at": _now()})

    def _credentials(self, account_id: str) -> dict:
        row = _read(self._credentials_path(account_id), 64_000)
        try:
            blob = base64.b64decode(row["protected"], validate=True)
            value = json.loads(protect(blob, decrypt=True).decode("utf-8"))
            if not isinstance(value, dict) or not value.get("access_token"):
                raise ValueError()
            return value
        except (KeyError, ValueError, UnicodeError, TypeError):
            raise WorkflowError("X 本地凭证无法读取，请重新连接账号。", 503) from None

    def _store_token(self, account_id: str, token: dict, old: dict | None = None) -> dict:
        access = str(token.get("access_token") or "")
        if not access:
            raise XApiError("X OAuth 没有返回 access token。")
        refresh = str(token.get("refresh_token") or (old or {}).get("refresh_token") or "")
        expires_in = token.get("expires_in", 7200)
        try:
            seconds = max(60, min(int(expires_in), 90 * 24 * 3600))
        except (TypeError, ValueError):
            seconds = 7200
        value = {"access_token": access, "refresh_token": refresh,
                 "expires_at": time.time() + seconds,
                 "scope": str(token.get("scope") or " ".join(SCOPES))[:1000]}
        self._save_credentials(account_id, value)
        return value

    def _token(self, account_id: str) -> tuple[str, dict]:
        creds = self._credentials(account_id)
        if float(creds.get("expires_at") or 0) > time.time() + 60:
            return str(creds["access_token"]), creds
        refresh = str(creds.get("refresh_token") or "")
        if not refresh:
            raise WorkflowError("X 授权已过期且没有 refresh token，请重新连接。", 422)
        cfg = self.config()
        if not cfg.get("client_id"):
            raise WorkflowError("请先配置 X Developer App Client ID。", 422)
        client = self.client_factory()
        try:
            updated = client.refresh(refresh, cfg["client_id"])
        finally:
            client.close()
        creds = self._store_token(account_id, updated, creds)
        return str(creds["access_token"]), creds

    def _pending_path(self, state: str) -> Path:
        return self.pending_dir / (hashlib.sha256(state.encode("ascii")).hexdigest() + ".json")

    def _start(self, account: dict) -> dict:
        cfg = self.config()
        client_id = str(cfg.get("client_id") or "")
        if not client_id:
            raise WorkflowError("请先配置 X Developer App Client ID。", 422)
        state_token = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        pending = {"account_id": account["id"], "created_at": time.time(),
                   "auth_revision": account.get("auth_revision", 0),
                   "operation_id": (account.get("operation") or {}).get("id"),
                   "protected_verifier": base64.b64encode(protect(verifier.encode("ascii"))).decode("ascii")}
        _atomic(self._pending_path(state_token), pending)
        query = urlencode({
            "response_type": "code", "client_id": client_id, "redirect_uri": callback_url(),
            "scope": " ".join(SCOPES), "state": state_token,
            "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
        })
        return {"account": self.workspace.accounts.get(account["id"]), "authorize_url": AUTHORIZE_URL + "?" + query,
                "callback_url": callback_url()}

    def connect_new(self, req: XConnectInput) -> dict:
        if not req.confirmed:
            raise WorkflowError("请确认允许 Ripple 打开 X OAuth 授权页。", 422)
        digest = fingerprint({"platform": "x", "label": req.label, "adapter": ADAPTER})
        with self.store.transaction() as state:
            accounts = state.setdefault("accounts", {})
            account = None
            for item in accounts.values():
                if item.get("creation_key") == req.idempotency_key:
                    if item.get("creation_digest") != digest:
                        raise WorkflowError("账号创建键已用于其他输入。")
                    account = item
                    break
            if account is None:
                if len(accounts) >= 100:
                    raise WorkflowError("最多保存 100 个账号。", 422)
                account = {"id": uuid.uuid4().hex, "workspace_id": DEFAULT_WORKSPACE_ID, "platform": "x", "label": req.label,
                           "status": "connecting", "auth_revision": 1, "identity": None, "checked_at": None,
                           "operation": {"id": uuid.uuid4().hex, "kind": "login", "state": "running", "started_at": _now()},
                           "created_at": _now(), "message": "等待 X OAuth 授权", "creation_key": req.idempotency_key,
                           "creation_digest": digest, "adapter": ADAPTER, "live_verified": False}
                accounts[account["id"]] = account
            else:
                account.update(status="connecting", identity=None, checked_at=None,
                               auth_revision=account.get("auth_revision", 0) + 1,
                               operation={"id": uuid.uuid4().hex, "kind": "login", "state": "running", "started_at": _now()},
                               message="等待 X OAuth 授权")
                self.workspace.accounts.invalidate_tasks(state, account["id"], "X 账号重新授权，原审批失效。")
            work = dict(account)
        return self._start(work)

    def reconnect(self, account_id: str, confirmed: bool) -> dict:
        if not confirmed:
            raise WorkflowError("请确认重新连接 X 账号。", 422)
        with self.store.transaction() as state:
            account = self.workspace.accounts._account(state, account_id)
            if account.get("adapter") != ADAPTER or account.get("platform") != "x":
                raise WorkflowError("这不是 Ripple Native X 账号。", 422)
            if (account.get("operation") or {}).get("state") in {"recovery_required"}:
                raise WorkflowError("账号仍有待核对的发布操作。")
            account.update(status="connecting", identity=None, checked_at=None,
                           auth_revision=account.get("auth_revision", 0) + 1,
                           operation={"id": uuid.uuid4().hex, "kind": "login", "state": "running", "started_at": _now()},
                           message="等待 X OAuth 授权")
            self.workspace.accounts.invalidate_tasks(state, account_id, "X 账号重新授权，原审批失效。")
            work = dict(account)
        return self._start(work)

    def callback(self, state_token: str, code: str) -> dict:
        if not state_token or len(state_token) > 256 or not code or len(code) > 4096:
            raise WorkflowError("X OAuth 回调参数无效。", 422)
        path = self._pending_path(state_token)
        pending = _read(path, 32_000)
        if not pending or time.time() - float(pending.get("created_at") or 0) > PENDING_TTL:
            path.unlink(missing_ok=True)
            raise WorkflowError("X OAuth 授权已过期，请返回 Ripple 重新连接。", 422)
        used = path.with_name(path.name + ".used-" + uuid.uuid4().hex)
        try:
            os.replace(path, used)
        except OSError:
            raise WorkflowError("X OAuth 状态已使用或无效，请重新连接。", 422) from None
        account_id = str(pending.get("account_id") or "")
        validated = False
        try:
            with self.store.transaction(write=False) as store:
                current = self.workspace.accounts._account(store, account_id)
                operation = current.get("operation") or {}
                if (current.get("adapter") != ADAPTER or current.get("status") != "connecting"
                        or current.get("auth_revision") != pending.get("auth_revision")
                        or operation.get("id") != pending.get("operation_id") or operation.get("state") != "running"):
                    raise WorkflowError("X OAuth 状态已失效，请返回 Ripple 重新连接。", 422)
            validated = True
            verifier = protect(base64.b64decode(pending["protected_verifier"], validate=True), decrypt=True).decode("ascii")
            cfg = self.config()
            client_id = str(cfg.get("client_id") or "")
            if not client_id:
                raise WorkflowError("X Client ID 配置已变化，请重新连接。", 422)
            client = self.client_factory()
            try:
                token = client.exchange(code, client_id, callback_url(), verifier)
                access = str(token.get("access_token") or "")
                if not access:
                    raise XApiError("X OAuth 没有返回 access token。")
                identity = client.me(access)
            finally:
                client.close()
            with self.store.transaction() as store:
                account = self.workspace.accounts._account(store, account_id)
                operation = account.get("operation") or {}
                if (account.get("adapter") != ADAPTER or account.get("status") != "connecting"
                        or account.get("auth_revision") != pending.get("auth_revision")
                        or operation.get("id") != pending.get("operation_id") or operation.get("state") != "running"):
                    raise WorkflowError("X OAuth 状态已失效，请返回 Ripple 重新连接。", 422)
                self._store_token(account_id, token)
                account.update(status="connected", checked_at=_now(),
                               identity={"name": identity["username"], "display_name": identity["name"],
                                         "remote_id": identity["id"], "logged_in": True},
                               message="X OAuth 已连接；发布前仍需逐条审核。")
                if account.get("operation"):
                    account["operation"]["state"] = "finished"
                return self.workspace.accounts.project(account)
        except Exception:
            if validated:
                with self.store.transaction() as store:
                    account = store.get("accounts", {}).get(account_id)
                    if account and account.get("adapter") == ADAPTER:
                        account.update(status="error", identity=None, checked_at=_now(), message="X OAuth 未完成，请重新连接。")
                        if account.get("operation"):
                            account["operation"]["state"] = "finished"
                try:
                    self._credentials_path(account_id).unlink(missing_ok=True)
                except OSError:
                    pass
            raise
        finally:
            used.unlink(missing_ok=True)

    def probe(self, account_id: str, confirmed: bool) -> dict:
        if not confirmed:
            raise WorkflowError("请确认检查 X 账号状态。", 422)
        try:
            token, _ = self._token(account_id)
            client = self.client_factory()
            try:
                identity = client.me(token)
            finally:
                client.close()
        except Exception:
            with self.store.transaction() as state:
                account = self.workspace.accounts._account(state, account_id)
                account.update(status="expired", identity=None, checked_at=_now(),
                               auth_revision=account.get("auth_revision", 0) + 1,
                               message="X 授权不可用，请重新连接。")
                self.workspace.accounts.invalidate_tasks(state, account_id, "X 授权检查失败，原审批失效。")
            raise
        with self.store.transaction() as state:
            account = self.workspace.accounts._account(state, account_id)
            previous = account.get("identity")
            current = {"name": identity["username"], "display_name": identity["name"],
                       "remote_id": identity["id"], "logged_in": True}
            account.update(status="connected", identity=current, checked_at=_now(), message="X OAuth 授权有效。")
            if previous and previous.get("remote_id") != current["remote_id"]:
                account["auth_revision"] += 1
                self.workspace.accounts.invalidate_tasks(state, account_id, "X 账号身份变化，原审批失效。")
            return self.workspace.accounts.project(account)

    def disconnect(self, account_id: str, confirmed: bool) -> dict:
        if not confirmed:
            raise WorkflowError("请确认断开 X 账号。", 422)
        with self.store.transaction() as state:
            account = self.workspace.accounts._account(state, account_id)
            if account.get("adapter") != ADAPTER:
                raise WorkflowError("这不是 Ripple Native X 账号。", 422)
            if (account.get("operation") or {}).get("state") in {"running", "recovery_required"} and (account.get("operation") or {}).get("kind") == "publish":
                raise WorkflowError("X 发布操作仍在进行或待核对，不能断开。")
            account.update(status="disconnected", identity=None, checked_at=None,
                           auth_revision=account.get("auth_revision", 0) + 1, operation=None,
                           message="已在 Ripple 中断开；如需撤销平台授权，请到 X 应用设置操作。")
            self.workspace.accounts.invalidate_tasks(state, account_id, "X 账号已断开，原审批失效。")
            projected = self.workspace.accounts.project(account)
        try:
            self._credentials_path(account_id).unlink(missing_ok=True)
        except OSError:
            pass
        return projected

    def purge_pending(self, account_id: str) -> int:
        removed = 0
        try:
            entries = list(self.pending_dir.iterdir())[:1000]
        except OSError:
            return 0
        for path in entries:
            if not path.is_file():
                continue
            row = _read(path, 32_000)
            if str(row.get("account_id") or "") != account_id:
                continue
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except OSError:
                pass
        return removed

    def problems(self, task: dict, state: dict) -> list[str]:
        content = task["content"]
        account = state.get("accounts", {}).get(content.get("account_id"))
        errors: list[str] = []
        if not self.config().get("client_id"):
            errors.append("X Developer App Client ID 尚未配置。")
        if not account or account.get("adapter") != ADAPTER or account.get("platform") != "x":
            return errors + ["请选择已连接的 Ripple Native X 账号。"]
        if account.get("status") != "connected":
            errors.append("X 账号尚未连接或授权已失效。")
        if (account.get("operation") or {}).get("state") in {"running", "recovery_required"}:
            errors.append("X 账号有未结束或待核对的操作。")
        body = str(content.get("body") or "")
        if not body.strip():
            errors.append("X 发布需要正文；标题仅作为 Ripple 本地任务名称。")
        if len(body) > 280:
            errors.append("Ripple Native X 首版正文限制为 280 字符。")
        if content.get("tags"):
            errors.append("X 的话题请直接写入正文；独立话题字段应留空。")
        if len(content.get("media") or []) > MAX_IMAGES:
            errors.append("X 首版最多支持 4 张图片。")
        for item in content.get("media_snapshot") or []:
            suffix = Path(item["path"]).suffix.lower()
            if suffix not in SUPPORTED_IMAGE_SUFFIXES:
                errors.append("X 首版只支持 PNG/JPEG/WebP 图片，不支持 GIF 或视频。")
                continue
            if int(item.get("bytes") or 0) > MAX_IMAGE_BYTES:
                errors.append("X 首版单张图片不能超过 5 MiB。")
        for item in content.get("media_snapshot") or []:
            try:
                actual = self.workspace.media_info(item["path"])
                if actual["sha256"] != item.get("sha256") or actual["bytes"] != item.get("bytes"):
                    errors.append("X 素材已变化，请保存新版本。")
            except (WorkflowError, OSError):
                errors.append("X 素材无法读取或已失效。")
        scheduled = content.get("scheduled_at")
        if scheduled and datetime.fromisoformat(scheduled) <= self.workspace.clock():
            errors.append("排期已过期，请调整时间。")
        return errors

    def publish(self, task: dict, account: dict, paths: list[str]) -> dict:
        try:
            token, _ = self._token(account["id"])
            client = self.client_factory()
            try:
                identity = client.me(token)
                if identity["id"] != (account.get("identity") or {}).get("remote_id"):
                    return {"state": "verification_required", "not_submitted": True,
                            "message": "X 账号身份已变化，未提交。"}
                media_ids: list[str] = []
                for raw in paths:
                    path = Path(raw)
                    if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
                        return {"state": "verification_required", "not_submitted": True,
                                "message": "X 素材类型不在首版支持范围内，未提交。"}
                    media_ids.append(client.upload_image(token, path))
                intent = self.workspace.accounts.directory(account["id"]) / "operations" / task["operation_id"] / "remote-intent.json"
                payload = {"text": task["content"]["body"], "media_ids": media_ids}
                _atomic(intent, {"task_id": task["id"], "version_id": task["version_id"],
                                 "operation_id": task["operation_id"], "payload_digest": fingerprint(payload), "at": _now()})
                try:
                    post_id = client.create_post(token, payload["text"], media_ids)
                except XApiError as exc:
                    return {"state": "unknown_result" if exc.outcome_unknown else "verification_required",
                            "not_submitted": not exc.outcome_unknown,
                            "message": "X 提交结果无法确认，请人工核对；不会自动重试。" if exc.outcome_unknown else str(exc)}
                username = identity["username"]
                return {"state": "accepted", "not_submitted": False, "post_id": post_id,
                        "candidate_url": f"https://x.com/{username}/status/{post_id}",
                        "message": "X API 已返回作品 ID；请在平台人工核对公开状态。", "evidence": "x-api-post-id"}
            finally:
                client.close()
        except XApiError as exc:
            return {"state": "verification_required", "not_submitted": True, "message": str(exc)}
        except WorkflowError as exc:
            return {"state": "verification_required", "not_submitted": True, "message": str(exc)}
        except Exception:
            return {"state": "verification_required", "not_submitted": True,
                    "message": "X 发布准备失败，未确认创建作品。"}

    def query(self, task: dict, account: dict) -> dict | None:
        receipt = task.get("receipt") or {}
        post_id = str(receipt.get("post_id") or "")
        if not post_id:
            return None
        return {"state": "accepted", "not_submitted": False, "post_id": post_id,
                "candidate_url": receipt.get("candidate_url"),
                "message": "已有 X 作品 ID；Ripple 不重复提交，请人工核对公开状态。", "evidence": "x-api-post-id"}
