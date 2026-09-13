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
from .acp_client import AcpProcessClient


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
    """Resolve an externally managed ACP command when no provisioning service is wired."""
    explicit = os.environ.get("RIPPLE_CLAUDE_ACP_BIN", "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        if path.is_file():
            if path.suffix.lower() == ".js":
                node = shutil.which("node") or shutil.which("node.exe")
                if not node:
                    raise AgentRuntimeError("Claude ACP bridge 需要 Node.js。", 503)
                return [node, str(path.resolve())]
            return [str(path.resolve())]
        raise AgentRuntimeError(f"RIPPLE_CLAUDE_ACP_BIN 无效：{explicit}", 503)

    direct = shutil.which("claude-agent-acp") or shutil.which("claude-agent-acp.cmd")
    if direct:
        return [direct]
    raise AgentRuntimeError("已检测到 Claude Code，但缺少 Ripple 使用的 Claude ACP bridge。", 503)

_AcpTurnClient = AcpProcessClient

class ClaudeCodeAgentAdapter:
    runtime_id = "claude_code"
    display_name = "Claude Code"

    def __init__(self, private_dir: Path, *, tool_token: str, client_factory=None, provisioning_service=None):
        self.private_dir = private_dir.resolve() / "claude-code"
        self.workspace = self.private_dir / "workspace"
        self.map_path = self.private_dir / "session-map.json"
        self.tool_token = tool_token
        self._client_factory = client_factory or _AcpTurnClient
        self._provisioning_service = provisioning_service
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._active: dict[str, tuple[_AcpTurnClient, str]] = {}
        self._version = ""
        self._auth_checked_at = 0.0
        self._authenticated = False
        self._auth_detail = ""

    def _command(self) -> list[str]:
        if self._provisioning_service is not None:
            return self._provisioning_service.resolve_command("claude_acp")
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
        return {
            "runtime": self.runtime_id,
            "name": self.display_name,
            "installed": native,
            "authenticated": authenticated,
            "bridge_installed": bridge,
            "ready": native and bridge and authenticated,
            "version": self._version_text() if native else "",
            "detail": detail,
        }

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

    def has_active_turns(self) -> bool:
        with self._lock:
            return bool(self._active)

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
