from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ripple.agent_runtime import AgentRuntimeError
from ripple.agent_tool_bridge import AgentToolLeaseStore, RippleAgentToolBridge, install_agent_tool_bridge


async def _echo_executor(lease, tool_id, arguments):
    return {"session": lease.web_session_id, "runtime": lease.runtime_id, "tool": tool_id, "arguments": arguments}


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + token,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }


def _rpc(client: TestClient, token: str, method: str, params: dict, request_id: int = 1):
    return client.post(
        "/mcp/",
        headers=_headers(token),
        json={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
    )


def test_lease_store_rejects_unregistered_tools_and_revocation():
    store = AgentToolLeaseStore()
    lease = store.issue("web", "opencode", ["ripple_ideas_list"], turn_id="turn")
    assert store.require(lease.token, "ripple_ideas_list").web_session_id == "web"
    with pytest.raises(AgentRuntimeError, match="未授权"):
        store.require(lease.token, "ripple_accounts")
    with pytest.raises(AgentRuntimeError, match="未注册"):
        store.issue("web", "opencode", ["bash"])
    store.revoke(lease.token)
    with pytest.raises(AgentRuntimeError, match="无效或已过期"):
        store.require(lease.token)


def test_mcp_bridge_lists_only_lease_tools_and_rechecks_calls():
    bridge = RippleAgentToolBridge(_echo_executor)
    app = FastAPI()
    install_agent_tool_bridge(app, bridge, mount_path="/mcp")
    lease = bridge.issue("web-session", "claude_code", ["ripple_ideas_list"], turn_id="turn-1")

    with TestClient(app, base_url="http://127.0.0.1") as client:
        listed = _rpc(client, lease.token, "tools/list", {}, 1)
        assert listed.status_code == 200
        tools = listed.json()["result"]["tools"]
        assert [row["name"] for row in tools] == ["ripple_ideas_list"]

        allowed = _rpc(client, lease.token, "tools/call", {"name": "ripple_ideas_list", "arguments": {"limit": 3}}, 2)
        assert allowed.status_code == 200
        payload = json.loads(allowed.json()["result"]["content"][0]["text"])
        assert payload == {
            "session": "web-session", "runtime": "claude_code", "tool": "ripple_ideas_list", "arguments": {"limit": 3},
        }

        denied = _rpc(client, lease.token, "tools/call", {"name": "ripple_accounts", "arguments": {}}, 3)
        assert denied.status_code == 200
        assert denied.json()["result"]["isError"] is True
        assert "未授权" in denied.json()["result"]["content"][0]["text"]

        invalid = _rpc(client, "invalid-token", "tools/list", {}, 4)
        assert invalid.status_code == 403


def test_bridge_lifespan_can_restart_for_host_test_clients():
    bridge = RippleAgentToolBridge(_echo_executor)
    app = FastAPI()
    install_agent_tool_bridge(app, bridge, mount_path="/mcp")

    for index in range(2):
        lease = bridge.issue(f"web-{index}", "codex", ["ripple_accounts"])
        with TestClient(app, base_url="http://127.0.0.1") as client:
            response = _rpc(client, lease.token, "tools/list", {}, index + 1)
            assert response.status_code == 200
            assert [row["name"] for row in response.json()["result"]["tools"]] == ["ripple_accounts"]


def test_web_bridge_executes_existing_ripple_tool_boundary(monkeypatch):
    from web import app as webapp

    monkeypatch.setattr(webapp, "_read_ideas", lambda: [{
        "id": "idea1", "title": "Bridge idea", "note": "n", "source": "fixture", "status": "pending", "created": 1,
    }])
    lease = webapp._AGENT_TOOL_BRIDGE.issue("web-integration", "claude_code", ["ripple_ideas_list"], turn_id="turn")
    headers = {
        "Authorization": "Bearer " + lease.token,
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with TestClient(webapp.app, base_url="http://127.0.0.1") as client:
        response = client.post(
            "/api/agent/mcp/",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "ripple_ideas_list", "arguments": {"limit": 1}}},
        )
    assert response.status_code == 200
    result = response.json()["result"]
    assert result["isError"] is False
    assert json.loads(result["content"][0]["text"])["items"][0]["title"] == "Bridge idea"


def test_bridge_argument_size_is_bounded():
    async def run():
        bridge = RippleAgentToolBridge(_echo_executor)
        lease = bridge.issue("web", "hermes", ["ripple_operation"])
        from ripple import agent_tool_bridge as module
        marker = module._CURRENT_LEASE_TOKEN.set(lease.token)
        try:
            with pytest.raises(AgentRuntimeError, match="超过安全上限"):
                await bridge._call("ripple_operation", {"input": {"blob": "x" * 170_000}})
        finally:
            module._CURRENT_LEASE_TOKEN.reset(marker)

    asyncio.run(run())
