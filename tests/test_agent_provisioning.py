from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from ripple.agent_provisioning import AgentAdapterProvisioningService
from ripple.agent_runtime import AgentRuntimeError


def _runtime(*, installed: bool = True, ready: bool = False, detail: str = "") -> dict:
    return {
        "installed": installed,
        "native_detected": installed,
        "ready": ready,
        "selectable": ready,
        "healthy": ready,
        "detail": detail,
    }


def test_adapter_options_are_hidden_until_native_agent_is_detected(tmp_path: Path):
    service = AgentAdapterProvisioningService(tmp_path / "private")

    assert service.options_for_runtime("claude_code", _runtime(installed=False)) == []
    assert not service.root.exists(), "read-only detection must not materialize provisioning storage"


def test_manifest_distinguishes_builtin_and_installable_adapters(tmp_path: Path):
    service = AgentAdapterProvisioningService(tmp_path / "private")

    opencode = service.options_for_runtime("opencode", _runtime(ready=True))
    assert [(row["id"], row["state"], row["actions"]) for row in opencode] == [
        ("opencode_native", "ready", []),
    ]

    claude = service.options_for_runtime("claude_code", _runtime())
    assert claude[0]["id"] == "claude_acp"
    assert claude[0]["state"] == "missing"
    assert [row["id"] for row in claude[0]["actions"]] == ["install"]
    assert claude[0]["package"] == "@agentclientprotocol/claude-agent-acp"
    assert claude[0]["version"] == "0.76.0"

    codex = service.options_for_runtime("codex", _runtime(detail="restricted probe failed"))
    assert codex[0]["id"] == "codex_acp"
    assert codex[0]["state"] == "missing"
    assert [row["id"] for row in codex[0]["actions"]] == ["install"]
    assert codex[0]["package"] == "@agentclientprotocol/codex-acp"

    hermes = service.options_for_runtime("hermes", _runtime())
    assert hermes[0]["id"] == "hermes_transport"
    assert hermes[0]["state"] == "verification_failed"
    assert hermes[0]["actions"] == []


def test_action_requires_detected_native_agent_and_matching_runtime(tmp_path: Path, monkeypatch):
    service = AgentAdapterProvisioningService(tmp_path / "private")

    with pytest.raises(AgentRuntimeError, match="未检测") as absent:
        service.perform("claude_code", "claude_acp", "install", _runtime(installed=False))
    assert absent.value.status == 409

    with pytest.raises(AgentRuntimeError, match="不属于") as mismatch:
        service.perform("codex", "claude_acp", "install", _runtime())
    assert mismatch.value.status == 404

    called: list[str] = []
    monkeypatch.setattr(service, "_install", lambda spec: called.append(spec.id) or {"ok": True, "adapter_id": spec.id})
    result = service.perform("claude_code", "claude_acp", "install", _runtime())
    assert result["ok"] is True
    assert called == ["claude_acp"]


def test_builtin_adapter_cannot_be_installed(tmp_path: Path):
    service = AgentAdapterProvisioningService(tmp_path / "private")

    for runtime_id, adapter_id in (("opencode", "opencode_native"), ("hermes", "hermes_transport")):
        with pytest.raises(AgentRuntimeError, match="不允许") as error:
            service.perform(runtime_id, adapter_id, "install", _runtime())
        assert error.value.status == 409


def test_managed_receipt_controls_ready_state_and_uninstall_ownership(tmp_path: Path):
    service = AgentAdapterProvisioningService(tmp_path / "private")
    spec = service.spec("claude_acp")
    root = service._artifact_root(spec)
    package_root = root / service._package_rel(spec.package)
    package_root.mkdir(parents=True)
    (package_root / "package.json").write_text(json.dumps({"name": spec.package, "version": spec.version}), encoding="utf-8")
    (package_root / spec.entrypoint).parent.mkdir(parents=True, exist_ok=True)
    (package_root / spec.entrypoint).write_text("// fixture", encoding="utf-8")
    (root / "package-lock.json").write_text(json.dumps({
        "packages": {service._package_rel(spec.package).as_posix(): {"integrity": spec.integrity}},
    }), encoding="utf-8")

    unverified = service.inspect(spec, _runtime())
    assert unverified["state"] == "installed_unverified"
    assert {row["id"] for row in unverified["actions"]} == {"verify", "repair"}
    with pytest.raises(AgentRuntimeError, match="不是 Ripple 管理"):
        service._uninstall(spec)

    service._atomic_json(service._receipt_path(spec), {
        "schema": 1,
        "managed_by": "ripple",
        "adapter_id": spec.id,
        "runtime_id": spec.runtime_id,
        "package": spec.package,
        "version": spec.version,
        "integrity": spec.integrity,
        "verified": True,
    })
    ready = service.inspect(spec, _runtime())
    assert ready["state"] == "ready"
    assert {row["id"] for row in ready["actions"]} == {"verify", "uninstall"}

    result = service._uninstall(spec)
    assert result["state"] == "missing"
    assert not root.exists()


def test_acp_verifier_requires_real_initialize_response(tmp_path: Path):
    service = AgentAdapterProvisioningService(tmp_path / "private")
    spec = service.spec("claude_acp")
    agent = tmp_path / "fake_acp.py"
    agent.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        "    msg=json.loads(line)\n"
        "    if msg.get('method')=='initialize':\n"
        "        print(json.dumps({'jsonrpc':'2.0','id':msg['id'],'result':{'protocolVersion':1}}),flush=True)\n"
        "        break\n",
        encoding="utf-8",
    )

    proof = service._verify_acp([sys.executable, str(agent)], spec, timeout=5)
    assert proof == {"protocol": "acp-stdio", "handshake": True}


def test_manifest_rejects_unlocked_npm_adapter(tmp_path: Path):
    manifest = tmp_path / "bad.json"
    manifest.write_text(json.dumps({
        "schema": 1,
        "adapters": [{
            "id": "bad_adapter",
            "runtime_id": "claude_code",
            "label": "Bad",
            "kind": "npm_acp",
            "protocol": "acp-stdio",
            "enabled": True,
            "description": "fixture",
            "source_url": "https://example.invalid",
            "package": "package-from-client",
            "version": "latest",
            "integrity": "",
            "entrypoint": "../../escape.js",
        }],
    }), encoding="utf-8")

    with pytest.raises(RuntimeError, match="invalid npm package|lacks a locked npm artifact"):
        AgentAdapterProvisioningService(tmp_path / "private", manifest_path=manifest)
