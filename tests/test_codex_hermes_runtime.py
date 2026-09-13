from __future__ import annotations

from pathlib import Path

from ripple.codex_runtime import CodexAgentAdapter, CodexAcpAgentAdapter
from ripple.agent_runtime import AgentToolBridgeConfig
from ripple.hermes_runtime import HermesAgentAdapter


def test_codex_safe_model_strips_host_capabilities():
    row = {
        "slug": "fixture",
        "display_name": "Fixture",
        "shell_type": "unified_exec",
        "apply_patch_tool_type": "freeform",
        "supports_search_tool": True,
        "experimental_supported_tools": ["danger"],
        "include_skills_usage_instructions": True,
        "include_plugin_usage_instructions": True,
        "include_apps_usage_instructions": True,
        "node_repl_disabled": False,
    }
    safe = CodexAgentAdapter._safe_model(row)
    assert safe["shell_type"] == "disabled"
    assert safe["apply_patch_tool_type"] is None
    assert safe["supports_search_tool"] is False
    assert safe["experimental_supported_tools"] == []
    assert safe["include_skills_usage_instructions"] is False
    assert safe["include_plugin_usage_instructions"] is False
    assert safe["include_apps_usage_instructions"] is False
    assert safe["node_repl_disabled"] is True


def test_codex_tool_probe_extracts_function_names():
    payload = {
        "tools": [
            {"type": "function", "name": "request_user_input"},
            {"type": "function", "function": {"name": "shell_exec"}},
        ]
    }
    assert CodexAgentAdapter._tool_names(payload) == ["request_user_input", "shell_exec"]


def test_codex_mcp_bridge_is_auto_approved_only_for_leased_ripple_server(tmp_path: Path):
    adapter = CodexAgentAdapter(tmp_path / "private", tool_token="token")
    adapter._features = set()
    adapter._native_model_rows = lambda: []
    adapter._command = lambda: ["codex"]
    args = adapter._common_exec_args(
        "fixture", "auto", "system",
        tool_bridge=AgentToolBridgeConfig(endpoint="http://127.0.0.1:7860/api/agent/mcp/", token="lease-token"),
    )
    overrides = [args[i + 1] for i, value in enumerate(args[:-1]) if value == "-c"]
    assert 'mcp_servers.ripple.default_tools_approval_mode="approve"' in overrides
    assert 'approval_policy="never"' in overrides


class FakeProvisioning:
    def resolve_command(self, adapter_id: str):
        assert adapter_id == "codex_acp"
        return ["node", "codex-acp.js"]


def test_codex_acp_config_disables_native_tools_and_does_not_copy_global_mcp(tmp_path: Path, monkeypatch):
    adapter = CodexAcpAgentAdapter(tmp_path / "private", tool_token="token", provisioning_service=FakeProvisioning())
    monkeypatch.setattr(adapter._native, "_profile_config", lambda: {
        "model": "gpt-safe",
        "mcp_servers": {"global-danger": {"command": "danger"}},
        "features": {"shell_tool": True},
    })
    config = adapter._safe_config("SYSTEM", "gpt-safe")

    assert config["approval_policy"] == "never"
    assert config["features"]["shell_tool"] is False
    assert config["features"]["view_image"] is False
    assert config["tools"]["update_plan"]["enabled"] is False
    assert config["tools"]["experimental_request_user_input"]["enabled"] is False
    assert "mcp_servers" not in config
    assert config["developer_instructions"] == "SYSTEM"


def test_codex_acp_only_injects_leased_loopback_ripple_mcp(tmp_path: Path):
    server = CodexAcpAgentAdapter._mcp_servers(AgentToolBridgeConfig(
        endpoint="http://127.0.0.1:7860/api/agent/mcp/", token="lease-token",
    ))
    assert server == [{
        "name": "ripple", "type": "http", "url": "http://127.0.0.1:7860/api/agent/mcp/",
        "headers": [{"name": "Authorization", "value": "Bearer lease-token"}],
    }]


def test_hermes_docker_wrapper_reports_missing_docker(tmp_path: Path, monkeypatch):
    wrapper = tmp_path / "hermes.cmd"
    wrapper.write_text("@echo off\ndocker exec -it hermes-agent hermes %*\n", encoding="utf-8")
    monkeypatch.setenv("RIPPLE_HERMES_BIN", str(wrapper))
    monkeypatch.setattr("ripple.hermes_runtime.shutil.which", lambda _name: None)
    adapter = HermesAgentAdapter(tmp_path / "private", tool_token="token")
    state = adapter.detect()
    assert state["installed"] is True
    assert state["ready"] is False
    assert state["dependency"] == "docker"
    assert "Docker" in state["detail"]
    assert adapter.capabilities()["restricted_mode"] is False


class _FakeHermesClient:
    instances = []

    def __init__(self, command, cwd):
        self.command = command
        self.cwd = cwd
        self.prompts = []
        self.__class__.instances.append(self)

    def start(self):
        return None

    def request(self, method, params, *, timeout):
        if method == "session/new":
            return {"sessionId": "fixture-hermes-session-0001"}
        return {}

    def prompt(self, session_id, prompt, emit, *, timeout):
        self.prompts.append((session_id, prompt))
        emit("token", "ok")
        return {"stopReason": "end_turn"}

    def close(self):
        return None


def test_hermes_injects_system_context_only_on_new_session(tmp_path: Path, monkeypatch):
    _FakeHermesClient.instances.clear()
    adapter = HermesAgentAdapter(
        tmp_path / "private", tool_token="token", project_root=Path(__file__).parents[1],
        client_factory=_FakeHermesClient,
    )
    monkeypatch.setattr(adapter, "start_or_attach", lambda _tool_base: {"healthy": True})
    monkeypatch.setattr(adapter, "_command", lambda: ["python", "shim.py"])
    emitted = []

    first, first_remote = adapter.run_turn(
        "web-session", "FIRST", "SYSTEM", [], "http://127.0.0.1:7860", lambda kind, text: emitted.append((kind, text)),
    )
    second, second_remote = adapter.run_turn(
        "web-session", "SECOND", "SYSTEM", [], "http://127.0.0.1:7860", lambda kind, text: emitted.append((kind, text)),
    )

    assert first == second == "ok"
    assert first_remote == second_remote == "fixture-hermes-session-0001"
    assert _FakeHermesClient.instances[0].prompts[0][1][0]["text"] == "SYSTEM\n\nFIRST"
    assert _FakeHermesClient.instances[1].prompts[0][1][0]["text"] == "SECOND"
