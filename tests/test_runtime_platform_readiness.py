"""验证进程复用和平台发布就绪状态，全部使用隔离数据。"""
from pathlib import Path

import pytest

from ripple.accounts import AccountInput
from ripple.catalog import native_publish_available
from ripple.opencode_runtime import OpenCodeAgentAdapter, OpenCodeError
from ripple.publishing import CreateInput
from ripple.workspace import WorkspaceService


@pytest.mark.parametrize("mismatch", ["legacy", "token", "callback"])
def test_stale_opencode_is_not_healthy_or_reused(tmp_path, monkeypatch, mismatch):
    runtime = OpenCodeAgentAdapter(tmp_path, tmp_path / "private", lambda: {}, tool_token="current-token")
    callback = "http://127.0.0.1:7860"
    monkeypatch.setattr(runtime, "_settings", lambda: {"model": "fixture", "port": "4096"})
    monkeypatch.setattr(runtime, "_health", lambda _: {"healthy": True})
    old = OpenCodeAgentAdapter(tmp_path, tmp_path / "private", lambda: {},
                               tool_token="old-token" if mismatch == "token" else "current-token")
    instructions = [str(runtime.workspace_dir / "RIPPLE_AGENT.md")]
    if mismatch != "legacy":
        instructions.append(old._tool_binding_path("http://127.0.0.1:7861" if mismatch == "callback" else callback).as_posix())
    monkeypatch.setattr(runtime, "_request", lambda *args, **kwargs: {"model": "ripple/fixture", "instructions": instructions})
    status = runtime.status(callback)
    assert status["configured"] is True and status["healthy"] is False
    assert "工具连接已过期" in status["detail"]
    with pytest.raises(OpenCodeError, match="工具连接已过期"):
        runtime.ensure_started(callback)


def test_same_credentials_and_callback_can_reuse_runtime_after_restart(tmp_path, monkeypatch):
    old = OpenCodeAgentAdapter(tmp_path, tmp_path / "private", lambda: {}, tool_token="persisted-token")
    new = OpenCodeAgentAdapter(tmp_path, tmp_path / "private", lambda: {}, tool_token=old.tool_token)
    callback = "http://127.0.0.1:7860"
    monkeypatch.setattr(new, "_settings", lambda: {"model": "fixture", "port": "4096"})
    monkeypatch.setattr(new, "_health", lambda _: {"healthy": True})
    monkeypatch.setattr(new, "_request", lambda *args, **kwargs: {
        "model": "ripple/fixture", "instructions": [str(old.workspace_dir / "RIPPLE_AGENT.md"), old._tool_binding_path(callback).as_posix()],
    })
    assert new.status(callback)["healthy"] is True
    assert new.ensure_started(callback + "/")["healthy"] is True
    assert old.tool_token not in str(old._tool_binding_path(callback))


def test_missing_publisher_does_not_disable_account_or_allow_publish(tmp_path, monkeypatch):
    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    try:
        account = work.accounts.create(AccountInput(platform="douyin", label="测试", idempotency_key="read-only-douyin"))
        with work.store.transaction() as state:
            state["accounts"][account["id"]].update(status="connected", identity={"remote_id": "owner", "logged_in": True})
        monkeypatch.setattr(work.accounts, "environment", {"browser": "chromium", "biliup": True})
        monkeypatch.setattr("ripple.workspace.native_publish_available", lambda platform: platform != "douyin")
        monkeypatch.setattr("ripple.accounts.native_publish_available", lambda platform: platform != "douyin")
        channel = next(row for row in work.channels() if row["id"] == "douyin")
        assert channel["connected"] is True and channel["adapter_available"] is True
        assert channel["publish_available"] is False and channel["direct_publish"] is False
        assert channel["local_schedule"] is False
        task = work.create(CreateInput(platform="douyin", title="测试", body="正文", account_id=account["id"],
                                     mode="real", idempotency_key="missing-publisher-task"))
        checked = work.preflight(task["id"], task["version_id"])
        assert checked["ok"] is False
        assert any("发布模块" in problem for problem in checked["problems"])
        assert checked["task"]["attempts"] == 0 and checked["task"]["approval"] is None
        monkeypatch.setattr("ripple.accounts.subprocess.Popen", lambda *args, **kwargs: pytest.fail("缺少发布模块时不能启动执行器"))
        result = work.accounts.run(account, "publish", "a" * 32)
        assert result["not_submitted"] is True and result["state"] == "failed_terminal"
        assert work.accounts.get(account["id"])["identity"]["remote_id"] == "owner"
    finally:
        work.close()


def test_publisher_probe_checks_packaged_file_without_importing(monkeypatch):
    monkeypatch.setattr(Path, "is_file", lambda path: path.name == "xhs_publish.py")
    assert native_publish_available("xiaohongshu") is True
    assert native_publish_available("douyin") is False
    assert native_publish_available("tiktok") is False


def test_xhs_notes_passes_verified_identity_and_does_not_accept_caller_override(tmp_path, monkeypatch):
    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    try:
        account = work.accounts.create(AccountInput(platform="xiaohongshu", label="测试", idempotency_key="xhs-notes-owner"))
        with work.store.transaction() as state:
            state["accounts"][account["id"]].update(status="connected", identity={"remote_id": "verified-owner", "logged_in": True})
        calls = []
        def run(account, operation, operation_id, **extra):
            calls.append((operation, extra["xhs_action"], extra["xhs_params"]))
            return {"state": "success", "data": {"items": []}}
        monkeypatch.setattr(work.accounts, "run", run)
        work.xhs_ops.notes(account["id"], 8)
        work.xhs_ops._worker(account["id"], "notes", {"expected_account_remote_id": "another-owner"})
        assert calls[0] == ("xhs_read", "notes", {"limit": 8, "expected_account_remote_id": "verified-owner"})
        assert calls[1][2]["expected_account_remote_id"] == "verified-owner"
    finally:
        work.close()
