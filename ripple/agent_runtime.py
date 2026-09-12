"""Ripple-owned abstraction for pluggable local Agent runtimes.

The web layer talks to :class:`AgentRuntimeManager`, not to a concrete Agent
implementation.  Adapters own provider-specific session and streaming details;
Ripple keeps the business capability broker and tool-auth boundary above them.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


class AgentRuntimeError(RuntimeError):
    """Runtime failure safe to surface through Ripple's HTTP boundary."""

    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class AgentToolBridgeConfig:
    endpoint: str
    token: str


@runtime_checkable
class AgentRuntimeAdapter(Protocol):
    """Minimum contract implemented by a local Agent runtime adapter."""

    runtime_id: str
    display_name: str
    tool_token: str

    def detect(self) -> dict: ...

    def capabilities(self) -> dict: ...

    def start_or_attach(self, tool_base: str) -> dict: ...

    def create_or_resume_session(self, web_session_id: str, tool_base: str) -> str: ...

    def status(self, tool_base: str, *, start: bool = False) -> dict: ...

    def run_turn(
        self,
        web_id: str,
        user_text: str,
        system_text: str,
        files: list[dict],
        tool_base: str,
        emit: Callable[[str, str], None],
        *,
        timeout: int = 600,
        model_id: str | None = None,
        effort: str = "auto",
        effort_mode: str = "auto",
        enabled_tools: list[str] | None = None,
        tool_bridge: AgentToolBridgeConfig | None = None,
    ) -> tuple[str, str]: ...

    def stop_turn(self, web_id: str, timeout: float = 5.0) -> bool: ...

    def delete_session(self, session_id: str) -> bool: ...

    def web_session_for_remote(self, remote_session_id: str) -> str | None: ...

    def close(self) -> None: ...


class AgentRuntimeManager:
    """Registry and dispatch boundary for Ripple's pluggable Agent runtimes.

    Phase 1 keeps one default runtime (OpenCode).  The registry already accepts
    additional adapters so later phases can bind a conversation to Claude Code,
    Codex or Hermes without reintroducing concrete runtime checks in web/app.py.
    """

    def __init__(self, adapters: list[AgentRuntimeAdapter], *, default_runtime: str, tool_token: str):
        token = str(tool_token or "").strip()
        if not token:
            raise ValueError("Agent tool token is required")
        self.tool_token = token
        self._adapters: dict[str, AgentRuntimeAdapter] = {}
        for adapter in adapters:
            self.register(adapter)
        if default_runtime not in self._adapters:
            raise AgentRuntimeError(f"默认 Agent Runtime 未注册：{default_runtime}", 500)
        self.default_runtime_id = default_runtime

    def register(self, adapter: AgentRuntimeAdapter) -> None:
        runtime_id = str(getattr(adapter, "runtime_id", "") or "").strip()
        if not runtime_id:
            raise AgentRuntimeError("Agent Runtime 缺少稳定标识。", 500)
        if runtime_id in self._adapters:
            raise AgentRuntimeError(f"Agent Runtime 重复注册：{runtime_id}", 409)
        if str(getattr(adapter, "tool_token", "") or "") != self.tool_token:
            raise AgentRuntimeError(f"Agent Runtime {runtime_id} 未绑定 Ripple Tool Token。", 500)
        self._adapters[runtime_id] = adapter

    def get(self, runtime_id: str | None = None) -> AgentRuntimeAdapter:
        target = runtime_id or self.default_runtime_id
        adapter = self._adapters.get(target)
        if adapter is None:
            raise AgentRuntimeError(f"Agent Runtime 未注册：{target}", 404)
        return adapter

    def runtime_ids(self) -> tuple[str, ...]:
        return tuple(self._adapters)

    def detect(self, runtime_id: str | None = None) -> dict | list[dict]:
        if runtime_id is not None:
            return self.get(runtime_id).detect()
        return [adapter.detect() for adapter in self._adapters.values()]

    def capabilities(self, runtime_id: str | None = None) -> dict:
        return self.get(runtime_id).capabilities()

    def start_or_attach(self, tool_base: str, *, runtime_id: str | None = None) -> dict:
        return self.get(runtime_id).start_or_attach(tool_base)

    def create_or_resume_session(self, web_session_id: str, tool_base: str, *, runtime_id: str | None = None) -> str:
        return self.get(runtime_id).create_or_resume_session(web_session_id, tool_base)

    def status(self, tool_base: str, *, start: bool = False, runtime_id: str | None = None) -> dict:
        adapter = self.get(runtime_id)
        value = adapter.status(tool_base, start=start)
        result = dict(value) if isinstance(value, dict) else {}
        result["runtime"] = adapter.runtime_id
        return result

    def run_turn(self, *args, runtime_id: str | None = None, **kwargs) -> tuple[str, str]:
        return self.get(runtime_id).run_turn(*args, **kwargs)

    def stop_turn(self, web_id: str, timeout: float = 5.0, *, runtime_id: str | None = None) -> bool:
        return self.get(runtime_id).stop_turn(web_id, timeout)

    def delete_session(self, session_id: str, *, runtime_id: str | None = None) -> bool:
        if runtime_id is not None:
            return self.get(runtime_id).delete_session(session_id)
        preferred = self.get()
        if preferred.web_session_for_remote(session_id) is not None:
            return preferred.delete_session(session_id)
        for rid, adapter in self._adapters.items():
            if rid == self.default_runtime_id:
                continue
            if adapter.web_session_for_remote(session_id) is not None:
                return adapter.delete_session(session_id)
        # Backward compatibility: older callers may only have a native session
        # key and no persisted reverse mapping yet.
        return preferred.delete_session(session_id)

    def web_session_for_remote(self, remote_session_id: str, *, runtime_id: str | None = None) -> str | None:
        if runtime_id is not None:
            return self.get(runtime_id).web_session_for_remote(remote_session_id)
        # Prefer the active/default runtime, then scan registered adapters.  Later
        # runtime-aware Tool Bridge requests can pass runtime_id explicitly.
        preferred = self.get()
        value = preferred.web_session_for_remote(remote_session_id)
        if value:
            return value
        for rid, adapter in self._adapters.items():
            if rid == self.default_runtime_id:
                continue
            value = adapter.web_session_for_remote(remote_session_id)
            if value:
                return value
        return None

    def close(self) -> None:
        for adapter in self._adapters.values():
            adapter.close()
