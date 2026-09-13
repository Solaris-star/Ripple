"""Trusted, opt-in provisioning for local Agent adapter artifacts.

Ripple ships only adapter metadata and built-in runtime code. External ACP packages
are downloaded only after an explicit action for an Agent that was detected on the
same host. Package identity, version, integrity, entrypoint and install root are
server-owned; callers never provide commands, URLs or filesystem paths.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from typing import Any, Iterator

from .agent_runtime import AgentRuntimeError


_MANIFEST_PATH = Path(__file__).with_name("agent_adapters.json")
_ID_RE = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_PACKAGE_RE = re.compile(r"^(?:@[a-z0-9._-]+/)?[a-z0-9._-]+$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$")
_ACTION_LABELS = {
    "install": "安装",
    "repair": "修复安装",
    "verify": "重新验证",
    "uninstall": "卸载适配",
}


@dataclass(frozen=True)
class AdapterSpec:
    id: str
    runtime_id: str
    label: str
    kind: str
    protocol: str
    enabled: bool
    description: str
    source_url: str
    package: str = ""
    version: str = ""
    integrity: str = ""
    entrypoint: str = ""
    binary: str = ""
    node_min: int = 0
    env_override: str = ""
    legacy_managed: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AdapterSpec":
        spec = cls(
            id=str(value.get("id") or ""),
            runtime_id=str(value.get("runtime_id") or ""),
            label=str(value.get("label") or ""),
            kind=str(value.get("kind") or ""),
            protocol=str(value.get("protocol") or ""),
            enabled=bool(value.get("enabled")),
            description=str(value.get("description") or ""),
            source_url=str(value.get("source_url") or ""),
            package=str(value.get("package") or ""),
            version=str(value.get("version") or ""),
            integrity=str(value.get("integrity") or ""),
            entrypoint=str(value.get("entrypoint") or ""),
            binary=str(value.get("binary") or ""),
            node_min=int(value.get("node_min") or 0),
            env_override=str(value.get("env_override") or ""),
            legacy_managed=tuple(str(item) for item in value.get("legacy_managed", []) if str(item)),
        )
        if not _ID_RE.fullmatch(spec.id) or not _ID_RE.fullmatch(spec.runtime_id):
            raise RuntimeError("Agent adapter manifest contains an invalid id")
        if spec.kind not in {"builtin", "npm_acp", "unavailable"}:
            raise RuntimeError(f"Agent adapter manifest contains an invalid kind: {spec.kind}")
        if spec.kind == "npm_acp":
            if not _PACKAGE_RE.fullmatch(spec.package) or not _VERSION_RE.fullmatch(spec.version):
                raise RuntimeError(f"Agent adapter manifest contains an invalid npm package: {spec.id}")
            if not spec.integrity.startswith("sha512-") or not spec.entrypoint.endswith(".js"):
                raise RuntimeError(f"Agent adapter manifest lacks a locked npm artifact: {spec.id}")
            entry = Path(spec.entrypoint)
            if entry.is_absolute() or ".." in entry.parts:
                raise RuntimeError(f"Agent adapter manifest contains an unsafe entrypoint: {spec.id}")
        return spec


class AgentAdapterProvisioningService:
    """Inspect and manage trusted adapter artifacts under Ripple private storage."""

    def __init__(self, private_dir: Path, *, manifest_path: Path | None = None):
        self.private_dir = private_dir.resolve()
        self.root = (self.private_dir / "agent-adapters").resolve()
        self.artifacts_root = (self.root / "artifacts").resolve()
        self.receipts_root = (self.root / "receipts").resolve()
        self.staging_root = (self.root / "staging").resolve()
        self._specs = self._load_manifest((manifest_path or _MANIFEST_PATH).resolve())
        self._locks = {adapter_id: threading.Lock() for adapter_id in self._specs}
        self._verified_unmanaged: set[str] = set()

    @staticmethod
    def _load_manifest(path: Path) -> dict[str, AdapterSpec]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError(f"Unable to load Agent adapter manifest: {path}") from exc
        if raw.get("schema") != 1 or not isinstance(raw.get("adapters"), list):
            raise RuntimeError("Unsupported Agent adapter manifest schema")
        specs: dict[str, AdapterSpec] = {}
        for value in raw["adapters"]:
            if not isinstance(value, dict):
                raise RuntimeError("Agent adapter manifest entry must be an object")
            spec = AdapterSpec.from_dict(value)
            if spec.id in specs:
                raise RuntimeError(f"Duplicate Agent adapter id: {spec.id}")
            specs[spec.id] = spec
        return specs

    def specs_for_runtime(self, runtime_id: str) -> tuple[AdapterSpec, ...]:
        return tuple(spec for spec in self._specs.values() if spec.runtime_id == runtime_id)

    def spec(self, adapter_id: str) -> AdapterSpec:
        spec = self._specs.get(str(adapter_id or ""))
        if spec is None:
            raise AgentRuntimeError("未知的 Agent 适配项。", 404)
        return spec

    def _artifact_root(self, spec: AdapterSpec) -> Path:
        return (self.artifacts_root / spec.id).resolve()

    def _receipt_path(self, spec: AdapterSpec) -> Path:
        return (self.receipts_root / f"{spec.id}.json").resolve()

    @staticmethod
    def _atomic_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with tmp.open("w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)

    def _assert_managed_path(self, path: Path) -> Path:
        value = path.resolve()
        if value == self.artifacts_root or self.artifacts_root not in value.parents:
            raise AgentRuntimeError("Agent 适配目录越过 Ripple 私有存储边界。", 500)
        if path.is_symlink():
            raise AgentRuntimeError("Agent 适配目录不能是符号链接。", 409)
        return value

    def _read_receipt(self, spec: AdapterSpec) -> dict[str, Any]:
        path = self._receipt_path(spec)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                return {}
            if value.get("schema") != 1 or value.get("adapter_id") != spec.id or value.get("managed_by") != "ripple":
                return {}
            return value
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _package_rel(package: str) -> Path:
        return Path("node_modules") / Path(*package.split("/"))

    def _managed_entry(self, spec: AdapterSpec) -> Path:
        return self._artifact_root(spec) / self._package_rel(spec.package) / spec.entrypoint

    def _managed_package_state(self, spec: AdapterSpec) -> tuple[bool, str, str]:
        root = self._artifact_root(spec)
        if root.is_symlink():
            return False, "", "受管安装目录被替换为符号链接。"
        package_root = root / self._package_rel(spec.package)
        package_json = package_root / "package.json"
        entry = package_root / spec.entrypoint
        try:
            payload = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return False, "", ""
        version = str(payload.get("version") or "")
        if str(payload.get("name") or "") != spec.package or version != spec.version or not entry.is_file() or entry.is_symlink():
            return False, version, "受管适配文件与可信清单不一致。"
        lock = root / "package-lock.json"
        try:
            lock_data = json.loads(lock.read_text(encoding="utf-8"))
            lock_entry = lock_data.get("packages", {}).get(self._package_rel(spec.package).as_posix(), {})
            integrity = str(lock_entry.get("integrity") or "")
        except (OSError, ValueError, TypeError, AttributeError):
            return False, version, "受管适配缺少可验证的 package-lock。"
        if integrity != spec.integrity:
            return False, version, "受管适配的完整性摘要与可信清单不一致。"
        return True, version, ""

    @staticmethod
    def _command_for_entry(entry: Path) -> list[str]:
        if entry.suffix.lower() == ".js":
            node = shutil.which("node") or shutil.which("node.exe")
            if not node:
                raise AgentRuntimeError("该 ACP 适配需要 Node.js。", 503)
            return [node, str(entry.resolve())]
        return [str(entry.resolve())]

    def _legacy_command(self, spec: AdapterSpec) -> list[str] | None:
        for relative in spec.legacy_managed:
            rel = Path(relative)
            if rel.is_absolute() or ".." in rel.parts:
                continue
            entry = (self.private_dir / rel).resolve()
            if self.private_dir in entry.parents and entry.is_file():
                return self._command_for_entry(entry)
        return None

    def _unmanaged_command(self, spec: AdapterSpec) -> list[str] | None:
        if spec.env_override:
            raw = os.environ.get(spec.env_override, "").strip()
            if raw:
                path = Path(raw).expanduser()
                if path.is_file():
                    return self._command_for_entry(path)
        direct = shutil.which(spec.binary) if spec.binary else None
        if direct:
            return [direct]
        return self._legacy_command(spec)

    def resolve_command(self, adapter_id: str) -> list[str]:
        spec = self.spec(adapter_id)
        if spec.kind != "npm_acp":
            raise AgentRuntimeError("该适配项没有外部执行入口。", 409)
        valid, _version, _detail = self._managed_package_state(spec)
        if valid:
            return self._command_for_entry(self._managed_entry(spec))
        unmanaged = self._unmanaged_command(spec)
        if unmanaged:
            return unmanaged
        raise AgentRuntimeError(f"缺少 {spec.label}。", 503)

    @staticmethod
    def _action_rows(actions: list[str]) -> list[dict[str, Any]]:
        return [
            {"id": action, "label": _ACTION_LABELS[action], "danger": action == "uninstall"}
            for action in actions
        ]

    def inspect(self, spec: AdapterSpec, runtime_state: dict[str, Any]) -> dict[str, Any]:
        base = {
            "id": spec.id,
            "runtime_id": spec.runtime_id,
            "label": spec.label,
            "kind": spec.kind,
            "protocol": spec.protocol,
            "description": spec.description,
            "source_url": spec.source_url,
            "managed": False,
            "package": spec.package,
            "version": spec.version,
            "actions": [],
        }
        if spec.kind == "builtin":
            ready = bool(runtime_state.get("selectable") or runtime_state.get("ready") or runtime_state.get("healthy"))
            detail = "内置适配已通过验证，无需安装。" if ready else str(runtime_state.get("detail") or "内置适配尚未通过运行时验证。")
            return {**base, "state": "ready" if ready else "verification_failed", "state_label": "已内置" if ready else "验证未通过", "detail": detail}
        if spec.kind == "unavailable" or not spec.enabled:
            return {**base, "state": "unavailable", "state_label": "暂不可安装", "detail": spec.description}

        valid, version, managed_detail = self._managed_package_state(spec)
        receipt = self._read_receipt(spec)
        if valid:
            verified = bool(
                receipt.get("package") == spec.package
                and receipt.get("version") == spec.version
                and receipt.get("integrity") == spec.integrity
                and receipt.get("verified") is True
            )
            actions = ["verify", "uninstall"] if verified else ["verify", "repair"]
            return {
                **base,
                "managed": True,
                "state": "ready" if verified else "installed_unverified",
                "state_label": "已安装" if verified else "待验证",
                "detail": "适配已安装并通过 ACP 握手验证。" if verified else "检测到受管安装，需完成 ACP 握手验证。",
                "installed_version": version,
                "actions": self._action_rows(actions),
            }
        if managed_detail:
            return {
                **base,
                "managed": True,
                "state": "verification_failed",
                "state_label": "安装异常",
                "detail": managed_detail,
                "installed_version": version,
                "actions": self._action_rows(["repair"]),
            }
        unmanaged = self._unmanaged_command(spec)
        if unmanaged:
            verified = spec.id in self._verified_unmanaged
            return {
                **base,
                "state": "present_unmanaged",
                "state_label": "外部适配已验证" if verified else "检测到外部适配",
                "detail": "Ripple 可以使用该外部适配，但不会修改或卸载它。" if verified else "检测到非 Ripple 管理的适配；可验证或改为项目内受管安装。",
                "actions": self._action_rows(["verify", "install"]),
            }
        return {
            **base,
            "state": "missing",
            "state_label": "可按需安装",
            "detail": f"仅在点击安装后下载 {spec.package}@{spec.version}。",
            "actions": self._action_rows(["install"]),
        }

    def options_for_runtime(self, runtime_id: str, runtime_state: dict[str, Any]) -> list[dict[str, Any]]:
        # A saved profile is not sufficient: installation actions appear only when
        # the current service process has detected the native Agent executable.
        if not bool(runtime_state.get("native_detected", runtime_state.get("installed"))):
            return []
        return [self.inspect(spec, runtime_state) for spec in self.specs_for_runtime(runtime_id)]

    @staticmethod
    def _minimal_env() -> dict[str, str]:
        keep = {
            "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
            "TEMP", "TMP", "TMPDIR", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
            "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
            "NPM_CONFIG_REGISTRY", "npm_config_registry", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS",
        }
        env = {key: value for key, value in os.environ.items() if key in keep}
        env["NO_COLOR"] = "1"
        env["NPM_CONFIG_AUDIT"] = "false"
        env["NPM_CONFIG_FUND"] = "false"
        env["NPM_CONFIG_UPDATE_NOTIFIER"] = "false"
        return env

    @staticmethod
    def _check_node(spec: AdapterSpec) -> tuple[str, str]:
        node = shutil.which("node") or shutil.which("node.exe")
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if not node or not npm:
            raise AgentRuntimeError("安装 ACP 适配需要 Node.js 与 npm。", 503)
        try:
            result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=8, check=False)
            major = int((result.stdout or result.stderr or "").strip().lstrip("v").split(".", 1)[0])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            raise AgentRuntimeError("无法确认 Node.js 版本。", 503) from exc
        if spec.node_min and major < spec.node_min:
            raise AgentRuntimeError(f"{spec.label} 需要 Node.js {spec.node_min}+。", 409)
        return node, npm

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=3)
        except Exception:
            try:
                process.kill()
                process.wait(timeout=2)
            except Exception:
                pass

    def _verify_acp(self, command: list[str], spec: AdapterSpec, *, timeout: float = 20) -> dict[str, Any]:
        workspace = self.private_dir / "agent-adapter-verification"
        workspace.mkdir(parents=True, exist_ok=True)
        process: subprocess.Popen | None = None
        stdout_queue: queue.Queue[str | None] = queue.Queue()
        stderr_tail: list[str] = []
        try:
            process = subprocess.Popen(
                command,
                cwd=str(workspace),
                env=self._minimal_env(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                start_new_session=os.name != "nt",
            )
            assert process.stdout is not None and process.stderr is not None and process.stdin is not None
            process_stdout = process.stdout
            process_stderr = process.stderr
            process_stdin = process.stdin

            def read_stdout() -> None:
                try:
                    for line in process_stdout:
                        stdout_queue.put(line)
                finally:
                    stdout_queue.put(None)

            def read_stderr() -> None:
                for line in process_stderr:
                    value = line.strip()
                    if value:
                        stderr_tail.append(value[:400])
                        if len(stderr_tail) > 8:
                            del stderr_tail[0]

            threading.Thread(target=read_stdout, daemon=True, name=f"ripple-{spec.id}-verify-out").start()
            threading.Thread(target=read_stderr, daemon=True, name=f"ripple-{spec.id}-verify-err").start()
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": 1,
                    "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
                    "clientInfo": {"name": "Ripple Adapter Verifier", "version": "0.2.7"},
                },
            }
            process_stdin.write(json.dumps(request, separators=(",", ":")) + "\n")
            process_stdin.flush()
            deadline = time.monotonic() + max(3.0, timeout)
            while time.monotonic() < deadline:
                try:
                    line = stdout_queue.get(timeout=min(.25, deadline - time.monotonic()))
                except queue.Empty:
                    if process.poll() is not None:
                        break
                    continue
                if line is None:
                    break
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("id") == 1 and "result" in message:
                    result = message.get("result")
                    if not isinstance(result, dict):
                        raise AgentRuntimeError(f"{spec.label} 返回了无效 ACP 初始化响应。", 502)
                    return {"protocol": spec.protocol, "handshake": True}
                if message.get("id") == 1 and "error" in message:
                    raise AgentRuntimeError(f"{spec.label} 拒绝 ACP 初始化。", 502)
                if "id" in message and message.get("method"):
                    denied = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "Verifier capability unavailable"}}
                    process_stdin.write(json.dumps(denied, separators=(",", ":")) + "\n")
                    process_stdin.flush()
            detail = " | ".join(stderr_tail)[-500:]
            raise AgentRuntimeError(f"{spec.label} ACP 握手超时或进程提前退出" + (f"：{detail}" if detail else "。"), 502)
        except OSError as exc:
            raise AgentRuntimeError(f"无法启动 {spec.label}。", 503) from exc
        finally:
            if process is not None:
                self._terminate(process)

    def _verify(self, spec: AdapterSpec) -> dict[str, Any]:
        command = self.resolve_command(spec.id)
        proof = self._verify_acp(command, spec)
        valid, version, _detail = self._managed_package_state(spec)
        if valid:
            receipt = {
                "schema": 1,
                "managed_by": "ripple",
                "adapter_id": spec.id,
                "runtime_id": spec.runtime_id,
                "kind": spec.kind,
                "package": spec.package,
                "version": spec.version,
                "integrity": spec.integrity,
                "entrypoint": spec.entrypoint,
                "verified": True,
                "verified_at": int(time.time()),
                "proof": proof,
            }
            self._atomic_json(self._receipt_path(spec), receipt)
            return {"ok": True, "state": "ready", "managed": True, "version": version, "detail": f"{spec.label} 已通过 ACP 握手验证。"}
        self._verified_unmanaged.add(spec.id)
        return {"ok": True, "state": "present_unmanaged", "managed": False, "detail": f"外部 {spec.label} 已通过 ACP 握手验证；Ripple 不管理其文件。"}

    @contextmanager
    def _operation_lock(self, spec: AdapterSpec) -> Iterator[None]:
        lock = self._locks[spec.id]
        if not lock.acquire(blocking=False):
            raise AgentRuntimeError("该适配项已有安装或维护操作正在进行。", 409)
        lock_handle = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            lock_path = self.root / f".{spec.id}.lock"
            lock_handle = lock_path.open("a+b")
            if os.name == "nt":
                import msvcrt
                try:
                    lock_handle.seek(0)
                    msvcrt.locking(lock_handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise AgentRuntimeError("该适配项正在由另一个 Ripple 进程维护。", 409) from exc
            else:
                import fcntl
                try:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise AgentRuntimeError("该适配项正在由另一个 Ripple 进程维护。", 409) from exc
            yield
        finally:
            if lock_handle is not None:
                try:
                    if os.name == "nt":
                        import msvcrt
                        lock_handle.seek(0)
                        msvcrt.locking(lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                lock_handle.close()
            lock.release()

    def _install(self, spec: AdapterSpec, *, timeout: int = 240) -> dict[str, Any]:
        _node, npm = self._check_node(spec)
        self.artifacts_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        stage = (self.staging_root / f"{spec.id}-{uuid.uuid4().hex}").resolve()
        target = self._assert_managed_path(self._artifact_root(spec))
        backup = (self.staging_root / f"{spec.id}-backup-{uuid.uuid4().hex}").resolve()
        stage.mkdir(parents=True, exist_ok=False)
        package_json = {
            "name": f"ripple-managed-{spec.id.replace('_', '-')}",
            "private": True,
            "version": "0.0.0",
            "dependencies": {spec.package: spec.version},
        }
        self._atomic_json(stage / "package.json", package_json)
        replaced = False
        try:
            result = subprocess.run(
                [npm, "install", "--save-exact", "--ignore-scripts", "--omit=dev", "--no-audit", "--no-fund"],
                cwd=str(stage),
                env=self._minimal_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(30, int(timeout)),
                check=False,
            )
            if result.returncode != 0:
                detail = " | ".join(line.strip() for line in (result.stderr or result.stdout or "").splitlines() if line.strip())[-500:]
                raise AgentRuntimeError("Agent 适配安装失败" + (f"：{detail}" if detail else "。"), 502)
            package_root = stage / self._package_rel(spec.package)
            entry = package_root / spec.entrypoint
            try:
                installed = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
                lock_data = json.loads((stage / "package-lock.json").read_text(encoding="utf-8"))
                lock_entry = lock_data.get("packages", {}).get(self._package_rel(spec.package).as_posix(), {})
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                raise AgentRuntimeError("安装产物缺少可验证的包元数据。", 502) from exc
            if installed.get("name") != spec.package or installed.get("version") != spec.version:
                raise AgentRuntimeError("安装产物的包名或版本与可信清单不一致。", 502)
            if str(lock_entry.get("integrity") or "") != spec.integrity:
                raise AgentRuntimeError("安装产物的完整性摘要与可信清单不一致。", 502)
            if not entry.is_file() or entry.is_symlink():
                raise AgentRuntimeError("安装产物缺少可信 ACP 入口。", 502)
            self._verify_acp(self._command_for_entry(entry), spec)

            if target.exists():
                if target.is_symlink():
                    raise AgentRuntimeError("受管适配目录被替换为符号链接，拒绝覆盖。", 409)
                os.replace(target, backup)
                replaced = True
            os.replace(stage, target)
            try:
                verified = self._verify(spec)
            except Exception:
                if target.exists():
                    shutil.rmtree(target)
                if replaced and backup.exists():
                    os.replace(backup, target)
                raise
            if backup.exists():
                shutil.rmtree(backup)
            return {**verified, "installed": True, "adapter_id": spec.id, "detail": f"{spec.label} 已按需安装并验证。"}
        except subprocess.TimeoutExpired as exc:
            raise AgentRuntimeError("Agent 适配安装超时，未启用任何半成品。", 504) from exc
        finally:
            if stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            if backup.exists() and not target.exists():
                os.replace(backup, target)

    def _uninstall(self, spec: AdapterSpec) -> dict[str, Any]:
        receipt = self._read_receipt(spec)
        if not receipt:
            raise AgentRuntimeError("该适配不是 Ripple 管理的安装，拒绝卸载。", 409)
        target = self._assert_managed_path(self._artifact_root(spec))
        if target.exists():
            shutil.rmtree(target)
        self._receipt_path(spec).unlink(missing_ok=True)
        return {"ok": True, "state": "missing", "managed": False, "adapter_id": spec.id, "detail": f"{spec.label} 已卸载；本机 Agent 与用户配置未被修改。"}

    def perform(self, runtime_id: str, adapter_id: str, action: str, runtime_state: dict[str, Any]) -> dict[str, Any]:
        spec = self.spec(adapter_id)
        if spec.runtime_id != runtime_id:
            raise AgentRuntimeError("适配项不属于该 Agent Runtime。", 404)
        if not bool(runtime_state.get("native_detected", runtime_state.get("installed"))):
            raise AgentRuntimeError("当前服务进程未检测到该本机 Agent，不能安装适配。", 409)
        normalized = str(action or "").strip().lower()
        allowed = {row["id"] for row in self.inspect(spec, runtime_state).get("actions", [])}
        if normalized not in allowed:
            raise AgentRuntimeError("当前状态不允许执行该适配操作。", 409)
        with self._operation_lock(spec):
            if normalized in {"install", "repair"}:
                return self._install(spec)
            if normalized == "verify":
                return self._verify(spec)
            if normalized == "uninstall":
                return self._uninstall(spec)
        raise AgentRuntimeError("未知的适配操作。", 400)
