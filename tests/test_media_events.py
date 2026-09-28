from __future__ import annotations

import json

import pytest

from ripple.acp_client import AcpProcessClient
from ripple.media_events import MediaTaskTracker, image_path


@pytest.mark.parametrize("value", [
    {"kind": "image", "path": "AI媒体生成/test.png"},
    {"structured_content": {"kind": "image", "path": "AI媒体生成/test.png"}},
    {"content": [{"type": "text", "text": '{"kind":"image","path":"AI媒体生成/test.png"}'}]},
])
def test_media_path_supports_runtime_result_shapes(value):
    assert image_path(value) == "AI媒体生成/test.png"


@pytest.mark.parametrize("path", ["../secret.png", "/tmp/x.png", "C:/secret.png", "_private/x.png", "a/.secret.png", "a/../x.png", "a/x.html"])
def test_media_path_rejects_unsafe_or_non_image_outputs(path):
    assert image_path({"kind": "image", "path": path}) is None


def test_media_status_requires_artifact_and_does_not_expose_raw_errors():
    tracker = MediaTaskTracker()
    events = []
    emit = lambda kind, text: events.append((kind, json.loads(text)))
    tracker.update("one", "ripple_generate_image", "running", None, emit)
    tracker.update("one", "ripple_generate_image", "running", None, emit)
    assert len(events) == 1
    tracker.update("one", "ripple_generate_image", "completed", {"isError": True, "text": "secret-token"}, emit)
    assert events[-1][1]["status"] == "failed"
    assert "secret-token" not in json.dumps(events)
    tracker.update("two", "ripple_generate_image", "completed", {}, emit)
    assert events[-1][1]["status"] == "interrupted"
    tracker.update("three", "ripple_generate_image", "completed", {"kind": "image", "path": "AI媒体生成/ok.webp"}, emit)
    assert events[-1][1]["status"] == "completed"
    assert events[-1][1]["path"] == "AI媒体生成/ok.webp"
    assert events[-1][1]["finished_at"] >= events[-1][1]["started_at"]


def test_acp_media_updates_keep_identity_without_repeating_tool_title(tmp_path):
    client = AcpProcessClient([], tmp_path)
    events = []
    client._emit = lambda kind, text: events.append((kind, text))
    client._handle_session_update({"update": {"sessionUpdate": "tool_call", "toolCallId": "image-one",
                                             "title": "mcp__ripple__ripple_generate_image", "status": "in_progress"}})
    client._handle_session_update({"update": {"sessionUpdate": "tool_call_update", "toolCallId": "image-one",
                                             "status": "completed", "rawOutput": {"kind": "image", "path": "AI媒体生成/ok.png"}}})
    media = [json.loads(text) for kind, text in events if kind == "media"]
    assert [row["status"] for row in media] == ["running", "completed"]
    assert media[0]["id"] == media[1]["id"] == "image-one"
    assert media[0]["started_at"] == media[1]["started_at"]
