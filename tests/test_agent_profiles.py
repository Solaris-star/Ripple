from __future__ import annotations

import json
from pathlib import Path

import pytest

from ripple.agent_profiles import AgentProfileRegistry
from ripple.publishing import WorkflowError


def fixture_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    claude = home / ".claude"
    (claude / "skills" / "writer").mkdir(parents=True)
    (claude / "agents").mkdir()
    (claude / "commands").mkdir()
    (claude / "rules").mkdir()
    (claude / "CLAUDE.md").write_text("Prefer concise answers.", encoding="utf-8")
    (claude / "skills" / "writer" / "SKILL.md").write_text("# Writer\nUse clear prose.", encoding="utf-8")
    (claude / "agents" / "editor.md").write_text("# Editor", encoding="utf-8")
    (claude / "commands" / "polish.md").write_text("# Polish", encoding="utf-8")
    (claude / ".credentials.json").write_text('{"token":"claude-secret"}', encoding="utf-8")
    (claude / "settings.json").write_text(json.dumps({
        "model": "sonnet-safe",
        "effortLevel": "high",
        "enabledPlugins": {"style-pack": True},
        "env": {"PRIVATE_TOKEN": "claude-env-secret"},
        "hooks": {"PostToolUse": [{"command": "curl secret.invalid"}]},
    }), encoding="utf-8")
    (home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"accessToken": "oauth-secret"},
        "mcpServers": {"notion": {"command": "node", "env": {"TOKEN": "mcp-secret"}}},
    }), encoding="utf-8")

    codex = home / ".codex"
    (codex / "skills" / "review").mkdir(parents=True)
    (codex / "agents").mkdir()
    (codex / "AGENTS.md").write_text("Review before edits.", encoding="utf-8")
    (codex / "skills" / "review" / "SKILL.md").write_text("# Review", encoding="utf-8")
    (codex / "agents" / "tester.md").write_text("# Tester", encoding="utf-8")
    (codex / "auth.json").write_text('{"token":"codex-secret"}', encoding="utf-8")
    (codex / "config.toml").write_text(
        'model = "gpt-safe"\n'
        'model_reasoning_effort = "xhigh"\n'
        '[mcp_servers.docs]\nurl = "https://example.invalid?token=codex-mcp-secret"\n'
        '[plugins.formatter]\nenabled = true\n',
        encoding="utf-8",
    )

    hermes = home / ".hermes"
    (hermes / "skills" / "social" / "post").mkdir(parents=True)
    (hermes / "memories").mkdir()
    (hermes / "SOUL.md").write_text("Friendly and precise.", encoding="utf-8")
    (hermes / "skills" / "social" / "post" / "SKILL.md").write_text("# Social post", encoding="utf-8")
    (hermes / "memories" / "MEMORY.md").write_text("private-memory-secret", encoding="utf-8")
    (hermes / "memories" / "USER.md").write_text("private-user-secret", encoding="utf-8")
    (hermes / ".env").write_text("API_KEY=hermes-secret", encoding="utf-8")
    (hermes / "auth.json").write_text('{"token":"hermes-auth-secret"}', encoding="utf-8")
    (hermes / "config.yaml").write_text(
        "model:\n  default: hermes-safe-model\n"
        "agent:\n  reasoning_effort: medium\n"
        "mcp_servers:\n  search: {url: 'https://secret.invalid'}\n"
        "quick_commands:\n  summarize: '/summarize'\n"
        "providers:\n  private:\n    api_key: hermes-provider-secret\n",
        encoding="utf-8",
    )
    return home


def by_runtime(state: dict, runtime_id: str) -> dict:
    return next(row for row in state["profiles"] if row["runtime_id"] == runtime_id)


def test_scan_exposes_only_safe_habit_metadata(tmp_path, monkeypatch):
    home = fixture_home(tmp_path)
    registry = AgentProfileRegistry(tmp_path / "private", home=home, environ={})
    monkeypatch.setattr(registry, "_binary", lambda _name: False)

    state = registry.scan()
    claude = by_runtime(state, "claude_code")
    codex = by_runtime(state, "codex")
    hermes = by_runtime(state, "hermes")

    assert claude["installed"] is True
    assert claude["model"] == "sonnet-safe" and claude["effort"] == "high"
    assert claude["components"]["skills"]["items"][0]["name"] == "writer"
    assert claude["components"]["mcp"]["items"] == [{"name": "notion"}]
    assert claude["blocked_detected"]["credentials"] is True
    assert claude["blocked_detected"]["hooks"] == 1

    assert codex["model"] == "gpt-safe" and codex["effort"] == "xhigh"
    assert codex["components"]["mcp"]["items"] == [{"name": "docs"}]
    assert codex["components"]["plugins"]["items"] == [{"name": "formatter"}]

    assert hermes["model"] == "hermes-safe-model" and hermes["effort"] == "medium"
    assert hermes["components"]["memory"]["mode"] == "snapshot_only"
    assert hermes["inheritance"]["memory"] is False
    assert hermes["components"]["memory"]["count"] == 2

    serialized = json.dumps(state, ensure_ascii=False)
    for secret in (
        "claude-secret", "claude-env-secret", "oauth-secret", "mcp-secret",
        "codex-secret", "codex-mcp-secret", "private-memory-secret", "private-user-secret",
        "hermes-secret", "hermes-auth-secret", "hermes-provider-secret", "secret.invalid",
        "curl secret.invalid",
    ):
        assert secret not in serialized
    assert "credentials" in claude["blocked_inheritance"]
    assert "hooks" in claude["blocked_inheritance"]


def test_scan_tracks_habit_changes_until_acknowledged(tmp_path, monkeypatch):
    home = fixture_home(tmp_path)
    registry = AgentProfileRegistry(tmp_path / "private", home=home, environ={})
    monkeypatch.setattr(registry, "_binary", lambda _name: False)

    first = registry.scan()
    assert by_runtime(first, "claude_code")["changed_since_review"] is False

    (home / ".claude" / "CLAUDE.md").write_text("A changed preference.", encoding="utf-8")
    second = registry.scan()
    assert by_runtime(second, "claude_code")["changed_since_review"] is True

    acknowledged = registry.acknowledge("claude_code:default")
    assert by_runtime(acknowledged, "claude_code")["changed_since_review"] is False
    third = registry.scan()
    assert by_runtime(third, "claude_code")["changed_since_review"] is False


def test_profile_guidance_reuses_rules_without_loading_secrets_or_inert_skill_bodies(tmp_path, monkeypatch):
    home = fixture_home(tmp_path)
    registry = AgentProfileRegistry(tmp_path / "private", home=home, environ={})
    monkeypatch.setattr(registry, "_binary", lambda _name: False)
    registry.scan()

    guidance = registry.guidance("claude_code:default")
    assert "Prefer concise answers." in guidance
    assert "writer" in guidance and "editor" in guidance and "polish" in guidance
    assert "notion" in guidance and "style-pack" in guidance
    assert "Use clear prose." not in guidance
    assert "claude-secret" not in guidance and "curl secret.invalid" not in guidance

    (home / ".claude" / "CLAUDE.md").write_text("changed without review", encoding="utf-8")
    registry.scan()
    assert registry.guidance("claude_code:default") == ""


def test_profile_default_and_inheritance_are_persisted(tmp_path, monkeypatch):
    home = fixture_home(tmp_path)
    registry = AgentProfileRegistry(tmp_path / "private", home=home, environ={})
    monkeypatch.setattr(registry, "_binary", lambda _name: False)
    registry.scan()

    state = registry.set_default("claude_code:default")
    assert state["default_profile"] == "claude_code:default"
    updated = registry.update_inheritance("claude_code:default", {"mcp": False, "skills": True, "memory": True})
    claude = by_runtime(updated, "claude_code")
    assert claude["inheritance"]["mcp"] is False
    assert claude["inheritance"]["skills"] is True
    assert claude["inheritance"]["memory"] is True

    scanned = registry.scan()
    claude = by_runtime(scanned, "claude_code")
    assert scanned["default_profile"] == "claude_code:default"
    assert claude["inheritance"]["mcp"] is False
    assert claude["inheritance"]["memory"] is True

    with pytest.raises(WorkflowError, match="不支持的继承项"):
        registry.update_inheritance("claude_code:default", {"credentials": True})
