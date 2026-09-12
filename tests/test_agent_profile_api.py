from __future__ import annotations

import json

from fastapi.testclient import TestClient

from ripple.agent_profiles import AgentProfileRegistry


def test_agent_profile_api_scans_selects_and_updates_inheritance(tmp_path, monkeypatch):
    from web import app as webapp

    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / "CLAUDE.md").write_text("Keep replies short.", encoding="utf-8")
    (claude / "settings.json").write_text(json.dumps({"model": "sonnet", "effortLevel": "high"}), encoding="utf-8")

    registry = AgentProfileRegistry(tmp_path / "private", home=home, environ={})
    monkeypatch.setattr(registry, "_binary", lambda _name: False)
    monkeypatch.setattr(webapp, "_AGENT_PROFILES", registry)
    # Profile discovery is the unit under test here; restricted-runtime probing is
    # covered separately by test_native_agent_runtime_api and must not depend on
    # whichever Agent binaries happen to exist on the CI host.
    monkeypatch.setattr(webapp, "_agent_runtime_selectable", lambda runtime_id: (
        runtime_id == "claude_code",
        {"installed": runtime_id == "claude_code"},
        {"healthy": runtime_id == "claude_code", "configured": runtime_id == "claude_code", "detail": ""},
        {"restricted_mode": runtime_id == "claude_code"},
    ))

    with TestClient(webapp.app, base_url="http://localhost") as client:
        scanned = client.post("/api/agent/profiles/scan")
        assert scanned.status_code == 200
        body = scanned.json()
        claude_profile = next(row for row in body["profiles"] if row["id"] == "claude_code:default")
        assert claude_profile["installed"] is True
        assert claude_profile["model"] == "sonnet"
        assert claude_profile["components"]["rules"]["count"] == 1

        selected = client.put("/api/agent/profiles/default", json={"profile_id": "claude_code:default"})
        assert selected.status_code == 200
        assert selected.json()["default_profile"] == "claude_code:default"

        inheritance = client.put(
            "/api/agent/profiles/claude_code:default/inheritance",
            json={"inheritance": {"mcp": False, "skills": True}},
        )
        assert inheritance.status_code == 200
        profile = next(row for row in inheritance.json()["profiles"] if row["id"] == "claude_code:default")
        assert profile["inheritance"]["mcp"] is False

        rejected = client.put(
            "/api/agent/profiles/claude_code:default/inheritance",
            json={"inheritance": {"credentials": True}},
        )
        assert rejected.status_code == 422

        acknowledged = client.post("/api/agent/profiles/claude_code:default/acknowledge")
        assert acknowledged.status_code == 200
