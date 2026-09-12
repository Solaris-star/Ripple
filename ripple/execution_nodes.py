"""Browser execution-node registry and bounded command protocol.

The Ripple server owns workflow state. Browser profiles stay on the node that
executes browser work. The protocol deliberately exposes only a closed command
vocabulary; it is not a remote shell.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import secrets
import socket
import subprocess
import threading
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .catalog import available_browser_channels, browser_executable, browser_label, interactive_browser_channels
from .publishing import WorkflowError
from .tenancy import DEFAULT_WORKSPACE_ID

LOCAL_NODE_ID = "local"
NODE_TTL_SECONDS = 45
PAIRING_TTL_SECONDS = 300
ALLOWED_CAPABILITIES = {
    "browser.interactive", "browser.automation", "browser.chrome", "browser.edge", "browser.chromium",
    "x.login", "x.probe", "x.publish",
}
ALLOWED_COMMANDS = {"login.start", "account.probe", "publish.execute"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_host_name() -> str:
    value = socket.gethostname().strip()[:80]
    return value or "Local Browser Node"


class PairingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False


class NodeRegisterInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    pairing_code: str = Field(min_length=6, max_length=32, pattern=r"^[A-Z0-9-]+$")
    name: str = Field(min_length=1, max_length=80)
    platform: str = Field(min_length=1, max_length=40)
    capabilities: list[str] = Field(default_factory=list, max_length=32)
    browsers: list[str] = Field(default_factory=list, max_length=8)

    @field_validator("capabilities")
    @classmethod
    def validate_capabilities(cls, values: list[str]):
        if any(value not in ALLOWED_CAPABILITIES for value in values):
            raise ValueError("包含未授权的执行节点能力")
        return list(dict.fromkeys(values))

    @field_validator("browsers")
    @classmethod
    def validate_browsers(cls, values: list[str]):
        allowed = {"msedge", "chrome", "chromium"}
        if any(value not in allowed for value in values):
            raise ValueError("未知浏览器通道")
        return list(dict.fromkeys(values))


class NodeAuthInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    node_token: SecretStr = Field(min_length=20, max_length=512)


class NodeClaimInput(NodeAuthInput):
    limit: int = Field(default=1, ge=1, le=5)


class NodeIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    logged_in: bool = False
    name: str = Field(default="", max_length=80)
    remote_id: str = Field(default="", max_length=100)


class NodeResultInput(NodeAuthInput):
    state: str = Field(pattern=r"^(connected|disconnected|expired|waiting_user|verification_required|accepted|unknown_result|failed_terminal|error)$")
    message: str = Field(default="", max_length=500)
    identity: NodeIdentity | None = None
    not_submitted: bool | None = None
    evidence: str | None = Field(default=None, max_length=80)


class ExecutionNodeService:
    def __init__(self, store, private: Path):
        self.store = store
        self.private = private.resolve()
        self._pairings: dict[str, tuple[float, str]] = {}
        self._guard = threading.Lock()

    def local(self, workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict:
        browsers = available_browser_channels()
        interactive = interactive_browser_channels()
        capabilities = ["browser.automation"] if browsers else []
        if interactive:
            capabilities.append("browser.interactive")
        if "msedge" in browsers:
            capabilities.append("browser.edge")
        if "chrome" in browsers:
            capabilities.append("browser.chrome")
        if "chromium" in browsers:
            capabilities.append("browser.chromium")
        if browsers:
            capabilities.extend(["x.probe", "x.publish"])
        if interactive:
            capabilities.append("x.login")
        return {
            "id": LOCAL_NODE_ID,
            "workspace_id": workspace_id,
            "name": _safe_host_name(),
            "kind": "local",
            "platform": os.name,
            "online": True,
            "capabilities": list(dict.fromkeys(capabilities)),
            "browsers": browsers,
            "interactive_browsers": interactive,
            "last_seen": utc_now(),
        }

    @staticmethod
    def _public_remote(item: dict) -> dict:
        value = {k: deepcopy(v) for k, v in item.items() if k != "token_hash"}
        last = float(item.get("last_seen_ts") or 0)
        value["online"] = time.time() - last <= NODE_TTL_SECONDS
        value.pop("last_seen_ts", None)
        return value

    def list(self, workspace_id: str = DEFAULT_WORKSPACE_ID) -> list[dict]:
        with self.store.transaction(write=False) as state:
            remote = [self._public_remote(item) for item in state.get("execution_nodes", {}).values()
                      if item.get("workspace_id", DEFAULT_WORKSPACE_ID) == workspace_id]
        local = self.local(); local["workspace_id"] = workspace_id
        return [local, *sorted(remote, key=lambda row: row["name"].lower())]

    def get(self, node_id: str) -> dict:
        if node_id == LOCAL_NODE_ID:
            return self.local()
        with self.store.transaction(write=False) as state:
            item = state.get("execution_nodes", {}).get(node_id)
            if not item:
                raise WorkflowError("执行节点不存在。", 404)
            return self._public_remote(item)

    def get_from_state(self, state: dict, node_id: str) -> dict:
        """Resolve a node while caller already owns the workspace transaction."""
        if node_id == LOCAL_NODE_ID:
            return self.local()
        item = state.get("execution_nodes", {}).get(node_id)
        if not item:
            raise WorkflowError("执行节点不存在。", 404)
        return self._public_remote(item)

    def ensure_available_from_state(self, state: dict, node_id: str, *, capability: str | None = None) -> dict:
        node = self.get_from_state(state, node_id)
        if not node.get("online"):
            raise WorkflowError("执行设备当前离线。", 409)
        if capability and capability not in set(node.get("capabilities") or []):
            raise WorkflowError("执行设备不支持此操作。", 422)
        return node

    def ensure_available(self, node_id: str, *, capability: str | None = None) -> dict:
        node = self.get(node_id)
        if not node.get("online"):
            raise WorkflowError("执行设备当前离线。", 409)
        if capability and capability not in set(node.get("capabilities") or []):
            raise WorkflowError("执行设备不支持此操作。", 422)
        return node

    def browser_channels(self, node_id: str, *, interactive: bool = False) -> list[str]:
        node = self.ensure_available(node_id)
        return list(node.get("interactive_browsers") if interactive else node.get("browsers") or [])

    def create_pairing(self, confirmed: bool, workspace_id: str = DEFAULT_WORKSPACE_ID) -> dict:
        if not confirmed:
            raise WorkflowError("请确认创建执行节点配对码。", 422)
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        digest = _token_hash(code)
        with self._guard:
            now = time.time()
            self._pairings = {key: value for key, value in self._pairings.items() if value[0] > now}
            self._pairings[digest] = (now + PAIRING_TTL_SECONDS, workspace_id)
        return {"pairing_code": code[:4] + "-" + code[4:], "expires_in": PAIRING_TTL_SECONDS}

    def register(self, req: NodeRegisterInput) -> dict:
        code = req.pairing_code.replace("-", "")
        digest = _token_hash(code)
        with self._guard:
            pairing = self._pairings.pop(digest, None)
        if not pairing or pairing[0] <= time.time():
            raise WorkflowError("执行节点配对码无效或已过期。", 401)
        _expiry, workspace_id = pairing
        node_id = uuid.uuid4().hex
        token = secrets.token_urlsafe(36)
        now_ts = time.time()
        item = {
            "id": node_id, "name": req.name, "kind": "remote", "platform": req.platform,
            "workspace_id": workspace_id,
            "capabilities": req.capabilities, "browsers": req.browsers,
            "interactive_browsers": [value for value in req.browsers if value in {"msedge", "chrome"}],
            "token_hash": _token_hash(token), "created_at": utc_now(), "last_seen": utc_now(), "last_seen_ts": now_ts,
        }
        with self.store.transaction() as state:
            state.setdefault("execution_nodes", {})[node_id] = item
        return {"node": self._public_remote(item), "node_token": token}

    def _authorized(self, state: dict, node_id: str, token: str) -> dict:
        item = state.get("execution_nodes", {}).get(node_id)
        if not item or not secrets.compare_digest(item.get("token_hash", ""), _token_hash(token)):
            raise WorkflowError("执行节点认证失败。", 401)
        return item

    def heartbeat(self, node_id: str, token: str) -> dict:
        with self.store.transaction() as state:
            item = self._authorized(state, node_id, token)
            item["last_seen"] = utc_now(); item["last_seen_ts"] = time.time()
            return self._public_remote(item)

    def enqueue(self, node_id: str, kind: str, *, account_id: str, profile_id: str, browser_channel: str, payload: dict | None = None) -> dict:
        if kind not in ALLOWED_COMMANDS:
            raise WorkflowError("不支持的执行节点指令。", 422)
        if node_id == LOCAL_NODE_ID:
            raise WorkflowError("本地节点不使用远程命令队列。", 422)
        self.ensure_available(node_id)
        command = {
            "id": uuid.uuid4().hex, "node_id": node_id, "kind": kind,
            "workspace_id": self.get(node_id).get("workspace_id", DEFAULT_WORKSPACE_ID),
            "account_id": account_id, "profile_id": profile_id, "browser_channel": browser_channel,
            "payload": deepcopy(payload or {}), "state": "pending", "created_at": utc_now(), "updated_at": utc_now(),
        }
        with self.store.transaction() as state:
            state.setdefault("execution_node_commands", {})[command["id"]] = command
        return deepcopy(command)

    def claim(self, node_id: str, token: str, limit: int = 1) -> list[dict]:
        with self.store.transaction() as state:
            item = self._authorized(state, node_id, token)
            item["last_seen"] = utc_now(); item["last_seen_ts"] = time.time()
            commands = [row for row in state.get("execution_node_commands", {}).values()
                        if row.get("node_id") == node_id and row.get("state") == "pending"]
            commands.sort(key=lambda row: row["created_at"])
            claimed = []
            for row in commands[:limit]:
                row["state"] = "claimed"; row["updated_at"] = utc_now()
                claimed.append(deepcopy(row))
            return claimed

    def complete(self, node_id: str, command_id: str, token: str, result: NodeResultInput) -> dict:
        with self.store.transaction() as state:
            item = self._authorized(state, node_id, token)
            item["last_seen"] = utc_now(); item["last_seen_ts"] = time.time()
            command = state.get("execution_node_commands", {}).get(command_id)
            if not command or command.get("node_id") != node_id:
                raise WorkflowError("执行节点任务不存在。", 404)
            if command.get("state") not in {"pending", "claimed"}:
                raise WorkflowError("执行节点任务已结束。", 409)
            command["state"] = "finished"; command["updated_at"] = utc_now()
            command["result"] = result.model_dump(exclude={"node_token"})
            return deepcopy(command)

    def command(self, command_id: str) -> dict | None:
        with self.store.transaction(write=False) as state:
            item = state.get("execution_node_commands", {}).get(command_id)
            return deepcopy(item) if item else None

    def launch_native_x_login(self, account: dict, private_dir: Path, browser_channel: str) -> None:
        if account.get("execution_node_id", LOCAL_NODE_ID) != LOCAL_NODE_ID:
            raise WorkflowError("远程执行节点需要由 Browser Node 客户端打开登录窗口。", 409)
        if browser_channel not in interactive_browser_channels():
            raise WorkflowError("Google/X 人工登录需要本机安装 Google Chrome 或 Microsoft Edge。", 503)
        executable = browser_executable(browser_channel)
        if not executable:
            raise WorkflowError("所选浏览器不可用。", 503)
        profile = (private_dir / "browser" / "XProfile").resolve()
        if private_dir.resolve() not in profile.parents:
            raise WorkflowError("浏览器 Profile 路径无效。", 422)
        profile.mkdir(parents=True, exist_ok=True)
        argv = [str(executable), f"--user-data-dir={profile}", "--profile-directory=Default", "--new-window", "https://x.com/i/flow/login"]
        # No Playwright, remote-debugging, webdriver or user profile reuse is involved.
        subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                         start_new_session=os.name != "nt")
