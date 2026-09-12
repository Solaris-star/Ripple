"""Loopback API for the Ripple content and account workspace."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import hmac
import os
from pathlib import Path
import re
import uuid
from typing import Any

from fastapi import APIRouter, FastAPI, HTTPException, Query, Request, File, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel, ConfigDict, Field

from .accounts import AccountInput, ConnectionAction, DeleteAccountInput, SmsInput
from .bridge import BridgeInput
from .rest_bridge import RestConfig, RemoteImport
from .x_adapter import XConfigInput, XConnectInput
from .wechat_adapter import WeChatConnectInput, WeChatReconnectInput
from .batches import BatchInput, derive_batch
from .library import MotherCreate, MotherRevision
from .variants import VariantRevision, VariantTaskCreate
from .blog_connector import BlogConnectorInput, BlogConnectorDelete
from .execution_nodes import PairingRequest, NodeRegisterInput, NodeAuthInput, NodeClaimInput, NodeResultInput
from .auth import (
    SESSION_COOKIE, CSRF_COOKIE, OwnerSetupInput, LoginInput, UserCreateInput, UserDeleteInput,
    server_security_config,
)
from .tenancy import DEFAULT_WORKSPACE_ID
from .catalog import environment_capabilities, install_environment_component
from .publishing import ApprovalInput, CreateInput, RevisionInput, VersionAction, WorkflowError, EXTENSIONS
from .store import StoreError
from .workspace import WorkspaceService, ReceiptInput, MAX_VIDEO_BYTES, MAX_IMAGE_BYTES

logger = logging.getLogger("ripple")


class WatermarkPathInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    path: str = Field(min_length=3, max_length=1200)


class WatermarkCleanPathInput(WatermarkPathInput):
    confirmed: bool = False


class WatermarkTaskInput(VersionAction):
    confirmed: bool = False


class OperationExecuteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input: dict[str, Any] = Field(default_factory=dict)
    source: dict[str, Any] = Field(default_factory=dict)


class EnvironmentInstallInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False


class XhsLocatorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note_id: str = Field(default="", max_length=100)
    url: str = Field(default="", max_length=4096)


class XhsCommentsInput(XhsLocatorInput):
    limit: int = Field(default=50, ge=1, le=100)


class XhsInteractionItem(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(default="", max_length=100)
    nickname: str = Field(default="", max_length=80)
    content: str = Field(default="", max_length=500)
    reply: str = Field(default="", max_length=1000)


class XhsInteractionDraftInput(XhsLocatorInput):
    account_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: str = Field(pattern=r"^(reply|delete|comment)$")
    items: list[XhsInteractionItem] = Field(default_factory=list, max_length=20)
    text: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


class XhsInteractionExecuteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False


class InteractionRemoteCommentsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    target_id: str = Field(default="", max_length=100)
    target_url: str = Field(default="", max_length=4096)
    target_label: str = Field(default="", max_length=120)
    limit: int = Field(default=100, ge=1, le=100)


class InteractionItemInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    id: str = Field(default="", max_length=100)
    nickname: str = Field(default="", max_length=80)
    content: str = Field(default="", max_length=500)
    reply: str = Field(default="", max_length=1000)


class InteractionDraftInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    platform: str = Field(min_length=1, max_length=40)
    account_id: str = Field(default="", max_length=32)
    source_id: str = Field(default="", max_length=32)
    target_id: str = Field(default="", max_length=100)
    target_url: str = Field(default="", max_length=4096)
    kind: str = Field(pattern=r"^(reply|delete|comment)$")
    items: list[InteractionItemInput] = Field(default_factory=list, max_length=20)
    text: str = Field(default="", max_length=1000)
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


class InteractionRevisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_updated_at: str = Field(min_length=10, max_length=80)
    items: list[InteractionItemInput] = Field(default_factory=list, max_length=20)
    text: str = Field(default="", max_length=1000)


class InteractionVersionAction(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_updated_at: str = Field(min_length=10, max_length=80)


class InteractionExecuteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False


class InteractionResolveInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    result: str = Field(pattern=r"^(verified|not_submitted)$")
    confirmed: bool = False
    note: str = Field(default="", max_length=500)


class _OAuthCallbackAccessFilter(logging.Filter):
    def filter(self, record):
        try:
            return "/api/ripple/x/oauth/callback" not in record.getMessage()
        except Exception:
            return True


# Also protects deployments started with `uvicorn web.app:app`, where the CLI
# would otherwise log the callback query containing the short-lived auth code.
logging.getLogger("uvicorn.access").addFilter(_OAuthCallbackAccessFilter())


def install(app: FastAPI, outputs: Path, *, private: Path | None = None) -> WorkspaceService:
    service = WorkspaceService(outputs, private=private)
    app.state.ripple = service
    router = APIRouter(prefix="/api/ripple", tags=["Ripple"])

    def _cookie_response(payload: dict, session: dict | None = None, *, clear: bool = False):
        response = JSONResponse(payload)
        security = service.auth.security()
        if clear:
            response.delete_cookie(SESSION_COOKIE, path="/")
            response.delete_cookie(CSRF_COOKIE, path="/")
        elif session:
            response.set_cookie(SESSION_COOKIE, session["session_token"], max_age=session["max_age"], path="/",
                                secure=bool(security["secure_cookie"]), httponly=True, samesite="lax")
            response.set_cookie(CSRF_COOKIE, session["csrf_token"], max_age=session["max_age"], path="/",
                                secure=bool(security["secure_cookie"]), httponly=False, samesite="lax")
        return response

    @router.get("/auth/status")
    def auth_status(request: Request):
        return service.auth.status(request.cookies.get(SESSION_COOKIE, ""))

    @router.post("/auth/setup")
    def auth_setup(req: OwnerSetupInput):
        result = service.auth.setup(req)
        return _cookie_response({"user": result["user"]}, result)

    @router.post("/auth/login")
    def auth_login(req: LoginInput, request: Request):
        client_key = request.client.host if request.client else "unknown"
        result = service.auth.login(req, client_key)
        return _cookie_response({"user": result["user"]}, result)

    @router.post("/auth/logout")
    def auth_logout(request: Request):
        result = service.auth.logout(request.cookies.get(SESSION_COOKIE, ""))
        return _cookie_response(result, clear=True)

    @router.get("/auth/me")
    def auth_me(request: Request):
        user = service.auth.authenticate(request.cookies.get(SESSION_COOKIE, ""))
        if not user:
            raise HTTPException(401, "请先登录 Ripple。")
        return user

    @router.get("/auth/users")
    def auth_users(request: Request):
        return service.auth.list_users(request.cookies.get(SESSION_COOKIE, ""))

    @router.post("/auth/users", status_code=201)
    def auth_user_create(req: UserCreateInput, request: Request):
        return service.auth.create_user(request.cookies.get(SESSION_COOKIE, ""), req)

    @router.delete("/auth/users/{user_id}")
    def auth_user_delete(user_id: str, req: UserDeleteInput, request: Request):
        return service.auth.delete_user(request.cookies.get(SESSION_COOKIE, ""), user_id, req.confirmed)

    @router.get("/status")
    def status():
        channels = service.channels()
        bridge = service.bridge.status()
        return {"brand": "Ripple", "version": "0.2.7", "local_only": service.auth.mode == "local",
                "deployment_mode": service.auth.mode,
                "live_publishing": any(c["direct_publish"] for c in channels), "native_adapters": True,
                "ai_enabled": os.environ.get("RIPPLE_ENABLE_AI") == "1", "environment": service.accounts.environment,
                "scheduler": "服务运行期间执行；过期任务须重新审核",
                "aitoearn": {"server_relay": "not_verified", "ai_relay": "not_configured", "enabled": False,
                              "mcp_configured": bridge['configured'], "mcp_connection": bridge['connection']}}

    @router.get("/channels")
    def channels():
        return service.channels()

    @router.get("/environment")
    def environment():
        service.accounts.refresh_environment()
        return environment_capabilities()

    @router.post("/environment/{component}/install")
    async def environment_install(component: str, req: EnvironmentInstallInput):
        if not req.confirmed:
            raise HTTPException(422, "请确认安装该本地环境组件。")
        if component not in {"browser", "bilibili"}:
            raise HTTPException(404, "未知环境组件。")
        try:
            result = await asyncio.to_thread(install_environment_component, component)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc
        service.accounts.refresh_environment()
        return result

    @router.get("/operations")
    def operations():
        return service.operations.registry()

    @router.get("/operations/templates")
    def operation_templates():
        return service.operations.templates()

    @router.get("/operations/results")
    def operation_results(operation_id: str = Query("", max_length=80), source_ref: str = Query("", max_length=200),
                          limit: int = Query(30, ge=1, le=100)):
        return service.operations.results(operation_id=operation_id, source_ref=source_ref, limit=limit)

    @router.post("/operations/{operation_id}/execute")
    def operation_execute(operation_id: str, req: OperationExecuteInput):
        return service.operations.execute(operation_id, req.input, req.source)

    def _workspace_id(request: Request) -> str:
        user = getattr(request.state, "ripple_user", None) or service.auth.authenticate(request.cookies.get(SESSION_COOKIE, ""))
        return str((user or {}).get("workspace_id") or DEFAULT_WORKSPACE_ID)

    @router.get("/execution-nodes")
    def execution_nodes(request: Request):
        return {"items": service.execution_nodes.list(_workspace_id(request))}

    @router.post("/execution-nodes/pairing")
    def execution_node_pairing(req: PairingRequest, request: Request):
        if service.auth.mode == "server":
            user = service.auth.require_role(request.cookies.get(SESSION_COOKIE, ""), {"owner", "admin"})
            workspace_id = user["workspace_id"]
        else:
            workspace_id = _workspace_id(request)
        return service.execution_nodes.create_pairing(req.confirmed, workspace_id)

    @router.post("/execution-nodes/register", status_code=201)
    def execution_node_register(req: NodeRegisterInput):
        return service.execution_nodes.register(req)

    @router.post("/execution-nodes/{node_id}/heartbeat")
    def execution_node_heartbeat(node_id: str, req: NodeAuthInput):
        return service.execution_nodes.heartbeat(node_id, req.node_token.get_secret_value())

    @router.post("/execution-nodes/{node_id}/commands/claim")
    def execution_node_claim(node_id: str, req: NodeClaimInput):
        return {"items": service.execution_nodes.claim(node_id, req.node_token.get_secret_value(), req.limit)}

    @router.post("/execution-nodes/{node_id}/commands/{command_id}/complete")
    def execution_node_complete(node_id: str, command_id: str, req: NodeResultInput):
        command = service.execution_nodes.complete(node_id, command_id, req.node_token.get_secret_value(), req)
        if command.get("kind") in {"login.start", "account.probe"}:
            service.accounts.apply_node_command(command)
        return {"ok": True}

    @router.get("/accounts")
    def accounts():
        return service.accounts.list()

    @router.post("/accounts", status_code=201)
    def create_account(req: AccountInput):
        return service.accounts.create(req)

    @router.get("/accounts/{account_id}")
    def account(account_id: str):
        return service.accounts.get(account_id)

    @router.delete("/accounts/{account_id}")
    def delete_account(account_id: str, req: DeleteAccountInput):
        return service.delete_account(account_id, req.confirmed)

    @router.post("/accounts/{account_id}/login")
    def login(account_id: str, req: ConnectionAction):
        return service.account_action(account_id, "login", req)

    @router.post("/accounts/{account_id}/probe")
    def probe(account_id: str, req: ConnectionAction):
        return service.account_action(account_id, "probe", req)

    @router.post("/accounts/{account_id}/disconnect")
    def disconnect(account_id: str, req: ConnectionAction):
        return service.disconnect_account(account_id, req.confirmed)

    @router.post("/wechat/connect", status_code=201)
    def wechat_connect(req: WeChatConnectInput):
        return service.wechat.connect_new(req)

    @router.put("/wechat/accounts/{account_id}/connect")
    def wechat_reconnect(account_id: str, req: WeChatReconnectInput):
        return service.wechat.reconnect(account_id, req)

    @router.get("/x/config")
    def x_config():
        return service.x.status()

    @router.put("/x/config")
    def x_configure(req: XConfigInput):
        return service.x.save(req)

    @router.post("/x/connect", status_code=201)
    def x_connect(req: XConnectInput):
        return service.x.connect_new(req)

    @router.post("/x/accounts/{account_id}/connect")
    def x_reconnect(account_id: str, req: ConnectionAction):
        return service.x.reconnect(account_id, req.confirmed)

    @router.get("/x/oauth/callback", response_class=HTMLResponse)
    def x_oauth_callback(state: str = "", code: str = "", error: str = ""):
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                   "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'"}
        if error:
            return HTMLResponse("<main style='font-family:system-ui;padding:40px'><h1>X 授权未完成</h1><p>请关闭此页并返回 Ripple 重新连接。</p></main>", status_code=400, headers=headers)
        try:
            account = service.x.callback(state, code)
        except Exception:
            # Never reflect the authorization code/state or transport detail into HTML.
            return HTMLResponse("<main style='font-family:system-ui;padding:40px'><h1>X 授权未完成</h1><p>授权已过期或无法验证。请关闭此页并返回 Ripple 重新连接。</p></main>", status_code=400, headers=headers)
        label = str(account.get("label") or "X 账号").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return HTMLResponse(f"<main style='font-family:system-ui;padding:40px'><h1>X 已连接</h1><p>{label} 已授权给 Ripple。可以关闭此页并返回 Ripple。</p></main>", headers=headers)

    @router.post("/accounts/{account_id}/sms")
    def sms(account_id: str, req: SmsInput):
        return service.accounts.sms(account_id, req)

    @router.get("/accounts/{account_id}/qr/{operation_id}")
    def qr(account_id: str, operation_id: str):
        a = service.accounts.get(account_id)
        op = a.get("operation") or {}
        if not re.fullmatch(r"[a-f0-9]{32}", operation_id) or op.get("id") != operation_id or op.get("kind") != "login" or op.get("state") != "running":
            raise HTTPException(404, "二维码已过期，请重新登录。")
        path = service.accounts.directory(account_id) / "operations" / operation_id / "qr.png"
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            raise HTTPException(404, "二维码尚未就绪。")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "no-store"})

    @router.get("/interactions/capabilities")
    def interaction_capabilities():
        return service.interactions.capabilities()

    @router.get("/interactions/sources")
    def interaction_sources(platform: str = Query("", max_length=40), limit: int = Query(24, ge=1, le=24)):
        return service.interactions.sources(platform=platform, limit=limit)

    @router.get("/interactions/sources/{source_id}")
    def interaction_source(source_id: str):
        return service.interactions.get_source(source_id)

    @router.get("/interactions/sources/{source_id}/analysis")
    def interaction_source_analysis(source_id: str):
        return service.interactions.analyze_source(source_id)

    @router.get("/interactions/accounts/{account_id}/contents")
    def interaction_remote_contents(account_id: str, limit: int = Query(30, ge=1, le=30)):
        return service.interactions.remote_contents(account_id, limit)

    @router.post("/interactions/accounts/{account_id}/comments")
    def interaction_remote_comments(account_id: str, req: InteractionRemoteCommentsInput):
        return service.interactions.remote_comments(
            account_id=account_id, target_id=req.target_id, target_url=req.target_url,
            target_label=req.target_label, limit=req.limit,
        )

    @router.get("/interactions")
    def interactions(platform: str = Query("", max_length=40), account_id: str = Query("", max_length=32),
                     status: str = Query("", max_length=30), limit: int = Query(100, ge=1, le=200)):
        return service.interactions.list(platform=platform, account_id=account_id, status=status, limit=limit)

    @router.post("/interactions", status_code=201)
    def interaction_draft(req: InteractionDraftInput):
        return service.interactions.create_draft(
            platform=req.platform, account_id=req.account_id, source_id=req.source_id,
            target_id=req.target_id, target_url=req.target_url, kind=req.kind,
            items=[item.model_dump() for item in req.items], text=req.text,
            idempotency_key=req.idempotency_key,
        )

    @router.patch("/interactions/{interaction_id}")
    def interaction_update(interaction_id: str, req: InteractionRevisionInput):
        return service.interactions.update_draft(
            interaction_id, expected_updated_at=req.expected_updated_at,
            items=[item.model_dump() for item in req.items], text=req.text,
        )

    @router.post("/interactions/{interaction_id}/cancel")
    def interaction_cancel(interaction_id: str, req: InteractionVersionAction):
        return service.interactions.cancel_draft(interaction_id, expected_updated_at=req.expected_updated_at)

    @router.post("/interactions/{interaction_id}/execute")
    def interaction_execute(interaction_id: str, req: InteractionExecuteInput):
        return service.interactions.execute(interaction_id, req.confirmed)

    @router.post("/interactions/{interaction_id}/refresh-result")
    def interaction_refresh(interaction_id: str):
        return service.interactions.refresh_result(interaction_id)

    @router.post("/interactions/{interaction_id}/resolve-unknown")
    def interaction_resolve_unknown(interaction_id: str, req: InteractionResolveInput):
        return service.interactions.resolve_unknown(
            interaction_id, result=req.result, confirmed=req.confirmed, note=req.note,
        )

    @router.post("/interactions/{interaction_id}/verify-platform")
    def interaction_verify(interaction_id: str):
        return service.interactions.verify_platform(interaction_id)

    @router.get("/xiaohongshu/accounts/{account_id}/feed")
    def xhs_feed(account_id: str, limit: int = Query(12, ge=1, le=30)):
        return service.xhs_ops.feed(account_id, limit)

    @router.get("/xiaohongshu/accounts/{account_id}/search")
    def xhs_search(account_id: str, q: str = Query(min_length=1, max_length=100), limit: int = Query(12, ge=1, le=30)):
        return service.xhs_ops.search(account_id, q, limit)

    @router.get("/xiaohongshu/accounts/{account_id}/notes")
    def xhs_notes(account_id: str, limit: int = Query(20, ge=1, le=30)):
        return service.xhs_ops.notes(account_id, limit)

    @router.post("/xiaohongshu/accounts/{account_id}/note")
    def xhs_note(account_id: str, req: XhsLocatorInput):
        return service.xhs_ops.note(account_id, note_id=req.note_id, url=req.url)

    @router.post("/xiaohongshu/accounts/{account_id}/comments")
    def xhs_comments(account_id: str, req: XhsCommentsInput):
        return service.xhs_ops.comments(account_id, note_id=req.note_id, url=req.url, limit=req.limit)

    @router.get("/xiaohongshu/accounts/{account_id}/snapshots")
    def xhs_snapshots(account_id: str, kind: str = Query("", max_length=30), limit: int = Query(30, ge=1, le=100)):
        return service.xhs_ops.snapshots(account_id, kind=kind, limit=limit)

    @router.get("/xiaohongshu/interactions")
    def xhs_interactions(account_id: str = Query("", max_length=32), limit: int = Query(100, ge=1, le=200)):
        return service.xhs_ops.interactions(account_id, limit)

    @router.post("/xiaohongshu/interactions", status_code=201)
    def xhs_interaction_draft(req: XhsInteractionDraftInput):
        return service.xhs_ops.draft_interaction(
            account_id=req.account_id, note_id=req.note_id, url=req.url, kind=req.kind,
            items=[item.model_dump() for item in req.items], text=req.text, idempotency_key=req.idempotency_key,
        )

    @router.post("/xiaohongshu/interactions/{interaction_id}/execute")
    def xhs_interaction_execute(interaction_id: str, req: XhsInteractionExecuteInput):
        return service.xhs_ops.execute_interaction(interaction_id, req.confirmed)

    @router.post("/xiaohongshu/interactions/{interaction_id}/query")
    def xhs_interaction_query(interaction_id: str):
        return service.xhs_ops.query_interaction(interaction_id)

    # Legacy compatibility routes retained for existing local configurations.
    # Ripple's primary UI does not expose the upstream product identity or use these as defaults.
    @router.get("/integrations/aitoearn")
    def bridge_status():
        return service.bridge.status()

    @router.put("/integrations/aitoearn")
    def bridge_configure(req: BridgeInput):
        return service.bridge.save(req)

    @router.post("/integrations/aitoearn/probe")
    def bridge_probe(req: ConnectionAction):
        return service.bridge.probe(req.confirmed)

    @router.get('/integrations/aitoearn-rest')
    def rest_status():
        return service.rest.status()

    @router.put('/integrations/aitoearn-rest')
    def rest_configure(req: RestConfig):
        return service.rest.save(req)

    @router.post('/integrations/aitoearn-rest/discover')
    def rest_discover(req: ConnectionAction):
        return service.rest.discover(req.confirmed)

    @router.post('/integrations/aitoearn-rest/import')
    def rest_import(req: RemoteImport):
        return service.rest.import_accounts(req)

    @router.get('/blog/connectors')
    def blog_connectors():
        return {"items": service.blogs.list()}

    @router.post('/blog/connectors', status_code=201)
    def create_blog_connector(req: BlogConnectorInput):
        return service.blogs.save(req)

    @router.put('/blog/connectors/{connector_id}')
    def update_blog_connector(connector_id: str, req: BlogConnectorInput):
        return service.blogs.save(req, connector_id)

    @router.post('/blog/connectors/{connector_id}/probe')
    def probe_blog_connector(connector_id: str):
        return service.blogs.probe(connector_id)

    @router.delete('/blog/connectors/{connector_id}')
    def delete_blog_connector(connector_id: str, req: BlogConnectorDelete):
        return service.blogs.delete(connector_id, req.confirmed)

    @router.post('/contents/{source_id}/variants', status_code=201)
    def variants(source_id: str, req: BatchInput):
        return derive_batch(service, source_id, req)

    @router.get('/contents/{source_id}/variants')
    def list_variants(source_id: str):
        service.library.get(source_id)
        return {"items": service.variants.list_for_source(source_id)}

    @router.get('/variants/{variant_id}')
    def get_variant(variant_id: str):
        return service.variants.get(variant_id)

    @router.put('/variants/{variant_id}')
    def revise_variant(variant_id: str, req: VariantRevision):
        return service.variants.revise(variant_id, req)

    @router.delete('/variants/{variant_id}')
    def delete_variant(variant_id: str, req: VersionAction):
        return service.variants.delete(variant_id, req.expected_version)

    @router.get('/variants/{variant_id}/tasks')
    def list_variant_tasks(variant_id: str):
        return {"items": service.variants.tasks(variant_id)}

    @router.post('/variants/{variant_id}/tasks', status_code=201)
    def create_variant_task(variant_id: str, req: VariantTaskCreate):
        return service.variants.create_task(variant_id, req)

    @router.get("/contents")
    def contents():
        return service.library.list()

    @router.post("/contents", status_code=201)
    def create_content(req: MotherCreate):
        return service.library.create(req)

    @router.get("/contents/{source_id}")
    def content(source_id: str):
        return service.library.get(source_id)

    @router.put("/contents/{source_id}")
    def revise_content(source_id: str, req: MotherRevision):
        return service.library.revise(source_id, req)

    @router.post("/media", status_code=201)
    async def upload(file: UploadFile = File(...)):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in EXTENSIONS:
            raise HTTPException(422, "请选择 PNG/JPEG/WebP/GIF 或 MP4/MOV/WebM 文件。")
        maximum = MAX_VIDEO_BYTES if suffix in {".mp4", ".mov", ".webm"} else MAX_IMAGE_BYTES
        relative = "ripple-media/" + uuid.uuid4().hex + suffix
        target = service.outputs / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        total = 0
        try:
            with target.open("xb") as out:
                while chunk := await file.read(1024 * 1024):
                    total += len(chunk)
                    if total > maximum:
                        raise HTTPException(413, "图片最大 20 MiB，视频最大 512 MiB。")
                    out.write(chunk)
            info = await asyncio.to_thread(service.media_info, relative)
            return {**info, "name": Path(file.filename or "media").name[:160]}
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            await file.close()


    @router.post("/media/watermark/inspect")
    def inspect_watermark(req: WatermarkPathInput):
        return service.inspect_watermark(req.path)

    @router.post("/media/watermark/clean", status_code=201)
    def clean_watermark(req: WatermarkCleanPathInput):
        if not req.confirmed:
            raise WorkflowError("请确认深度清理会重建整张图片像素；原图会保留。", 422)
        return service.clean_watermark(req.path)

    @router.get("/calendar")
    def calendar():
        return service.calendar()

    @router.get("/tasks")
    def tasks(limit: int = Query(200, ge=1, le=200), offset: int = Query(0, ge=0, le=500)):
        return service.list(limit, offset)

    @router.post("/tasks", status_code=201)
    def create(req: CreateInput):
        return service.create(req)

    @router.get("/tasks/{task_id}")
    def task(task_id: str):
        return service.get(task_id)

    @router.put("/tasks/{task_id}")
    def revise(task_id: str, req: RevisionInput):
        return service.revise(task_id, req)

    @router.post("/tasks/{task_id}/preflight")
    def preflight(task_id: str, req: VersionAction):
        return service.preflight(task_id, req.expected_version)


    @router.post("/tasks/{task_id}/watermark-clean")
    def clean_task_watermarks(task_id: str, req: WatermarkTaskInput):
        return service.clean_task_watermarks(task_id, req.expected_version, req.confirmed)

    @router.post("/tasks/{task_id}/approve")
    def approve(task_id: str, req: ApprovalInput):
        return service.approve(task_id, req)

    @router.post("/tasks/{task_id}/dispatch")
    def dispatch(task_id: str, req: VersionAction):
        return service.dispatch(task_id, req.expected_version)

    @router.post("/tasks/{task_id}/query")
    def query(task_id: str, req: VersionAction):
        return service.query(task_id, req.expected_version)

    @router.post("/tasks/{task_id}/receipt")
    def receipt(task_id: str, req: ReceiptInput):
        return service.confirm_receipt(task_id, req)

    @router.post("/tasks/{task_id}/cancel")
    def cancel(task_id: str, req: VersionAction):
        return service.cancel(task_id, req.expected_version)

    @router.get("/tasks/{task_id}/export")
    def download(task_id: str):
        t = service.get(task_id)
        if t["status"] != "exported" or t["content"]["mode"] != "blog":
            raise HTTPException(409, "该任务尚未完成 Blog 导出。")
        path = service.export_path(t)
        if not path.is_file():
            raise HTTPException(404, "导出文件不存在。")
        return FileResponse(path, media_type="application/zip", filename="ripple-blog-" + task_id[:8] + ".zip")

    @app.exception_handler(WorkflowError)
    async def workflow_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(StoreError)
    async def store_error(_request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, exc):
        # Never echo a submitted SMS code/token in validation error input/context.
        return JSONResponse({"detail": [{"loc": list(e["loc"]), "msg": "输入格式或必填字段不符合要求", "type": e["type"]} for e in exc.errors()]}, status_code=422)

    app.include_router(router)
    security = server_security_config()
    trusted_hosts = security["trusted_hosts"] or ["localhost", "127.0.0.1", "[::1]"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)
    cors_origins = ["http://127.0.0.1:5173", "http://localhost:5173"]
    if security["public_origin"]:
        cors_origins.append(security["public_origin"])
    app.add_middleware(CORSMiddleware, allow_origins=list(dict.fromkeys(cors_origins)), allow_credentials=True,
                       allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Content-Type", "X-Ripple-CSRF"])

    def _requires_admin(path: str, method: str) -> bool:
        if method in {"GET", "HEAD", "OPTIONS"}:
            return False
        control_prefixes = (
            "/api/env",
            "/api/model-config",
            "/api/media-model-config",
            "/api/agent/profiles",
            "/api/ripple/environment",
            "/api/ripple/integrations",
            "/api/ripple/blog/connectors",
            "/api/ripple/x/config",
            "/api/ripple/x/connect",
            "/api/ripple/x/accounts",
            "/api/ripple/wechat",
            "/api/ripple/accounts",
            "/api/ripple/execution-nodes/pairing",
            "/api/ripple/media/connections",
        )
        return any(path == prefix or path.startswith(prefix + "/") for prefix in control_prefixes)

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        same_origin = str(request.base_url).rstrip("/")
        allowed = {same_origin, "http://localhost:5173", "http://127.0.0.1:5173"}
        if security["public_origin"]:
            allowed.add(security["public_origin"])
        path = request.url.path
        x_callback = request.method == "GET" and path == "/api/ripple/x/oauth/callback"
        public_navigation = request.method == "GET" and not path.startswith("/api/")
        cross_site = (origin and origin not in allowed) or request.headers.get("sec-fetch-site") == "cross-site"
        if not (x_callback or public_navigation) and cross_site:
            return JSONResponse({"detail": "Ripple 仅接受可信来源请求。"}, status_code=403)
        if service.auth.mode == "server" and path.startswith("/api/"):
            public_auth = path in {"/api/ripple/auth/status", "/api/ripple/auth/setup", "/api/ripple/auth/login"}
            node_auth = (path == "/api/ripple/execution-nodes/register"
                         or re.fullmatch(r"/api/ripple/execution-nodes/[a-f0-9]{32}/heartbeat", path)
                         or re.fullmatch(r"/api/ripple/execution-nodes/[a-f0-9]{32}/commands/claim", path)
                         or re.fullmatch(r"/api/ripple/execution-nodes/[a-f0-9]{32}/commands/[a-f0-9]{32}/complete", path))
            if not public_auth and not node_auth:
                session_token = request.cookies.get(SESSION_COOKIE, "")
                user = service.auth.authenticate(session_token)
                if not user:
                    return JSONResponse({"detail": "请先登录 Ripple。"}, status_code=401)
                request.state.ripple_user = user
                if _requires_admin(path, request.method) and user.get("role") not in {"owner", "admin"}:
                    return JSONResponse({"detail": "当前用户没有管理 Ripple 配置或账号连接的权限。"}, status_code=403)
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    csrf = request.headers.get("x-ripple-csrf", "")
                    csrf_cookie = request.cookies.get(CSRF_COOKIE, "")
                    if not csrf or not hmac.compare_digest(csrf, csrf_cookie) or not service.auth.csrf_valid(session_token, csrf):
                        return JSONResponse({"detail": "安全校验已失效，请刷新后重试。"}, status_code=403)
        if path.startswith(("/api/accounts", "/api/analytics", "/api/login", "/api/logout")):
            return JSONResponse({"detail": "请使用 Ripple 的独立账号入口。"}, status_code=503)
        if request.method == "POST" and path.startswith("/api/publish/"):
            return JSONResponse({"detail": "请通过 Ripple 的版本审核后执行发布。"}, status_code=403)
        if request.method == "POST" and path == "/api/skill" and os.environ.get("RIPPLE_ENABLE_AI") != "1":
            return JSONResponse({"detail": "旧技能执行入口未启用。Ripple Agent 对话请使用受控工具。"}, status_code=503)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        if service.auth.mode == "server" and security.get("secure_cookie"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Referrer-Policy"] = "no-referrer" if x_callback else "same-origin"
        if path.startswith("/api/ripple"):
            response.headers["Cache-Control"] = "no-store"
        return response

    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(instance):
        async with previous_lifespan(instance):
            await asyncio.to_thread(service.recover)
            stop = asyncio.Event()

            async def scheduler():
                while not stop.is_set():
                    try:
                        await asyncio.to_thread(service.tick)
                    except Exception:
                        logger.error("Ripple scheduler paused this tick; inspect local state integrity.")
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=2)
                    except asyncio.TimeoutError:
                        pass

            worker = asyncio.create_task(scheduler())
            try:
                yield
            finally:
                stop.set()
                await worker
                runtime = getattr(instance.state, "agent_runtime", None) or getattr(instance.state, "opencode", None)
                if runtime is not None:
                    await asyncio.to_thread(runtime.close)
                await asyncio.to_thread(service.close)

    app.router.lifespan_context = lifespan
    return service
