"""Restricted Hermes runtime through the user's native ``hermes acp`` install."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading
import time
from typing import Any, Callable

from .acp_client import AcpProcessClient
from .agent_runtime import AgentRuntimeError, AgentToolBridgeConfig


_THREAD_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,180}$")
_MAX_TEXT_ATTACHMENT = 256 * 1024
_MAX_IMAGE_ATTACHMENT = 12 * 1024 * 1024


def _safe_loopback_url(value: str) -> bool:
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "http"
        and (parsed.hostname or "").lower().rstrip(".") in {"127.0.0.1", "localhost", "::1"}
        and not parsed.username and not parsed.password and not parsed.fragment
    )


class HermesAgentAdapter:
    runtime_id = "hermes"
    display_name = "Hermes"

    def __init__(self, private_dir: Path, *, tool_token: str, project_root: Path | None = None, client_factory=None):
        self.private_dir = private_dir.resolve() / "hermes"
        self.workspace = self.private_dir / "workspace"
        self.map_path = self.private_dir / "session-map.json"
        self.tool_token = tool_token
        self.project_root = (project_root or Path(__file__).resolve().parents[1]).resolve()
        self.shim_path = self.project_root / "ripple" / "hermes_acp_shim.py"
        self._client_factory = client_factory or AcpProcessClient
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._active: dict[str, tuple[AcpProcessClient, str]] = {}
        self._version = ""
        self._entry_cache = ""
        self._entry_signature = ""
        self._entry_checked_at = 0.0
        self._check_version = ""
        self._check_ok = False
        self._check_detail = "Hermes ACP restricted transport 尚未验证。"
        self._check_at = 0.0

    def _entry(self) -> str:
        explicit = os.environ.get("RIPPLE_HERMES_BIN", "").strip()
        signature = explicit or os.environ.get("PATH", "")
        if signature == self._entry_signature and time.monotonic() - self._entry_checked_at < 30:
            return self._entry_cache
        candidates: list[str] = []
        if explicit:
            candidates.append(explicit)
        else:
            for name in ("hermes.exe", "hermes", "hermes.cmd"):
                direct = shutil.which(name)
                if direct:
                    candidates.append(direct)
            for directory in os.environ.get("PATH", "").split(os.pathsep):
                if not directory:
                    continue
                for name in (("hermes.exe", "hermes.cmd", "hermes") if os.name == "nt" else ("hermes",)):
                    candidate = Path(directory) / name
                    if candidate.is_file():
                        candidates.append(str(candidate))
        unique: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            try:
                key = str(Path(candidate).resolve())
            except OSError:
                key = str(Path(candidate).absolute())
            if key not in seen:
                seen.add(key); unique.append(candidate)
        selected = unique[0] if unique else ""
        # Prefer the detected installation that can actually start native ACP.
        for candidate in unique:
            if "docker" in self._wrapper_text(candidate).lower():
                continue
            try:
                probe = subprocess.run([candidate, "acp", "--check"], capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=12, check=False)
                if probe.returncode == 0 and "ACP check OK" in (probe.stdout or probe.stderr or ""):
                    selected = candidate
                    break
            except (OSError, subprocess.SubprocessError):
                continue
        self._entry_signature, self._entry_cache, self._entry_checked_at = signature, selected, time.monotonic()
        return selected

    @staticmethod
    def _wrapper_text(path: str) -> str:
        try:
            value = Path(path)
            if value.suffix.lower() not in {".cmd", ".bat", ".ps1", ".sh"} or value.stat().st_size > 256 * 1024:
                return ""
            return value.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    @staticmethod
    def _python_for_entry(entry: str) -> str:
        path = Path(entry)
        try:
            script_path = path.resolve()
        except OSError:
            script_path = path
        if os.name == "nt":
            for candidate in (script_path.with_name("python.exe"), script_path.with_name("python3.exe")):
                if candidate.is_file():
                    return str(candidate.absolute())
        for candidate in (script_path.with_name("python"), script_path.with_name("python3"), script_path.with_name("python3.11"), script_path.with_name("python3.12")):
            if candidate.is_file():
                # Preserve the venv path rather than resolving its interpreter symlink;
                # Python uses argv[0] to locate the venv's site-packages.
                return str(candidate.absolute())
        try:
            first = script_path.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
        except (OSError, IndexError):
            first = ""
        if first.startswith("#!"):
            candidate = first[2:].strip().strip('"')
            if Path(candidate).is_file():
                return str(Path(candidate).absolute())
        return ""

    def _native_command(self) -> list[str]:
        entry = self._entry()
        if not entry:
            raise AgentRuntimeError("未检测到 Hermes。", 503)
        wrapper = self._wrapper_text(entry).lower()
        if "docker" in wrapper:
            raise AgentRuntimeError("检测到 Hermes Docker wrapper；当前 Ripple 适配只支持本机原生 Hermes ACP。", 409)
        return [entry]

    def _command(self) -> list[str]:
        entry = self._native_command()[0]
        python = self._python_for_entry(entry)
        if not python:
            raise AgentRuntimeError("检测到 Hermes，但无法定位其 Python Runtime。", 503)
        if not self.shim_path.is_file():
            raise AgentRuntimeError("Ripple Hermes restricted ACP shim 缺失。", 500)
        return [python, str(self.shim_path)]

    def _version_text(self) -> str:
        if self._version:
            return self._version
        try:
            result = subprocess.run(self._native_command() + ["--version"], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=10, check=False)
            line = (result.stdout or result.stderr or "").strip().splitlines()[0]
            self._version = line[:160]
        except (OSError, subprocess.SubprocessError, AgentRuntimeError, IndexError):
            self._version = ""
        return self._version

    def _restricted_check(self) -> tuple[bool, str]:
        version = self._version_text()
        if version and self._check_version == version and (self._check_ok or time.monotonic() - self._check_at < 30):
            return self._check_ok, self._check_detail
        try:
            self.workspace.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(self._command() + ["--ripple-check"], cwd=str(self.workspace.parent),
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=60, check=False)
            payload = json.loads((result.stdout or "").strip().splitlines()[-1]) if (result.stdout or "").strip() else {}
            ok = bool(result.returncode == 0 and payload.get("ok") is True and payload.get("policy") == "ripple-mcp-only"
                      and payload.get("builtin_tools") == [])
            detail = "" if ok else str(payload.get("reason") or (result.stderr or result.stdout or "Hermes ACP check failed")).strip()[-500:]
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, IndexError, AgentRuntimeError) as exc:
            ok, detail = False, str(exc)[:500]
        self._check_version, self._check_ok, self._check_detail, self._check_at = version, ok, detail, time.monotonic()
        return ok, detail

    def model_catalog(self) -> dict:
        values: dict[str, str] = {}
        for key in ("model.default", "model.provider"):
            try:
                result = subprocess.run(self._native_command() + ["config", "get", key], capture_output=True, text=True,
                                        encoding="utf-8", errors="replace", timeout=10, check=False)
                if result.returncode == 0:
                    values[key] = (result.stdout or "").strip().splitlines()[0][:200]
            except (OSError, subprocess.SubprocessError, AgentRuntimeError, IndexError):
                pass
        model = values.get("model.default", "")
        provider = values.get("model.provider", "")
        model_id = f"{provider}:{model}" if provider and model and not model.startswith(provider + ":") else model
        rows: list[dict] = []
        seen: set[str] = set()

        def _add(identifier: str) -> None:
            identifier = str(identifier or "").strip()
            if identifier and identifier not in seen:
                seen.add(identifier)
                rows.append({"id": identifier, "name": identifier.split(":", 1)[-1],
                             "effort_levels": ["auto"], "supports_reasoning": True, "source": "native_hermes"})

        # Live provider catalog first — the configured endpoint usually serves
        # more models than the single config default.
        for url, api_key in self._model_list_endpoints():
            try:
                import urllib.request
                headers = {"Accept": "application/json", "User-Agent": "Ripple-Hermes/1.0"}
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                request = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.loads(response.read().decode("utf-8", "replace"))
                for row in payload.get("data", []) if isinstance(payload, dict) else []:
                    name = str(row.get("id") or "").strip() if isinstance(row, dict) else ""
                    if name:
                        _add(f"{provider}:{name}" if provider and not name.startswith(provider + ":") else name)
                if rows:
                    break
            except (OSError, ValueError):
                continue
        # Config default last so the user's chosen default is never dropped.
        _add(model_id)
        return {"models": rows, "default_model": model_id or (rows[0]["id"] if rows else "")}

    def _model_list_endpoints(self) -> list[tuple[str, str]]:
        """Resolve the configured provider's live /v1/models URL and API key."""
        provider = os.environ.get("RIPPLE_HERMES_MODEL_PROVIDER", "").strip()
        if not provider:
            try:
                result = subprocess.run(self._native_command() + ["config", "get", "model.provider"], capture_output=True,
                                        text=True, encoding="utf-8", errors="replace", timeout=10, check=False)
                if result.returncode == 0:
                    provider = (result.stdout or "").strip().splitlines()[0][:200]
            except (OSError, subprocess.SubprocessError, AgentRuntimeError, IndexError):
                provider = ""
        if not provider:
            return []
        try:
            script = (
                "import json,sys;"
                "sys.path.insert(0,'');"
                "from hermes_cli.runtime_provider import resolve_runtime_provider;"
                f"r=resolve_runtime_provider(requested={provider!r});"
                "print(json.dumps({'base_url':r.get('base_url') or '','api_key':r.get('api_key') or ''}))"
            )
            result = subprocess.run([self._python_for_entry(self._entry()), "-c", script], capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=30, check=False)
            payload = json.loads((result.stdout or "").strip().splitlines()[-1]) if (result.stdout or "").strip() else {}
            base = str(payload.get("base_url") or "").strip().rstrip("/")
            api_key = str(payload.get("api_key") or "").strip()
            if base.startswith(("http://", "https://")):
                return [(base + "/models", api_key)]
        except (OSError, subprocess.SubprocessError, ValueError, TypeError, IndexError, AgentRuntimeError):
            pass
        return []

    def _new_client(self):
        self.workspace.mkdir(parents=True, exist_ok=True)
        if self._client_factory is AcpProcessClient:
            env = os.environ.copy()
            env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
            env["NO_COLOR"] = "1"
            return AcpProcessClient(self._command(), self.workspace, env=env, label="Hermes ACP")
        return self._client_factory(self._command(), self.workspace)

    def detect(self) -> dict:
        entry = self._entry()
        if not entry:
            return {"runtime": self.runtime_id, "name": self.display_name, "installed": False, "ready": False, "detail": "未检测到 Hermes。"}
        wrapper = self._wrapper_text(entry).lower()
        if "docker" in wrapper:
            docker = shutil.which("docker.exe") or shutil.which("docker")
            if not docker:
                return {"runtime": self.runtime_id, "name": self.display_name, "installed": True, "ready": False,
                        "source": "docker_wrapper", "dependency": "docker", "detail": "检测到 Hermes，但当前入口依赖 Docker；本机未检测到 Docker。"}
            return {"runtime": self.runtime_id, "name": self.display_name, "installed": True, "ready": False,
                    "source": "docker_wrapper", "dependency": "native_acp", "detail": "检测到 Hermes Docker wrapper；请在执行节点安装原生 Hermes ACP Runtime。"}
        ready, detail = self._restricted_check()
        return {"runtime": self.runtime_id, "name": self.display_name, "installed": True, "ready": ready,
                "source": "native_acp", "version": self._version_text(),
                "dependency": "" if ready else "restricted_transport", "detail": detail}

    def capabilities(self) -> dict:
        ready = bool(self.detect().get("ready"))
        return {"streaming": ready, "thinking": ready, "attachments": ready, "attachment_types": ["text", "image"],
                "session_resume": ready, "cancel": ready, "native_model_selection": ready, "native_effort": False,
                "mcp": ready, "ripple_tools": ready, "profile_import": True, "restricted_mode": ready}

    def status(self, tool_base: str, *, start: bool = False) -> dict:
        state = self.detect(); healthy = bool(state.get("ready"))
        return {"configured": bool(state.get("installed")), "healthy": healthy, "runtime": self.runtime_id,
                "version": state.get("version", ""), "detail": "" if healthy else state.get("detail", ""),
                "dependency": state.get("dependency", ""),
                "capabilities": self.capabilities() if healthy else {"restricted_mode": False, "profile_import": True}}

    def start_or_attach(self, tool_base: str) -> dict:
        value = self.status(tool_base, start=False)
        if not value.get("healthy"):
            raise AgentRuntimeError(str(value.get("detail") or "Hermes Runtime 当前不可用。"), 503)
        return value

    def _read_map(self) -> dict[str, str]:
        try:
            raw = json.loads(self.map_path.read_text(encoding="utf-8"))
            return {str(key): str(value) for key, value in raw.items() if isinstance(value, str) and _THREAD_ID_RE.fullmatch(value)}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_map(self, value: dict[str, str]) -> None:
        self.map_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.map_path.with_suffix(f".{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.map_path)
        finally:
            tmp.unlink(missing_ok=True)

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
        raise AgentRuntimeError("Hermes ACP 会话需要在首轮请求中创建。", 409)

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
            elif (mime.startswith("text/") or path.suffix.lower() in {".txt", ".md", ".json", ".yaml", ".yml", ".csv", ".html"}) and size <= _MAX_TEXT_ATTACHMENT:
                try:
                    prompt.append({"type": "text", "text": f"\n--- Explicit attachment: {name} ---\n{path.read_text(encoding='utf-8')}\n--- End attachment ---"})
                except (OSError, UnicodeError):
                    continue
            else:
                prompt.append({"type": "text", "text": f"\n[Attachment {name} ({mime}) is present, but this restricted Hermes adapter cannot read that binary type.]"})
        return prompt

    def run_turn(self, web_id: str, user_text: str, system_text: str, files: list[dict], tool_base: str,
                 emit: Callable[[str, str], None], *, timeout: int = 600, model_id: str | None = None,
                 effort: str = "auto", effort_mode: str = "auto", enabled_tools: list[str] | None = None,
                 tool_bridge: AgentToolBridgeConfig | None = None) -> tuple[str, str]:
        self.start_or_attach(tool_base)
        if enabled_tools and tool_bridge is None:
            raise AgentRuntimeError("Hermes 需要 Ripple MCP lease 才能使用业务能力。", 403)
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
            client = self._new_client(); client.start()
            mcp_servers = self._mcp_servers(tool_bridge)
            existing = self.mapped_session(web_id)
            created = not bool(existing)
            if existing:
                result = client.request("session/load", {"sessionId": existing, "cwd": str(self.workspace), "mcpServers": mcp_servers}, timeout=90)
                session_id = existing
            else:
                result = client.request("session/new", {"cwd": str(self.workspace), "mcpServers": mcp_servers}, timeout=90)
                session_id = str((result or {}).get("sessionId") or "") if isinstance(result, dict) else ""
                if not session_id:
                    raise AgentRuntimeError("Hermes ACP 未返回会话标识。", 502)
            if model_id:
                try:
                    client.request("session/set_model", {"sessionId": session_id, "modelId": model_id}, timeout=20)
                except AgentRuntimeError:
                    pass
            client.request("session/set_mode", {"sessionId": session_id, "modeId": "default"}, timeout=20)
            with self._lock:
                self._active[web_id] = (client, session_id)
            prompt_text = user_text if not created else (system_text[:48000] + "\n\n" + user_text).strip()
            result = client.prompt(session_id, self._prompt_parts(prompt_text, files), capture, timeout=timeout)
            if created:
                with self._lock:
                    mapping = self._read_map(); mapping[web_id] = session_id; self._save_map(mapping)
            if str(result.get("stopReason") or "") in {"cancelled", "refusal"} and not collected:
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
        client.close(); return True

    def delete_session(self, session_id: str) -> bool:
        with self._lock:
            mapping = self._read_map(); next_map = {web: remote for web, remote in mapping.items() if remote != session_id}
            if next_map == mapping:
                return False
            self._save_map(next_map)
        return True

    def close(self) -> None:
        with self._lock:
            active = list(self._active.values()); self._active.clear()
        for client, session_id in active:
            client.cancel(session_id); client.close()
