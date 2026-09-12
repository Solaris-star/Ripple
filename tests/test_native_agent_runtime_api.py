from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ripple.agent_profiles import AgentProfileRegistry


def test_runtime_catalog_exposes_supported_and_fail_closed_adapters():
    from web import app as webapp

    with TestClient(webapp.app, base_url="http://127.0.0.1") as client:
        response = client.get("/api/agent/runtimes")
    assert response.status_code == 200
    items = {row["runtime"]: row for row in response.json()["items"]}
    assert {"opencode", "claude_code", "codex", "hermes"}.issubset(items)
    codex = items["codex"]
    if codex.get("selectable"):
        assert codex["capabilities"]["restricted_mode"] is True
        assert codex["models"] and codex["current_model"]
    assert items["hermes"]["capabilities"]["restricted_mode"] is False
    assert items["claude_code"]["capabilities"]["profile_import"] is True
    assert all("connection_state" in row and "current_model" in row and "models" in row for row in items.values())


def test_runtime_catalog_is_visible_before_scan_and_joins_model_after_scan(tmp_path, monkeypatch):
    from web import app as webapp

    home = tmp_path / "home"
    registry = AgentProfileRegistry(tmp_path / "private", home=home, environ={})
    monkeypatch.setattr(registry, "_binary", lambda _name: False)
    monkeypatch.setattr(webapp, "_AGENT_PROFILES", registry)
    names = {"opencode": "OpenCode", "claude_code": "Claude Code", "codex": "Codex", "hermes": "Hermes"}
    monkeypatch.setattr(webapp, "_agent_runtime_selectable", lambda runtime_id: (
        False, {"runtime": runtime_id, "name": names[runtime_id], "installed": False},
        {"configured": False, "healthy": False, "detail": ""}, {"restricted_mode": runtime_id == "opencode"},
    ))

    with TestClient(webapp.app, base_url="http://127.0.0.1") as client:
        profiles_before = client.get("/api/agent/profiles")
        runtimes_before = client.get("/api/agent/runtimes")
        assert profiles_before.status_code == 200 and profiles_before.json()["profiles"] == []
        assert [row["runtime"] for row in runtimes_before.json()["items"]] == ["opencode", "claude_code", "codex", "hermes"]

        claude = home / ".claude"
        claude.mkdir(parents=True)
        (claude / "settings.json").write_text(json.dumps({"model": "claude-sonnet-fixture", "effortLevel": "high"}), encoding="utf-8")
        scanned = client.post("/api/agent/profiles/scan")
        assert scanned.status_code == 200
        runtimes_after = client.get("/api/agent/runtimes")
        claude_runtime = next(row for row in runtimes_after.json()["items"] if row["runtime"] == "claude_code")
        assert claude_runtime["current_model"] == "claude-sonnet-fixture"
        assert claude_runtime["current_effort"] == "high"


def test_session_config_can_bind_native_runtime_and_matching_profile(tmp_path, monkeypatch):
    from web import app as webapp

    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "CLAUDE.md").write_text("Use terse prose.", encoding="utf-8")
    (claude / "settings.json").write_text(json.dumps({"model": "sonnet"}), encoding="utf-8")
    registry = AgentProfileRegistry(tmp_path / "private", home=home, environ={})
    monkeypatch.setattr(registry, "_binary", lambda _name: False)
    registry.scan()
    monkeypatch.setattr(webapp, "_AGENT_PROFILES", registry)
    original_selectable = webapp._agent_runtime_selectable
    monkeypatch.setattr(webapp, "_agent_runtime_selectable", lambda runtime_id: (
        (True, {"installed": True}, {"healthy": True, "detail": ""}, {"restricted_mode": True})
        if runtime_id == "claude_code" else original_selectable(runtime_id)
    ))

    with TestClient(webapp.app, base_url="http://127.0.0.1") as client:
        saved = client.put("/api/agent/sessions/native-session/config", json={
            "runtime_id": "claude_code", "profile_id": "claude_code:default",
        })
        assert saved.status_code == 200
        assert saved.json()["runtime_id"] == "claude_code"
        assert saved.json()["profile_id"] == "claude_code:default"

        mismatched = client.put("/api/agent/sessions/native-session/config", json={"profile_id": "opencode:default"})
        assert mismatched.status_code == 422


def test_session_config_rejects_runtime_that_is_not_restricted(monkeypatch):
    from web import app as webapp

    monkeypatch.setattr(webapp, "_agent_runtime_selectable", lambda runtime_id: (
        False, {"runtime": runtime_id, "installed": True},
        {"healthy": False, "detail": "restricted probe failed"}, {"restricted_mode": False},
    ))
    with TestClient(webapp.app, base_url="http://127.0.0.1") as client:
        response = client.put("/api/agent/sessions/fail-closed/config", json={"runtime_id": "codex"})
    assert response.status_code == 409
    assert "restricted probe failed" in response.json()["detail"]


def test_default_profile_rejects_non_executable_runtime(tmp_path, monkeypatch):
    from web import app as webapp

    home = tmp_path / "home"
    codex = home / ".codex"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text('model = "gpt-safe"\n', encoding="utf-8")
    registry = AgentProfileRegistry(tmp_path / "private", home=home, environ={})
    monkeypatch.setattr(registry, "_binary", lambda _name: False)
    registry.scan()
    monkeypatch.setattr(webapp, "_AGENT_PROFILES", registry)
    monkeypatch.setattr(webapp, "_agent_runtime_selectable", lambda runtime_id: (
        False, {"runtime": runtime_id, "installed": True},
        {"healthy": False, "detail": "runtime blocked"}, {"restricted_mode": False},
    ))
    with TestClient(webapp.app, base_url="http://127.0.0.1") as client:
        response = client.put("/api/agent/profiles/default", json={"profile_id": "codex:default"})
    assert response.status_code == 409
    assert "runtime blocked" in response.json()["detail"]
