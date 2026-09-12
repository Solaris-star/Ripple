from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from ripple.agent_runtime import AgentToolBridgeConfig
from ripple.claude_code_runtime import ClaudeCodeAgentAdapter


class FakeClient:
    created: list["FakeClient"] = []

    def __init__(self, command, cwd):
        self.command = command
        self.cwd = cwd
        self.requests = []
        self.cancelled = []
        self.closed = False
        FakeClient.created.append(self)

    def start(self):
        return None

    def request(self, method, params, *, timeout=60):
        self.requests.append((method, params, timeout))
        if method == "session/new":
            return {"sessionId": "claude-session-1"}
        if method == "session/load":
            return {"sessionId": params["sessionId"]}
        return {}

    def prompt(self, session_id, prompt, emit, *, timeout):
        self.requests.append(("session/prompt", {"sessionId": session_id, "prompt": prompt}, timeout))
        emit("thinking", "thinking")
        emit("token", "CLAUDE_OK")
        return {"stopReason": "end_turn"}

    def cancel(self, session_id):
        self.cancelled.append(session_id)

    def close(self):
        self.closed = True


def make_adapter(tmp_path, monkeypatch):
    FakeClient.created.clear()
    adapter = ClaudeCodeAgentAdapter(tmp_path / "private", tool_token="tool-token", client_factory=FakeClient)
    monkeypatch.setattr(adapter, "_binary", lambda: str(tmp_path / "claude.exe"))
    monkeypatch.setattr(adapter, "_command", lambda: ["node", "claude-agent-acp.js"])
    monkeypatch.setattr(adapter, "_version_text", lambda: "2.1.fixture")
    monkeypatch.setattr(adapter, "detect", lambda: {
        "runtime": "claude_code", "name": "Claude Code", "installed": True, "bridge_installed": True,
        "ready": True, "version": "2.1.fixture", "detail": "",
    })
    return adapter


def test_claude_detect_reports_authentication_state(tmp_path, monkeypatch):
    from ripple import claude_code_runtime as module

    adapter = ClaudeCodeAgentAdapter(tmp_path / "private", tool_token="token", client_factory=FakeClient)
    monkeypatch.setattr(adapter, "_binary", lambda: str(tmp_path / "claude.exe"))
    monkeypatch.setattr(adapter, "_command", lambda: ["node", "claude-agent-acp.js"])
    monkeypatch.setattr(adapter, "_version_text", lambda: "fixture")
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(
        returncode=0, stdout='{"loggedIn": false}', stderr='',
    ))
    state = adapter.detect()
    assert state["installed"] is True
    assert state["authenticated"] is False
    assert state["ready"] is False
    assert "登录" in state["detail"]


def test_claude_adapter_disables_native_tools_and_uses_only_turn_mcp(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, monkeypatch)
    events = []
    bridge = AgentToolBridgeConfig("http://127.0.0.1:7860/api/agent/mcp/", "lease-token")

    text, session_id = adapter.run_turn(
        "web-1", "hello", "SYSTEM", [], "http://127.0.0.1:7860",
        lambda kind, value: events.append((kind, value)),
        model_id="sonnet", effort="high", enabled_tools=["ripple_ideas_list"], tool_bridge=bridge,
    )

    assert text == "CLAUDE_OK" and session_id == "claude-session-1"
    client = FakeClient.created[0]
    method, params, _ = next(row for row in client.requests if row[0] == "session/new")
    assert method == "session/new"
    assert params["mcpServers"] == [{
        "name": "ripple", "type": "http", "url": bridge.endpoint,
        "headers": [{"name": "Authorization", "value": "Bearer lease-token"}],
    }]
    meta = params["_meta"]
    assert meta["disableBuiltInTools"] is True
    options = meta["claudeCode"]["options"]
    assert options["tools"] == []
    assert options["settingSources"] == []
    assert options["hooks"] == {}
    assert options["mcpServers"] == {}
    assert options["model"] == "sonnet"
    assert options["effort"] == "high"
    assert ("thinking", "thinking") in events and ("token", "CLAUDE_OK") in events
    assert client.closed is True


def test_claude_adapter_resumes_native_session_with_fresh_bridge_lease(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, monkeypatch)
    first = AgentToolBridgeConfig("http://127.0.0.1:7860/api/agent/mcp/", "lease-one")
    second = AgentToolBridgeConfig("http://127.0.0.1:7860/api/agent/mcp/", "lease-two")
    adapter.run_turn("web-1", "one", "S", [], "base", lambda *_: None, enabled_tools=["ripple_accounts"], tool_bridge=first)
    adapter.run_turn("web-1", "two", "S", [], "base", lambda *_: None, enabled_tools=["ripple_accounts"], tool_bridge=second)

    assert len(FakeClient.created) == 2
    load = next(row for row in FakeClient.created[1].requests if row[0] == "session/load")
    assert load[1]["sessionId"] == "claude-session-1"
    assert load[1]["mcpServers"][0]["headers"][0]["value"] == "Bearer lease-two"
    assert adapter.mapped_session("web-1") == "claude-session-1"


def test_claude_adapter_embeds_only_explicit_text_and_image_attachments(tmp_path, monkeypatch):
    adapter = make_adapter(tmp_path, monkeypatch)
    text_file = tmp_path / "note.md"; text_file.write_text("attachment text", encoding="utf-8")
    image_file = tmp_path / "image.png"; image_file.write_bytes(b"\x89PNGfixture")
    pdf_file = tmp_path / "doc.pdf"; pdf_file.write_bytes(b"%PDF fixture")
    bridge = AgentToolBridgeConfig("http://127.0.0.1:7860/api/agent/mcp/", "lease")

    adapter.run_turn("web", "inspect", "S", [
        {"path": str(text_file), "name": "note.md", "mime": "text/markdown"},
        {"path": str(image_file), "name": "image.png", "mime": "image/png"},
        {"path": str(pdf_file), "name": "doc.pdf", "mime": "application/pdf"},
    ], "base", lambda *_: None, enabled_tools=[], tool_bridge=bridge)

    prompt = next(row for row in FakeClient.created[0].requests if row[0] == "session/prompt")[1]["prompt"]
    assert any(row.get("type") == "image" and row.get("mimeType") == "image/png" for row in prompt)
    joined = "\n".join(str(row.get("text") or "") for row in prompt if row.get("type") == "text")
    assert "attachment text" in joined
    assert "cannot read that binary type" in joined
    assert str(pdf_file) not in joined
