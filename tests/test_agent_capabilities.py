from __future__ import annotations

import pytest

from ripple.agent_capabilities import AgentCapabilityRegistry
from ripple.publishing import WorkflowError


def skills():
    return [
        {"name": "skill-xhs-analyzer", "description": "read", "layer": "discover", "apiConfigured": True},
        {"name": "xhs-note-creator", "description": "write", "layer": "produce", "apiConfigured": True},
        {"name": "skill-xhs-comment-reply", "description": "reply", "layer": "publish", "apiConfigured": True},
        {"name": "text-polisher", "description": "polish", "layer": "produce", "apiConfigured": True},
        {"name": "unbound-skill", "description": "guidance only", "layer": "produce", "apiConfigured": True},
    ]


def models(reg: AgentCapabilityRegistry):
    return reg.save_models([
        {"id": "plain-model", "name": "Plain", "effort_levels": ["auto"]},
        {"id": "reasoning-model", "name": "Reason", "effort_levels": ["auto", "low", "medium", "high", "extra_high"]},
    ], "plain-model")


def test_legacy_model_migrates_as_default_without_fabricated_effort(tmp_path):
    reg = AgentCapabilityRegistry(tmp_path)
    state = reg.model_state("legacy-model")
    assert state["default_model"] == "legacy-model"
    assert state["models"] == [{"id": "legacy-model", "name": "legacy-model", "effort_levels": ["auto"], "supports_reasoning": False, "source": "legacy"}]


def test_session_effort_uses_instruction_fallback_for_any_model(tmp_path):
    reg = AgentCapabilityRegistry(tmp_path); state = models(reg)
    cfg = reg.update_session("web-session", {"model": "reasoning-model", "effort": "high"}, state, skills())
    assert cfg["model"] == "reasoning-model" and cfg["effort"] == "high"
    assert cfg["effort_mode"] == "instruction_fallback"
    plain = reg.update_session("plain-session", {"model": "plain-model", "effort": "extra_high"}, state, skills())
    assert plain["effort"] == "extra_high" and plain["effort_mode"] == "instruction_fallback"
    with pytest.raises(WorkflowError, match="Effort 无效"):
        reg.update_session("bad-session", {"effort": "ultra"}, state, skills())


def test_session_persists_runtime_and_matching_profile(tmp_path):
    reg = AgentCapabilityRegistry(tmp_path); state = models(reg)
    scope = {
        "runtime_ids": ["opencode", "claude_code", "codex"],
        "default_runtime": "opencode",
        "profile_runtimes": {"opencode:default": "opencode", "claude_code:default": "claude_code", "codex:default": "codex"},
        "default_profile": "opencode:default",
    }
    cfg = reg.update_session("native", {"runtime_id": "claude_code", "profile_id": "claude_code:default"}, state, skills(), **scope)
    assert cfg["runtime_id"] == "claude_code" and cfg["profile_id"] == "claude_code:default"
    assert reg.get_session("native", state, skills(), **scope)["runtime_id"] == "claude_code"
    switched = reg.update_session("native", {"runtime_id": "codex"}, state, skills(), **scope)
    assert switched["profile_id"] == "codex:default"
    with pytest.raises(WorkflowError, match="不匹配"):
        reg.update_session("native", {"profile_id": "claude_code:default"}, state, skills(), **scope)
    with pytest.raises(WorkflowError, match="Runtime 未注册"):
        reg.update_session("native", {"runtime_id": "unknown"}, state, skills(), **scope)


def test_session_model_is_validated_against_selected_runtime(tmp_path):
    reg = AgentCapabilityRegistry(tmp_path); state = models(reg)
    scope = {
        "runtime_ids": ["opencode", "codex"],
        "default_runtime": "codex",
        "profile_runtimes": {"opencode:default": "opencode", "codex:default": "codex"},
        "default_profile": "codex:default",
        "runtime_model_states": {
            "opencode": state,
            "codex": {"default_model": "codex-a", "models": [
                {"id": "codex-a", "name": "Codex A", "effort_levels": ["auto", "high"]},
                {"id": "codex-b", "name": "Codex B", "effort_levels": ["auto", "medium"]},
            ]},
        },
    }
    cfg = reg.get_session("codex-model", state, skills(), **scope)
    assert cfg["runtime_id"] == "codex" and cfg["model"] == "codex-a"
    changed = reg.update_session("codex-model", {"model": "codex-b"}, state, skills(), **scope)
    assert changed["model"] == "codex-b"
    with pytest.raises(WorkflowError, match="当前 Agent"):
        reg.update_session("codex-model", {"model": "plain-model"}, state, skills(), **scope)


def test_runtime_profile_lock_prevents_mid_conversation_switch(tmp_path):
    reg = AgentCapabilityRegistry(tmp_path); state = models(reg)
    scope = {
        "runtime_ids": ["opencode", "claude_code"],
        "default_runtime": "opencode",
        "profile_runtimes": {"opencode:default": "opencode", "claude_code:default": "claude_code"},
        "default_profile": "opencode:default",
    }
    reg.update_session("locked", {"runtime_id": "claude_code", "profile_id": "claude_code:default"}, state, skills(), **scope)
    reg.lock_runtime_profile("locked", "claude_code", "claude_code:default")
    cfg = reg.get_session("locked", state, skills(), **scope)
    assert cfg["runtime_locked"] is True
    assert cfg["runtime_id"] == "claude_code" and cfg["profile_id"] == "claude_code:default"
    with pytest.raises(WorkflowError, match="Runtime 已锁定"):
        reg.update_session("locked", {"runtime_id": "opencode"}, state, skills(), **scope)
    with pytest.raises(WorkflowError, match="Profile 已锁定"):
        reg.update_session("locked", {"profile_id": "opencode:default"}, state, skills(), **scope)
    # Effort/Skill controls stay mutable after Runtime/Profile are fixed.
    changed = reg.update_session("locked", {"effort": "high"}, state, skills(), **scope)
    assert changed["runtime_locked"] is True and changed["effort"] == "high"


def test_skill_injection_grants_only_manifest_bound_tools_and_raw_tool_stays_internal(tmp_path):
    reg = AgentCapabilityRegistry(tmp_path); state = models(reg)
    reg.update_session("s", {"enabled_tools": []}, state, skills())
    turn = reg.resolve_turn("s", state, skills(), turn_skills=["text-polisher"])
    assert turn["skills"] == ["text-polisher"]
    assert set(turn["tools"]) == {"ripple_operation", "ripple_content_read", "ripple_content_draft"}
    assert "bash" not in turn["tools"] and "read" not in turn["tools"]
    unbound = reg.resolve_turn("s", state, skills(), turn_skills=["unbound-skill"])
    assert unbound["tools"] == []
    with pytest.raises(WorkflowError, match="未注册"):
        reg.resolve_turn("s", state, skills(), turn_tools=["bash"])


def test_plugin_expands_only_registered_bundle_capabilities(tmp_path):
    reg = AgentCapabilityRegistry(tmp_path); state = models(reg)
    reg.update_session("s", {"enabled_plugins": ["xiaohongshu_ops"], "enabled_tools": []}, state, skills())
    turn = reg.resolve_turn("s", state, skills())
    assert {"skill-xhs-analyzer", "xhs-note-creator", "skill-xhs-comment-reply"}.issubset(turn["skills"])
    assert {"ripple_xhs_read", "ripple_interaction_draft", "ripple_interactions_read", "ripple_operation"}.issubset(turn["tools"])
    assert all(name.startswith("ripple_") for name in turn["tools"])
    with pytest.raises(WorkflowError, match="未注册 Plugin"):
        reg.update_session("x", {"enabled_plugins": ["arbitrary-code"]}, state, skills())


def test_mcp_mock_namespace_selection_and_no_remote_execution(tmp_path):
    reg = AgentCapabilityRegistry(tmp_path); state = models(reg)
    ext = reg.save_extensions([
        {"id": "mockdocs", "name": "Mock Docs", "transport": "mock", "enabled": True,
         "tools": [{"id": "search", "name": "Search", "description": "mock", "risk": "read"}]},
        {"id": "remote", "name": "Remote", "transport": "streamable_http", "endpoint": "https://example.invalid/mcp", "enabled": True,
         "tools": [{"id": "read", "name": "Read", "description": "remote", "risk": "read"}]},
    ])
    mock = next(row for row in reg.catalog(skills())["mcp_tools"] if row["server_id"] == "mockdocs")
    remote = next(row for row in reg.catalog(skills())["mcp_tools"] if row["server_id"] == "remote")
    assert mock["id"] == "ripple_mcp__mockdocs__search" and mock["ready"] is True
    assert remote["ready"] is False
    reg.update_session("s", {"enabled_mcp_tools": [mock["id"]]}, state, skills())
    turn = reg.resolve_turn("s", state, skills())
    assert "ripple_mcp" in turn["tools"] and turn["mcp_tools"] == [mock["id"]]
    result = reg.execute_mcp(mock["id"], {"q": "hello"})
    assert result["result"] == {"echo": {"q": "hello"}, "adapter": "ripple-mock"}
    with pytest.raises(WorkflowError, match="不可用|未连接"):
        reg.update_session("remote-s", {"enabled_mcp_tools": [remote["id"]]}, state, skills())


def test_readiness_filters_unavailable_tool_even_if_saved_default(tmp_path):
    reg = AgentCapabilityRegistry(tmp_path); state = models(reg)
    cfg = reg.get_session("s", state, skills())
    assert "ripple_generate_video" in cfg["enabled_tools"]
    turn = reg.resolve_turn("s", state, skills(), readiness={"ripple_generate_video": False})
    assert "ripple_generate_video" not in turn["tools"]
    with pytest.raises(WorkflowError, match="未就绪"):
        reg.resolve_turn("s", state, skills(), turn_tools=["ripple_generate_video"], readiness={"ripple_generate_video": False})
