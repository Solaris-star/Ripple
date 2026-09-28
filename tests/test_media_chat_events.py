from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest


def test_image_events_replay_current_log_schema_and_terminal_key(tmp_path, monkeypatch):
    from web import app as webapp
    monkeypatch.setattr(webapp, "SESSIONS_DIR", tmp_path)
    path = webapp._job_event_file("image-turn")
    path.parent.mkdir(parents=True)
    image = {"id": "image-one", "tool": "ripple_generate_image", "status": "running", "started_at": 1000}
    rows = [{"id": 1, "type": "token", "text": "正文"},
            {"id": 2, "type": "media", "text": json.dumps(image)},
            {"id": 3, "type": "done", "text": "", "sessionKey": "session-one"}]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    async def collect():
        response = await webapp.api_chat_job_stream("image-turn", after=1)
        return [item async for item in response.body_iterator]
    events = asyncio.run(collect())
    assert [item["event"] for item in events] == ["media", "done"]
    assert json.loads(json.loads(events[0]["data"])) == image
    assert json.loads(events[1]["data"]) == {"sessionKey": "session-one"}


@pytest.mark.parametrize("task_status,runtime_error,expected", [
    ("running", False, "interrupted"),
    ("pending", True, "interrupted"),
    ("completed", False, "completed"),
    ("failed", False, "failed"),
])
def test_media_state_survives_stream_end_and_snapshot(tmp_path, monkeypatch, task_status, runtime_error, expected):
    from web import app as webapp
    from ripple.agent_runtime import AgentRuntimeError

    monkeypatch.setattr(webapp, "SESSIONS_DIR", tmp_path)
    config = {"runtime_id": "opencode", "profile_id": "", "skills": [], "mcp_tools": [], "tools": ["ripple_generate_image"],
              "model": "fixture", "effort": "auto", "effort_mode": "auto"}
    monkeypatch.setattr(webapp, "_AGENT_CAPABILITIES", SimpleNamespace(
        resolve_turn=lambda *args, **kwargs: config, model_state=lambda *args: {}, lock_runtime_profile=lambda *args: None))
    monkeypatch.setattr(webapp, "_model_config_status", lambda: {"default_model": "fixture"})
    monkeypatch.setattr(webapp, "get_skills", lambda: [])
    monkeypatch.setattr(webapp, "_agent_tool_readiness", lambda: {})
    monkeypatch.setattr(webapp, "_agent_session_registry_args", lambda: {})
    monkeypatch.setattr(webapp, "_agent_tool_base", lambda: "http://localhost")
    for name in ("_ripple_agent_system", "_chat_content_profile_guidance", "_agent_skill_guidance", "_agent_mcp_guidance"):
        monkeypatch.setattr(webapp, name, lambda *args: "")
    monkeypatch.setattr(webapp, "_CrossProcLock", lambda *_: SimpleNamespace(acquire=lambda *args: True, release=lambda: None))
    task = {"id": "image-one", "tool": "ripple_generate_image", "status": task_status, "started_at": 1000}
    if task_status == "completed":
        task.update(path="AI媒体生成/ok.png", finished_at=2000)
    elif task_status == "failed":
        task.update(error="图片工具执行失败", finished_at=2000)
    def run_turn(*args, **kwargs):
        emit = args[5]
        emit("media", json.dumps(task))
        if runtime_error:
            raise AgentRuntimeError("运行时中断", 504)
        return "正文已保留", "session-one"
    monkeypatch.setattr(webapp, "_AGENT_RUNTIME", SimpleNamespace(default_runtime_id="opencode", run_turn=run_turn,
        get=lambda *_: SimpleNamespace(status=lambda *args, **kwargs: {"healthy": True})))
    async def collect():
        response = await webapp.api_chat_stream_agent(webapp.ChatRequest(sessionId="media-fixture", turnId="media-turn", message="配图"))
        events = [item async for item in response.body_iterator]
        snapshot = await webapp.api_chat_last("media-fixture", "media-turn")
        return events, snapshot
    events, snapshot = asyncio.run(collect())
    media = [json.loads(json.loads(item["data"])) for item in events if item["event"] == "media"]
    assert media[-1]["status"] == expected
    assert snapshot["mediaTasks"][-1] == media[-1]
    assert events[-1]["event"] == "done"
