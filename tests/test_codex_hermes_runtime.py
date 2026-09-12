from __future__ import annotations

from pathlib import Path

from ripple.codex_runtime import CodexAgentAdapter
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
