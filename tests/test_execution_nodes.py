from __future__ import annotations

import subprocess
from pathlib import Path
import uuid

import pytest
from pydantic import ValidationError

from ripple import execution_nodes
from ripple.accounts import AccountInput, ConnectionAction
from ripple.execution_nodes import NodeRegisterInput, NodeResultInput
from ripple.publishing import CreateInput, WorkflowError
from ripple.workspace import WorkspaceService


def node_capabilities():
    return ["browser.interactive", "browser.automation", "browser.chrome", "x.login", "x.probe"]


def register_remote(work: WorkspaceService):
    pairing = work.execution_nodes.create_pairing(True)
    result = work.execution_nodes.register(NodeRegisterInput(
        pairing_code=pairing["pairing_code"], name="Client A", platform="windows",
        capabilities=node_capabilities(), browsers=["chrome"],
    ))
    return result["node"], result["node_token"]


def test_execution_node_pairing_tokens_and_capabilities_are_bounded(tmp_path):
    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    try:
        with pytest.raises(ValidationError):
            NodeRegisterInput(pairing_code="ABCD-EFGH", name="bad", platform="windows",
                              capabilities=["shell.exec"], browsers=["chrome"])
        node, token = register_remote(work)
        assert node["kind"] == "remote" and node["online"] is True
        assert token and "node_token" not in node and "token_hash" not in node
        listed = next(row for row in work.execution_nodes.list() if row["id"] == node["id"])
        assert "node_token" not in listed and "token_hash" not in listed
        with pytest.raises(WorkflowError, match="认证失败"):
            work.execution_nodes.heartbeat(node["id"], "wrong-token")
        assert work.execution_nodes.heartbeat(node["id"], token)["online"] is True
    finally:
        work.close()


def test_remote_x_login_and_probe_use_closed_command_queue(tmp_path):
    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    try:
        node, token = register_remote(work)
        account = work.accounts.create(AccountInput(
            platform="x", label="Remote X", idempotency_key=uuid.uuid4().hex, execution_node_id=node["id"],
        ))
        started = work.accounts.start(account["id"], "login", ConnectionAction(
            confirmed=True, headed=True, execution_node_id=node["id"], browser_channel="chrome",
        ))
        assert started["operation"]["state"] == "waiting_node"
        claimed = work.execution_nodes.claim(node["id"], token, 1)
        assert len(claimed) == 1
        command = claimed[0]
        assert command["kind"] == "login.start"
        assert command["profile_id"] == account["id"]
        assert set(command["payload"]) <= {"platform", "login_url", "label"}
        assert "cookie" not in str(command).lower() and "shell" not in str(command).lower()
        completed = work.execution_nodes.complete(node["id"], command["id"], token, NodeResultInput(
            node_token=token, state="waiting_user", message="browser opened",
        ))
        work.accounts.apply_node_command(completed)
        waiting = work.accounts.get(account["id"])
        assert waiting["login_state"] == "waiting_user"

        checking = work.accounts.start(account["id"], "probe", ConnectionAction(
            confirmed=True, headed=False, execution_node_id=node["id"], browser_channel="chrome",
        ))
        assert checking["operation"]["state"] == "waiting_node"
        probe = work.execution_nodes.claim(node["id"], token, 1)[0]
        assert probe["kind"] == "account.probe"
        completed = work.execution_nodes.complete(node["id"], probe["id"], token, NodeResultInput(
            node_token=token, state="connected", message="connected",
            identity={"logged_in": True, "name": "remote_fixture", "remote_id": "x-web:remote_fixture"},
        ))
        work.accounts.apply_node_command(completed)
        connected = work.accounts.get(account["id"])
        assert connected["status"] == "connected"
        assert connected["identity"]["name"] == "remote_fixture"
        assert connected["execution_node_id"] == node["id"]
    finally:
        work.close()


def test_remote_x_publish_is_blocked_before_dispatch(tmp_path):
    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    try:
        node, _token = register_remote(work)
        account = work.accounts.create(AccountInput(
            platform="x", label="Remote Publish", idempotency_key=uuid.uuid4().hex, execution_node_id=node["id"],
        ))
        with work.store.transaction() as state:
            row = state["accounts"][account["id"]]
            row.update(status="connected", auth_revision=2,
                       identity={"name": "remote_fixture", "remote_id": "x-web:remote_fixture", "logged_in": True})
        task = work.create(CreateInput(title="remote", body="body", platform="x", account_id=account["id"],
                                       mode="real", idempotency_key=uuid.uuid4().hex))
        result = work.preflight(task["id"], task["version_id"])
        assert result["ok"] is False
        assert any("远程 Browser Node" in problem and "尚未启用" in problem for problem in result["problems"])
    finally:
        work.close()


def test_native_x_login_launch_has_no_automation_flags(tmp_path, monkeypatch):
    # The release supports testing this command contract on headless/Linux CI even
    # though real interactive X login is only advertised when a browser is present.
    monkeypatch.setattr(execution_nodes, "interactive_browser_channels", lambda: ["chrome"])
    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    try:
        account = work.accounts.create(AccountInput(platform="x", label="native", idempotency_key=uuid.uuid4().hex))
        fake_exe = tmp_path / "chrome.exe"
        fake_exe.write_bytes(b"fixture")
        monkeypatch.setattr(execution_nodes, "browser_executable", lambda channel: fake_exe if channel == "chrome" else None)
        calls = []
        monkeypatch.setattr(subprocess, "Popen", lambda argv, **kwargs: calls.append((argv, kwargs)) or object())
        work.execution_nodes.launch_native_x_login(account, work.accounts.directory(account["id"]), "chrome")
        assert len(calls) == 1
        argv = calls[0][0]
        assert str(fake_exe) == argv[0]
        profile_arg = next(value for value in argv if value.startswith("--user-data-dir="))
        assert Path(profile_arg.split("=", 1)[1]) == work.accounts.directory(account["id"]) / "browser" / "XProfile"
        assert "--profile-directory=Default" in argv
        assert argv[-1] == "https://x.com/i/flow/login"
        lower = " ".join(argv).lower()
        assert "remote-debugging" not in lower
        assert "webdriver" not in lower
        assert "enable-automation" not in lower
        assert "playwright" not in lower
    finally:
        work.close()


def test_two_x_accounts_never_share_ripple_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(execution_nodes, "interactive_browser_channels", lambda: ["chrome"])
    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    try:
        one = work.accounts.create(AccountInput(platform="x", label="one", idempotency_key=uuid.uuid4().hex))
        two = work.accounts.create(AccountInput(platform="x", label="two", idempotency_key=uuid.uuid4().hex))
        assert one["profile_id"] != two["profile_id"]
        assert work.accounts.directory(one["id"]) / "browser" / "XProfile" != work.accounts.directory(two["id"]) / "browser" / "XProfile"
    finally:
        work.close()
