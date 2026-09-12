"""Hermes runtime discovery boundary.

Hermes installations vary between a native CLI and thin wrappers around a Docker
container.  Ripple reports those prerequisites precisely and keeps execution
fail-closed until a Hermes transport can prove the same restricted capability
boundary used by the other native adapters.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

from .agent_runtime import AgentRuntimeError, AgentToolBridgeConfig


class HermesAgentAdapter:
    runtime_id = "hermes"
    display_name = "Hermes"

    def __init__(self, private_dir: Path, *, tool_token: str):
        self.private_dir = private_dir.resolve() / "hermes"
        self.tool_token = tool_token

    @staticmethod
    def _entry() -> str:
        explicit = os.environ.get("RIPPLE_HERMES_BIN", "").strip()
        candidates = [explicit, shutil.which("hermes.exe") or "", shutil.which("hermes") or "", shutil.which("hermes.cmd") or ""]
        return next((value for value in candidates if value and Path(value).exists()), "")

    @staticmethod
    def _wrapper_text(path: str) -> str:
        try:
            value = Path(path)
            if value.suffix.lower() not in {".cmd", ".bat", ".ps1", ".sh"} or value.stat().st_size > 256 * 1024:
                return ""
            return value.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""

    def detect(self) -> dict:
        entry = self._entry()
        if not entry:
            return {"runtime": self.runtime_id, "name": self.display_name, "installed": False, "ready": False,
                    "dependency": "", "detail": "未检测到 Hermes。"}
        wrapper = self._wrapper_text(entry).lower()
        docker_backed = "docker" in wrapper
        if docker_backed:
            docker = shutil.which("docker.exe") or shutil.which("docker")
            if not docker:
                return {"runtime": self.runtime_id, "name": self.display_name, "installed": True, "ready": False,
                        "source": "docker_wrapper", "dependency": "docker", "detail": "检测到 Hermes，但当前入口依赖 Docker；本机未检测到 Docker。"}
            container = os.environ.get("RIPPLE_HERMES_CONTAINER", "hermes-agent").strip() or "hermes-agent"
            try:
                probe = subprocess.run([docker, "inspect", "-f", "{{.State.Running}}", container], capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=8, check=False)
                if probe.returncode != 0 or (probe.stdout or "").strip().lower() != "true":
                    return {"runtime": self.runtime_id, "name": self.display_name, "installed": True, "ready": False,
                            "source": "docker_wrapper", "dependency": "hermes_container",
                            "detail": f"检测到 Hermes Docker 入口，但容器 {container} 当前未运行。"}
            except (OSError, subprocess.SubprocessError):
                return {"runtime": self.runtime_id, "name": self.display_name, "installed": True, "ready": False,
                        "source": "docker_wrapper", "dependency": "docker", "detail": "Hermes Docker Runtime 当前不可连接。"}
            return {"runtime": self.runtime_id, "name": self.display_name, "installed": True, "ready": False,
                    "source": "docker_wrapper", "dependency": "restricted_transport",
                    "detail": "Hermes Runtime 已就绪，但当前版本尚未完成 Ripple restricted-mode transport 验证。"}
        return {"runtime": self.runtime_id, "name": self.display_name, "installed": True, "ready": False,
                "source": "native", "dependency": "restricted_transport",
                "detail": "检测到原生 Hermes；当前版本尚未完成 Ripple restricted-mode transport 验证。"}

    def capabilities(self) -> dict:
        state = self.detect()
        return {"streaming": False, "thinking": False, "attachments": False, "session_resume": False, "cancel": False,
                "native_model_selection": False, "native_effort": False, "mcp": False, "ripple_tools": False,
                "profile_import": bool(state.get("installed")), "restricted_mode": False}

    def status(self, tool_base: str, *, start: bool = False) -> dict:
        state = self.detect()
        return {"configured": bool(state.get("installed")), "healthy": False, "runtime": self.runtime_id,
                "version": "", "detail": state.get("detail", ""), "dependency": state.get("dependency", ""),
                "capabilities": self.capabilities()}

    def _unavailable(self) -> None:
        state = self.detect()
        raise AgentRuntimeError(str(state.get("detail") or "Hermes 当前不可用于 Ripple。"), 409)

    def start_or_attach(self, tool_base: str) -> dict:
        self._unavailable()

    def create_or_resume_session(self, web_session_id: str, tool_base: str) -> str:
        self._unavailable()

    def run_turn(self, web_id: str, user_text: str, system_text: str, files: list[dict], tool_base: str,
                 emit, *, timeout: int = 600, model_id: str | None = None, effort: str = "auto",
                 effort_mode: str = "auto", enabled_tools: list[str] | None = None,
                 tool_bridge: AgentToolBridgeConfig | None = None) -> tuple[str, str]:
        self._unavailable()

    def stop_turn(self, web_id: str, timeout: float = 5.0) -> bool:
        return False

    def delete_session(self, session_id: str) -> bool:
        return False

    def web_session_for_remote(self, remote_session_id: str) -> str | None:
        return None

    def close(self) -> None:
        return None
