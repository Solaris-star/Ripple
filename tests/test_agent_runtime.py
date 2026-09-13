from __future__ import annotations

import pytest

from ripple.agent_runtime import AgentRuntimeError, AgentRuntimeManager


class FakeAdapter:
    def __init__(self, runtime_id: str, token: str, remote_map: dict[str, str] | None = None):
        self.runtime_id = runtime_id
        self.display_name = runtime_id.title()
        self.tool_token = token
        self.remote_map = remote_map or {}
        self.calls: list[tuple] = []

    def detect(self) -> dict:
        self.calls.append(("detect",))
        return {"runtime": self.runtime_id, "name": self.display_name, "installed": True}

    def capabilities(self) -> dict:
        return {"streaming": True, "restricted_mode": True}

    def start_or_attach(self, tool_base: str) -> dict:
        self.calls.append(("start_or_attach", tool_base))
        return {"healthy": True}

    def create_or_resume_session(self, web_session_id: str, tool_base: str) -> str:
        self.calls.append(("create_or_resume_session", web_session_id, tool_base))
        return f"{self.runtime_id}-session"

    def status(self, tool_base: str, *, start: bool = False) -> dict:
        self.calls.append(("status", tool_base, start))
        return {"healthy": True, "version": "fixture"}

    def run_turn(self, *args, **kwargs) -> tuple[str, str]:
        self.calls.append(("run_turn", args, kwargs))
        return "ok", f"{self.runtime_id}-session"

    def stop_turn(self, web_id: str, timeout: float = 5.0) -> bool:
        self.calls.append(("stop_turn", web_id, timeout))
        return True

    def has_active_turns(self) -> bool:
        self.calls.append(("has_active_turns",))
        return False

    def delete_session(self, session_id: str) -> bool:
        self.calls.append(("delete_session", session_id))
        return True

    def web_session_for_remote(self, remote_session_id: str) -> str | None:
        self.calls.append(("web_session_for_remote", remote_session_id))
        return self.remote_map.get(remote_session_id)

    def close(self) -> None:
        self.calls.append(("close",))


def test_manager_routes_default_runtime_and_normalizes_status():
    token = "shared-tool-token"
    opencode = FakeAdapter("opencode", token)
    claude = FakeAdapter("claude_code", token)
    manager = AgentRuntimeManager([opencode, claude], default_runtime="opencode", tool_token=token)

    assert manager.runtime_ids() == ("opencode", "claude_code")
    assert manager.detect("opencode")["installed"] is True
    assert [item["runtime"] for item in manager.detect()] == ["opencode", "claude_code"]
    assert manager.capabilities()["restricted_mode"] is True
    assert manager.start_or_attach("http://127.0.0.1:7860") == {"healthy": True}
    assert manager.create_or_resume_session("web", "http://127.0.0.1:7860") == "opencode-session"
    assert manager.status("http://127.0.0.1:7860", start=True) == {
        "healthy": True, "version": "fixture", "runtime": "opencode",
    }
    assert manager.run_turn("web", "hello", "system", [], "base", lambda *_: None) == ("ok", "opencode-session")
    assert manager.get("claude_code") is claude
    assert ("status", "http://127.0.0.1:7860", True) in opencode.calls


def test_manager_rejects_unknown_runtime_and_token_mismatch():
    manager = AgentRuntimeManager([FakeAdapter("opencode", "token")], default_runtime="opencode", tool_token="token")

    with pytest.raises(AgentRuntimeError) as unknown:
        manager.get("missing")
    assert unknown.value.status == 404

    with pytest.raises(AgentRuntimeError) as mismatch:
        manager.register(FakeAdapter("claude_code", "other-token"))
    assert mismatch.value.status == 500


def test_manager_resolves_remote_sessions_and_delegates_lifecycle():
    token = "token"
    opencode = FakeAdapter("opencode", token)
    claude = FakeAdapter("claude_code", token, {"claude-remote": "web-claude"})
    manager = AgentRuntimeManager([opencode, claude], default_runtime="opencode", tool_token=token)

    assert manager.web_session_for_remote("claude-remote") == "web-claude"
    assert manager.stop_turn("web-default", 2.0) is True
    assert manager.has_active_turns("claude_code") is False
    assert manager.delete_session("remote-default") is True
    manager.close()

    assert ("stop_turn", "web-default", 2.0) in opencode.calls
    assert ("has_active_turns",) in claude.calls
    assert ("delete_session", "remote-default") in opencode.calls
    assert ("close",) in opencode.calls
    assert ("close",) in claude.calls
