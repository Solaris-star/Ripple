from __future__ import annotations

from pathlib import Path
import json
import threading

import pytest

from ripple.agent_runtime import AgentRuntimeAdapter, AgentRuntimeError
from ripple.opencode_runtime import OpenCodeAgentAdapter, OpenCodeRuntime, OpenCodeError


def env():
    return {
        "RIPPLE_ENABLE_AI": "1",
        "RIPPLE_LLM_BASE_URL": "https://example.invalid/v1",
        "RIPPLE_LLM_API_KEY": "fixture-secret",
        "RIPPLE_LLM_MODEL": "fixture-model",
        "RIPPLE_LLM_MODELS": "fixture-model,fixture-reasoning",
        "RIPPLE_OPENCODE_PORT": "4096",
    }


def runtime(tmp_path):
    root = tmp_path / "app"; root.mkdir()
    integration = root / "integrations" / "opencode"
    (integration / "tools").mkdir(parents=True)
    (integration / "RIPPLE_AGENT.md").write_text("fixture", encoding="utf-8")
    (integration / "tools" / "ripple_fixture.ts").write_text("export default {}", encoding="utf-8")
    return OpenCodeRuntime(root, tmp_path / "private", env)


def test_opencode_adapter_preserves_runtime_contract_and_legacy_alias(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    assert OpenCodeRuntime is OpenCodeAgentAdapter
    assert isinstance(rt, AgentRuntimeAdapter)
    assert isinstance(OpenCodeError("fixture"), AgentRuntimeError)
    monkeypatch.setattr(rt, "_health", lambda _settings: {"healthy": True, "version": "fixture-version"})
    status = rt.status("http://127.0.0.1:7860")
    assert status["runtime"] == "opencode"
    assert status["healthy"] is True


def test_private_config_uses_env_refs_and_denies_generic_tools(tmp_path):
    rt = runtime(tmp_path)
    settings = rt._settings()
    rt._write_config(settings)
    raw = rt.config_path.read_text(encoding="utf-8")
    assert "fixture-secret" not in raw
    cfg = json.loads(raw)
    assert cfg["model"] == "ripple/fixture-model"
    assert cfg["enabled_providers"] == ["ripple"]
    assert cfg["provider"]["ripple"]["options"]["apiKey"] == "{env:RIPPLE_LLM_API_KEY}"
    assert cfg["permission"] == {"*": "deny", "ripple_*": "allow"}
    assert all(cfg["tools"][name] is False for name in ["bash", "edit", "write", "read", "webfetch", "websearch"])
    assert (rt.workspace_dir / "RIPPLE_AGENT.md").read_text(encoding="utf-8") == "fixture"
    assert (rt.workspace_dir / "tools" / "ripple_fixture.ts").is_file()
    assert cfg["instructions"] == [str((rt.workspace_dir / "RIPPLE_AGENT.md").resolve())]


def test_run_turn_rejects_non_ripple_tool_even_when_requested(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    monkeypatch.setattr(rt, "get_or_create", lambda *a: "ses_fixture")
    monkeypatch.setattr(rt, "_messages", lambda _sid: [])
    with pytest.raises(OpenCodeError, match="Tool 权限校验"):
        rt.run_turn("w", "x", "sys", [], "http://127.0.0.1:7860", lambda *a: None, timeout=1, enabled_tools=["bash"])


def test_settings_reject_untrusted_public_http(tmp_path):
    rt = OpenCodeRuntime(tmp_path, tmp_path / "private", lambda: {
        **env(), "RIPPLE_LLM_BASE_URL": "http://remote.invalid/v1",
    })
    with pytest.raises(OpenCodeError):
        rt._settings()


def test_process_env_is_narrow_and_passes_tool_token(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    monkeypatch.setenv("SHOULD_NOT_LEAK_TO_OPENCODE", "private-value")
    monkeypatch.setenv("IMG_API_KEY", "media-provider-secret")
    out = rt._process_env(rt._settings(), "http://127.0.0.1:7860")
    assert out["RIPPLE_LLM_API_KEY"] == "fixture-secret"
    assert out["RIPPLE_AGENT_TOOL_TOKEN"] == rt.tool_token
    assert out["RIPPLE_TOOL_BASE"] == "http://127.0.0.1:7860"
    assert "SHOULD_NOT_LEAK_TO_OPENCODE" not in out
    assert "IMG_API_KEY" not in out


def test_run_turn_streams_text_and_activity_from_new_assistant(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    monkeypatch.setattr(rt, "get_or_create", lambda web_id, tool_base: "ses_fixture")
    states = [
        [],
        [{"info": {"id": "msg_a", "role": "assistant"}, "parts": [
            {"type": "text", "text": "Hel"},
            {"type": "tool", "tool": "ripple_ideas_add", "state": {"status": "running"}},
        ]}],
        [{"info": {"id": "msg_a", "role": "assistant", "time": {"completed": 1}}, "parts": [
            {"type": "text", "text": "Hello"},
            {"type": "tool", "tool": "ripple_ideas_add", "state": {"status": "completed"}},
            {"type": "tool", "tool": "ripple_content_draft", "state": {"status": "completed", "output": json.dumps({
                "kind": "content_draft", "id": "a" * 32, "version_id": "b" * 64,
                "title": "聊天创建的内容", "status": "draft",
            }, ensure_ascii=False)}},
        ]}],
    ]
    calls = []
    def messages(_sid):
        calls.append(1)
        return states.pop(0) if states else [{"info":{"id":"msg_a","role":"assistant","time":{"completed":1}},"parts":[{"type":"text","text":"Hello"}]}]
    monkeypatch.setattr(rt, "_messages", messages)
    requests = []
    monkeypatch.setattr(rt, "_request", lambda path, method="GET", body=None, **kw: requests.append((path, method, body)) or None)
    emitted = []
    text, sid = rt.run_turn("web-1", "hi", "system", [], "http://127.0.0.1:7860", lambda k, t: emitted.append((k, t)), timeout=5, model_id="fixture-reasoning", effort="high", effort_mode="instruction_fallback", enabled_tools=["ripple_ideas_add", "ripple_content_draft", "ripple_publish_draft", "ripple_media_capabilities", "ripple_generate_image", "ripple_generate_video"])
    assert text == "Hello" and sid == "ses_fixture"
    assert ("token", "Hel") in emitted and ("token", "lo") in emitted
    assert any(k == "activity" and "ripple_ideas_add" in t for k, t in emitted)
    artifact = next(json.loads(t) for k, t in emitted if k == "artifact")
    assert artifact == {"kind": "content_draft", "id": "a" * 32, "version_id": "b" * 64,
                        "title": "聊天创建的内容", "status": "draft"}
    prompt = next(body for path, method, body in requests if path.endswith("/prompt_async?directory=" + __import__('urllib').parse.quote(str(rt.project_root))))
    assert prompt["tools"]["bash"] is False and prompt["tools"]["ripple_publish_draft"] is True
    assert prompt["tools"]["ripple_media_capabilities"] is True
    assert prompt["tools"]["ripple_generate_image"] is True and prompt["tools"]["ripple_generate_video"] is True
    assert prompt["model"]["modelID"] == "fixture-reasoning"
    assert "Effort=high" in prompt["system"] and "instruction fallback" in prompt["system"]
    assert prompt["tools"].get("ripple_xhs_read") is not True
    assert prompt["tools"]["read"] is False and prompt["tools"]["webfetch"] is False


def test_content_tool_artifact_rejects_malformed_or_secret_shaped_output():
    assert OpenCodeRuntime._tool_artifact("ripple_content_draft", {"status": "running"}) is None
    assert OpenCodeRuntime._tool_artifact("ripple_content_draft", {"status": "completed", "output": "not-json"}) is None
    assert OpenCodeRuntime._tool_artifact("ripple_content_draft", {"status": "completed", "output": json.dumps({
        "kind": "content_draft", "id": "bad", "version_id": "c" * 64, "title": "x", "api_key": "secret",
    })}) is None
    artifact = OpenCodeRuntime._tool_artifact("ripple_content", {"status": "completed", "output": {
        "kind": "content_draft", "id": "d" * 32, "version_id": "e" * 64,
        "title": "T" * 250, "status": "draft", "token": "must-not-project",
    }})
    assert artifact is not None and set(artifact) == {"kind", "id", "version_id", "title", "status"}
    assert len(artifact["title"]) == 200


def test_run_turn_passes_only_explicit_file_parts(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    file = tmp_path / "attachment.txt"; file.write_text("hello")
    monkeypatch.setattr(rt, "get_or_create", lambda *a: "ses_fixture")
    sequence = [[], [{"info":{"id":"a","role":"assistant","time":{"completed":1}},"parts":[{"type":"text","text":"ok"}]}]]
    monkeypatch.setattr(rt, "_messages", lambda _sid: sequence.pop(0))
    captured = {}
    def request(path, method="GET", body=None, **kwargs):
        if "prompt_async" in path: captured["body"] = body
        return None
    monkeypatch.setattr(rt, "_request", request)
    assert rt.run_turn("w", "read", "sys", [{"path": str(file), "name": "a.txt"}], "http://127.0.0.1:7860", lambda *a: None, timeout=5)[0] == "ok"
    parts = captured["body"]["parts"]
    assert parts[1]["type"] == "file" and parts[1]["filename"] == "a.txt" and parts[1]["url"].startswith("file:")
    assert len(parts) == 2


def test_abort_releases_active_turn_before_any_assistant_output(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    rt.private_dir.mkdir(parents=True)
    rt._save_map({"web": "ses_remote"})
    monkeypatch.setattr(rt, "get_or_create", lambda *args: "ses_remote")
    monkeypatch.setattr(rt, "_messages", lambda _sid: [])
    prompt_started = threading.Event()
    requests = []
    def request(path, method="GET", body=None, **kwargs):
        requests.append((path, method))
        if "prompt_async" in path:
            prompt_started.set()
        return True
    monkeypatch.setattr(rt, "_request", request)
    result = {}
    def worker():
        result["value"] = rt.run_turn(
            "web", "long request", "system", [], "http://127.0.0.1:7860",
            lambda *_args: None, timeout=5,
        )
    thread = threading.Thread(target=worker)
    thread.start()
    assert prompt_started.wait(1)
    assert rt.stop_turn("web", timeout=2) is True
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert result["value"] == ("", "ses_remote")
    assert any("/abort" in path and method == "POST" for path, method in requests)


def test_abort_and_delete_use_mapped_remote_session(tmp_path, monkeypatch):
    rt = runtime(tmp_path)
    rt.private_dir.mkdir(parents=True)
    rt._save_map({"web": "ses_remote"})
    seen = []
    monkeypatch.setattr(rt, "_request", lambda path, method="GET", body=None, **kw: seen.append((path, method)) or True)
    assert rt.abort("web") is True
    assert rt.delete_session("ses_remote") is True
    assert rt.mapped_session("web") is None
    assert any("/abort" in p and m == "POST" for p, m in seen)
    assert any(p.startswith("/session/ses_remote?") and m == "DELETE" for p, m in seen)


def test_close_terminates_only_owned_process(tmp_path):
    rt = runtime(tmp_path)
    class P:
        def __init__(self): self.terminated = False
        def poll(self): return None
        def terminate(self): self.terminated = True
        def wait(self, timeout): return 0
    p = P(); rt._process = p; rt._owned = True
    rt.close()
    assert p.terminated is True and rt._process is None
