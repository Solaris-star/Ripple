"""Restricted Claude Code runtime through the Agent Client Protocol (ACP).

Ripple intentionally starts one short-lived ACP transport process per turn. Claude's
native session transcript is resumed between turns, while the Ripple MCP lease and
server credentials are freshly bound to each turn. Built-in Claude tools, settings,
hooks and client filesystem/terminal capabilities are disabled; only the brokered
Ripple MCP server is exposed.
"""
from __future__ import annotations

import base64
from collections import deque
from copy import deepcopy
import json
import mimetypes
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import uuid
from typing import Any, Callable

from .agent_runtime import AgentRuntimeError, AgentToolBridgeConfig


_CONTENT_ID_RE = re.compile(r"^[a-f0-9]{32}$")
_VERSION_ID_RE = re.compile(r"^[a-f0-9]{64}$")
_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".json", ".yaml", ".yml", ".csv", ".tsv", ".html", ".htm", ".xml"}
_MAX_TEXT_ATTACHMENT = 256 * 1024
_MAX_IMAGE_ATTACHMENT = 12 * 1024 * 1024


def _atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _safe_loopback_url(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and (parsed.hostname or "").lower().rstrip(".") in {"127.0.0.1", "localhost", "::1"}
        and not parsed.username and not parsed.password and not parsed.fragment
    )


def _native_claude_binary() -> str:
    explicit = os.environ.get("RIPPLE_CLAUDE_BIN", "").strip()
    candidates = [explicit, shutil.which("claude.exe") or ""]
    appdata = Path(os.environ.get("APPDATA", ""))
    candidates.append(str(appdata / "npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe"))
    candidates.append(str(Path.home() / ".local/bin/claude.exe"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(Path(candidate).resolve())
    # POSIX installs normally expose an executable script/binary directly.
    direct = shutil.which("claude")
    if direct and os.name != "nt":
        return direct
    raise AgentRuntimeError("未检测到可供 Ripple 使用的 Claude Code 可执行程序。", 503)


def _claude_acp_command() -> list[str]:
    explicit = os.environ.get("RIPPLE_CLAUDE_ACP_BIN", "").strip()
    if explicit:
        path = Path(explicit)
        if path.is_file():
            if path.suffix.lower() == ".js":
                node = shutil.which("node")
                if not node:
                    raise AgentRuntimeError("Claude ACP bridge 需要 Node.js。", 503)
                return [node, str(path.resolve())]
            return [str(path.resolve())]
    if os.name == "nt":
        appdata = Path(os.environ.get("APPDATA", ""))
        entry = appdata / "npm/node_modules/@zed-industries/claude-agent-acp/dist/index.js"
        node = shutil.which("node.exe") or shutil.which("node")
        if entry.is_file() and node:
            return [node, str(entry.resolve())]
    direct = shutil.which("claude-agent-acp")
    if direct:
        return [direct]
    raise AgentRuntimeError("已检测到 Claude Code，但缺少 Ripple 使用的 Claude ACP bridge。", 503)


class _AcpTurnClient:
    """Small JSON-RPC stdio client for one isolated ACP turn."""

    def __init__(self, command: list[str], cwd: Path):
        self.command = list(command)
        self.cwd = cwd
        self.process: subprocess.Popen | None = None
        self._next_id = 0
        self._pending: dict[int, queue.Queue] = {}
        self._pending_lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._emit: Callable[[str, str], None] | None = None
        self._session_id = ""
        self._stderr: deque[str] = deque(maxlen=20)
        self._closed = threading.Event()

    def start(self) -> None:
        if self.process and self.process.poll() is None:
            return
        env = os.environ.copy()
        env.setdefault("NO_COLOR", "1")
        try:
            self.process = subprocess.Popen(
                self.command,
                cwd=str(self.cwd),
                env=env,
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
        except OSError as exc:
            raise AgentRuntimeError("无法启动 Claude Code ACP bridge。", 503) from exc
        threading.Thread(target=self._reader, daemon=True, name="ripple-claude-acp-out").start()
        threading.Thread(target=self._stderr_reader, daemon=True, name="ripple-claude-acp-err").start()
        result = self.request("initialize", {
            "protocolVersion": 1,
            "clientCapabilities": {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False},
            "clientInfo": {"name": "Ripple", "version": "0.2.7"},
        }, timeout=30)
        if not isinstance(result, dict):
            self.close()
            raise AgentRuntimeError("Claude ACP 初始化失败。", 503)

    def _stderr_reader(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            for line in process.stderr:
                line = line.strip()
                if line:
                    self._stderr.append(line[:800])
        except (OSError, ValueError):
            pass

    def _reader(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and ("result" in message or "error" in message) and "method" not in message:
                    try:
                        request_id = int(message["id"])
                    except (TypeError, ValueError):
                        continue
                    with self._pending_lock:
                        waiter = self._pending.get(request_id)
                    if waiter is not None:
                        waiter.put(message)
                    continue
                method = str(message.get("method") or "")
                if "id" in message and method:
                    self._handle_agent_request(message)
                elif method == "session/update":
                    self._handle_session_update(message.get("params") if isinstance(message.get("params"), dict) else {})
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()

    def _send(self, value: dict) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise AgentRuntimeError("Claude ACP bridge 已退出。", 502)
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._write_lock:
            try:
                process.stdin.write(raw)
                process.stdin.flush()
            except (OSError, ValueError) as exc:
                raise AgentRuntimeError("Claude ACP bridge 通信失败。", 502) from exc

    def _handle_agent_request(self, message: dict) -> None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        if method == "session/request_permission":
            response = {"jsonrpc": "2.0", "id": request_id, "result": {"outcome": {"outcome": "cancelled"}}}
        else:
            # Ripple advertises no terminal/filesystem client capabilities. Any
            # unexpected reverse RPC is denied rather than delegated to the host.
            response = {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "Ripple client capability unavailable"}}
        try:
            self._send(response)
        except AgentRuntimeError:
            pass

    @staticmethod
    def _content_draft(value: Any) -> dict | None:
        candidates: list[dict] = []
        if isinstance(value, dict):
            candidates.append(value)
            for key in ("result", "output", "structuredContent", "rawOutput"):
                child = value.get(key)
                if isinstance(child, dict):
                    candidates.append(child)
        elif isinstance(value, str) and len(value) <= 64 * 1024:
            try:
                child = json.loads(value)
                if isinstance(child, dict):
                    candidates.append(child)
            except (ValueError, TypeError):
                pass
        for row in candidates:
            if row.get("kind") != "content_draft":
                continue
            content_id = str(row.get("id") or "")
            version_id = str(row.get("version_id") or "")
            if _CONTENT_ID_RE.fullmatch(content_id) and _VERSION_ID_RE.fullmatch(version_id):
                return {"kind": "content_draft", "id": content_id, "version_id": version_id,
                        "title": str(row.get("title") or "")[:200], "status": "draft"}
        return None

    def _handle_session_update(self, params: dict) -> None:
        if self._session_id and str(params.get("sessionId") or "") not in {"", self._session_id}:
            return
        update = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = str(update.get("sessionUpdate") or "")
        emit = self._emit
        if emit is None:
            return
        if kind in {"agent_message_chunk", "agent_thought_chunk"}:
            content = update.get("content") if isinstance(update.get("content"), dict) else {}
            text = str(content.get("text") or "") if content.get("type") == "text" else ""
            if text:
                emit("token" if kind == "agent_message_chunk" else "thinking", text)
            return
        if kind in {"tool_call", "tool_call_update"}:
            title = str(update.get("title") or update.get("kind") or "Ripple MCP")[:160]
            status = str(update.get("status") or ("running" if kind == "tool_call" else "updated"))
            emit("activity", f"调用 {title} · {status}")
            artifact = self._content_draft(update.get("rawOutput"))
            if artifact:
                emit("artifact", json.dumps(artifact, ensure_ascii=False, sort_keys=True))

    def request(self, method: str, params: dict, *, timeout: float = 60) -> Any:
        self._next_id += 1
        request_id = self._next_id
        waiter: queue.Queue = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._pending[request_id] = waiter
        try:
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + max(1.0, timeout)
            while time.monotonic() < deadline:
                try:
                    message = waiter.get(timeout=min(.25, max(.01, deadline - time.monotonic())))
                    if isinstance(message.get("error"), dict):
                        error = message["error"]
                        detail = str(error.get("message") or "").replace("\r", " ").replace("\n", " ")[:300]
                        raise AgentRuntimeError(f"Claude ACP 请求失败：{method}" + (f"（{detail}）" if detail else ""), 502)
                    return message.get("result")
                except queue.Empty:
                    process = self.process
                    if process is None or process.poll() is not None:
                        raise AgentRuntimeError("Claude ACP bridge 在请求期间退出。", 502)
            raise AgentRuntimeError(f"Claude ACP 请求超时：{method}", 504)
        finally:
            with self._pending_lock:
                self._pending.pop(request_id, None)

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def prompt(self, session_id: str, prompt: list[dict], emit: Callable[[str, str], None], *, timeout: float) -> dict:
        self._session_id = session_id
        self._emit = emit
        try:
            result = self.request("session/prompt", {"sessionId": session_id, "prompt": prompt}, timeout=timeout)
            return result if isinstance(result, dict) else {}
        finally:
            self._emit = None

    def cancel(self, session_id: str) -> None:
        if not session_id:
            return
        try:
            self.notify("session/cancel", {"sessionId": session_id})
        except AgentRuntimeError:
            pass

    def close(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=4)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass


class ClaudeCodeAgentAdapter:
    runtime_id = "claude_code"
    display_name = "Claude Code"

    def __init__(self, private_dir: Path, *, tool_token: str, client_factory=None):
        self.private_dir = private_dir.resolve() / "claude-code"
        self.workspace = self.private_dir / "workspace"
        self.map_path = self.private_dir / "session-map.json"
        self.tool_token = tool_token
        self._client_factory = client_factory or _AcpTurnClient
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._active: dict[str, tuple[_AcpTurnClient, str]] = {}
        self._version = ""
        self._auth_checked_at = 0.0
        self._authenticated = False
        self._auth_detail = ""

    def _command(self) -> list[str]:
        return _claude_acp_command()

    def _binary(self) -> str:
        return _native_claude_binary()

    def _version_text(self) -> str:
        if self._version:
            return self._version
        binary = self._binary()
        try:
            value = subprocess.run([binary, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8, check=False)
            self._version = (value.stdout or value.stderr or "").strip().splitlines()[0][:120]
        except (OSError, subprocess.SubprocessError, IndexError):
            self._version = ""
        return self._version

    def _auth_status(self) -> tuple[bool, str]:
        now = time.monotonic()
        if now - self._auth_checked_at < 15:
            return self._authenticated, self._auth_detail
        binary = self._binary()
        authenticated = False
        detail = "Claude Code 尚未登录或登录已失效。"
        for args in ([binary, "auth", "status", "--json"], [binary, "auth", "status"]):
            try:
                value = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=8, check=False)
            except (OSError, subprocess.SubprocessError):
                continue
            text = (value.stdout or value.stderr or "").strip()
            if "--json" in args and text:
                try:
                    payload = json.loads(text)
                except (ValueError, TypeError):
                    payload = None
                if isinstance(payload, dict):
                    logged = payload.get("loggedIn", payload.get("authenticated"))
                    if isinstance(logged, bool):
                        authenticated = logged
                        detail = "" if logged else detail
                        break
            if value.returncode == 0:
                lowered = text.lower()
                authenticated = not any(marker in lowered for marker in ("not logged", "logged out", "expired", "revoked", "unauth"))
                detail = "" if authenticated else detail
                break
        self._auth_checked_at = now
        self._authenticated = authenticated
        self._auth_detail = detail
        return authenticated, detail

    def detect(self) -> dict:
        native = False
        bridge = False
        authenticated = False
        detail = ""
        try:
            self._binary(); native = True
        except AgentRuntimeError as exc:
            detail = str(exc)
        if native:
            authenticated, auth_detail = self._auth_status()
            if not authenticated:
                detail = auth_detail
        try:
            self._command(); bridge = True
        except AgentRuntimeError as exc:
            if native and authenticated:
                detail = str(exc)
        return {"runtime": self.runtime_id, "name": self.display_name, "installed": native,
                "authenticated": authenticated, "bridge_installed": bridge, "ready": native and bridge and authenticated,
                "version": self._version_text() if native else "", "detail": detail}

    def capabilities(self) -> dict:
        ready = bool(self.detect().get("ready"))
        return {"streaming": ready, "thinking": ready, "attachments": ready, "attachment_types": ["text", "image"],
                "session_resume": ready, "cancel": ready, "native_model_selection": ready, "native_effort": ready,
                "mcp": ready, "ripple_tools": ready, "profile_import": True, "restricted_mode": ready}

    def status(self, tool_base: str, *, start: bool = False) -> dict:
        detected = self.detect()
        healthy = bool(detected.get("ready"))
        return {"configured": bool(detected.get("installed")), "healthy": healthy, "runtime": self.runtime_id,
                "version": detected.get("version", ""), "detail": "" if healthy else detected.get("detail", ""),
                "capabilities": self.capabilities() if healthy else {"restricted_mode": False, "profile_import": True}}

    def start_or_attach(self, tool_base: str) -> dict:
        value = self.status(tool_base, start=False)
        if not value.get("healthy"):
            raise AgentRuntimeError(str(value.get("detail") or "Claude Code Runtime 当前不可用。"), 503)
        self.workspace.mkdir(parents=True, exist_ok=True)
        return value

    def _read_map(self) -> dict[str, str]:
        try:
            if not self.map_path.is_file() or self.map_path.stat().st_size > 1024 * 1024:
                return {}
            raw = json.loads(self.map_path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and 8 <= len(v) <= 128}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_map(self, value: dict[str, str]) -> None:
        _atomic_json(self.map_path, value)

    def mapped_session(self, web_id: str) -> str | None:
        with self._lock:
            return self._read_map().get(web_id)

    def web_session_for_remote(self, remote_session_id: str) -> str | None:
        with self._lock:
            return next((web for web, remote in self._read_map().items() if remote == remote_session_id), None)

    def create_or_resume_session(self, web_session_id: str, tool_base: str) -> str:
        # Claude ACP needs the current MCP lease/system prompt to create or load a
        # session, so run_turn owns creation. This method exposes an existing map.
        existing = self.mapped_session(web_session_id)
        if existing:
            return existing
        raise AgentRuntimeError("Claude Code 会话需要在首轮请求中创建。", 409)

    @staticmethod
    def _effort(value: str) -> str | None:
        return {"low": "low", "medium": "medium", "high": "high", "extra_high": "max", "max": "max"}.get(value)

    def _meta(self, system_text: str, model_id: str | None, effort: str) -> dict:
        options: dict[str, Any] = {
            "tools": [],
            "settingSources": [],
            "hooks": {},
            "mcpServers": {},
            "pathToClaudeCodeExecutable": self._binary(),
        }
        if model_id and len(model_id) <= 200:
            options["model"] = model_id
        native_effort = self._effort(effort)
        if native_effort:
            options["effort"] = native_effort
            options["thinking"] = {"type": "adaptive"}
        return {"disableBuiltInTools": True, "systemPrompt": system_text[:48000], "claudeCode": {"options": options}}

    @staticmethod
    def _mcp_servers(tool_bridge: AgentToolBridgeConfig | None) -> list[dict]:
        if tool_bridge is None:
            return []
        if not _safe_loopback_url(tool_bridge.endpoint) or not tool_bridge.token:
            raise AgentRuntimeError("Ripple MCP Bridge 配置无效。", 422)
        return [{"name": "ripple", "type": "http", "url": tool_bridge.endpoint,
                 "headers": [{"name": "Authorization", "value": "Bearer " + tool_bridge.token}]}]

    @staticmethod
    def _prompt_parts(user_text: str, files: list[dict]) -> list[dict]:
        prompt: list[dict] = [{"type": "text", "text": user_text}]
        for item in files[:12]:
            try:
                path = Path(item["path"])
                if not path.is_file():
                    continue
                size = path.stat().st_size
            except (KeyError, OSError, TypeError, ValueError):
                continue
            name = str(item.get("name") or path.name)[:240]
            mime = str(item.get("mime") or mimetypes.guess_type(name)[0] or "application/octet-stream")[:120]
            if mime.startswith("image/") and size <= _MAX_IMAGE_ATTACHMENT:
                try:
                    prompt.append({"type": "image", "mimeType": mime, "data": base64.b64encode(path.read_bytes()).decode("ascii")})
                except OSError:
                    continue
            elif (mime.startswith("text/") or path.suffix.lower() in _TEXT_EXTENSIONS) and size <= _MAX_TEXT_ATTACHMENT:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                prompt.append({"type": "text", "text": f"\n--- Explicit attachment: {name} ---\n{text}\n--- End attachment ---"})
            else:
                prompt.append({"type": "text", "text": f"\n[Attachment {name} ({mime}) is present, but this restricted Claude adapter cannot read that binary type. Do not claim to have inspected its contents.]"})
        return prompt

    def run_turn(self, web_id: str, user_text: str, system_text: str, files: list[dict], tool_base: str,
                 emit: Callable[[str, str], None], *, timeout: int = 600, model_id: str | None = None,
                 effort: str = "auto", effort_mode: str = "auto", enabled_tools: list[str] | None = None,
                 tool_bridge: AgentToolBridgeConfig | None = None) -> tuple[str, str]:
        self.start_or_attach(tool_base)
        if enabled_tools and tool_bridge is None:
            raise AgentRuntimeError("Claude Code 需要 Ripple MCP lease 才能使用业务能力。", 403)
        lock = self._session_locks.setdefault(web_id, threading.Lock())
        if not lock.acquire(timeout=min(timeout, 300)):
            raise AgentRuntimeError("这个会话上一条仍在运行，请稍后重试。", 409)
        client: _AcpTurnClient | None = None
        session_id = ""
        collected: list[str] = []
        user_emit = emit

        def capture(kind: str, text: str) -> None:
            if kind == "token":
                collected.append(text)
            user_emit(kind, text)

        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
            client = self._client_factory(self._command(), self.workspace)
            client.start()
            mcp_servers = self._mcp_servers(tool_bridge)
            meta = self._meta(system_text, model_id, effort)
            existing = self.mapped_session(web_id)
            if existing:
                client.request("session/load", {"sessionId": existing, "cwd": str(self.workspace), "mcpServers": mcp_servers, "_meta": meta}, timeout=60)
                session_id = existing
            else:
                result = client.request("session/new", {"cwd": str(self.workspace), "mcpServers": mcp_servers, "_meta": meta}, timeout=60)
                session_id = str((result or {}).get("sessionId") or "") if isinstance(result, dict) else ""
                if not session_id:
                    raise AgentRuntimeError("Claude ACP 未返回会话标识。", 502)
                with self._lock:
                    mapping = self._read_map(); mapping[web_id] = session_id; self._save_map(mapping)
            with self._lock:
                self._active[web_id] = (client, session_id)
            result = client.prompt(session_id, self._prompt_parts(user_text, files), capture, timeout=timeout)
            stop_reason = str(result.get("stopReason") or "")
            if stop_reason in {"cancelled", "refusal"} and not collected:
                return "", session_id
            return "".join(collected), session_id
        finally:
            with self._lock:
                active = self._active.get(web_id)
                if active and active[0] is client:
                    self._active.pop(web_id, None)
            if client is not None:
                client.close()
            lock.release()

    def stop_turn(self, web_id: str, timeout: float = 5.0) -> bool:
        with self._lock:
            active = self._active.get(web_id)
        if not active:
            return False
        client, session_id = active
        client.cancel(session_id)
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                if self._active.get(web_id) is not active:
                    return True
            time.sleep(.05)
        client.close()
        return True

    def delete_session(self, session_id: str) -> bool:
        # Ripple only removes its mapping. Claude's native history belongs to the
        # user's Claude Code installation and is never deleted implicitly.
        with self._lock:
            mapping = self._read_map()
            next_map = {web: remote for web, remote in mapping.items() if remote != session_id}
            if next_map == mapping:
                return False
            self._save_map(next_map)
        return True

    def close(self) -> None:
        with self._lock:
            active = list(self._active.values())
            self._active.clear()
        for client, session_id in active:
            client.cancel(session_id)
            client.close()
