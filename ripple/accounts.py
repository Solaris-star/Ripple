"""Account metadata, explicit login lifecycle and per-account credentials."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

from pydantic import BaseModel, ConfigDict, Field
from .catalog import NATIVE, NAMES, environment_probe
from .catalog import X_BROWSER_ADAPTER
from .execution_nodes import LOCAL_NODE_ID, ExecutionNodeService
from .publishing import WorkflowError, fingerprint
from .tenancy import DEFAULT_WORKSPACE_ID


class AccountInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    platform: str
    label: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    execution_node_id: str | None = Field(default=None, pattern=r"^(local|[a-f0-9]{32})$")


class ConnectionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False
    headed: bool = True
    browser_channel: str | None = Field(default=None, pattern=r"^(msedge|chrome|chromium)$")
    execution_node_id: str | None = Field(default=None, pattern=r"^(local|[a-f0-9]{32})$")


class DeleteAccountInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False


class SmsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    code: str = Field(pattern=r"^[0-9]{4,8}$", repr=False)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AccountService:
    def __init__(self, store, private: Path, execution_nodes: ExecutionNodeService):
        self.store = store
        self.private = private.resolve()
        self.execution_nodes = execution_nodes
        self._processes = {}
        self._deleted_accounts: set[str] = set()
        self._guard = threading.Lock()
        self._stopping = threading.Event()
        self.environment = environment_probe()
        self._migrate_execution_bindings()

    def _migrate_execution_bindings(self):
        with self.store.transaction() as state:
            for account in state.get("accounts", {}).values():
                if account.get("adapter") in {"x-api", "wechat-api"}:
                    continue
                account.setdefault("execution_node_id", LOCAL_NODE_ID)
                account.setdefault("profile_id", account.get("id"))
                if not account.get("browser_channel"):
                    op = account.get("operation") or {}
                    if op.get("browser_channel"):
                        account["browser_channel"] = op["browser_channel"]

    def refresh_environment(self) -> dict:
        self.environment = environment_probe()
        return deepcopy(self.environment)

    def _account(self, state, account_id):
        account = state.get("accounts", {}).get(account_id)
        if not account:
            raise WorkflowError("账号不存在。", 404)
        return account

    def directory(self, account_id):
        if not re.fullmatch(r"[a-f0-9]{32}", account_id):
            raise WorkflowError("无效账号标识。", 422)
        path = self.private / "accounts" / account_id
        current = path
        while current != self.private.parent:
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise WorkflowError("账号目录不能使用符号链接。", 422)
            current = current.parent
        return path

    def list(self):
        with self.store.transaction(write=False) as state:
            return [self.project(a) for a in state.get("accounts", {}).values()]

    def get(self, account_id):
        with self.store.transaction(write=False) as state:
            return self.project(self._account(state, account_id))

    def project(self, account):
        value = deepcopy(account)
        value.pop("creation_key", None)
        value.pop("creation_digest", None)
        op = value.get("operation")
        if op and op.get("state") in {"waiting_user", "waiting_node"}:
            value["login_state"] = op["state"]
        if op and op.get("state") == "running":
            run = self.directory(value["id"]) / "operations" / op["id"]
            try:
                if (run / "status.json").stat().st_size < 8192:
                    st = json.loads((run / "status.json").read_text(encoding="utf-8"))
                    value["login_state"] = st.get("state", "starting")
                    value["qr_available"] = op.get("kind") == "login" and (run / "qr.png").is_file() and st.get("state") == "qr_ready"
            except (OSError, ValueError):
                value["login_state"] = "starting"
        if value.get("adapter") != "x-api":
            home = NATIVE.get(value.get("platform"), {}).get("home")
            if home:
                value["login_url"] = home
        return value

    def create(self, req: AccountInput):
        if req.platform not in NATIVE:
            raise WorkflowError("该渠道的 Ripple 本地账号连接尚未实现。", 422)
        node_id = req.execution_node_id or LOCAL_NODE_ID
        if req.platform != "x" and node_id != LOCAL_NODE_ID:
            raise WorkflowError("当前只有 X 浏览器账号支持绑定远程执行节点。", 422)
        if req.platform == "x":
            self.execution_nodes.ensure_available(node_id, capability="x.login")
        digest = fingerprint({"platform": req.platform, "label": req.label, "execution_node_id": node_id})
        with self.store.transaction() as state:
            accounts = state.setdefault("accounts", {})
            for a in accounts.values():
                if a["creation_key"] == req.idempotency_key:
                    if a["creation_digest"] != digest:
                        raise WorkflowError("账号创建键已用于其他输入。")
                    return self.project(a)
            if len(accounts) >= 100:
                raise WorkflowError("最多保存 100 个账号。", 422)
            account_id = uuid.uuid4().hex
            a = {"id": account_id, "workspace_id": DEFAULT_WORKSPACE_ID, "platform": req.platform, "label": req.label, "status": "disconnected",
                 "auth_revision": 0, "identity": None, "checked_at": None, "operation": None,
                 "created_at": now(), "message": "尚未登录", "creation_key": req.idempotency_key, "creation_digest": digest,
                 "adapter": NATIVE[req.platform].get("adapter", "native"), "live_verified": False,
                 "execution_node_id": node_id, "profile_id": account_id, "browser_channel": None}
            accounts[a["id"]] = a
            return self.project(a)

    def start(self, account_id: str, kind: str, req: ConnectionAction):
        if not req.confirmed:
            raise WorkflowError("请确认允许此账号连接目标平台。", 422)
        if kind not in {"login", "probe"}:
            raise WorkflowError("不支持的账号操作。", 422)
        launch_native = False
        remote_command = None
        with self.store.transaction() as state:
            a = self._account(state, account_id)
            existing = a.get("operation") or {}
            if existing.get("state") == "recovery_required":
                if existing.get("kind") == "interaction":
                    raise WorkflowError("上次互动结果待核对，请先到互动管理处理记录。")
                raise WorkflowError("上次发布中断，请先到发布工作台核对回执。")
            if existing.get("state") == "running":
                return self.project(a)
            if existing.get("state") == "waiting_node":
                return self.project(a)
            if existing.get("state") == "waiting_user":
                if kind == "login":
                    return self.project(a)
                # The user explicitly asked to verify after closing the native browser.
                a["operation"] = None
            if sum(bool(x.get("operation") and x["operation"].get("state") == "running") for x in state.get("accounts", {}).values()) >= 3:
                raise WorkflowError("已有三个账号操作运行中，请稍后。", 429)

            is_x_browser = a.get("platform") == "x" and a.get("adapter") == X_BROWSER_ADAPTER
            node_id = req.execution_node_id or a.get("execution_node_id") or LOCAL_NODE_ID
            if not is_x_browser and node_id != LOCAL_NODE_ID:
                raise WorkflowError("当前只有 X 浏览器账号支持选择远程执行设备。", 422)

            if is_x_browser:
                capability = "x.login" if kind == "login" else "x.probe"
                node = self.execution_nodes.ensure_available_from_state(state, node_id, capability=capability)
                candidates = list(node.get("interactive_browsers") if kind == "login" else node.get("browsers") or [])
                preferred = req.browser_channel or a.get("browser_channel")
                selected_browser = preferred if preferred in candidates else (candidates[0] if candidates else None)
                if not selected_browser:
                    if kind == "login":
                        raise WorkflowError("该执行设备没有可用于人工登录的 Chrome 或 Edge。", 503)
                    raise WorkflowError("该执行设备没有可用于账号检查的浏览器。", 503)
                a["execution_node_id"] = node_id
                a["profile_id"] = a.get("profile_id") or a["id"]
                a["browser_channel"] = selected_browser
            else:
                available_browsers = self.environment.get("browsers") or ([self.environment.get("browser")] if self.environment.get("browser") else [])
                selected_browser = None if a["platform"] == "bilibili" else (req.browser_channel if req.browser_channel in available_browsers else self.environment.get("browser"))
                if a["platform"] != "bilibili" and not selected_browser:
                    raise WorkflowError("未检测到可用浏览器，请安装 Chrome、Edge、Chromium 或设置浏览器通道。", 503)
                a["execution_node_id"] = LOCAL_NODE_ID
                a["profile_id"] = a.get("profile_id") or a["id"]
                if selected_browser:
                    a["browser_channel"] = selected_browser

            if kind == "login":
                a["auth_revision"] += 1
                a["status"] = "connecting"
                a["identity"] = None
                self.invalidate_tasks(state, a["id"], "账号重新登录，原审批失效。")

            op_state = "running"
            if is_x_browser and kind == "login":
                op_state = "waiting_user" if node_id == LOCAL_NODE_ID else "waiting_node"
            elif is_x_browser and node_id != LOCAL_NODE_ID:
                op_state = "waiting_node"
            op = {"id": uuid.uuid4().hex, "kind": kind, "state": op_state, "started_at": now(), "execution_node_id": node_id}
            if selected_browser:
                op["browser_channel"] = selected_browser
            a["operation"] = op
            if is_x_browser and kind == "login" and node_id == LOCAL_NODE_ID:
                a["message"] = "普通浏览器已打开。请完成人工登录并关闭该窗口，然后点击“检查状态”。"
                launch_native = True
            elif is_x_browser and node_id != LOCAL_NODE_ID:
                a["message"] = "正在等待执行设备处理浏览器任务。"
                remote_command = "login.start" if kind == "login" else "account.probe"
            work = deepcopy(a)

        if launch_native:
            try:
                self.execution_nodes.launch_native_x_login(work, self.directory(account_id), work["browser_channel"])
            except Exception as exc:
                with self.store.transaction() as state:
                    a = self._account(state, account_id)
                    if (a.get("operation") or {}).get("id") == work["operation"]["id"]:
                        a["operation"]["state"] = "finished"; a["status"] = "error"
                        a["message"] = "无法打开普通浏览器登录窗口。"
                if isinstance(exc, WorkflowError):
                    raise
                raise WorkflowError("无法打开普通浏览器登录窗口。", 503) from None
            return self.get(account_id)

        if remote_command:
            command = self.execution_nodes.enqueue(
                work["execution_node_id"], remote_command, account_id=work["id"], profile_id=work["profile_id"],
                browser_channel=work["browser_channel"], payload={"platform": "x", "login_url": "https://x.com/i/flow/login", "label": work.get("label", "")},
            )
            with self.store.transaction() as state:
                a = self._account(state, account_id)
                if (a.get("operation") or {}).get("id") == work["operation"]["id"]:
                    a["operation"]["command_id"] = command["id"]
            return self.get(account_id)

        threading.Thread(target=self._connect, args=(work, req.headed), daemon=True, name="ripple-connect").start()
        return self.get(account_id)

    def invalidate_tasks(self, state, account_id, reason):
        for task in state["tasks"].values():
            if task["content"].get("account_id") == account_id and task["status"] in {"draft", "review_ready", "approved", "scheduled", "failed_retryable", "verification_required"}:
                if task['content'].get('mode') == 'real' and task.get('attempts', 0) > 0 and not (task.get('receipt') or {}).get('not_submitted'):
                    # Reconnecting an account must not turn a possibly submitted
                    # task back into an approvable draft.
                    task['approval'] = None
                    continue
                task["status"] = "draft"
                task["approval"] = None
                task["events"].append({"at": now(), "status": "draft", "note": reason, "version_id": task["version_id"]})
                task["events"] = task["events"][-100:]

    def _connect(self, account, headed):
        op = account["operation"]
        with self._guard:
            if account["id"] in self._deleted_accounts:
                return
        try:
            result = self.run(account, op["kind"], op["id"], headed=headed)
        except Exception:
            result = {'state': 'error', 'message': '账号连接意外中断，未确认登录成功。请重新检查。'}
        with self.store.transaction() as state:
            a = state.get("accounts", {}).get(account["id"])
            if not a or not a.get("operation") or a["operation"]["id"] != op["id"]:
                return
            previous = a.get("identity")
            result_state = str(result.get("state") or "error")
            a["operation"]["state"] = "finished"
            a["status"] = "connecting" if result_state == "verification_required" else result_state if result_state in {"connected", "disconnected", "expired"} else "error"
            a["message"] = result.get("message", "连接未完成")
            a["checked_at"] = now()
            if a["status"] == "connected":
                a["identity"] = result.get("identity")
            if op["kind"] == "probe" and (a["status"] != "connected" or previous != a.get("identity")):
                a["auth_revision"] += 1
                self.invalidate_tasks(state, a["id"], "账号校验结果变化，需重新审核。")

    def apply_node_command(self, command: dict) -> dict:
        result = command.get("result") or {}
        account_id = command.get("account_id") or ""
        with self.store.transaction() as state:
            account = self._account(state, account_id)
            op = account.get("operation") or {}
            if op.get("command_id") != command.get("id"):
                return self.project(account)
            previous = deepcopy(account.get("identity"))
            result_state = str(result.get("state") or "error")
            account["message"] = str(result.get("message") or "执行设备已返回结果。")[:500]
            if result_state == "waiting_user":
                op["state"] = "waiting_user"
                account["status"] = "connecting"
                return self.project(account)
            op["state"] = "finished"
            account["checked_at"] = now()
            account["status"] = result_state if result_state in {"connected", "disconnected", "expired"} else "error"
            if account["status"] == "connected":
                identity = result.get("identity") if isinstance(result.get("identity"), dict) else None
                account["identity"] = identity
            elif command.get("kind") == "account.probe":
                account["identity"] = None
            if command.get("kind") == "account.probe" and (account["status"] != "connected" or previous != account.get("identity")):
                account["auth_revision"] += 1
                self.invalidate_tasks(state, account_id, "账号校验结果变化，需重新审核。")
            return self.project(account)

    def run(self, account, operation, operation_id, *, headed=True, **extra):
        ambiguous_operation = operation in {"publish", "xhs_interact"}
        if account.get("execution_node_id", LOCAL_NODE_ID) != LOCAL_NODE_ID:
            return {"state": "failed_terminal", "not_submitted": True,
                    "message": "远程 Browser Node 发布传输尚未启用；账号登录态仍保留在该执行设备，不会上传到服务器。"}
        with self._guard:
            if account["id"] in self._deleted_accounts:
                return {"state": "error", "message": "账号已删除，操作已取消。"}
        private = self.directory(account["id"])
        private.mkdir(parents=True, exist_ok=True)
        operation_state = account.get("operation") or {}
        payload = {"operation": operation, "platform": account["platform"], "operation_id": operation_id,
                   "private_dir": str(private), "headed": headed, "profile_label": str(account.get("label") or "")[:80],
                   "browser_channel": operation_state.get("browser_channel") or self.environment["browser"], **extra}
        # Model/API keys and personal tool configuration are not inherited.
        keep = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "PLAYWRIGHT_BROWSERS_PATH"}
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        env["PYTHONUTF8"] = "1"
        proc = None
        try:
            proc = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "ripple.native_worker"],
                cwd=Path(__file__).resolve().parents[1], env=env, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
            with self._guard:
                self._processes[operation_id] = proc
                cancelled = account["id"] in self._deleted_accounts
            if cancelled:
                self._kill(proc)
                return {"state": "error", "message": "账号已删除，操作已取消。"}
            proc.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            proc.stdin.close()
            deadline = time.monotonic() + (900 if operation == "publish" else 300)
            while proc.poll() is None:
                if self._stopping.is_set() or time.monotonic() > deadline:
                    self._kill(proc)
                    break
                time.sleep(.2)
            result = self.read_result(account["id"], operation_id)
            if result:
                return result
            return {"state": "unknown_result" if ambiguous_operation else "error", "message": "操作中断或超时，结果未确认。"}
        except OSError:
            # A broken pipe after spawn cannot prove that the child did not act.
            spawned = proc is not None
            return {"state": ("unknown_result" if spawned else "failed_terminal") if ambiguous_operation else "error",
                    "not_submitted": not spawned,
                    "message": "执行进程通信中断，请先核对结果。" if spawned and ambiguous_operation else "平台读取进程通信中断。" if spawned else "无法启动平台执行进程。"}
        finally:
            if proc and proc.poll() is None:
                self._kill(proc)
            with self._guard:
                self._processes.pop(operation_id, None)

    @staticmethod
    def _kill(proc):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    def read_result(self, account_id, operation_id):
        if not re.fullmatch(r"[a-f0-9]{32}", operation_id):
            return None
        path = self.directory(account_id) / "operations" / operation_id / "result.json"
        try:
            if path.stat().st_size > 1024 * 1024:
                return None
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if value.get("operation_id") == operation_id else None
        except (OSError, ValueError, AttributeError):
            return None

    def sms(self, account_id, req: SmsInput):
        with self.store.transaction(write=False) as state:
            a = self._account(state, account_id)
            op = a.get("operation") or {}
            if op.get("id") != req.operation_id or op.get("state") != "running":
                raise WorkflowError("验证码操作已过期。")
            run_dir = self.directory(account_id) / "operations" / op["id"]
            try:
                status = json.loads((run_dir / "status.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                raise WorkflowError("当前没有等待验证码的操作。") from None
            if status.get("state") != "sms_required":
                raise WorkflowError("当前没有等待验证码的操作。")
            temp = run_dir / "sms.tmp"
            temp.write_text(req.code, encoding="utf-8")
            os.replace(temp, run_dir / "sms.code")
        return {"ok": True}

    def disconnect(self, account_id, confirmed):
        if not confirmed:
            raise WorkflowError("请确认断开该账号。", 422)
        with self.store.transaction() as state:
            a = self._account(state, account_id)
            if a.get("operation") and a["operation"]["state"] in {"running", "recovery_required"}:
                raise WorkflowError("账号操作正在进行或待核对，不能断开。")
            a.update(status="disconnected", identity=None, auth_revision=a["auth_revision"] + 1, checked_at=None, message="已在 Ripple 中断开；平台侧授权请到平台设置撤销。")
            self.invalidate_tasks(state, account_id, "账号已断开，原审批失效。")
            # Quarantine rather than delete credentials, but future logins use a new directory.
            a["operation"] = None
            directory = self.directory(account_id)
            for name in ("browser", "cookies.json"):
                path = directory / name
                if path.exists():
                    path.rename(directory / (name + ".disconnected-" + uuid.uuid4().hex))
            return self.project(a)

    def delete(self, account_id: str, confirmed: bool):
        if not confirmed:
            raise WorkflowError("请确认删除该账号及本机登录数据。", 422)
        directory = self.directory(account_id)
        operation_id = None
        label = "账号"
        with self.store.transaction() as state:
            a = self._account(state, account_id)
            label = a.get("label") or label
            op = a.get("operation") or {}
            if op.get("kind") == "publish" and op.get("state") in {"running", "recovery_required"}:
                raise WorkflowError("该账号有正在发布或待核对的真实发布，不能删除。")
            for task in state.get("tasks", {}).values():
                content = task.get("content") or {}
                if content.get("mode") != "real" or content.get("account_id") != account_id:
                    continue
                receipt = task.get("receipt") or {}
                status = task.get("status")
                if content.get("platform") == "wechat" and receipt.get("draft_only") is True:
                    # A verified WeChat draft is a completed remote write, not an
                    # unresolved public publication. Deleting the local account does
                    # not remove that draft from WeChat and therefore does not need to
                    # keep the credential solely for publication reconciliation.
                    continue
                if status in {"dispatching", "accepted", "unknown_result"}:
                    raise WorkflowError("该账号仍有关联的真实发布待核对，不能删除。")
                if status == "verification_required" and receipt.get("not_submitted") is not True:
                    raise WorkflowError("该账号仍有关联的真实发布待核对，不能删除。")
            interaction_rows = state.get("interactions", {})
            if isinstance(interaction_rows, dict):
                for interaction in interaction_rows.values():
                    if (interaction.get("account_id") == account_id
                            and interaction.get("status") in {"dispatching", "unknown_result"}):
                        raise WorkflowError("该账号仍有关联的真实互动待核对，不能删除。")
            legacy_rows = state.get("xhs_interactions", {})
            if isinstance(legacy_rows, dict):
                for legacy_id, interaction in legacy_rows.items():
                    # Once a legacy record has been copied into the generic ledger, the
                    # generic row is authoritative. Keep the old table as backup only.
                    if isinstance(interaction_rows, dict) and legacy_id in interaction_rows:
                        continue
                    if (interaction.get("account_id") == account_id
                            and interaction.get("status") in {"dispatching", "unknown_result"}):
                        raise WorkflowError("该账号仍有关联的小红书互动待核对，不能删除。")
            if op.get("state") == "running":
                operation_id = op.get("id")
            self.invalidate_tasks(state, account_id, "账号正在从 Ripple 删除，请重新选择账号。")
            a.update(status="deleting", identity=None, checked_at=None,
                     auth_revision=a.get("auth_revision", 0) + 1, operation=None,
                     message="正在删除本机账号与登录数据。")
            with self._guard:
                self._deleted_accounts.add(account_id)
        if operation_id:
            with self._guard:
                proc = self._processes.get(operation_id)
            if proc and proc.poll() is None:
                self._kill(proc)
        for attempt in range(4):
            try:
                if directory.exists():
                    shutil.rmtree(directory)
                break
            except OSError:
                if attempt == 3:
                    raise WorkflowError("本机登录目录仍被浏览器占用；请关闭该账号的登录窗口后再次删除。", 409) from None
                time.sleep(.2)
        with self.store.transaction() as state:
            state.get("accounts", {}).pop(account_id, None)
        return {"deleted": True, "id": account_id, "label": label}

    def recover(self):
        with self.store.transaction() as state:
            for a in state.get("accounts", {}).values():
                op = a.get("operation") or {}
                if op.get("state") != "running" or op.get("kind") == "publish":
                    continue
                if op.get("kind") == "interaction":
                    # A service restart after persisting interaction intent cannot prove
                    # the platform write did not happen. Keep the account and block new
                    # browser work until the interaction ledger is reconciled.
                    op["state"] = "recovery_required"
                    a["message"] = "服务重启时有互动结果未确认；请到互动管理核对。"
                    continue
                a.update(status="disconnected", message="服务重启，登录任务已失效；请重新连接。")
                op["state"] = "interrupted"
                a["auth_revision"] += 1
                self.invalidate_tasks(state, a["id"], "登录任务中断，审批失效。")

    def close(self):
        self._stopping.set()
        with self._guard:
            procs = list(self._processes.values())
        for proc in procs:
            if proc.poll() is None:
                self._kill(proc)
