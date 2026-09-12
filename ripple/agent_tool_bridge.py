"""Authenticated MCP bridge for Ripple-owned Agent tools.

Native Agent runtimes connect to one loopback Streamable HTTP MCP endpoint.  A
short-lived lease binds that connection to one Ripple web session, one runtime
and the Broker-produced tool allowlist. Tool listing and every invocation are
rechecked against the lease; the bridge never grants shell/file/network powers.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass
import json
import secrets
import threading
import time
from typing import Any, Awaitable, Callable

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from .agent_capabilities import INTERNAL_TOOLS
from .agent_runtime import AgentRuntimeError


MAX_ARGUMENT_BYTES = 160_000
DEFAULT_LEASE_SECONDS = 900
MAX_LEASES = 256
_CURRENT_LEASE_TOKEN: ContextVar[str | None] = ContextVar("ripple_agent_tool_lease", default=None)


@dataclass(frozen=True)
class AgentToolLease:
    token: str
    web_session_id: str
    runtime_id: str
    turn_id: str
    tools: tuple[str, ...]
    issued_at: float
    expires_at: float

    def public(self, endpoint: str = "") -> dict[str, Any]:
        return {
            "web_session_id": self.web_session_id,
            "runtime_id": self.runtime_id,
            "turn_id": self.turn_id,
            "tools": list(self.tools),
            "expires_at": self.expires_at,
            "endpoint": endpoint,
        }


class AgentToolLeaseStore:
    """In-memory, bounded, fail-closed capability leases for MCP turns."""

    def __init__(self, *, default_ttl: int = DEFAULT_LEASE_SECONDS):
        self.default_ttl = max(30, min(int(default_ttl), 3600))
        self._leases: dict[str, AgentToolLease] = {}
        self._lock = threading.RLock()

    def _purge(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        expired = [token for token, lease in self._leases.items() if lease.expires_at <= current]
        for token in expired:
            self._leases.pop(token, None)
        if len(self._leases) > MAX_LEASES:
            ordered = sorted(self._leases.values(), key=lambda lease: lease.issued_at)
            for lease in ordered[: len(self._leases) - MAX_LEASES]:
                self._leases.pop(lease.token, None)

    def issue(
        self,
        web_session_id: str,
        runtime_id: str,
        tools: list[str] | tuple[str, ...],
        *,
        turn_id: str = "",
        ttl: int | None = None,
    ) -> AgentToolLease:
        allowed: list[str] = []
        for value in tools:
            name = str(value or "").strip()
            if name not in INTERNAL_TOOLS:
                raise AgentRuntimeError(f"Agent Tool lease 包含未注册能力：{name or '空'}", 422)
            if name not in allowed:
                allowed.append(name)
        if not web_session_id or len(web_session_id) > 256 or not runtime_id or len(runtime_id) > 80:
            raise AgentRuntimeError("Agent Tool lease 会话信息无效。", 422)
        now = time.time()
        duration = self.default_ttl if ttl is None else max(30, min(int(ttl), 3600))
        lease = AgentToolLease(
            token=secrets.token_urlsafe(36),
            web_session_id=web_session_id,
            runtime_id=runtime_id,
            turn_id=str(turn_id or "")[:128],
            tools=tuple(allowed[:80]),
            issued_at=now,
            expires_at=now + duration,
        )
        with self._lock:
            self._purge(now)
            self._leases[lease.token] = lease
            self._purge(now)
        return lease

    def require(self, token: str, tool_id: str | None = None) -> AgentToolLease:
        if not token:
            raise AgentRuntimeError("Ripple MCP lease 缺失。", 403)
        now = time.time()
        with self._lock:
            self._purge(now)
            lease = self._leases.get(token)
            if lease is None or lease.expires_at <= now:
                raise AgentRuntimeError("Ripple MCP lease 无效或已过期。", 403)
            if tool_id is not None and tool_id not in lease.tools:
                raise AgentRuntimeError(f"当前 Ripple 会话未授权 Tool：{tool_id}", 403)
            return lease

    def revoke(self, token: str) -> None:
        if not token:
            return
        with self._lock:
            self._leases.pop(token, None)

    def revoke_session(self, web_session_id: str) -> None:
        with self._lock:
            for token in [token for token, lease in self._leases.items() if lease.web_session_id == web_session_id]:
                self._leases.pop(token, None)


ToolExecutor = Callable[[AgentToolLease, str, dict[str, Any]], Awaitable[Any]]


class _LeaseAuthMiddleware:
    def __init__(self, app, leases: AgentToolLeaseStore):
        self.app = app
        self.leases = leases

    @staticmethod
    def _bearer(scope: dict[str, Any]) -> str:
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw = headers.get(b"authorization", b"").decode("latin-1", "ignore").strip()
        if raw.lower().startswith("bearer "):
            return raw[7:].strip()
        return headers.get(b"x-ripple-agent-lease", b"").decode("latin-1", "ignore").strip()

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        token = self._bearer(scope)
        try:
            self.leases.require(token)
        except AgentRuntimeError as exc:
            response = JSONResponse({"detail": str(exc)}, status_code=exc.status)
            await response(scope, receive, send)
            return
        marker = _CURRENT_LEASE_TOKEN.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            _CURRENT_LEASE_TOKEN.reset(marker)


class RippleAgentToolBridge:
    """Streamable HTTP MCP facade over the Ripple business-tool boundary."""

    def __init__(self, executor: ToolExecutor, *, leases: AgentToolLeaseStore | None = None):
        self.executor = executor
        self.leases = leases or AgentToolLeaseStore()
        self.mcp = FastMCP(
            "Ripple Tool Bridge",
            instructions=(
                "Only Ripple business tools granted to this conversation are visible. "
                "Publishing and interaction tools create drafts only; user confirmation remains in Ripple."
            ),
            host="127.0.0.1",
            streamable_http_path="/",
            stateless_http=True,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=["127.0.0.1", "127.0.0.1:*", "localhost", "localhost:*", "[::1]", "[::1]:*"],
                allowed_origins=["http://127.0.0.1:*", "http://localhost:*"],
            ),
            json_response=True,
        )
        self._register_tools()
        self._raw_app = self.mcp.streamable_http_app()
        self._app = _LeaseAuthMiddleware(self._raw_app, self.leases)
        self._lifespan_started = False
        self._lifespan_active = False
        self._install_filtered_listing()

    def _current_lease(self, tool_id: str | None = None) -> AgentToolLease:
        token = _CURRENT_LEASE_TOKEN.get() or ""
        return self.leases.require(token, tool_id)

    async def _call(self, tool_id: str, arguments: dict[str, Any] | None = None) -> Any:
        lease = self._current_lease(tool_id)
        payload = dict(arguments or {})
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise AgentRuntimeError("Ripple MCP Tool 参数无法序列化。", 422) from exc
        if len(encoded) > MAX_ARGUMENT_BYTES:
            raise AgentRuntimeError("Ripple MCP Tool 参数超过安全上限。", 422)
        return await self.executor(lease, tool_id, payload)

    def _install_filtered_listing(self) -> None:
        default_list = self.mcp.list_tools

        @self.mcp._mcp_server.list_tools()
        async def list_tools():
            lease = self._current_lease()
            tools = await default_list()
            allowed = set(lease.tools)
            return [tool for tool in tools if tool.name in allowed]

    def _register_tools(self) -> None:
        add = self.mcp.add_tool

        async def ripple_trends(platforms: str = "", limit: int = 6):
            return await self._call("ripple_trends", {"platforms": platforms, "limit": limit})
        add(ripple_trends, name="ripple_trends", description=INTERNAL_TOOLS["ripple_trends"]["description"])

        async def ripple_xhs_read(operation: str, account_id: str = "", query: str = "", note_id: str = "", url: str = "", limit: int = 12):
            return await self._call("ripple_xhs_read", {"operation": operation, "account_id": account_id, "query": query, "note_id": note_id, "url": url, "limit": limit})
        add(ripple_xhs_read, name="ripple_xhs_read", description=INTERNAL_TOOLS["ripple_xhs_read"]["description"])

        async def ripple_ideas_list(limit: int = 20):
            return await self._call("ripple_ideas_list", {"limit": limit})
        add(ripple_ideas_list, name="ripple_ideas_list", description=INTERNAL_TOOLS["ripple_ideas_list"]["description"])

        async def ripple_ideas_add(title: str, note: str = ""):
            return await self._call("ripple_ideas_add", {"title": title, "note": note})
        add(ripple_ideas_add, name="ripple_ideas_add", description=INTERNAL_TOOLS["ripple_ideas_add"]["description"])

        async def ripple_persona(name: str):
            return await self._call("ripple_persona", {"name": name})
        add(ripple_persona, name="ripple_persona", description=INTERNAL_TOOLS["ripple_persona"]["description"])

        async def ripple_operation(operation: str, input: dict[str, Any], source: dict[str, Any] | None = None):
            return await self._call("ripple_operation", {"operation": operation, "input": input, "source": source or {}})
        add(ripple_operation, name="ripple_operation", description=INTERNAL_TOOLS["ripple_operation"]["description"])

        async def ripple_content_draft(title: str | None = None, body: str | None = None, tags: str | None = None,
                                       media: list[str] | None = None, content_id: str | None = None,
                                       expected_version: str | None = None):
            return await self._call("ripple_content_draft", {"title": title, "body": body, "tags": tags, "media": media,
                                                               "content_id": content_id, "expected_version": expected_version})
        add(ripple_content_draft, name="ripple_content_draft", description=INTERNAL_TOOLS["ripple_content_draft"]["description"])

        async def ripple_content_read(operation: str, content_id: str = "", limit: int = 20):
            return await self._call("ripple_content_read", {"operation": operation, "content_id": content_id, "limit": limit})
        add(ripple_content_read, name="ripple_content_read", description=INTERNAL_TOOLS["ripple_content_read"]["description"])

        async def ripple_media_capabilities():
            return await self._call("ripple_media_capabilities", {})
        add(ripple_media_capabilities, name="ripple_media_capabilities", description=INTERNAL_TOOLS["ripple_media_capabilities"]["description"])

        async def ripple_generate_image(prompt: str, size: str = "1024x1024", resolution: str = "2k",
                                        provider_id: str | None = None, model: str | None = None):
            return await self._call("ripple_generate_image", {"prompt": prompt, "size": size, "resolution": resolution,
                                                               "provider_id": provider_id, "model": model})
        add(ripple_generate_image, name="ripple_generate_image", description=INTERNAL_TOOLS["ripple_generate_image"]["description"])

        async def ripple_generate_video(prompt: str, ratio: str = "9:16", duration: int | None = None,
                                        provider_id: str | None = None, model: str | None = None):
            return await self._call("ripple_generate_video", {"prompt": prompt, "ratio": ratio, "duration": duration,
                                                               "provider_id": provider_id, "model": model})
        add(ripple_generate_video, name="ripple_generate_video", description=INTERNAL_TOOLS["ripple_generate_video"]["description"])

        async def ripple_publish_draft(title: str, platform: str, body: str = "", account_id: str = "", tags: str = ""):
            return await self._call("ripple_publish_draft", {"title": title, "platform": platform, "body": body,
                                                              "account_id": account_id, "tags": tags})
        add(ripple_publish_draft, name="ripple_publish_draft", description=INTERNAL_TOOLS["ripple_publish_draft"]["description"])

        async def ripple_publish_status(task_id: str = "", limit: int = 20):
            return await self._call("ripple_publish_status", {"task_id": task_id, "limit": limit})
        add(ripple_publish_status, name="ripple_publish_status", description=INTERNAL_TOOLS["ripple_publish_status"]["description"])

        async def ripple_interaction_draft(platform: str, kind: str, account_id: str = "", source_id: str = "",
                                           target_id: str = "", target_url: str = "", items: list[dict[str, Any]] | None = None,
                                           text: str = ""):
            return await self._call("ripple_interaction_draft", {"platform": platform, "kind": kind, "account_id": account_id,
                                                                  "source_id": source_id, "target_id": target_id, "target_url": target_url,
                                                                  "items": items or [], "text": text})
        add(ripple_interaction_draft, name="ripple_interaction_draft", description=INTERNAL_TOOLS["ripple_interaction_draft"]["description"])

        async def ripple_interactions_read(operation: str, platform: str = "", limit: int = 50):
            return await self._call("ripple_interactions_read", {"operation": operation, "platform": platform, "limit": limit})
        add(ripple_interactions_read, name="ripple_interactions_read", description=INTERNAL_TOOLS["ripple_interactions_read"]["description"])

        async def ripple_mcp(capability_id: str, arguments: dict[str, Any] | None = None):
            return await self._call("ripple_mcp", {"capability_id": capability_id, "arguments": arguments or {}})
        add(ripple_mcp, name="ripple_mcp", description=INTERNAL_TOOLS["ripple_mcp"]["description"])

        async def ripple_accounts():
            return await self._call("ripple_accounts", {})
        add(ripple_accounts, name="ripple_accounts", description=INTERNAL_TOOLS["ripple_accounts"]["description"])

        async def ripple_analytics():
            return await self._call("ripple_analytics", {})
        add(ripple_analytics, name="ripple_analytics", description=INTERNAL_TOOLS["ripple_analytics"]["description"])

    def asgi_app(self):
        # Mount the bridge itself so a restarted host lifespan (common in tests)
        # can swap in a fresh SDK session manager without remounting routes.
        return self

    async def __call__(self, scope, receive, send):
        await self._app(scope, receive, send)

    @asynccontextmanager
    async def lifespan(self):
        if self._lifespan_active:
            raise RuntimeError("Ripple Tool Bridge lifespan is already active")
        if self._lifespan_started:
            # MCP 1.x session managers are single-run. FastAPI TestClient may
            # restart one application several times, so recreate only the HTTP
            # manager while retaining the stable tool registry/handlers.
            self.mcp._session_manager = None
            self._raw_app = self.mcp.streamable_http_app()
            self._app = _LeaseAuthMiddleware(self._raw_app, self.leases)
        self._lifespan_started = True
        self._lifespan_active = True
        try:
            async with self.mcp.session_manager.run():
                yield
        finally:
            self._lifespan_active = False

    def issue(self, web_session_id: str, runtime_id: str, tools: list[str], *, turn_id: str = "", ttl: int | None = None) -> AgentToolLease:
        return self.leases.issue(web_session_id, runtime_id, tools, turn_id=turn_id, ttl=ttl)

    def revoke(self, token: str) -> None:
        self.leases.revoke(token)


def install_agent_tool_bridge(app, bridge: RippleAgentToolBridge, *, mount_path: str = "/api/agent/mcp") -> None:
    """Mount bridge and compose its session-manager lifecycle with host FastAPI."""
    app.mount(mount_path, bridge.asgi_app(), name="ripple-agent-mcp")
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance):
        async with previous_lifespan(instance):
            async with bridge.lifespan():
                yield

    app.router.lifespan_context = lifespan
