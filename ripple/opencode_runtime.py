"""Managed loopback OpenCode runtime for Ripple conversations.

OpenCode owns model/session/tool-loop execution. Ripple owns business data,
attachment validation and the publishing approval boundary. The managed server
is loopback-only and receives only a narrow environment with a private config.
"""
from __future__ import annotations

from copy import deepcopy
import json
import mimetypes
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Callable

from .agent_capabilities import GENERIC_DENY, INTERNAL_TOOLS
from .agent_runtime import AgentRuntimeError
from .agent_runtime import AgentToolBridgeConfig


class OpenCodeError(AgentRuntimeError):
    """OpenCode-specific compatibility name for the shared runtime error."""


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


def _valid_base(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    local = host in {"localhost", "127.0.0.1", "::1"}
    return bool(host and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                and (parsed.scheme == "https" or (local and parsed.scheme == "http")))


class OpenCodeAgentAdapter:
    runtime_id = "opencode"
    display_name = "OpenCode"

    def __init__(self, project_root: Path, private_dir: Path, env_provider: Callable[[], dict[str, str]], *, tool_token: str | None = None):
        self.project_root = project_root.resolve()
        self.integration_dir = self.project_root / "integrations" / "opencode"
        self.workspace_dir = self.project_root / ".opencode"
        self.private_dir = private_dir.resolve() / "opencode"
        self.env_provider = env_provider
        self.config_path = self.private_dir / "opencode.json"
        self.map_path = self.private_dir / "session-map.json"
        self.tool_token = tool_token or secrets.token_urlsafe(32)
        self._process: subprocess.Popen | None = None
        self._owned = False
        self._lock = threading.RLock()
        self._session_locks: dict[str, threading.Lock] = {}
        self._abort_events: dict[str, threading.Event] = {}

    def detect(self) -> dict:
        try:
            self._binary()
            return {"runtime": self.runtime_id, "name": self.display_name, "installed": True, "source": "local_binary"}
        except OpenCodeError as exc:
            return {"runtime": self.runtime_id, "name": self.display_name, "installed": False, "source": "local_binary", "detail": str(exc)}

    def capabilities(self) -> dict:
        return {
            "streaming": True,
            "thinking": True,
            "attachments": True,
            "session_resume": True,
            "cancel": True,
            "native_model_selection": True,
            "native_effort": False,
            "ripple_tools": True,
            "restricted_mode": True,
        }

    def start_or_attach(self, tool_base: str) -> dict:
        return self.ensure_started(tool_base)

    def create_or_resume_session(self, web_session_id: str, tool_base: str) -> str:
        return self.get_or_create(web_session_id, tool_base)

    def _settings(self) -> dict[str, str]:
        env = self.env_provider()
        def value(name: str, default: str = "") -> str:
            current = os.environ.get(name, "").strip()
            return current or str(env.get(name, "") or "").strip() or default
        enabled = value("RIPPLE_ENABLE_AI").lower() in {"1", "true", "yes", "on"}
        base = value("RIPPLE_LLM_BASE_URL")
        key = value("RIPPLE_LLM_API_KEY")
        model = value("RIPPLE_LLM_MODEL")
        raw_models = value("RIPPLE_LLM_MODELS")
        models = [item.strip() for item in raw_models.split(",") if item.strip()] if raw_models else []
        if model and model not in models: models.insert(0, model)
        port = value("RIPPLE_OPENCODE_PORT", "4096")
        if not enabled or not base or not key or not model or not _valid_base(base):
            raise OpenCodeError("Ripple Agent 尚未配置可用的模型服务。", 409)
        try:
            port_number = int(port)
            if not 1024 <= port_number <= 65535:
                raise ValueError()
        except ValueError:
            raise OpenCodeError("RIPPLE_OPENCODE_PORT 无效。", 422) from None
        return {"base": base.rstrip("/"), "key": key, "model": model, "models": models or [model], "port": str(port_number)}

    @staticmethod
    def _binary() -> str:
        explicit = os.environ.get("RIPPLE_OPENCODE_BIN", "").strip()
        candidates = [explicit, shutil.which("opencode.exe") or "", shutil.which("opencode") or ""]
        for candidate in candidates:
            if not candidate:
                continue
            path = Path(candidate)
            if path.suffix.lower() == ".exe" and path.is_file():
                return str(path)
            # npm wrappers are .cmd/.ps1; execute the package's native binary instead.
            npm_root = path.parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
            if npm_root.is_file():
                return str(npm_root)
        roaming = Path(os.environ.get("APPDATA", "")) / "npm/node_modules/opencode-ai/bin/opencode.exe"
        if roaming.is_file():
            return str(roaming)
        raise OpenCodeError("未检测到 OpenCode。请先安装 opencode-ai。", 503)

    def _base(self, settings: dict[str, str] | None = None) -> str:
        settings = settings or self._settings()
        return f"http://127.0.0.1:{settings['port']}"

    def _request(self, path: str, method: str = "GET", body=None, *, timeout: float = 15,
                 settings: dict[str, str] | None = None):
        raw = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self._base(settings) + path,
            data=raw,
            method=method,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPRedirectHandler())
        try:
            with opener.open(request, timeout=timeout) as response:
                payload = response.read(4 * 1024 * 1024 + 1)
                if len(payload) > 4 * 1024 * 1024:
                    raise OpenCodeError("OpenCode 响应超过安全上限。")
                if not payload:
                    return None
                return json.loads(payload.decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise OpenCodeError("OpenCode 会话不存在。", 404) from exc
            raise OpenCodeError(f"OpenCode 请求失败（HTTP {exc.code}）。") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OpenCodeError("Ripple Agent Runtime 当前不可连接。") from exc
        except (ValueError, TypeError) as exc:
            raise OpenCodeError("OpenCode 返回了无法解析的响应。") from exc

    def _health(self, settings: dict[str, str]) -> dict | None:
        try:
            value = self._request("/global/health", timeout=1.5, settings=settings)
            return value if isinstance(value, dict) and value.get("healthy") is True else None
        except OpenCodeError:
            return None

    def _materialize_workspace(self) -> Path:
        """Materialize Ripple-managed OpenCode files into ignored project-local state."""
        instructions_src = self.integration_dir / "RIPPLE_AGENT.md"
        tools_src = self.integration_dir / "tools"
        if not instructions_src.is_file() or not tools_src.is_dir():
            raise OpenCodeError("Ripple 的 OpenCode 集成模板不完整。", 500)
        tools_dst = self.workspace_dir / "tools"
        tools_dst.mkdir(parents=True, exist_ok=True)
        instructions_dst = self.workspace_dir / "RIPPLE_AGENT.md"
        shutil.copy2(instructions_src, instructions_dst)
        expected: set[str] = set()
        for source in sorted(tools_src.glob("ripple_*.ts")):
            expected.add(source.name)
            shutil.copy2(source, tools_dst / source.name)
        for stale in tools_dst.glob("ripple_*.ts"):
            if stale.name not in expected:
                stale.unlink(missing_ok=True)
        return instructions_dst.resolve()

    def _write_config(self, settings: dict[str, str]) -> None:
        model = settings["model"]
        instructions = self._materialize_workspace()
        config = {
            "$schema": "https://opencode.ai/config.json",
            "model": f"ripple/{model}",
            "small_model": f"ripple/{model}",
            "enabled_providers": ["ripple"],
            "provider": {
                "ripple": {
                    "npm": "@ai-sdk/openai-compatible",
                    "name": "Ripple Model",
                    "options": {
                        "baseURL": "{env:RIPPLE_LLM_BASE_URL}",
                        "apiKey": "{env:RIPPLE_LLM_API_KEY}",
                    },
                    "models": {item: {"name": item} for item in settings.get("models", [model])},
                }
            },
            "instructions": [str(instructions)],
            "permission": {"*": "deny", "ripple_*": "allow"},
            "tools": {
                "bash": False, "edit": False, "write": False, "read": False, "grep": False,
                "glob": False, "webfetch": False, "websearch": False, "question": False,
                "lsp": False, "patch": False, "skill": False, "todowrite": False,
            },
        }
        _atomic_json(self.config_path, config)

    def _process_env(self, settings: dict[str, str], tool_base: str) -> dict[str, str]:
        keep = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
                "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)"}
        env = {k: v for k, v in os.environ.items() if k.upper() in keep}
        env.update({
            "RIPPLE_LLM_BASE_URL": settings["base"],
            "RIPPLE_LLM_API_KEY": settings["key"],
            "RIPPLE_LLM_MODEL": settings["model"],
            "RIPPLE_LLM_MODELS": ",".join(settings.get("models", [settings["model"]])),
            "RIPPLE_AGENT_TOOL_TOKEN": self.tool_token,
            "RIPPLE_TOOL_BASE": tool_base.rstrip("/"),
            "OPENCODE_CONFIG": str(self.config_path),
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        })
        return env

    def ensure_started(self, tool_base: str) -> dict:
        settings = self._settings()
        with self._lock:
            healthy = self._health(settings)
            if healthy:
                # Never silently attach to an unrelated server on the chosen port.
                config = self._request("/config", timeout=3, settings=settings)
                if not isinstance(config, dict) or config.get("model") != f"ripple/{settings['model']}":
                    raise OpenCodeError("OpenCode 端口已被其他配置占用，请调整 RIPPLE_OPENCODE_PORT。", 409)
                return {"healthy": True, "version": healthy.get("version", ""), "model": settings["model"], "port": int(settings["port"])}
            self._write_config(settings)
            executable = self._binary()
            try:
                self.private_dir.mkdir(parents=True, exist_ok=True)
                self._process = subprocess.Popen(
                    [executable, "serve", "--hostname", "127.0.0.1", "--port", settings["port"], "--pure"],
                    cwd=str(self.project_root), env=self._process_env(settings, tool_base),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
                self._owned = True
            except OSError as exc:
                self._process = None
                raise OpenCodeError("无法启动 OpenCode Runtime。") from exc
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    raise OpenCodeError("OpenCode Runtime 启动后立即退出。")
                healthy = self._health(settings)
                if healthy:
                    return {"healthy": True, "version": healthy.get("version", ""), "model": settings["model"], "port": int(settings["port"])}
                time.sleep(.2)
            self.close()
            raise OpenCodeError("OpenCode Runtime 启动超时。")

    def status(self, tool_base: str, *, start: bool = False) -> dict:
        try:
            settings = self._settings()
        except OpenCodeError as exc:
            return {"configured": False, "healthy": False, "detail": str(exc), "runtime": self.runtime_id}
        if start:
            try:
                value = self.start_or_attach(tool_base)
                return {"configured": True, "runtime": self.runtime_id, **value}
            except OpenCodeError as exc:
                return {"configured": True, "healthy": False, "detail": str(exc), "runtime": self.runtime_id, "model": settings["model"]}
        health = self._health(settings)
        return {"configured": True, "healthy": bool(health), "runtime": self.runtime_id,
                "version": (health or {}).get("version", ""), "model": settings["model"], "port": int(settings["port"])}

    def _read_map(self) -> dict[str, str]:
        try:
            if not self.map_path.is_file() or self.map_path.stat().st_size > 1024 * 1024:
                return {}
            value = json.loads(self.map_path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in value.items() if str(v).startswith("ses")}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_map(self, value: dict[str, str]) -> None:
        _atomic_json(self.map_path, value)

    def mapped_session(self, web_id: str) -> str | None:
        with self._lock:
            return self._read_map().get(web_id)

    def web_session_for_remote(self, remote_session_id: str) -> str | None:
        with self._lock:
            for web_id, remote_id in self._read_map().items():
                if remote_id == remote_session_id:
                    return web_id
        return None

    def get_or_create(self, web_id: str, tool_base: str) -> str:
        self.ensure_started(tool_base)
        with self._lock:
            mapping = self._read_map()
            existing = mapping.get(web_id)
            if existing:
                try:
                    value = self._request(f"/session/{urllib.parse.quote(existing)}?directory={urllib.parse.quote(str(self.project_root))}")
                    if isinstance(value, dict) and value.get("id") == existing:
                        return existing
                except OpenCodeError:
                    mapping.pop(web_id, None)
            response = self._request(
                f"/session?directory={urllib.parse.quote(str(self.project_root))}", "POST",
                {"title": "Ripple 对话 " + web_id[:12]}, timeout=10,
            )
            if not isinstance(response, dict) or not str(response.get("id", "")).startswith("ses"):
                raise OpenCodeError("OpenCode 未返回有效会话标识。")
            session_id = str(response["id"])
            mapping[web_id] = session_id
            self._save_map(mapping)
            return session_id

    @staticmethod
    def _tool_artifact(tool_name: str, state: dict) -> dict | None:
        """Project a small, non-secret tool result into chat UI metadata."""
        if tool_name not in {"ripple_content_draft", "ripple_content"} or str(state.get("status") or "") != "completed":
            return None
        raw = state.get("output")
        if raw is None:
            raw = state.get("result")
        if isinstance(raw, str):
            if len(raw) > 64 * 1024:
                return None
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError):
                return None
        if not isinstance(raw, dict) or raw.get("kind") != "content_draft":
            return None
        content_id = str(raw.get("id") or "")
        version_id = str(raw.get("version_id") or "")
        if not re.fullmatch(r"[a-f0-9]{32}", content_id) or not re.fullmatch(r"[a-f0-9]{64}", version_id):
            return None
        return {
            "kind": "content_draft",
            "id": content_id,
            "version_id": version_id,
            "title": str(raw.get("title") or "")[:200],
            "status": "draft",
        }

    @staticmethod
    def _message_text(row: dict) -> tuple[str, str, list[str], list[dict], dict | None]:
        info = row.get("info") if isinstance(row, dict) else None
        parts = row.get("parts") if isinstance(row, dict) else None
        if not isinstance(info, dict) or not isinstance(parts, list):
            return "", "", [], [], None
        text, reasoning, activities, artifacts = [], [], [], []
        for part in parts:
            if not isinstance(part, dict):
                continue
            kind = part.get("type")
            if kind == "text" and part.get("text"):
                text.append(str(part["text"]))
            elif kind in {"reasoning", "thinking"} and part.get("text"):
                reasoning.append(str(part["text"]))
            elif kind == "tool":
                tool_name = str(part.get("tool") or part.get("name") or "Ripple 工具")
                state = part.get("state") if isinstance(part.get("state"), dict) else {}
                status = str(state.get("status") or "running")
                activities.append(f"{tool_name} · {status}")
                artifact = OpenCodeAgentAdapter._tool_artifact(tool_name, state)
                if artifact:
                    artifacts.append(artifact)
        return "".join(text), "".join(reasoning), activities, artifacts, info

    def _messages(self, session_id: str) -> list[dict]:
        value = self._request(
            f"/session/{urllib.parse.quote(session_id)}/message?directory={urllib.parse.quote(str(self.project_root))}",
            timeout=10,
        )
        return value if isinstance(value, list) else []

    def run_turn(self, web_id: str, user_text: str, system_text: str, files: list[dict], tool_base: str,
                 emit: Callable[[str, str], None], *, timeout: int = 600, model_id: str | None = None,
                 effort: str = "auto", effort_mode: str = "auto", enabled_tools: list[str] | None = None,
                 tool_bridge: AgentToolBridgeConfig | None = None) -> tuple[str, str]:
        # Serialize the whole remote turn, including session creation. The active
        # cancellation event belongs only to the lock owner, so a follow-up turn
        # can wait safely without overwriting the turn the user is stopping.
        lock = self._session_locks.setdefault(web_id, threading.Lock())
        if not lock.acquire(timeout=min(timeout, 300)):
            raise OpenCodeError("这个会话上一条仍在运行，请稍后重试。", 409)
        cancel = threading.Event()
        with self._lock:
            self._abort_events[web_id] = cancel
        session_id = ""
        try:
            session_id = self.create_or_resume_session(web_id, tool_base)
            if cancel.is_set():
                self._abort_session(session_id)
                return "", session_id
            before = self._messages(session_id)
            before_ids = {str((row.get("info") or {}).get("id")) for row in before if isinstance(row, dict)}
            parts: list[dict] = [{"type": "text", "text": user_text}]
            for item in files:
                path = Path(item["path"])
                mime = str(item.get("mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream")
                parts.append({"type": "file", "mime": mime, "filename": str(item.get("name") or path.name), "url": path.as_uri()})
            settings = self._settings()
            selected_model = (model_id or settings["model"]).strip()
            if selected_model not in settings.get("models", [settings["model"]]):
                raise OpenCodeError("所选模型未在 Ripple Agent 模型列表中启用。", 422)
            # Custom OpenCode tools are enabled by default unless explicitly overridden.
            # Start from deny for every Ripple-managed tool (plus legacy aliases), then
            # turn on only the Broker-produced allowlist for this turn.
            tools = {name: False for name in GENERIC_DENY}
            tools.update({name: False for name in INTERNAL_TOOLS})
            tools.update({"ripple_content": False, "ripple_publish": False})
            for name in enabled_tools or []:
                if not str(name).startswith("ripple_"):
                    raise OpenCodeError("Agent Tool 权限校验失败。", 403)
                tools[str(name)] = True
            effective_system = system_text
            if effort != "auto":
                if effort_mode != "instruction_fallback":
                    raise OpenCodeError("当前 Runtime 不支持请求的 Effort 模式。", 422)
                effort_text = {"low": "简洁推理，优先速度。", "medium": "使用适中的推理深度。", "high": "在需要时进行更深入的推理与检查。", "extra_high": "对复杂问题进行最充分的推理与自检。"}.get(effort, "")
                effective_system += f"\n\nRipple 会话 Effort={effort}（instruction fallback）：{effort_text} 不得因此扩大工具权限或外部副作用。"
            body = {"model": {"providerID": "ripple", "modelID": selected_model}, "agent": "build",
                    "system": effective_system, "tools": tools, "parts": parts}
            # The legacy async endpoint explicitly starts the agent loop and returns immediately.
            self._request(
                f"/session/{urllib.parse.quote(session_id)}/prompt_async?directory={urllib.parse.quote(str(self.project_root))}",
                "POST", body, timeout=15,
            )
            started = time.monotonic()
            emitted_text = ""
            emitted_reasoning = ""
            seen_activities: set[str] = set()
            seen_artifacts: set[str] = set()
            assistant_id = ""
            last_change = time.monotonic()
            while time.monotonic() - started < timeout:
                if cancel.is_set():
                    return emitted_text, session_id
                rows = self._messages(session_id)
                candidates = []
                for row in rows:
                    info = row.get("info") if isinstance(row, dict) else None
                    if not isinstance(info, dict) or info.get("role") != "assistant":
                        continue
                    message_id = str(info.get("id") or "")
                    if message_id and message_id not in before_ids:
                        candidates.append(row)
                if candidates:
                    row = candidates[-1]
                    text, reasoning, activities, artifacts, info = self._message_text(row)
                    assistant_id = str((info or {}).get("id") or assistant_id)
                    if text.startswith(emitted_text) and len(text) > len(emitted_text):
                        delta = text[len(emitted_text):]
                        emitted_text = text
                        emit("token", delta)
                        last_change = time.monotonic()
                    elif text and text != emitted_text:
                        # Defensive fallback when provider rewrites a part instead of appending.
                        emitted_text = text
                    if reasoning.startswith(emitted_reasoning) and len(reasoning) > len(emitted_reasoning):
                        delta = reasoning[len(emitted_reasoning):]
                        emitted_reasoning = reasoning
                        emit("thinking", delta)
                    for activity in activities:
                        if activity not in seen_activities:
                            seen_activities.add(activity)
                            emit("activity", "调用 " + activity)
                    for artifact in artifacts:
                        key = json.dumps(artifact, ensure_ascii=False, sort_keys=True)
                        if key not in seen_artifacts:
                            seen_artifacts.add(key)
                            emit("artifact", key)
                    error = (info or {}).get("error")
                    if error:
                        raise OpenCodeError("模型或工具执行失败，请检查 Agent Runtime 状态。", 502)
                    completed = ((info or {}).get("time") or {}).get("completed")
                    if completed:
                        return emitted_text, session_id
                # If a provider has no explicit completion timestamp, an idle session with visible output is terminal.
                if emitted_text and time.monotonic() - last_change > 1.0:
                    try:
                        status = self._request("/session/status", timeout=3)
                        current = status.get(session_id) if isinstance(status, dict) else None
                        if current in (None, "idle") or (isinstance(current, dict) and current.get("type") == "idle"):
                            return emitted_text, session_id
                    except OpenCodeError:
                        pass
                time.sleep(.25)
            self.abort(web_id)
            raise OpenCodeError("Ripple Agent 本轮执行超时。", 504)
        finally:
            with self._lock:
                if self._abort_events.get(web_id) is cancel:
                    self._abort_events.pop(web_id, None)
            lock.release()

    def _abort_session(self, session_id: str) -> bool:
        if not session_id:
            return False
        try:
            result = self._request(
                f"/session/{urllib.parse.quote(session_id)}/abort?directory={urllib.parse.quote(str(self.project_root))}",
                "POST", {}, timeout=8,
            )
            return bool(result) if result is not None else True
        except OpenCodeError:
            return False

    def abort(self, web_id: str) -> bool:
        with self._lock:
            active = self._abort_events.get(web_id)
            if active is not None:
                active.set()
        remote = self._abort_session(self.mapped_session(web_id) or "")
        return active is not None or remote

    def stop_turn(self, web_id: str, timeout: float = 5.0) -> bool:
        """Cancel the active turn and wait for that exact turn to release its lock."""
        with self._lock:
            active = self._abort_events.get(web_id)
            if active is not None:
                active.set()
        remote = self._abort_session(self.mapped_session(web_id) or "")
        stopped = active is not None or remote
        if active is None:
            return stopped
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._lock:
                if self._abort_events.get(web_id) is not active:
                    return stopped
            time.sleep(.05)
        return stopped

    def delete_session(self, session_id: str) -> bool:
        if not session_id.startswith("ses"):
            return False
        try:
            result = self._request(
                f"/session/{urllib.parse.quote(session_id)}?directory={urllib.parse.quote(str(self.project_root))}",
                "DELETE", timeout=8,
            )
            with self._lock:
                mapping = self._read_map()
                mapping = {k: v for k, v in mapping.items() if v != session_id}
                self._save_map(mapping)
            return result is not False
        except OpenCodeError:
            return False

    def close(self) -> None:
        with self._lock:
            process = self._process
            self._process = None
            owned = self._owned
            self._owned = False
        if owned and process and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=8)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass


# Backward-compatible import name for tests and third-party local integrations.
OpenCodeRuntime = OpenCodeAgentAdapter
