from __future__ import annotations

from fastapi.testclient import TestClient

from ripple.agent_capabilities import AgentCapabilityRegistry
from ripple.workspace import WorkspaceService


def test_agent_runtime_env_projects_private_model_registry_without_secret_duplication(tmp_path, monkeypatch):
    from web import app as webapp

    registry = AgentCapabilityRegistry(tmp_path / "private")
    registry.save_models([
        {"id": "plain", "name": "Plain", "effort_levels": ["auto"]},
        {"id": "reason", "name": "Reason", "effort_levels": ["auto", "high"]},
    ], "reason")
    monkeypatch.setattr(webapp, "_AGENT_CAPABILITIES", registry)
    monkeypatch.setattr(webapp, "_read_env", lambda: {
        "RIPPLE_ENABLE_AI": "1", "RIPPLE_LLM_BASE_URL": "https://example.invalid/v1",
        "RIPPLE_LLM_API_KEY": "fixture-secret", "RIPPLE_LLM_MODEL": "plain",
    })
    for name in ("RIPPLE_LLM_MODEL", "RIPPLE_LLM_MODELS"):
        monkeypatch.delenv(name, raising=False)
    env = webapp._agent_runtime_env()
    assert env["RIPPLE_LLM_MODEL"] == "reason"
    assert env["RIPPLE_LLM_MODELS"] == "plain,reason"
    assert env["RIPPLE_LLM_API_KEY"] == "fixture-secret"


def test_agent_capability_and_session_config_api_are_server_authoritative(tmp_path, monkeypatch):
    from web import app as webapp

    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    registry = AgentCapabilityRegistry(tmp_path / "private")
    registry.save_models([
        {"id": "plain", "name": "Plain", "effort_levels": ["auto"]},
        {"id": "reason", "name": "Reason", "effort_levels": ["auto", "low", "high"]},
    ], "plain")
    monkeypatch.setattr(webapp.app.state, "ripple", service)
    monkeypatch.setattr(webapp, "_AGENT_CAPABILITIES", registry)
    monkeypatch.setattr(webapp, "_model_config_status", lambda: {
        "enabled": True, "base_url": "https://example.invalid/v1", "model": "plain", "default_model": "plain",
        "models": registry.model_state("plain")["models"], "api_key_set": True,
    })
    monkeypatch.setattr(webapp, "_agent_session_registry_args", lambda: {
        "runtime_ids": ["opencode"],
        "default_runtime": "opencode",
        "profile_runtimes": {},
        "default_profile": "",
        "runtime_model_states": {"opencode": registry.model_state("plain")},
    })

    with TestClient(webapp.app, base_url="http://localhost") as client:
        catalog = client.get("/api/agent/capabilities")
        assert catalog.status_code == 200
        body = catalog.json()
        assert body["default_model"] == "plain"
        assert "tools" not in body and "generic_denied" not in body
        assert any(row["id"] == "text-polisher" for row in body["skills"])

        cfg = client.put("/api/agent/sessions/web-session/config", json={"model": "reason", "effort": "high", "pinned_skills": ["text-polisher"]})
        assert cfg.status_code == 200
        assert cfg.json()["effort_mode"] == "instruction_fallback"
        assert client.get("/api/agent/sessions/web-session/config").json()["model"] == "reason"

        forbidden = client.put("/api/agent/sessions/web-session/config", json={"enabled_tools": ["bash"]})
        assert forbidden.status_code == 422
        public_cfg = client.get("/api/agent/sessions/web-session/config").json()
        assert "enabled_tools" not in public_cfg and public_cfg["pinned_skills"] == ["text-polisher"]

        fallback = client.put("/api/agent/sessions/plain/config", json={"model": "plain", "effort": "high"})
        assert fallback.status_code == 200
        assert fallback.json()["effort"] == "high" and fallback.json()["effort_mode"] == "instruction_fallback"

        raw_turn = client.post("/api/chat", json={"message": "x", "sessionId": "raw-turn", "turnTools": ["ripple_accounts"]})
        assert raw_turn.status_code == 422


def test_mcp_tool_endpoint_requires_current_session_selection(tmp_path, monkeypatch):
    from web import app as webapp

    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    registry = AgentCapabilityRegistry(tmp_path / "private")
    model_state = registry.save_models([{"id": "plain", "name": "Plain", "effort_levels": ["auto"]}], "plain")
    registry.save_extensions([{ "id": "mockdocs", "name": "Mock Docs", "transport": "mock", "enabled": True,
        "tools": [{"id": "search", "name": "Search", "description": "mock search", "risk": "read"}] }])
    capability_id = registry.catalog([])["mcp_tools"][0]["id"]
    registry.update_session("web-session", {"enabled_mcp_tools": [capability_id]}, model_state, [])
    monkeypatch.setattr(webapp.app.state, "ripple", service)
    monkeypatch.setattr(webapp, "_AGENT_CAPABILITIES", registry)
    monkeypatch.setattr(webapp, "_model_config_status", lambda: {"default_model": "plain", "models": model_state["models"]})
    monkeypatch.setattr(webapp._AGENT_RUNTIME, "web_session_for_remote", lambda remote: "web-session" if remote == "ses_remote" else None)
    headers = {"X-Ripple-Agent-Token": webapp._AGENT_RUNTIME.tool_token}

    with TestClient(webapp.app, base_url="http://localhost") as client:
        denied = client.post("/api/ripple-agent/tools/mcp", headers=headers, json={"remote_session_id": "ses_other", "capability_id": capability_id, "arguments": {"q": "x"}})
        assert denied.status_code == 403
        ok = client.post("/api/ripple-agent/tools/mcp", headers=headers, json={"remote_session_id": "ses_remote", "capability_id": capability_id, "arguments": {"q": "x"}})
        assert ok.status_code == 200
        assert ok.json()["result"] == {"echo": {"q": "x"}, "adapter": "ripple-mock"}


def test_internal_read_tools_require_agent_token(tmp_path, monkeypatch):
    from web import app as webapp
    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    monkeypatch.setattr(webapp.app.state, "ripple", service)
    with TestClient(webapp.app, base_url="http://localhost") as client:
        assert client.post("/api/ripple-agent/tools/content/read", json={"operation": "list"}).status_code == 403
        headers = {"X-Ripple-Agent-Token": webapp._AGENT_RUNTIME.tool_token}
        result = client.post("/api/ripple-agent/tools/content/read", headers=headers, json={"operation": "list", "limit": 10})
        assert result.status_code == 200 and result.json()["kind"] == "content_list"
        accounts = client.post("/api/ripple-agent/tools/accounts", headers=headers, json={})
        assert accounts.status_code == 200 and accounts.json()["kind"] == "accounts"
