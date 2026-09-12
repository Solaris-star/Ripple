"""Ripple deployment identity, workspace ownership and session authentication.

Local mode keeps the existing single-user loopback experience. Server mode requires
an authenticated user session and a workspace membership. Passwords and session
secrets are never stored in plaintext.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
import uuid
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .publishing import WorkflowError
from .tenancy import DEFAULT_WORKSPACE_ID

SESSION_COOKIE = "ripple_session"
CSRF_COOKIE = "ripple_csrf"
SESSION_TTL_SECONDS = 30 * 24 * 60 * 60
LOGIN_WINDOW_SECONDS = 10 * 60
LOGIN_MAX_ATTEMPTS = 10
WORKSPACE_SCOPED_COLLECTIONS = (
    "contents", "variants", "variant_batches", "tasks", "accounts", "execution_nodes",
    "execution_node_commands", "interactions", "interaction_sources", "operation_results",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def deployment_mode() -> str:
    raw = os.environ.get("RIPPLE_DEPLOYMENT_MODE")
    if raw is None or not raw.strip():
        return "local"
    value = raw.strip().lower()
    if value not in {"local", "server"}:
        raise RuntimeError("RIPPLE_DEPLOYMENT_MODE must be local or server; refusing an ambiguous security mode.")
    return value


def bootstrap_code() -> str:
    return (os.environ.get("RIPPLE_BOOTSTRAP_CODE") or "").strip()


def public_origin() -> str:
    return (os.environ.get("RIPPLE_PUBLIC_ORIGIN") or "").strip().rstrip("/")


def server_security_config() -> dict:
    mode = deployment_mode()
    origin = public_origin()
    hosts = [value.strip() for value in (os.environ.get("RIPPLE_TRUSTED_HOSTS") or "").split(",") if value.strip()]
    parsed = urlsplit(origin) if origin else None
    origin_valid = bool(parsed and parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                        and not parsed.query and not parsed.fragment and parsed.path in {"", "/"})
    if origin_valid and parsed and parsed.hostname not in hosts:
        hosts.append(parsed.hostname)
    if mode == "local":
        hosts = list(dict.fromkeys(["localhost", "127.0.0.1", "[::1]", *hosts]))
    return {"mode": mode, "public_origin": origin, "public_origin_valid": origin_valid,
            "trusted_hosts": hosts, "secure_cookie": mode == "server" and origin_valid}


def _hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _password_hash(password: str, salt_hex: str | None = None) -> tuple[str, str]:
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return salt.hex(), digest.hex()


def _safe_user(item: dict) -> dict:
    return {key: deepcopy(value) for key, value in item.items() if key not in {"password_hash", "password_salt"}}


class OwnerSetupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9._@+-]+$")
    email: str = Field(default="", max_length=200)
    password: SecretStr
    setup_code: SecretStr
    workspace_name: str = Field(default="Ripple Workspace", min_length=1, max_length=100)
    confirmed: bool = False

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: SecretStr):
        raw = value.get_secret_value()
        if len(raw) < 12 or len(raw) > 256:
            raise ValueError("密码长度必须为 12-256 个字符")
        return value


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    username: str = Field(min_length=1, max_length=200)
    password: SecretStr


class UserCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    username: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9._@+-]+$")
    email: str = Field(default="", max_length=200)
    password: SecretStr
    role: str = Field(default="member", pattern=r"^(admin|member)$")

    @field_validator("password")
    @classmethod
    def strong_password(cls, value: SecretStr):
        raw = value.get_secret_value()
        if len(raw) < 12 or len(raw) > 256:
            raise ValueError("密码长度必须为 12-256 个字符")
        return value


class UserDeleteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False


class AuthService:
    def __init__(self, store):
        self.store = store
        self.mode = deployment_mode()
        self._attempts: dict[str, list[float]] = {}
        self._attempt_guard = threading.Lock()
        self._bootstrap_workspace()

    def _bootstrap_workspace(self):
        with self.store.transaction() as state:
            workspaces = state.setdefault("workspaces", {})
            workspaces.setdefault(DEFAULT_WORKSPACE_ID, {
                "id": DEFAULT_WORKSPACE_ID, "name": "Ripple Workspace", "created_at": _now_iso(),
            })
            state.setdefault("users", {})
            state.setdefault("memberships", {})
            state.setdefault("auth_sessions", {})
            # Existing single-user records become owned by the default workspace. This
            # is metadata-only and keeps legacy identifiers/relationships untouched.
            for collection in WORKSPACE_SCOPED_COLLECTIONS:
                rows = state.get(collection)
                if not isinstance(rows, dict):
                    continue
                for item in rows.values():
                    if isinstance(item, dict):
                        item.setdefault("workspace_id", DEFAULT_WORKSPACE_ID)
            snapshots = state.get("xhs_snapshots")
            if isinstance(snapshots, list):
                for item in snapshots:
                    if isinstance(item, dict):
                        item.setdefault("workspace_id", DEFAULT_WORKSPACE_ID)

    def security(self) -> dict:
        cfg = server_security_config()
        cfg["configured"] = self.mode == "local" or bool(cfg["public_origin_valid"] and cfg["trusted_hosts"] and cfg["secure_cookie"])
        return cfg

    def _membership(self, state: dict, user_id: str, workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict | None:
        return next((row for row in state.get("memberships", {}).values()
                     if row.get("user_id") == user_id and row.get("workspace_id") == workspace_id), None)

    def _session_user(self, state: dict, token: str) -> tuple[dict, dict, dict] | None:
        if not token:
            return None
        digest = _hash_token(token)
        session = state.get("auth_sessions", {}).get(digest)
        if not session or float(session.get("expires_at") or 0) <= time.time():
            return None
        user = state.get("users", {}).get(session.get("user_id"))
        if not user or user.get("disabled"):
            return None
        membership = self._membership(state, user["id"], session.get("workspace_id") or DEFAULT_WORKSPACE_ID)
        if not membership:
            return None
        return user, membership, session

    def authenticate(self, token: str) -> dict | None:
        if self.mode == "local":
            return {"id": "local", "username": "Local User", "email": "", "role": "owner",
                    "workspace_id": DEFAULT_WORKSPACE_ID, "workspace_name": "Ripple Workspace", "local": True}
        with self.store.transaction(write=False) as state:
            found = self._session_user(state, token)
            if not found:
                return None
            user, membership, session = found
            workspace = state.get("workspaces", {}).get(membership["workspace_id"], {})
            return {**_safe_user(user), "role": membership["role"], "workspace_id": membership["workspace_id"],
                    "workspace_name": workspace.get("name", "Ripple Workspace"), "local": False,
                    "session_id": session.get("id", "")}

    def csrf_valid(self, token: str, csrf_value: str) -> bool:
        if self.mode == "local":
            return True
        if not token or not csrf_value:
            return False
        with self.store.transaction(write=False) as state:
            found = self._session_user(state, token)
            if not found:
                return False
            session = found[2]
            return hmac.compare_digest(str(session.get("csrf_hash") or ""), _hash_token(csrf_value))

    def setup_required(self) -> bool:
        if self.mode == "local":
            return False
        with self.store.transaction(write=False) as state:
            return not any(not row.get("disabled") for row in state.get("users", {}).values())

    def status(self, token: str = "") -> dict:
        user = self.authenticate(token)
        security = self.security()
        return {"mode": self.mode, "local": self.mode == "local", "setup_required": self.setup_required(),
                "authenticated": bool(user), "user": user, "security": {
                    "configured": security["configured"], "secure_cookie": security["secure_cookie"],
                    "public_origin": security["public_origin"],
                    "bootstrap_configured": self.mode == "local" or len(bootstrap_code()) >= 24,
                }}

    def _require_server_security(self):
        if self.mode != "server":
            raise WorkflowError("本机模式不需要创建 Ripple 登录账号。", 409)
        cfg = self.security()
        if not cfg["configured"]:
            raise WorkflowError("Server 模式必须配置 HTTPS 的 RIPPLE_PUBLIC_ORIGIN 和可信 Host 后才能创建管理员。", 503)

    def _new_session(self, state: dict, user_id: str, workspace_id: str) -> dict:
        now = time.time()
        sessions = state.setdefault("auth_sessions", {})
        for key, row in list(sessions.items()):
            if float(row.get("expires_at") or 0) <= now:
                sessions.pop(key, None)
        token = secrets.token_urlsafe(48)
        csrf = secrets.token_urlsafe(32)
        digest = _hash_token(token)
        sessions[digest] = {
            "id": uuid.uuid4().hex, "user_id": user_id, "workspace_id": workspace_id,
            "csrf_hash": _hash_token(csrf), "created_at": now, "last_seen": now,
            "expires_at": now + SESSION_TTL_SECONDS,
        }
        return {"session_token": token, "csrf_token": csrf, "max_age": SESSION_TTL_SECONDS}

    def setup(self, req: OwnerSetupInput) -> dict:
        self._require_server_security()
        expected = bootstrap_code()
        if len(expected) < 24:
            raise WorkflowError("Server 首次初始化前必须配置至少 24 字符的 RIPPLE_BOOTSTRAP_CODE。", 503)
        if not req.setup_code.get_secret_value() or not hmac.compare_digest(expected, req.setup_code.get_secret_value()):
            raise WorkflowError("Server 初始化码无效。", 403)
        if not req.confirmed:
            raise WorkflowError("请确认创建 Server 管理员。", 422)
        with self.store.transaction() as state:
            if any(not row.get("disabled") for row in state.get("users", {}).values()):
                raise WorkflowError("Ripple Server 已完成管理员初始化。", 409)
            workspace = state["workspaces"][DEFAULT_WORKSPACE_ID]
            workspace["name"] = req.workspace_name
            user_id = uuid.uuid4().hex
            salt, password_hash = _password_hash(req.password.get_secret_value())
            user = {"id": user_id, "username": req.username, "email": req.email, "password_salt": salt,
                    "password_hash": password_hash, "disabled": False, "created_at": _now_iso()}
            state["users"][user_id] = user
            membership_id = uuid.uuid4().hex
            state["memberships"][membership_id] = {"id": membership_id, "user_id": user_id,
                "workspace_id": DEFAULT_WORKSPACE_ID, "role": "owner", "created_at": _now_iso()}
            session = self._new_session(state, user_id, DEFAULT_WORKSPACE_ID)
            return {"user": {**_safe_user(user), "role": "owner", "workspace_id": DEFAULT_WORKSPACE_ID,
                             "workspace_name": workspace["name"]}, **session}

    def _login_key(self, username: str, client_key: str) -> str:
        return hashlib.sha256(f"{username.casefold()}|{client_key}".encode()).hexdigest()

    def check_login_rate(self, username: str, client_key: str):
        key = self._login_key(username, client_key)
        now = time.time()
        with self._attempt_guard:
            values = [ts for ts in self._attempts.get(key, []) if now - ts <= LOGIN_WINDOW_SECONDS]
            self._attempts[key] = values
            if len(values) >= LOGIN_MAX_ATTEMPTS:
                raise WorkflowError("登录尝试过多，请稍后再试。", 429)

    def _record_failed_login(self, username: str, client_key: str):
        key = self._login_key(username, client_key)
        with self._attempt_guard:
            self._attempts.setdefault(key, []).append(time.time())

    def login(self, req: LoginInput, client_key: str = "") -> dict:
        self._require_server_security()
        self.check_login_rate(req.username, client_key)
        candidate = req.username.casefold()
        with self.store.transaction() as state:
            user = next((row for row in state.get("users", {}).values()
                         if not row.get("disabled") and (str(row.get("username", "")).casefold() == candidate
                         or str(row.get("email", "")).casefold() == candidate)), None)
            valid = False
            if user:
                _, digest = _password_hash(req.password.get_secret_value(), str(user.get("password_salt") or ""))
                valid = hmac.compare_digest(str(user.get("password_hash") or ""), digest)
            if not valid:
                self._record_failed_login(req.username, client_key)
                raise WorkflowError("用户名或密码错误。", 401)
            membership = self._membership(state, user["id"])
            if not membership:
                raise WorkflowError("用户没有可用 Workspace。", 403)
            session = self._new_session(state, user["id"], membership["workspace_id"])
            workspace = state["workspaces"][membership["workspace_id"]]
            return {"user": {**_safe_user(user), "role": membership["role"],
                             "workspace_id": membership["workspace_id"], "workspace_name": workspace["name"]}, **session}

    def logout(self, token: str) -> dict:
        if self.mode == "local":
            return {"ok": True}
        digest = _hash_token(token) if token else ""
        with self.store.transaction() as state:
            state.get("auth_sessions", {}).pop(digest, None)
        return {"ok": True}

    def require_role(self, token: str, roles: set[str]) -> dict:
        user = self.authenticate(token)
        if not user:
            raise WorkflowError("请先登录 Ripple。", 401)
        if user.get("role") not in roles:
            raise WorkflowError("当前用户没有此管理权限。", 403)
        return user

    def list_users(self, token: str) -> dict:
        current = self.require_role(token, {"owner", "admin"})
        with self.store.transaction(write=False) as state:
            rows = []
            for membership in state.get("memberships", {}).values():
                if membership.get("workspace_id") != current["workspace_id"]:
                    continue
                user = state.get("users", {}).get(membership.get("user_id"))
                if user:
                    rows.append({**_safe_user(user), "role": membership["role"]})
            return {"items": sorted(rows, key=lambda row: row["created_at"])}

    def create_user(self, token: str, req: UserCreateInput) -> dict:
        current = self.require_role(token, {"owner", "admin"})
        raw = req.password.get_secret_value()
        with self.store.transaction() as state:
            needle = req.username.casefold()
            if any(str(row.get("username", "")).casefold() == needle for row in state.get("users", {}).values()):
                raise WorkflowError("用户名已存在。", 409)
            user_id = uuid.uuid4().hex
            salt, password_hash = _password_hash(raw)
            user = {"id": user_id, "username": req.username, "email": req.email, "password_salt": salt,
                    "password_hash": password_hash, "disabled": False, "created_at": _now_iso()}
            state["users"][user_id] = user
            mid = uuid.uuid4().hex
            state["memberships"][mid] = {"id": mid, "user_id": user_id, "workspace_id": current["workspace_id"],
                "role": req.role, "created_at": _now_iso()}
            return {**_safe_user(user), "role": req.role}

    def delete_user(self, token: str, user_id: str, confirmed: bool) -> dict:
        current = self.require_role(token, {"owner"})
        if not confirmed:
            raise WorkflowError("请确认移除该用户。", 422)
        if user_id == current["id"]:
            raise WorkflowError("不能移除当前登录的 Owner。", 409)
        with self.store.transaction() as state:
            user = state.get("users", {}).get(user_id)
            if not user:
                raise WorkflowError("用户不存在。", 404)
            memberships = state.get("memberships", {})
            relevant = [key for key, row in memberships.items() if row.get("user_id") == user_id and row.get("workspace_id") == current["workspace_id"]]
            if not relevant:
                raise WorkflowError("用户不属于当前 Workspace。", 404)
            for key in relevant:
                memberships.pop(key, None)
            for key, session in list(state.get("auth_sessions", {}).items()):
                if session.get("user_id") == user_id:
                    state["auth_sessions"].pop(key, None)
            user["disabled"] = True
            return {"id": user_id, "deleted": True}
