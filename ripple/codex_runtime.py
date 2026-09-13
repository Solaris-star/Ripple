"""Restricted Codex CLI runtime for Ripple.

The adapter deliberately uses ``codex exec`` as a short-lived transport for each
turn.  User auth and native thread persistence stay owned by Codex, while Ripple
provides a private working directory, a sanitized model catalog with generic
coding/web tools disabled, and one short-lived loopback MCP lease.

A runtime is selectable only after a local probe captures the exact Responses
request produced by the installed Codex binary and proves that no generic
shell/file/web/host-affecting tools are exposed. A narrowly allowlisted passive
UI tool may remain. Probe failure is fail-closed.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import base64
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
import tomllib
import urllib.parse
import uuid
from typing import Any, Callable

from .agent_runtime import AgentRuntimeError, AgentToolBridgeConfig
from .acp_client import AcpProcessClient


_MAX_MODEL_CACHE = 12 * 1024 * 1024
_MAX_TEXT_ATTACHMENT = 256 * 1024
_MAX_IMAGE_ATTACHMENT = 12 * 1024 * 1024
_SAFE_MODEL_LIMIT = 64
_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,180}$")
_DENIED_TOOL_MARKERS = (
    "shell", "exec", "command", "terminal", "stdin", "patch", "file_change", "write_file", "edit_file",
    "web_search", "websearch", "browser", "computer", "image_generation", "spawn_agent", "collaboration",
    "node_repl", "python", "powershell", "bash",
)
_FEATURES_TO_DISABLE = {
    "shell_tool", "unified_exec", "unified_exec_tty", "view_image", "sleep_tool", "web_search_request",
    "standalone_web_search", "search_tool", "in_app_browser", "browser_use", "browser_use_external",
    "computer_use", "image_generation", "plugins", "apps", "multi_agent", "multi_agent_v2", "tool_suggest",
    "skill_search", "hooks", "request_permissions_tool", "default_mode_request_user_input", "code_mode",
}
_FEATURES_TO_ENABLE = {"skip_host_skill_discovery"}
_ALLOWED_PASSIVE_TOOLS = {"request_user_input"}
_EFFORT_TO_CODEX = {"low": "low", "medium": "medium", "high": "high", "extra_high": "xhigh", "xhigh": "xhigh"}
_EFFORT_FROM_CODEX = {"low": "low", "medium": "medium", "high": "high", "xhigh": "extra_high", "max": "extra_high"}


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
    return bool(
        parsed.scheme == "http"
        and (parsed.hostname or "").lower().rstrip(".") in {"127.0.0.1", "localhost", "::1"}
        and not parsed.username and not parsed.password and not parsed.fragment
    )


def _toml_override(key: str, value: Any) -> str:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (int, float)):
        rendered = str(value)
    else:
        rendered = json.dumps(str(value), ensure_ascii=False)
    return f"{key}={rendered}"


def _codex_command() -> list[str]:
    explicit = os.environ.get("RIPPLE_CODEX_BIN", "").strip()
    if explicit:
        path = Path(explicit)
        if path.is_file():
            if path.suffix.lower() == ".js":
                node = shutil.which("node.exe") or shutil.which("node")
                if not node:
                    raise AgentRuntimeError("Codex CLI 需要 Node.js。", 503)
                return [node, str(path.resolve())]
            if path.suffix.lower() not in {".cmd", ".bat", ".ps1"}:
                return [str(path.resolve())]
    direct = shutil.which("codex.exe")
    if direct:
        return [direct]
    appdata = Path(os.environ.get("APPDATA", ""))
    node = shutil.which("node.exe") or shutil.which("node")
    for entry in (
        appdata / "npm/node_modules/@openai/codex/bin/codex.js",
        Path.home() / "AppData/Roaming/npm/node_modules/@openai/codex/bin/codex.js",
    ):
        if node and entry.is_file():
            return [node, str(entry.resolve())]
    direct = shutil.which("codex")
    if direct and os.name != "nt":
        return [direct]
    raise AgentRuntimeError("未检测到可供 Ripple 使用的 Codex CLI。", 503)


class _CaptureHandler(BaseHTTPRequestHandler):
    requests: queue.Queue = queue.Queue()
    hits: queue.Queue = queue.Queue()
    protocol_version = "HTTP/1.1"

    def handle_expect_100(self) -> bool:
        self.send_response_only(100)
        self.end_headers()
        return True

    def log_message(self, _format: str, *_args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        # Some Codex provider paths probe `/models` before the first Responses
        # request. Return the one static probe model so the capability request
        # itself can reach do_POST, where we inspect the exact tool surface.
        body = json.dumps({"object": "list", "data": [{"id": "ripple-probe", "object": "model"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler contract
        self.hits.put({"path": self.path, "content_length": self.headers.get("content-length"), "transfer_encoding": self.headers.get("transfer-encoding")})
        try:
            raw = b""
            if "chunked" in str(self.headers.get("transfer-encoding") or "").lower():
                parts: list[bytes] = []
                total = 0
                while total <= 4 * 1024 * 1024:
                    size_line = self.rfile.readline(64).strip().split(b";", 1)[0]
                    size = int(size_line, 16)
                    if size <= 0:
                        self.rfile.readline(2)
                        break
                    chunk = self.rfile.read(min(size, 4 * 1024 * 1024 + 1 - total))
                    parts.append(chunk); total += len(chunk)
                    if size > len(chunk): self.rfile.read(size - len(chunk))
                    self.rfile.readline(2)
                raw = b"".join(parts)
            else:
                length = min(int(self.headers.get("content-length") or 0), 4 * 1024 * 1024)
                raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
        except Exception:
            payload = {}
        self.requests.put(payload)
        body = json.dumps({"error": {"message": "Ripple restricted-mode probe complete"}}).encode("utf-8")
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class CodexAgentAdapter:
    runtime_id = "codex"
    display_name = "Codex"

    def __init__(self, private_dir: Path, *, tool_token: str):
        self.private_dir = private_dir.resolve() / "codex"
        self.workspace = self.private_dir / "workspace"
        self.map_path = self.private_dir / "session-map.json"
        self.catalog_path = self.private_dir / "safe-model-catalog.json"
        self.tool_token = tool_token
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._active: dict[str, subprocess.Popen] = {}
        self._version = ""
        self._auth_checked_at = 0.0
        self._authenticated = False
        self._auth_detail = ""
        self._features: set[str] | None = None
        self._probe_version = ""
        self._probe_ok = False
        self._probe_detail = "Codex restricted-mode 尚未验证。"
        self._probe_checked_at = 0.0

    @staticmethod
    def _home() -> Path:
        return Path(os.environ.get("CODEX_HOME", "").strip() or (Path.home() / ".codex")).expanduser().resolve()

    def _command(self) -> list[str]:
        return _codex_command()

    def _version_text(self) -> str:
        if self._version:
            return self._version
        try:
            value = subprocess.run(self._command() + ["--version"], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=8, check=False, env=self._process_env())
            self._version = (value.stdout or value.stderr or "").strip().splitlines()[0][:120]
        except (OSError, subprocess.SubprocessError, IndexError, AgentRuntimeError):
            self._version = ""
        return self._version

    def _auth_status(self) -> tuple[bool, str]:
        now = time.monotonic()
        if now - self._auth_checked_at < 20:
            return self._authenticated, self._auth_detail
        authenticated = False
        detail = "Codex CLI 尚未登录或登录已失效。"
        try:
            value = subprocess.run(self._command() + ["login", "status"], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=10, check=False, env=self._process_env())
            text = (value.stdout or value.stderr or "").strip()
            authenticated = value.returncode == 0 and "logged in" in text.lower()
            if authenticated:
                detail = ""
        except (OSError, subprocess.SubprocessError, AgentRuntimeError):
            pass
        self._auth_checked_at = now
        self._authenticated = authenticated
        self._auth_detail = detail
        return authenticated, detail

    def _profile_config(self) -> dict:
        path = self._home() / "config.toml"
        try:
            if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
                return {}
            value = tomllib.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            return {}

    def _native_model_rows(self) -> list[dict]:
        path = self._home() / "models_cache.json"
        rows: list[dict] = []
        try:
            if path.is_file() and path.stat().st_size <= _MAX_MODEL_CACHE:
                raw = json.loads(path.read_text(encoding="utf-8"))
                values = raw.get("models") if isinstance(raw, dict) else raw
                if isinstance(values, list):
                    rows = [deepcopy(row) for row in values if isinstance(row, dict) and row.get("slug")][:_SAFE_MODEL_LIMIT]
        except (OSError, UnicodeError, ValueError, TypeError):
            rows = []
        configured = str(self._profile_config().get("model") or "").strip()
        if configured and not any(str(row.get("slug")) == configured for row in rows):
            rows.insert(0, self._fallback_model(configured))
        return rows[:_SAFE_MODEL_LIMIT]

    @staticmethod
    def _fallback_model(model_id: str) -> dict:
        return {
            "slug": model_id, "display_name": model_id, "description": None,
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [
                {"effort": "low", "description": "Low"}, {"effort": "medium", "description": "Medium"},
                {"effort": "high", "description": "High"}, {"effort": "xhigh", "description": "Extra High"},
            ],
            "shell_type": "disabled", "visibility": "list", "supported_in_api": True, "priority": 0,
            "additional_speed_tiers": [], "service_tiers": [], "default_service_tier": None,
            "availability_nux": None, "upgrade": None, "base_instructions": "", "model_messages": None,
            "include_skills_usage_instructions": False, "include_plugin_usage_instructions": False,
            "include_apps_usage_instructions": False, "supports_reasoning_summaries": True,
            "supports_reasoning_summary_parameter": True, "default_reasoning_summary": "auto",
            "support_verbosity": False, "default_verbosity": None, "apply_patch_tool_type": None,
            "web_search_tool_type": "text", "supports_search_tool": False,
            "truncation_policy": {"mode": "bytes", "limit": 10000}, "supports_parallel_tool_calls": True,
            "supports_image_detail_original": False, "input_modalities": ["text", "image"],
            "context_window": 272000, "max_context_window": 272000, "experimental_supported_tools": [],
            "use_responses_lite": False, "tool_mode": None, "multi_agent_version": None,
            "multi_agent_reasoning_effort": None, "node_repl_auto_review_required": False,
            "node_repl_disabled": True, "requires_sandboxed_review": False,
        }

    @staticmethod
    def _safe_model(row: dict) -> dict:
        value = deepcopy(row)
        value["shell_type"] = "disabled"
        value["apply_patch_tool_type"] = None
        value["supports_search_tool"] = False
        value["web_search_tool_type"] = "text"
        value["experimental_supported_tools"] = []
        value["tool_mode"] = None
        value["multi_agent_version"] = None
        value["multi_agent_reasoning_effort"] = None
        value["use_responses_lite"] = False
        value["include_skills_usage_instructions"] = False
        value["include_plugin_usage_instructions"] = False
        value["include_apps_usage_instructions"] = False
        value["node_repl_disabled"] = True
        value["node_repl_auto_review_required"] = False
        return value

    def model_catalog(self) -> dict:
        rows = self._native_model_rows()
        config = self._profile_config()
        configured = str(config.get("model") or "").strip()
        if not rows and configured:
            rows = [self._fallback_model(configured)]
        models = []
        for row in rows:
            model_id = str(row.get("slug") or "").strip()
            if not model_id or len(model_id) > 200:
                continue
            efforts: list[str] = ["auto"]
            levels = row.get("supported_reasoning_levels")
            if isinstance(levels, list):
                for level in levels:
                    raw = str(level.get("effort") if isinstance(level, dict) else level or "").strip().lower()
                    mapped = _EFFORT_FROM_CODEX.get(raw)
                    if mapped and mapped not in efforts:
                        efforts.append(mapped)
            models.append({"id": model_id, "name": str(row.get("display_name") or model_id)[:200],
                           "effort_levels": efforts, "supports_reasoning": len(efforts) > 1, "source": "codex_native"})
        default_model = configured if configured and any(row["id"] == configured for row in models) else (models[0]["id"] if models else "")
        return {"models": models, "default_model": default_model}

    def _write_safe_catalog(self, model_id: str) -> None:
        source = next((row for row in self._native_model_rows() if str(row.get("slug")) == model_id), None)
        safe = self._safe_model(source or self._fallback_model(model_id))
        _atomic_json(self.catalog_path, {"models": [safe]})

    def _feature_names(self) -> set[str]:
        if self._features is not None:
            return self._features
        found: set[str] = set()
        try:
            value = subprocess.run(self._command() + ["features", "list"], capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", timeout=12, check=False, env=self._process_env())
            for line in (value.stdout or "").splitlines():
                name = line.strip().split(None, 1)[0] if line.strip() else ""
                if re.fullmatch(r"[a-z0-9_]+", name):
                    found.add(name)
        except (OSError, subprocess.SubprocessError, AgentRuntimeError):
            pass
        self._features = found
        return found

    def _process_env(self, tool_token: str = "") -> dict[str, str]:
        keep = {
            "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE", "HOME",
            "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "CODEX_HOME", "CODEX_API_KEY", "OPENAI_API_KEY",
        }
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        env["CODEX_HOME"] = str(self._home())
        env["NO_COLOR"] = "1"
        env["NO_PROXY"] = "127.0.0.1,localhost,::1"
        env["no_proxy"] = env["NO_PROXY"]
        if tool_token:
            env["RIPPLE_CODEX_MCP_TOKEN"] = tool_token
        return env

    def _common_exec_args(self, model_id: str, effort: str, system_text: str, *, tool_bridge: AgentToolBridgeConfig | None,
                          probe_provider: str = "", probe_base: str = "") -> list[str]:
        self._write_safe_catalog(model_id)
        args = self._command() + ["exec", "--json", "--color", "never", "--ignore-user-config", "--ignore-rules",
                                  "--skip-git-repo-check", "--sandbox", "read-only", "--model", model_id]
        overrides = [
            _toml_override("model_catalog_json", str(self.catalog_path)),
            _toml_override("developer_instructions", system_text[:48000]),
            _toml_override("include_permissions_instructions", False),
            _toml_override("include_apps_instructions", False),
            _toml_override("approval_policy", "never"),
            _toml_override("web_search", "disabled"),
        ]
        native_effort = _EFFORT_TO_CODEX.get(effort)
        if native_effort:
            overrides.append(_toml_override("model_reasoning_effort", native_effort))
        if probe_provider and probe_base:
            overrides.extend([
                _toml_override("model_provider", probe_provider),
                _toml_override(f"model_providers.{probe_provider}.name", "Ripple Probe"),
                _toml_override(f"model_providers.{probe_provider}.base_url", probe_base),
                _toml_override(f"model_providers.{probe_provider}.env_key", "RIPPLE_CODEX_PROBE_KEY"),
                _toml_override(f"model_providers.{probe_provider}.wire_api", "responses"),
                _toml_override(f"model_providers.{probe_provider}.requires_openai_auth", False),
                _toml_override(f"model_providers.{probe_provider}.supports_websockets", False),
            ])
        if tool_bridge is not None:
            if not _safe_loopback_url(tool_bridge.endpoint) or not tool_bridge.token:
                raise AgentRuntimeError("Ripple MCP Bridge 配置无效。", 422)
            overrides.extend([
                _toml_override("mcp_servers.ripple.transport", "streamable_http"),
                _toml_override("mcp_servers.ripple.url", tool_bridge.endpoint),
                _toml_override("mcp_servers.ripple.bearer_token_env_var", "RIPPLE_CODEX_MCP_TOKEN"),
                _toml_override("mcp_servers.ripple.enabled", True),
                _toml_override("mcp_servers.ripple.required", True),
                _toml_override("mcp_servers.ripple.default_tools_approval_mode", "approve"),
            ])
        for value in overrides:
            args += ["-c", value]
        supported = self._feature_names()
        for name in sorted(_FEATURES_TO_DISABLE & supported):
            args += ["--disable", name]
        for name in sorted(_FEATURES_TO_ENABLE & supported):
            args += ["--enable", name]
        return args

    @staticmethod
    def _tool_names(payload: dict) -> list[str]:
        values = payload.get("tools") if isinstance(payload, dict) else None
        if not isinstance(values, list):
            return []
        names = []
        for row in values:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            if not name and isinstance(row.get("function"), dict):
                name = str(row["function"].get("name") or "")
            if not name:
                name = str(row.get("type") or "")
            if name:
                names.append(name)
        return names

    def _restricted_probe(self) -> tuple[bool, str]:
        version = self._version_text()
        if version and self._probe_version == version and (self._probe_ok or time.monotonic() - self._probe_checked_at < 30):
            return self._probe_ok, self._probe_detail
        catalog = self.model_catalog()
        model_id = str(catalog.get("default_model") or "")
        if not model_id:
            self._probe_version, self._probe_ok = version, False
            self._probe_detail = "Codex 已安装，但未找到可验证的模型元数据。"
            return False, self._probe_detail
        self.workspace.mkdir(parents=True, exist_ok=True)
        capture: queue.Queue = queue.Queue(maxsize=4)
        hits: queue.Queue = queue.Queue(maxsize=4)
        handler = type("RippleCodexProbeHandler", (_CaptureHandler,), {"requests": capture, "hits": hits})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="ripple-codex-probe")
        thread.start()
        process = None
        try:
            base = f"http://127.0.0.1:{server.server_port}"
            args = self._common_exec_args(model_id, "low", "Reply only with OK. Do not use tools.", tool_bridge=None,
                                          probe_provider="ripple_probe", probe_base=base)
            args += ["--ephemeral", "-C", str(self.workspace), "-"]
            probe_env = self._process_env(); probe_env["RIPPLE_CODEX_PROBE_KEY"] = "ripple-probe"
            process = subprocess.Popen(args, cwd=str(self.workspace), env=probe_env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                       errors="replace", creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                                       start_new_session=os.name != "nt")
            result: dict[str, str] = {"stdout": "", "stderr": ""}
            def communicate() -> None:
                try:
                    result["stdout"], result["stderr"] = process.communicate("probe", timeout=15)
                except subprocess.TimeoutExpired:
                    try: process.kill()
                    except OSError: pass
                    try: result["stdout"], result["stderr"] = process.communicate(timeout=3)
                    except subprocess.TimeoutExpired: pass
            io_thread = threading.Thread(target=communicate, daemon=True, name="ripple-codex-probe-io")
            io_thread.start()
            try:
                payload = capture.get(timeout=8)
                if process.poll() is None:
                    try: process.kill()
                    except OSError: pass
            except queue.Empty:
                payload = None
                if process.poll() is None:
                    try: process.kill()
                    except OSError: pass
            io_thread.join(timeout=3)
            stdout, stderr = result["stdout"], result["stderr"]
            if payload is None:
                self._probe_version, self._probe_ok, self._probe_checked_at = version, False, time.monotonic()
                diagnostic = " | ".join(line.strip() for line in (stderr or stdout).splitlines() if line.strip())[-800:]
                hit = None
                try: hit = hits.get_nowait()
                except queue.Empty: pass
                prefix = "Codex restricted-mode probe 收到 HTTP 请求但未能解析模型 payload。" if hit else "Codex restricted-mode probe 未观察到模型请求。"
                self._probe_detail = prefix + (f" {hit}" if hit else "") + (f" {diagnostic}" if diagnostic else "")
                return False, self._probe_detail
            tools = self._tool_names(payload)
            denied = [name for name in tools if any(marker in name.lower() for marker in _DENIED_TOOL_MARKERS)]
            unknown = [name for name in tools if name not in _ALLOWED_PASSIVE_TOOLS and name not in denied]
            if denied or unknown:
                detail = "Codex restricted-mode probe 仍暴露未授权模型工具：" + ", ".join((denied + unknown)[:8])
                self._probe_version, self._probe_ok, self._probe_detail, self._probe_checked_at = version, False, detail, time.monotonic()
                return False, detail
            self._probe_version, self._probe_ok, self._probe_detail, self._probe_checked_at = version, True, "", time.monotonic()
            return True, ""
        except (OSError, subprocess.SubprocessError, AgentRuntimeError) as exc:
            self._probe_version, self._probe_ok, self._probe_checked_at = version, False, time.monotonic()
            self._probe_detail = f"Codex restricted-mode probe 失败：{str(exc)[:180]}"
            return False, self._probe_detail
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)
            if process is not None and process.poll() is None:
                try: process.kill()
                except OSError: pass

    def detect(self) -> dict:
        try:
            self._command()
        except AgentRuntimeError as exc:
            return {"runtime": self.runtime_id, "name": self.display_name, "installed": False, "ready": False, "detail": str(exc)}
        authenticated, auth_detail = self._auth_status()
        ready = False
        detail = auth_detail
        if authenticated:
            ready, detail = self._restricted_probe()
        return {"runtime": self.runtime_id, "name": self.display_name, "installed": True, "authenticated": authenticated,
                "ready": ready, "version": self._version_text(), "detail": detail, **self.model_catalog()}

    def capabilities(self) -> dict:
        state = self.detect(); ready = bool(state.get("ready"))
        return {"streaming": False, "thinking": ready, "attachments": ready, "attachment_types": ["text", "image"],
                "session_resume": ready, "cancel": ready, "native_model_selection": ready, "native_effort": ready,
                "mcp": ready, "ripple_tools": ready, "profile_import": True, "restricted_mode": ready}

    def status(self, tool_base: str, *, start: bool = False) -> dict:
        detected = self.detect(); healthy = bool(detected.get("ready"))
        return {"configured": bool(detected.get("installed")), "healthy": healthy, "runtime": self.runtime_id,
                "version": detected.get("version", ""), "detail": "" if healthy else detected.get("detail", ""),
                "capabilities": self.capabilities() if healthy else {"restricted_mode": False, "profile_import": True}}

    def start_or_attach(self, tool_base: str) -> dict:
        value = self.status(tool_base, start=False)
        if not value.get("healthy"):
            raise AgentRuntimeError(str(value.get("detail") or "Codex Runtime 当前不可用。"), 503)
        self.workspace.mkdir(parents=True, exist_ok=True)
        return value

    def _read_map(self) -> dict[str, str]:
        try:
            if not self.map_path.is_file() or self.map_path.stat().st_size > 1024 * 1024:
                return {}
            raw = json.loads(self.map_path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in raw.items() if isinstance(v, str) and _THREAD_ID_RE.fullmatch(v)}
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
        existing = self.mapped_session(web_session_id)
        if existing:
            return existing
        raise AgentRuntimeError("Codex 会话需要在首轮请求中创建。", 409)

    @staticmethod
    def _attachment_args(files: list[dict]) -> tuple[list[str], str]:
        images: list[str] = []
        text_parts: list[str] = []
        for item in files[:12]:
            try:
                path = Path(item["path"])
                if not path.is_file(): continue
                size = path.stat().st_size
            except (KeyError, OSError, TypeError, ValueError):
                continue
            name = str(item.get("name") or path.name)[:240]
            mime = str(item.get("mime") or "")
            if (mime.startswith("image/") or path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif"}) and size <= _MAX_IMAGE_ATTACHMENT:
                images.append(str(path.resolve()))
            elif size <= _MAX_TEXT_ATTACHMENT and (mime.startswith("text/") or path.suffix.lower() in {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".html"}):
                try: text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError): continue
                text_parts.append(f"\n--- Explicit attachment: {name} ---\n{text}\n--- End attachment ---")
            else:
                text_parts.append(f"\n[Attachment {name} is present but this restricted Codex adapter did not read its binary contents.]")
        return images, "".join(text_parts)

    @staticmethod
    def _artifact_from_mcp(item: dict) -> dict | None:
        if str(item.get("type") or "") != "mcp_tool_call":
            return None
        result = item.get("result")
        candidates: list[Any] = []
        if isinstance(result, dict):
            candidates.extend([result.get("structured_content"), result.get("structuredContent")])
            content = result.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        try: candidates.append(json.loads(part["text"]))
                        except (ValueError, TypeError): pass
        for value in candidates:
            if isinstance(value, dict) and value.get("kind") == "content_draft":
                cid, vid = str(value.get("id") or ""), str(value.get("version_id") or "")
                if re.fullmatch(r"[a-f0-9]{32}", cid) and re.fullmatch(r"[a-f0-9]{64}", vid):
                    return {"kind": "content_draft", "id": cid, "version_id": vid, "title": str(value.get("title") or "")[:200], "status": "draft"}
        return None

    def run_turn(self, web_id: str, user_text: str, system_text: str, files: list[dict], tool_base: str,
                 emit: Callable[[str, str], None], *, timeout: int = 600, model_id: str | None = None,
                 effort: str = "auto", effort_mode: str = "auto", enabled_tools: list[str] | None = None,
                 tool_bridge: AgentToolBridgeConfig | None = None) -> tuple[str, str]:
        self.start_or_attach(tool_base)
        if enabled_tools and tool_bridge is None:
            raise AgentRuntimeError("Codex 需要 Ripple MCP lease 才能使用业务能力。", 403)
        model_state = self.model_catalog(); target_model = str(model_id or model_state.get("default_model") or "")
        allowed_models = {row["id"] for row in model_state.get("models", [])}
        if not target_model or target_model not in allowed_models:
            raise AgentRuntimeError("所选 Codex 模型当前不可用。", 422)
        lock = self._session_locks.setdefault(web_id, threading.Lock())
        if not lock.acquire(timeout=min(timeout, 300)):
            raise AgentRuntimeError("这个会话上一条仍在运行，请稍后重试。", 409)
        process: subprocess.Popen | None = None
        thread_id = self.mapped_session(web_id) or ""
        final_path = self.private_dir / "last" / f"{web_id}.txt"
        try:
            self.workspace.mkdir(parents=True, exist_ok=True); final_path.parent.mkdir(parents=True, exist_ok=True)
            final_path.unlink(missing_ok=True)
            args = self._common_exec_args(target_model, effort, system_text, tool_bridge=tool_bridge)
            images, attachment_text = self._attachment_args(files)
            for image in images:
                args += ["--image", image]
            args += ["--output-last-message", str(final_path)]
            if thread_id:
                args += ["resume", thread_id, "-"]
            else:
                args += ["-C", str(self.workspace), "-"]
            prompt = (user_text or "") + attachment_text
            env = self._process_env(tool_bridge.token if tool_bridge else "")
            process = subprocess.Popen(args, cwd=str(self.workspace), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                                       start_new_session=os.name != "nt")
            with self._lock: self._active[web_id] = process
            if process.stdin is not None:
                process.stdin.write(prompt); process.stdin.close()
            stderr_tail: deque[str] = deque(maxlen=12)
            def read_err() -> None:
                if process and process.stderr:
                    for line in process.stderr:
                        text = line.strip()
                        if text: stderr_tail.append(text[:600])
            err_thread = threading.Thread(target=read_err, daemon=True, name="ripple-codex-err"); err_thread.start()
            deadline = time.monotonic() + max(10, timeout)
            if process.stdout is not None:
                for line in process.stdout:
                    if time.monotonic() > deadline:
                        self.stop_turn(web_id); raise AgentRuntimeError("Codex 请求超时。", 504)
                    try: event = json.loads(line)
                    except (ValueError, TypeError): continue
                    if not isinstance(event, dict): continue
                    etype = str(event.get("type") or "")
                    if etype == "thread.started":
                        candidate = str(event.get("thread_id") or "")
                        if _THREAD_ID_RE.fullmatch(candidate):
                            thread_id = candidate
                            with self._lock:
                                mapping = self._read_map(); mapping[web_id] = thread_id; self._save_map(mapping)
                    elif etype in {"item.started", "item.updated", "item.completed"}:
                        item = event.get("item") if isinstance(event.get("item"), dict) else {}
                        kind = str(item.get("type") or "")
                        if kind == "reasoning" and etype == "item.completed":
                            text = str(item.get("text") or "")
                            if text: emit("thinking", text[:8000])
                        elif kind == "mcp_tool_call":
                            name = str(item.get("tool") or "Ripple MCP")[:120]
                            emit("activity", f"调用 {name} · {'完成' if etype == 'item.completed' else '运行中'}")
                            artifact = self._artifact_from_mcp(item) if etype == "item.completed" else None
                            if artifact: emit("artifact", json.dumps(artifact, ensure_ascii=False, sort_keys=True))
                        elif kind in {"command_execution", "file_change", "web_search"}:
                            self.stop_turn(web_id)
                            raise AgentRuntimeError(f"Codex restricted-mode 运行时出现未授权能力：{kind}", 403)
            remaining = max(1.0, deadline - time.monotonic())
            try: code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self.stop_turn(web_id); raise AgentRuntimeError("Codex 请求超时。", 504) from None
            err_thread.join(timeout=1)
            if code != 0:
                detail = " | ".join(stderr_tail)[-1200:]
                raise AgentRuntimeError("Codex 执行失败" + (f"：{detail}" if detail else "。"), 502)
            try: final = final_path.read_text(encoding="utf-8").strip()[:2 * 1024 * 1024]
            except (OSError, UnicodeError): final = ""
            if final: emit("token", final)
            if not thread_id:
                raise AgentRuntimeError("Codex 未返回会话标识。", 502)
            return final, thread_id
        finally:
            with self._lock:
                if self._active.get(web_id) is process: self._active.pop(web_id, None)
            lock.release()

    def stop_turn(self, web_id: str, timeout: float = 5.0) -> bool:
        with self._lock: process = self._active.get(web_id)
        if process is None or process.poll() is not None: return False
        try:
            process.terminate(); process.wait(timeout=max(.2, timeout)); return True
        except Exception:
            try: process.kill(); process.wait(timeout=2); return True
            except Exception: return False

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            mapping = self._read_map(); keys = [key for key, value in mapping.items() if value == session_id]
            for key in keys: mapping.pop(key, None)
            self._save_map(mapping)
        if not _THREAD_ID_RE.fullmatch(session_id): return bool(keys)
        try:
            subprocess.run(self._command() + ["delete", session_id], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=10, check=False, env=self._process_env())
        except (OSError, subprocess.SubprocessError, AgentRuntimeError):
            pass
        return bool(keys)

    def close(self) -> None:
        with self._lock: active = list(self._active.items())
        for web_id, _process in active: self.stop_turn(web_id, 1.0)


class CodexAcpAgentAdapter:
    """Restricted Codex runtime backed by the official opt-in Codex ACP package.

    The ACP package bundles a compatible Codex app-server. Ripple points it at an
    isolated CODEX_HOME that references only the user's auth file, disables every
    native tool and global MCP, then injects exactly one short-lived Ripple MCP
    lease. A synthetic provider probe proves the model-visible tool surface before
    the runtime becomes selectable.
    """

    runtime_id = "codex"
    display_name = "Codex"
    adapter_id = "codex_acp"

    _DISABLED_FEATURES = {
        "view_image", "shell_tool", "unified_exec", "web_search_request", "standalone_web_search",
        "search_tool", "in_app_browser", "browser_use", "browser_use_external", "computer_use",
        "image_generation", "plugins", "apps", "multi_agent", "multi_agent_v2", "tool_suggest",
        "tool_search", "hooks", "request_permissions_tool", "default_mode_request_user_input",
        "code_mode", "goals", "memories", "js_repl",
    }
    _PROVIDER_KEYS = {
        "name", "base_url", "env_key", "wire_api", "requires_openai_auth", "supports_websockets",
        "query_params", "http_headers", "request_max_retries", "stream_max_retries", "stream_idle_timeout_ms",
    }

    def __init__(self, private_dir: Path, *, tool_token: str, provisioning_service, client_factory=None):
        self.private_dir = private_dir.resolve() / "codex-acp"
        self.workspace = self.private_dir / "workspace"
        self.home = self.private_dir / "home"
        self.map_path = self.private_dir / "session-map.json"
        self.catalog_path = self.private_dir / "safe-model-catalog.json"
        self.tool_token = tool_token
        self._provisioning_service = provisioning_service
        self._client_factory = client_factory or AcpProcessClient
        self._native = CodexAgentAdapter(private_dir, tool_token=tool_token)
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._active: dict[str, tuple[AcpProcessClient, str]] = {}
        self._probe_version = ""
        self._probe_ok = False
        self._probe_detail = "Codex ACP restricted-mode 尚未验证。"
        self._probe_checked_at = 0.0

    def _command(self) -> list[str]:
        return self._provisioning_service.resolve_command(self.adapter_id)

    def _native_command(self) -> list[str]:
        return self._native._command()

    def _version_text(self) -> str:
        native = self._native._version_text()
        try:
            result = subprocess.run(self._command() + ["--version"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=8, check=False)
            acp = (result.stdout or result.stderr or "").strip().splitlines()[0][:80]
        except (OSError, subprocess.SubprocessError, AgentRuntimeError, IndexError):
            acp = ""
        return " · ".join(value for value in (native, acp) if value)

    def model_catalog(self) -> dict:
        return self._native.model_catalog()

    @staticmethod
    def _link_or_copy(source: Path, target: Path) -> None:
        if not source.is_file():
            target.unlink(missing_ok=True)
            return
        if target.is_symlink():
            try:
                if target.resolve() == source.resolve():
                    return
            except OSError:
                pass
            target.unlink(missing_ok=True)
        elif target.exists():
            try:
                if os.path.samefile(source, target):
                    return
            except OSError:
                pass
            target.unlink(missing_ok=True)
        try:
            target.symlink_to(source.resolve())
        except OSError:
            try:
                os.link(source, target)
            except OSError:
                shutil.copy2(source, target)
                try:
                    target.chmod(0o600)
                except OSError:
                    pass

    def _prepare_home(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        try:
            self.home.chmod(0o700)
        except OSError:
            pass
        native_home = self._native._home()
        self._link_or_copy(native_home / "auth.json", self.home / "auth.json")
        model_cache = native_home / "models_cache.json"
        if model_cache.is_file() and model_cache.stat().st_size <= _MAX_MODEL_CACHE:
            try:
                shutil.copy2(model_cache, self.home / "models_cache.json")
            except OSError:
                pass

    def _safe_config(self, system_text: str = "", model_id: str = "", *, probe_base: str = "") -> dict:
        source = self._native._profile_config()
        config: dict[str, Any] = {
            "approval_policy": "never",
            "web_search": "disabled",
            "include_apps_instructions": False,
            "include_permissions_instructions": False,
            "features": {name: False for name in sorted(self._DISABLED_FEATURES)},
            "tools": {
                "update_plan": {"enabled": False},
                "experimental_request_user_input": {"enabled": False},
            },
        }
        if system_text:
            config["developer_instructions"] = system_text[:48000]
        selected_model = str(model_id or source.get("model") or "").strip()
        if selected_model:
            config["model"] = selected_model[:200]
        provider_id = str(source.get("model_provider") or "").strip()
        providers = source.get("model_providers") if isinstance(source.get("model_providers"), dict) else {}
        provider = providers.get(provider_id) if provider_id and isinstance(providers.get(provider_id), dict) else None
        if provider_id and provider:
            config["model_provider"] = provider_id
            config["model_providers"] = {
                provider_id: {key: deepcopy(value) for key, value in provider.items() if key in self._PROVIDER_KEYS}
            }
        if probe_base:
            config["model"] = "ripple-probe"
            config["model_provider"] = "ripple_probe"
            config.setdefault("model_providers", {})["ripple_probe"] = {
                "name": "Ripple Probe", "base_url": probe_base, "env_key": "RIPPLE_CODEX_PROBE_KEY",
                "wire_api": "responses", "requires_openai_auth": False, "supports_websockets": False,
            }
        return config

    def _process_env(self, system_text: str = "", model_id: str = "", *, tool_token: str = "", probe_base: str = "") -> dict[str, str]:
        self._prepare_home()
        env = self._native._process_env(tool_token)
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        source = self._native._profile_config()
        provider_id = str(source.get("model_provider") or "").strip()
        providers = source.get("model_providers") if isinstance(source.get("model_providers"), dict) else {}
        provider = providers.get(provider_id) if provider_id and isinstance(providers.get(provider_id), dict) else {}
        env_key = str(provider.get("env_key") or "").strip()
        if env_key and os.environ.get(env_key):
            env[env_key] = os.environ[env_key]
        env["CODEX_HOME"] = str(self.home)
        env["INITIAL_AGENT_MODE"] = "read-only"
        env["CODEX_CONFIG"] = json.dumps(self._safe_config(system_text, model_id, probe_base=probe_base), ensure_ascii=False)
        env["NO_PROXY"] = "127.0.0.1,localhost,::1"
        env["no_proxy"] = env["NO_PROXY"]
        if probe_base:
            env["RIPPLE_CODEX_PROBE_KEY"] = "ripple-probe"
        env.pop("CODEX_PATH", None)  # use the exact Codex version bundled by the locked ACP package
        return env

    def _new_client(self, env: dict[str, str]):
        if self._client_factory is AcpProcessClient:
            return AcpProcessClient(self._command(), self.workspace, env=env, label="Codex ACP")
        return self._client_factory(self._command(), self.workspace)

    def _restricted_probe(self) -> tuple[bool, str]:
        version = self._version_text()
        if version and self._probe_version == version and (self._probe_ok or time.monotonic() - self._probe_checked_at < 30):
            return self._probe_ok, self._probe_detail
        _CaptureHandler.requests = queue.Queue()
        _CaptureHandler.hits = queue.Queue()
        server = ThreadingHTTPServer(("127.0.0.1", 0), _CaptureHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True, name="ripple-codex-acp-probe")
        thread.start()
        client = None
        try:
            base = f"http://127.0.0.1:{server.server_port}/v1"
            self.workspace.mkdir(parents=True, exist_ok=True)
            env = self._process_env(probe_base=base)
            client = self._new_client(env)
            client.start()
            result = client.request("session/new", {"cwd": str(self.workspace), "mcpServers": []}, timeout=60)
            session_id = str((result or {}).get("sessionId") or "") if isinstance(result, dict) else ""
            if not session_id:
                raise AgentRuntimeError("Codex ACP probe 未返回会话标识。", 502)
            client.prompt(session_id, [{"type": "text", "text": "Reply with probe."}], lambda *_: None, timeout=60)
            payload = _CaptureHandler.requests.get(timeout=10)
            tools = self._native._tool_names(payload)
            if tools:
                detail = "Codex ACP restricted-mode 仍暴露非 Ripple 工具：" + ", ".join(tools[:12])
                self._probe_version, self._probe_ok, self._probe_detail, self._probe_checked_at = version, False, detail, time.monotonic()
                return False, detail
            self._probe_version, self._probe_ok, self._probe_detail, self._probe_checked_at = version, True, "", time.monotonic()
            return True, ""
        except (OSError, queue.Empty, AgentRuntimeError, subprocess.SubprocessError) as exc:
            detail = f"Codex ACP restricted-mode probe 失败：{str(exc)[:240]}"
            self._probe_version, self._probe_ok, self._probe_detail, self._probe_checked_at = version, False, detail, time.monotonic()
            return False, detail
        finally:
            if client is not None:
                client.close()
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def detect(self) -> dict:
        try:
            self._native_command()
        except AgentRuntimeError as exc:
            return {"runtime": self.runtime_id, "name": self.display_name, "installed": False, "ready": False, "detail": str(exc)}
        authenticated, detail = self._native._auth_status()
        bridge = False
        try:
            self._command(); bridge = True
        except AgentRuntimeError as exc:
            if authenticated:
                detail = str(exc)
        ready = False
        if authenticated and bridge:
            ready, detail = self._restricted_probe()
        return {
            "runtime": self.runtime_id, "name": self.display_name, "installed": True,
            "authenticated": authenticated, "bridge_installed": bridge, "ready": ready,
            "version": self._version_text(), "detail": detail, **self.model_catalog(),
        }

    def capabilities(self) -> dict:
        ready = bool(self.detect().get("ready"))
        return {
            "streaming": ready, "thinking": ready, "attachments": ready,
            "attachment_types": ["text", "image"], "session_resume": ready, "cancel": ready,
            "native_model_selection": ready, "native_effort": ready, "mcp": ready,
            "ripple_tools": ready, "profile_import": True, "restricted_mode": ready,
        }

    def status(self, tool_base: str, *, start: bool = False) -> dict:
        detected = self.detect(); healthy = bool(detected.get("ready"))
        return {
            "configured": bool(detected.get("installed")), "healthy": healthy, "runtime": self.runtime_id,
            "version": detected.get("version", ""), "detail": "" if healthy else detected.get("detail", ""),
            "capabilities": self.capabilities() if healthy else {"restricted_mode": False, "profile_import": True},
        }

    def start_or_attach(self, tool_base: str) -> dict:
        value = self.status(tool_base, start=False)
        if not value.get("healthy"):
            raise AgentRuntimeError(str(value.get("detail") or "Codex ACP Runtime 当前不可用。"), 503)
        self.workspace.mkdir(parents=True, exist_ok=True)
        return value

    def _read_map(self) -> dict[str, str]:
        try:
            if not self.map_path.is_file() or self.map_path.stat().st_size > 1024 * 1024:
                return {}
            raw = json.loads(self.map_path.read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in raw.items() if isinstance(value, str) and _THREAD_ID_RE.fullmatch(value)}
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
        existing = self.mapped_session(web_session_id)
        if existing:
            return existing
        raise AgentRuntimeError("Codex ACP 会话需要在首轮请求中创建。", 409)

    @staticmethod
    def _mcp_servers(tool_bridge: AgentToolBridgeConfig | None) -> list[dict]:
        if tool_bridge is None:
            return []
        if not _safe_loopback_url(tool_bridge.endpoint) or not tool_bridge.token:
            raise AgentRuntimeError("Ripple MCP Bridge 配置无效。", 422)
        return [{
            "name": "ripple", "type": "http", "url": tool_bridge.endpoint,
            "headers": [{"name": "Authorization", "value": "Bearer " + tool_bridge.token}],
        }]

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
            elif (mime.startswith("text/") or path.suffix.lower() in {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".html"}) and size <= _MAX_TEXT_ATTACHMENT:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                prompt.append({"type": "text", "text": f"\n--- Explicit attachment: {name} ---\n{text}\n--- End attachment ---"})
            else:
                prompt.append({"type": "text", "text": f"\n[Attachment {name} ({mime}) is present, but this restricted Codex ACP adapter cannot read that binary type.]"})
        return prompt

    def run_turn(self, web_id: str, user_text: str, system_text: str, files: list[dict], tool_base: str,
                 emit: Callable[[str, str], None], *, timeout: int = 600, model_id: str | None = None,
                 effort: str = "auto", effort_mode: str = "auto", enabled_tools: list[str] | None = None,
                 tool_bridge: AgentToolBridgeConfig | None = None) -> tuple[str, str]:
        self.start_or_attach(tool_base)
        if enabled_tools and tool_bridge is None:
            raise AgentRuntimeError("Codex 需要 Ripple MCP lease 才能使用业务能力。", 403)
        model_state = self.model_catalog()
        target_model = str(model_id or model_state.get("default_model") or "")
        allowed_models = {row["id"] for row in model_state.get("models", [])}
        if target_model and allowed_models and target_model not in allowed_models:
            raise AgentRuntimeError("所选 Codex 模型当前不可用。", 422)
        lock = self._session_locks.setdefault(web_id, threading.Lock())
        if not lock.acquire(timeout=min(timeout, 300)):
            raise AgentRuntimeError("这个会话上一条仍在运行，请稍后重试。", 409)
        client = None
        session_id = ""
        collected: list[str] = []

        def capture(kind: str, text: str) -> None:
            if kind == "token":
                collected.append(text)
            emit(kind, text)

        try:
            self.workspace.mkdir(parents=True, exist_ok=True)
            env = self._process_env(system_text, target_model, tool_token=tool_bridge.token if tool_bridge else "")
            client = self._new_client(env)
            client.start()
            mcp_servers = self._mcp_servers(tool_bridge)
            existing = self.mapped_session(web_id)
            if existing:
                client.request("session/load", {"sessionId": existing, "cwd": str(self.workspace), "mcpServers": mcp_servers}, timeout=60)
                session_id = existing
            else:
                result = client.request("session/new", {"cwd": str(self.workspace), "mcpServers": mcp_servers}, timeout=60)
                session_id = str((result or {}).get("sessionId") or "") if isinstance(result, dict) else ""
                if not session_id:
                    raise AgentRuntimeError("Codex ACP 未返回会话标识。", 502)
                with self._lock:
                    mapping = self._read_map(); mapping[web_id] = session_id; self._save_map(mapping)
            client.request("session/set_mode", {"sessionId": session_id, "modeId": "read-only"}, timeout=20)
            if target_model:
                client.request("session/set_config_option", {"sessionId": session_id, "configId": "model", "value": target_model}, timeout=20)
            native_effort = {"low": "low", "medium": "medium", "high": "high", "extra_high": "xhigh", "xhigh": "xhigh"}.get(effort)
            if native_effort:
                client.request("session/set_config_option", {"sessionId": session_id, "configId": "reasoning_effort", "value": native_effort}, timeout=20)
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
            client.cancel(session_id); client.close()
