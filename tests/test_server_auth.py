from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ripple.api import install
from ripple.tenancy import DEFAULT_WORKSPACE_ID


def server_app(tmp_path, monkeypatch):
    monkeypatch.setenv("RIPPLE_DEPLOYMENT_MODE", "server")
    monkeypatch.setenv("RIPPLE_PUBLIC_ORIGIN", "https://testserver")
    monkeypatch.setenv("RIPPLE_TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("RIPPLE_BOOTSTRAP_CODE", "fixture-bootstrap-code-1234567890")
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    return app, service


def csrf_headers(client: TestClient) -> dict[str, str]:
    value = client.cookies.get("ripple_csrf")
    assert value
    return {"X-Ripple-CSRF": value}


def test_local_mode_remains_frictionless(tmp_path, monkeypatch):
    monkeypatch.delenv("RIPPLE_DEPLOYMENT_MODE", raising=False)
    monkeypatch.delenv("RIPPLE_PUBLIC_ORIGIN", raising=False)
    monkeypatch.delenv("RIPPLE_TRUSTED_HOSTS", raising=False)
    app = FastAPI()
    install(app, tmp_path / "outputs", private=tmp_path / "private")
    with TestClient(app, base_url="http://localhost") as client:
        status = client.get("/api/ripple/auth/status")
        assert status.status_code == 200
        payload = status.json()
        assert payload["mode"] == "local" and payload["authenticated"] is True and payload["setup_required"] is False
        created = client.post("/api/ripple/contents", json={"title": "Local", "idempotency_key": "local-auth-content"})
        assert created.status_code == 201
        assert created.json()["workspace_id"] == DEFAULT_WORKSPACE_ID


def test_server_mode_requires_setup_login_session_and_csrf(tmp_path, monkeypatch):
    app, service = server_app(tmp_path, monkeypatch)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            initial = client.get("/api/ripple/auth/status")
            assert initial.status_code == 200
            assert initial.json()["setup_required"] is True
            assert initial.json()["authenticated"] is False
            assert client.get("/api/ripple/channels").status_code == 401

            setup = client.post("/api/ripple/auth/setup", json={
                "username": "owner", "email": "owner@example.com", "password": "correct-horse-battery",
                "setup_code": "fixture-bootstrap-code-1234567890", "workspace_name": "Ripple Audit Workspace", "confirmed": True,
            })
            assert setup.status_code == 200
            assert setup.json()["user"]["role"] == "owner"
            assert client.cookies.get("ripple_session")
            assert client.cookies.get("ripple_csrf")
            assert client.get("/api/ripple/channels").status_code == 200
            x_config = client.get("/api/ripple/x/config")
            assert x_config.status_code == 200
            assert x_config.json()["callback_url"] == "https://testserver/api/ripple/x/oauth/callback"

            blocked = client.post("/api/ripple/contents", json={"title": "Blocked", "idempotency_key": "server-blocked-content"})
            assert blocked.status_code == 403
            created = client.post("/api/ripple/contents", headers=csrf_headers(client), json={"title": "Owned", "idempotency_key": "server-owned-content"})
            assert created.status_code == 201
            assert created.json()["workspace_id"] == DEFAULT_WORKSPACE_ID

            added = client.post("/api/ripple/auth/users", headers=csrf_headers(client), json={
                "username": "editor", "email": "", "password": "member-password-123", "role": "member",
            })
            assert added.status_code == 201 and added.json()["role"] == "member"
            users = client.get("/api/ripple/auth/users")
            assert users.status_code == 200 and {row["username"] for row in users.json()["items"]} == {"owner", "editor"}

            assert client.post("/api/ripple/auth/logout", json={}).status_code == 403
            logged_out = client.post("/api/ripple/auth/logout", headers=csrf_headers(client), json={})
            assert logged_out.status_code == 200
            assert client.get("/api/ripple/channels").status_code == 401

            wrong = client.post("/api/ripple/auth/login", json={"username": "owner", "password": "totally-wrong-password"})
            assert wrong.status_code == 401
            login = client.post("/api/ripple/auth/login", json={"username": "owner", "password": "correct-horse-battery"})
            assert login.status_code == 200
            assert client.get("/api/ripple/auth/me").json()["workspace_name"] == "Ripple Audit Workspace"
    finally:
        service.close()


def test_server_setup_requires_operator_bootstrap_code(tmp_path, monkeypatch):
    app, service = server_app(tmp_path, monkeypatch)
    try:
        with TestClient(app, base_url="https://testserver") as client:
            state = client.get("/api/ripple/auth/status").json()
            assert state["security"]["bootstrap_configured"] is True
            rejected = client.post("/api/ripple/auth/setup", json={
                "username": "owner", "password": "correct-horse-battery", "setup_code": "wrong-bootstrap-code-123456789",
                "workspace_name": "Ripple", "confirmed": True,
            })
            assert rejected.status_code == 403
            assert client.get("/api/ripple/auth/status").json()["setup_required"] is True
    finally:
        service.close()


def test_member_cannot_mutate_control_plane(tmp_path, monkeypatch):
    app, service = server_app(tmp_path, monkeypatch)
    monkeypatch.setattr("ripple.api.install_environment_component", lambda component: {"component": component, "ok": True})
    try:
        with TestClient(app, base_url="https://testserver") as owner, TestClient(app, base_url="https://testserver") as member:
            setup = owner.post("/api/ripple/auth/setup", json={
                "username": "owner", "password": "correct-horse-battery", "setup_code": "fixture-bootstrap-code-1234567890",
                "workspace_name": "Ripple", "confirmed": True,
            })
            assert setup.status_code == 200
            created = owner.post("/api/ripple/auth/users", headers=csrf_headers(owner), json={
                "username": "member", "password": "member-password-123", "role": "member",
            })
            assert created.status_code == 201
            assert member.post("/api/ripple/auth/login", json={"username": "member", "password": "member-password-123"}).status_code == 200
            admin_only_requests = [
                ("post", "/api/ripple/environment/browser/install", {"confirmed": True}),
                ("post", "/api/ripple/accounts", {"platform": "x", "label": "blocked", "idempotency_key": "blocked-member-account"}),
                ("post", "/api/ripple/wechat/connect", {"label": "blocked", "app_id": "wx1234567890abcdef", "app_secret": "blocked-secret", "confirmed": True}),
                ("post", "/api/ripple/blog/connectors", {"label": "blocked", "openapi_url": "https://example.com/openapi.json", "token": "blocked", "confirmed": True}),
            ]
            for method, path, body in admin_only_requests:
                blocked = member.request(method, path, headers=csrf_headers(member), json=body)
                assert blocked.status_code == 403, path
            allowed = owner.post("/api/ripple/environment/browser/install", headers=csrf_headers(owner), json={"confirmed": True})
            assert allowed.status_code == 200
    finally:
        service.close()


def test_invalid_deployment_mode_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("RIPPLE_DEPLOYMENT_MODE", "sevrer")
    app = FastAPI()
    with pytest.raises(RuntimeError, match="local or server"):
        install(app, tmp_path / "outputs", private=tmp_path / "private")


def test_server_setup_refuses_insecure_public_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("RIPPLE_DEPLOYMENT_MODE", "server")
    monkeypatch.setenv("RIPPLE_PUBLIC_ORIGIN", "http://testserver")
    monkeypatch.setenv("RIPPLE_TRUSTED_HOSTS", "testserver")
    monkeypatch.setenv("RIPPLE_BOOTSTRAP_CODE", "fixture-bootstrap-code-1234567890")
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    try:
        with TestClient(app, base_url="http://testserver") as client:
            state = client.get("/api/ripple/auth/status").json()
            assert state["security"]["configured"] is False
            setup = client.post("/api/ripple/auth/setup", json={
                "username": "owner", "password": "correct-horse-battery", "setup_code": "fixture-bootstrap-code-1234567890", "workspace_name": "Ripple", "confirmed": True,
            })
            assert setup.status_code == 503
    finally:
        service.close()
