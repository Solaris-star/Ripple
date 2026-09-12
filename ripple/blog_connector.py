"""Brand-neutral Blog connector registry and OpenAPI capability adapter.

Ripple never assumes a CMS product name or fixed endpoint path. A Blog target is
connected by a full OpenAPI URL. Compatible servers advertise semantic
operations using the `x-blog-capability` OpenAPI extension.
"""
from __future__ import annotations

import base64
from copy import deepcopy
import re
from urllib.parse import quote, urljoin, urlsplit
import uuid

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from .secrets import protect
from .publishing import WorkflowError
from .rest_bridge import atomic_write, read_json

PROTOCOL_VERSION = "1.0"
REQUIRED_ARTICLE_CAPABILITIES = {"article.create", "article.read", "article.update", "article.publish"}
OPTIONAL_CAPABILITIES = {
    "article.list", "article.unpublish", "article.archive", "article.versions", "article.restore",
    "thought.list", "thought.create", "thought.read", "thought.update", "thought.publish",
    "thought.unpublish", "thought.archive", "thought.versions", "thought.restore", "media.upload",
}
ALL_CAPABILITIES = REQUIRED_ARTICLE_CAPABILITIES | OPTIONAL_CAPABILITIES


def openapi_url_allowed(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError()
        if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError()
        if parsed.path in {"", "/"}:
            raise ValueError()
    except ValueError:
        raise WorkflowError("请填写完整的 Blog OpenAPI URL；公网连接必须使用 HTTPS，本机允许 HTTP。", 422) from None
    return value


class BlogConnectorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=80)
    openapi_url: str = Field(min_length=8, max_length=2048)
    token: SecretStr = Field(default_factory=lambda: SecretStr(""), max_length=4096)
    confirmed: bool = False


class BlogConnectorDelete(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmed: bool = False


class OpenApiBlogClient:
    def __init__(self, openapi_url: str, token: str, *, transport=None):
        self.openapi_url = openapi_url_allowed(openapi_url)
        self.token = token
        self.http = httpx.Client(timeout=httpx.Timeout(30, connect=8), follow_redirects=False, trust_env=False, transport=transport)
        self.spec: dict = {}
        self.server = ""
        self.operations: dict[str, dict] = {}
        self.marker: dict = {}

    def close(self):
        self.http.close()

    def discover(self) -> dict:
        headers = {"Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.http.get(self.openapi_url, headers=headers)
        except httpx.HTTPError:
            raise WorkflowError("无法连接 Blog OpenAPI。", 503) from None
        if response.status_code != 200 or len(response.content) > 2 * 1024 * 1024:
            raise WorkflowError("Blog OpenAPI 不可读取。", 502)
        try:
            spec = response.json()
        except ValueError:
            raise WorkflowError("Blog OpenAPI 不是有效 JSON。", 502) from None
        marker = spec.get("x-blog-connector") if isinstance(spec, dict) else None
        if not isinstance(marker, dict) or str(marker.get("version") or "") != PROTOCOL_VERSION:
            raise WorkflowError("该 OpenAPI 尚未声明兼容的 Blog Connector 协议。", 422)
        servers = spec.get("servers") or []
        if not servers or not isinstance(servers[0], dict) or not servers[0].get("url"):
            raise WorkflowError("Blog OpenAPI 缺少 server URL。", 422)
        server = str(servers[0]["url"]).rstrip("/")
        openapi_url_allowed(server + "/health")
        spec_origin = urlsplit(self.openapi_url)
        server_origin = urlsplit(server)
        if (server_origin.scheme, server_origin.hostname, server_origin.port) != (spec_origin.scheme, spec_origin.hostname, spec_origin.port):
            raise WorkflowError("Blog OpenAPI 的 server 必须与文档 URL 同源，避免把 Agent Token 转发到其他站点。", 422)
        operations: dict[str, dict] = {}
        for path, methods in (spec.get("paths") or {}).items():
            if not isinstance(methods, dict) or not isinstance(path, str) or not path.startswith("/"):
                continue
            for method, operation in methods.items():
                if method.lower() not in {"get", "post", "put", "patch", "delete"} or not isinstance(operation, dict):
                    continue
                capability = operation.get("x-blog-capability")
                if capability in ALL_CAPABILITIES:
                    if capability in operations:
                        raise WorkflowError(f"Blog OpenAPI 重复声明能力：{capability}", 422)
                    operations[capability] = {"method": method.upper(), "path": path}
        missing = sorted(REQUIRED_ARTICLE_CAPABILITIES - set(operations))
        if missing:
            raise WorkflowError("Blog OpenAPI 缺少文章能力：" + "、".join(missing), 422)
        templates = {}
        for kind, key in (("article", "articlePublicUrlTemplate"), ("thought", "thoughtPublicUrlTemplate")):
            value = marker.get(key)
            if isinstance(value, str) and value.strip() and len(value) <= 2048:
                templates[kind] = value.strip()
        self.spec, self.server, self.operations, self.marker = spec, server, operations, marker
        return {"protocol": PROTOCOL_VERSION, "capabilities": sorted(operations), "content_types": marker.get("contentTypes") or ["article"],
                "server_url": server, "public_url_templates": templates}

    def _operation(self, capability: str, **params) -> tuple[str, str]:
        if not self.operations:
            self.discover()
        operation = self.operations.get(capability)
        if not operation:
            raise WorkflowError(f"此 Blog 未提供能力：{capability}", 422)
        path = operation["path"]
        for key, value in params.items():
            path = path.replace("{" + key + "}", quote(str(value), safe=""))
        if "{" in path:
            raise WorkflowError("Blog OpenAPI 路径参数不完整。", 422)
        return operation["method"], self.server + path

    def request(self, capability: str, *, params: dict | None = None, json_body=None, content: bytes | None = None, headers: dict | None = None) -> dict:
        method, url = self._operation(capability, **(params or {}))
        request_headers = {"Accept": "application/json", **(headers or {})}
        if self.token:
            request_headers["Authorization"] = f"Bearer {self.token}"
        try:
            response = self.http.request(method, url, headers=request_headers, json=json_body if content is None else None, content=content)
        except httpx.HTTPError:
            raise WorkflowError("Blog 请求未完成，未确认远端状态。", 503) from None
        try:
            payload = response.json() if response.content else {}
        except ValueError:
            payload = {}
        if response.status_code in {401, 403}:
            raise WorkflowError("Blog Token 无效或缺少所需权限。", response.status_code)
        if response.status_code == 409:
            current = payload.get("currentRevision") if isinstance(payload, dict) else None
            detail = f"（远端 revision {current}）" if current is not None else ""
            raise WorkflowError("Blog 内容已被其他编辑器更新" + detail + "，请先拉取最新版本。", 409)
        if response.status_code >= 400:
            message = payload.get("error") if isinstance(payload, dict) else None
            raise WorkflowError(str(message or "Blog 请求失败。")[:300], response.status_code)
        if not isinstance(payload, dict):
            raise WorkflowError("Blog 返回格式不匹配。", 502)
        return payload


class BlogConnectorService:
    def __init__(self, workspace, *, transport=None):
        self.workspace = workspace
        self.path = workspace.private / "integrations" / "blog-connectors.json"
        self.transport = transport

    def _state(self) -> dict:
        value = read_json(self.path) if self.path.exists() else {}
        items = value.get("items", {})
        if not isinstance(items, dict):
            raise WorkflowError("Blog 连接配置无法读取。", 503)
        return {"items": items}

    def _write(self, state: dict):
        atomic_write(self.path, state)

    @staticmethod
    def _public(item: dict) -> dict:
        return {key: deepcopy(value) for key, value in item.items() if key != "token_protected"}

    def _token(self, item: dict) -> str:
        encoded = item.get("token_protected") or ""
        if not encoded:
            return ""
        try:
            return protect(base64.b64decode(encoded), decrypt=True).decode("utf-8")
        except Exception:
            raise WorkflowError("Blog Token 无法从系统密钥存储读取。", 503) from None

    def _client(self, item: dict) -> OpenApiBlogClient:
        return OpenApiBlogClient(item["openapi_url"], self._token(item), transport=self.transport)

    def list(self) -> list[dict]:
        state = self._state()
        return [self._public(item) for item in state["items"].values()]

    def get(self, connector_id: str, *, private=False) -> dict:
        item = self._state()["items"].get(connector_id)
        if not item:
            raise WorkflowError("Blog 连接不存在。", 404)
        return deepcopy(item) if private else self._public(item)

    def save(self, req: BlogConnectorInput, connector_id: str | None = None) -> dict:
        if not req.confirmed:
            raise WorkflowError("请确认保存此 Blog 连接。", 422)
        openapi_url = openapi_url_allowed(req.openapi_url)
        state = self._state()
        current = state["items"].get(connector_id or "")
        supplied_token = req.token.get_secret_value()
        if current and not supplied_token:
            previous = urlsplit(str(current.get("openapi_url") or ""))
            updated = urlsplit(openapi_url)
            previous_origin = (previous.scheme, previous.hostname, previous.port)
            updated_origin = (updated.scheme, updated.hostname, updated.port)
            if previous_origin != updated_origin:
                raise WorkflowError("Blog OpenAPI 地址已更换域名或端口；为避免把旧 Token 发送到新站点，请重新输入 Agent Token。", 422)
        token = supplied_token or (self._token(current) if current else "")
        if not token:
            raise WorkflowError("请输入 Blog Agent Token。", 422)
        client = OpenApiBlogClient(openapi_url, token, transport=self.transport)
        try:
            info = client.discover()
            if "article.list" in client.operations:
                client.request("article.list")
        finally:
            client.close()
        item_id = connector_id or uuid.uuid4().hex
        item = {
            "id": item_id, "label": req.label, "openapi_url": openapi_url,
            "token_protected": base64.b64encode(protect(token.encode("utf-8"))).decode("ascii"),
            "protocol": info["protocol"], "capabilities": info["capabilities"], "content_types": info["content_types"],
            "server_url": info["server_url"], "public_url_templates": info["public_url_templates"],
            "status": "connected", "updated_at": self.workspace.clock().isoformat(),
        }
        state["items"][item_id] = item
        self._write(state)
        return self._public(item)

    def probe(self, connector_id: str) -> dict:
        item = self.get(connector_id, private=True)
        client = self._client(item)
        try:
            info = client.discover()
            if "article.list" in client.operations:
                client.request("article.list")
        finally:
            client.close()
        item.update(protocol=info["protocol"], capabilities=info["capabilities"], content_types=info["content_types"],
                    server_url=info["server_url"], public_url_templates=info["public_url_templates"],
                    status="connected", updated_at=self.workspace.clock().isoformat())
        state = self._state(); state["items"][connector_id] = item; self._write(state)
        return self._public(item)

    def delete(self, connector_id: str, confirmed: bool) -> dict:
        if not confirmed:
            raise WorkflowError("请确认断开此 Blog 连接。", 422)
        state = self._state()
        if connector_id not in state["items"]:
            raise WorkflowError("Blog 连接不存在。", 404)
        with self.workspace.store.transaction(write=False) as workspace_state:
            bound = [variant for variant in workspace_state.get("variants", {}).values() if variant.get("content", {}).get("target_id") == connector_id]
            if bound:
                raise WorkflowError("仍有平台版本使用此 Blog，请先改为其他目标或 Markdown 导出。")
        del state["items"][connector_id]; self._write(state)
        return {"id": connector_id, "deleted": True}

    @staticmethod
    def _item(payload: dict) -> dict:
        item = payload.get("item")
        if not isinstance(item, dict) or not item.get("id") or not isinstance(item.get("revision"), int):
            raise WorkflowError("Blog 返回的内容记录缺少 id/revision。", 502)
        return item

    def upload_media(self, connector_id: str, rel: str) -> dict:
        item = self.get(connector_id, private=True)
        if "media.upload" not in item.get("capabilities", []):
            raise WorkflowError("此 Blog 未提供媒体上传能力。", 422)
        info = self.workspace.media_info(rel)
        path = self.workspace.safe_media_path(rel)
        suffix = path.suffix.lower()
        kind = "video" if suffix in {".mp4", ".mov", ".webm"} else "image"
        client = self._client(item)
        try:
            client.discover()
            payload = client.request("media.upload", content=path.read_bytes(), headers={"Content-Type": "application/octet-stream", "x-filename": quote(path.name), "x-upload-kind": kind})
        finally:
            client.close()
        uploaded = payload.get("item")
        if not isinstance(uploaded, dict) or not uploaded.get("id") or not uploaded.get("url"):
            raise WorkflowError("Blog 媒体上传返回格式不匹配。", 502)
        return {"local": rel, "sha256": info["sha256"], "remote_id": str(uploaded["id"]), "url": str(uploaded["url"]), "kind": kind}

    @staticmethod
    def _binding_key(connector_id: str, kind: str) -> str:
        return f"{connector_id}:{kind}"

    def _public_url(self, connector: dict, kind: str, remote: dict) -> str:
        direct = remote.get("publicUrl") or remote.get("url")
        if isinstance(direct, str) and direct.startswith(("https://", "http://")):
            return direct
        template = (connector.get("public_url_templates") or {}).get(kind)
        if not isinstance(template, str) or not template:
            return ""
        values = {"id": str(remote.get("id") or ""), "slug": str(remote.get("data", {}).get("slug") or "")}
        rendered = template
        for key, value in values.items():
            rendered = rendered.replace("{" + key + "}", quote(value, safe=""))
        if "{" in rendered or "}" in rendered:
            return ""
        base = str(connector.get("server_url") or connector.get("openapi_url") or "")
        return urljoin(base.rstrip("/") + "/", rendered)

    def _save_binding(self, variant_id: str, connector_id: str, kind: str, remote: dict, *, public_url: str = "") -> dict:
        binding = {"connector_id": connector_id, "kind": kind, "remote_id": str(remote["id"]),
                   "revision": int(remote["revision"]), "slug": str(remote.get("data", {}).get("slug") or ""),
                   "public_url": public_url}
        with self.workspace.store.transaction() as state:
            variant = state.get("variants", {}).get(variant_id)
            if not variant:
                raise WorkflowError("平台版本不存在。", 404)
            variant.setdefault("bindings", {})[self._binding_key(connector_id, kind)] = binding
        return binding

    def _binding(self, variant: dict, connector_id: str, kind: str) -> dict:
        bindings = variant.get("bindings") or {}
        current = bindings.get(self._binding_key(connector_id, kind))
        if isinstance(current, dict):
            return deepcopy(current)
        legacy = bindings.get(connector_id)
        return deepcopy(legacy) if isinstance(legacy, dict) and legacy.get("kind") == kind else {}

    def publish_article(self, task: dict, variant: dict) -> dict:
        connector_id = task["content"]["account_id"]
        config = self.get(connector_id, private=True)
        client = self._client(config)
        try:
            client.discover()
            binding = self._binding(variant, connector_id, "article")
            uploaded = []
            if task["content"].get("media") and "media.upload" in client.operations:
                for rel in task["content"]["media"]:
                    uploaded.append(self.upload_media(connector_id, rel))
            options = variant.get("content", {}).get("options") or {}
            tags = [part.strip() for part in str(task["content"].get("tags") or "").replace("，", ",").split(",") if part.strip()]
            article = {
                "slug": str(options.get("slug") or self._slug(task["content"]["title"])),
                "title": task["content"]["title"], "body": task["content"].get("body", ""),
                "excerpt": str(options.get("excerpt") or ""), "tags": tags,
            }
            if options.get("date"):
                article["date"] = str(options["date"])
            if options.get("cover"):
                article["cover"] = str(options["cover"])
            elif uploaded:
                first_image = next((row for row in uploaded if row["kind"] == "image"), None)
                if first_image:
                    article["cover"] = first_image["url"]
            if binding:
                remote = self._item(client.request("article.read", params={"id": binding["remote_id"]}))
                if int(remote["revision"]) != int(binding.get("revision") or -1):
                    raise WorkflowError("Blog 文章自上次同步后已被修改，请先拉取远端版本。", 409)
                remote = self._item(client.request("article.update", params={"id": binding["remote_id"]}, json_body={"expectedRevision": remote["revision"], "data": article}))
            else:
                remote = self._item(client.request("article.create", json_body=article))
            draft_url = self._public_url(config, "article", remote)
            self._save_binding(task["variant_id"], connector_id, "article", remote, public_url=draft_url)
            published = self._item(client.request("article.publish", params={"id": remote["id"]}, json_body={"expectedRevision": remote["revision"]}))
            public_url = self._public_url(config, "article", published)
            saved = self._save_binding(task["variant_id"], connector_id, "article", published, public_url=public_url)
            return {"state": "published", "message": "Blog 文章已更新并发布。", "adapter": "blog-openapi", "remote_id": published["id"], "remote_revision": published["revision"], "slug": saved["slug"] or article["slug"], "public_url": saved["public_url"], "media": uploaded}
        finally:
            client.close()

    def publish_thought(self, task: dict, variant: dict) -> dict:
        connector_id = task["content"]["account_id"]
        config = self.get(connector_id, private=True)
        client = self._client(config)
        try:
            client.discover()
            required = {"thought.create", "thought.read", "thought.update", "thought.publish"}
            missing = sorted(required - set(client.operations))
            if missing:
                raise WorkflowError("此 Blog 不支持完整想法发布能力：" + "、".join(missing), 422)
            if task["content"].get("media"):
                raise WorkflowError("当前 Blog Connector 的想法类型不接受媒体；请改用文章版本或移除素材。", 422)
            options = variant.get("content", {}).get("options") or {}
            tag = str(options.get("thought_tag") or "")
            if not tag:
                tag = next((part.strip() for part in str(task["content"].get("tags") or "").replace("，", ",").split(",") if part.strip()), "")
            thought = {"text": str(task["content"].get("body") or task["content"].get("title") or "").strip(), "tag": tag}
            if options.get("date"):
                thought["date"] = str(options["date"])
            if not thought["text"]:
                raise WorkflowError("想法正文不能为空。", 422)
            binding = self._binding(variant, connector_id, "thought")
            if binding:
                remote = self._item(client.request("thought.read", params={"id": binding["remote_id"]}))
                if int(remote["revision"]) != int(binding.get("revision") or -1):
                    raise WorkflowError("Blog 想法自上次同步后已被修改，请先拉取远端版本。", 409)
                remote = self._item(client.request("thought.update", params={"id": binding["remote_id"]}, json_body={"expectedRevision": remote["revision"], "data": thought}))
            else:
                remote = self._item(client.request("thought.create", json_body=thought))
            draft_url = self._public_url(config, "thought", remote)
            self._save_binding(task["variant_id"], connector_id, "thought", remote, public_url=draft_url)
            published = self._item(client.request("thought.publish", params={"id": remote["id"]}, json_body={"expectedRevision": remote["revision"]}))
            public_url = self._public_url(config, "thought", published)
            saved = self._save_binding(task["variant_id"], connector_id, "thought", published, public_url=public_url)
            return {"state": "published", "message": "Blog 想法已更新并发布。", "adapter": "blog-openapi", "remote_id": published["id"], "remote_revision": published["revision"], "public_url": saved["public_url"]}
        finally:
            client.close()

    def publish(self, task: dict, variant: dict) -> dict:
        content_type = str((variant.get("content", {}).get("options") or {}).get("content_type") or "article")
        if content_type == "thought":
            return self.publish_thought(task, variant)
        if content_type != "article":
            raise WorkflowError("未知 Blog 内容类型。", 422)
        return self.publish_article(task, variant)

    def query(self, task: dict, variant: dict) -> dict | None:
        connector_id = task["content"]["account_id"]
        kind = str((variant.get("content", {}).get("options") or {}).get("content_type") or "article")
        if kind not in {"article", "thought"}:
            return None
        binding = self._binding(variant, connector_id, kind)
        if not binding:
            return {"state": "verification_required", "not_submitted": True, "message": "Blog 未记录远端内容绑定，可以安全重新创建发布任务。"}
        config = self.get(connector_id, private=True)
        client = self._client(config)
        try:
            client.discover()
            remote = self._item(client.request(f"{kind}.read", params={"id": binding["remote_id"]}))
        finally:
            client.close()
        public_url = self._public_url(config, kind, remote)
        self._save_binding(task["variant_id"], connector_id, kind, remote, public_url=public_url)
        if remote.get("status") == "published":
            return {"state": "published", "message": "已从 Blog 读取到公开状态。", "adapter": "blog-openapi", "remote_id": remote["id"], "remote_revision": remote["revision"], "public_url": public_url}
        return {"state": "verification_required", "not_submitted": True, "message": "Blog 远端记录存在但尚未公开，可以在确认后重新执行发布。", "adapter": "blog-openapi", "remote_id": remote["id"], "remote_revision": remote["revision"]}

    @staticmethod
    def _slug(title: str) -> str:
        ascii_slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]
        return ascii_slug or "post-" + uuid.uuid4().hex[:12]
