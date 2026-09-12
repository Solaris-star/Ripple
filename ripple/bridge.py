"""Legacy-compatible Streamable HTTP MCP connection and read-only capability discovery.

Contract inspected at upstream 9413d73918271bd716b9ea59f61c9f59e485619b.
No remote publishing tools are exposed until media/option/account contracts have
been integrated end-to-end. An MCP connection is not a social account binding.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid

from filelock import FileLock
import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr
import yaml

from .publishing import WorkflowError
from .secrets import protect

PINNED_SHA = "9413d73918271bd716b9ea59f61c9f59e485619b"
READ_TOOLS = {"listChannelPlatforms", "getChannelPlatform", "listChannelPublishRecords", "getChannelPublishRecordByTaskId", "getChannelWorkDetail"}


class BridgeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    endpoint: str = Field(min_length=8, max_length=2048)
    api_key: SecretStr = Field(default_factory=lambda: SecretStr(""), max_length=4096)
    confirmed: bool = False


def endpoint_allowed(endpoint: str) -> str:
    try:
        p = urlsplit(endpoint)
        if p.scheme not in {"https", "http"} or not p.hostname or p.username or p.password or p.query or p.fragment:
            raise ValueError("invalid")
        if p.scheme == "http" and p.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("http allowed only on loopback")
        if not p.path or p.path == "/" or p.port == 0:
            raise ValueError("full endpoint required")
    except ValueError:
        raise WorkflowError("请填写完整的 HTTPS MCP 端点；本机地址允许 HTTP，不允许查询参数或内嵌凭据。", 422) from None
    return endpoint


class McpClient:
    def __init__(self, endpoint, key, *, transport=None):
        self.endpoint = endpoint_allowed(endpoint)
        self.headers = {"x-api-key": key, "Accept": "application/json, text/event-stream"}
        self.http = httpx.Client(timeout=httpx.Timeout(20, connect=8), follow_redirects=False, trust_env=False, transport=transport)

    def close(self):
        self.http.close()

    def request(self, method, params=None, notification=False):
        identifier = uuid.uuid4().hex
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        if not notification:
            payload["id"] = identifier
        try:
            with self.http.stream("POST", self.endpoint, json=payload, headers=self.headers) as response:
                if response.status_code in {401, 403}:
                    raise WorkflowError("兼容 MCP 服务拒绝认证，请检查 API Key 与权限。", 422)
                if not 200 <= response.status_code < 300:
                    raise WorkflowError(f"兼容 MCP 服务返回 HTTP {response.status_code}；请检查完整端点。", 422)
                if response.headers.get("mcp-session-id"):
                    self.headers["Mcp-Session-Id"] = response.headers["mcp-session-id"][:200]
                if notification:
                    return None
                content_type = response.headers.get("content-type", "")
                buffer = bytearray()
                total = 0
                deadline = time.monotonic() + 30
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if time.monotonic() > deadline:
                        raise WorkflowError("MCP 响应超过等待时限。", 422)
                    buffer.extend(chunk)
                    if total > 1024 * 1024:
                        raise WorkflowError("MCP 响应超过 1 MiB 安全上限。", 422)
                    if "text/event-stream" in content_type:
                        normalized = bytes(buffer).replace(b"\r\n", b"\n")
                        while b"\n\n" in normalized:
                            block, normalized = normalized.split(b"\n\n", 1)
                            data = b"\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith(b"data:"))
                            if not data:
                                continue
                            item = json.loads(data)
                            if item.get("id") == identifier:
                                return self._result(item)
                        buffer = bytearray(normalized)
                if "text/event-stream" not in content_type:
                    item = json.loads(buffer)
                    if item.get("id") == identifier:
                        return self._result(item)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise WorkflowError("无法完成 MCP 连接或响应解析；未执行任何发布操作。", 422) from None
        raise WorkflowError("MCP 未返回与本次请求对应的结果。", 422)

    @staticmethod
    def _result(item):
        if item.get("error"):
            raise WorkflowError("MCP 服务返回调用错误；未执行发布。", 422)
        return item.get("result")

    def inspect(self):
        init = self.request("initialize", {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "Ripple", "version": "0.2.0"}})
        protocol = str((init or {}).get("protocolVersion", ""))
        if protocol not in {"2024-11-05", "2025-03-26", "2025-06-18"}:
            raise WorkflowError("MCP 协议版本未经过本地适配，请更新连接器。", 422)
        self.headers["MCP-Protocol-Version"] = protocol
        self.request("notifications/initialized", notification=True)
        names, cursor, cursors = [], None, set()
        for _ in range(5):
            result = self.request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            for tool in result.get("tools", [])[:200]:
                name = tool.get("name", "")
                if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", name):
                    names.append(name)
            cursor = result.get("nextCursor")
            if not cursor:
                break
            if cursor in cursors:
                raise WorkflowError("MCP 工具分页游标重复。", 422)
            cursors.add(cursor)
        platforms = []
        if "listChannelPlatforms" in names:
            result = self.request("tools/call", {"name": "listChannelPlatforms", "arguments": {}}) or {}
            if result.get("isError"):
                raise WorkflowError("渠道元数据读取失败；服务可能缺少对应权限。", 422)
            for block in result.get("content", [])[:5]:
                text = block.get("text", "") if block.get("type") == "text" else ""
                if len(text) > 128000 or re.search(r"(^|\s)[&*][A-Za-z0-9_-]+", text):
                    continue
                try:
                    parsed = yaml.safe_load(text)
                    if isinstance(parsed, list):
                        for p in parsed[:30]:
                            if isinstance(p, dict):
                                platform = str(p.get("platform", ""))[:80]
                                if platform:
                                    platforms.append({"platform": platform, "name": str(p.get("name", p.get("displayName", platform)))[:80]})
                except yaml.YAMLError:
                    continue
        return {"protocol": protocol, "tool_count": len(set(names)), "readonly_tools": sorted(set(names) & READ_TOOLS),
                "remote_publish_tool_advertised": "createChannelPublishFlow" in names, "platforms": platforms,
                "publishing_enabled": False, "server_name": str((init or {}).get("serverInfo", {}).get("name", ""))[:100]}


class BridgeService:
    def __init__(self, private: Path):
        self.path = private / "integrations" / "aitoearn.json"

    def _read(self):
        if not self.path.exists():
            return {}
        if self.path.stat().st_size > 128000:
            raise WorkflowError("桥接配置异常，已停止读取。", 503)
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except ValueError:
            raise WorkflowError("桥接配置损坏，已停止读取。", 503) from None

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def status(self):
        data = self._read()
        return {"configured": bool(data.get("protected_key")), "endpoint": data.get("endpoint", ""),
            "checked_at": data.get("checked_at"), "connection": data.get("connection", "not_configured"),
            "inspection": data.get("inspection"), "upstream_sha": PINNED_SHA, "publishing_enabled": False,
            "key_storage": "Windows DPAPI" if os.name == "nt" else "AES-256-GCM machine-local key",
            "server_relay": "managed_by_upstream_not_verified", "ai_relay": "not_configured"}

    def save(self, req: BridgeInput):
        if not req.confirmed:
            raise WorkflowError("请确认连接地址和密钥的使用范围。", 422)
        endpoint = endpoint_allowed(req.endpoint)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with FileLock(str(self.path) + ".lock", timeout=5):
            old = self._read()
            key = req.api_key.get_secret_value().strip()
            if key:
                encrypted = base64.b64encode(protect(key.encode("utf-8"))).decode("ascii")
            elif old.get("endpoint") == endpoint and old.get("protected_key"):
                encrypted = old["protected_key"]
            else:
                raise WorkflowError("新连接需要填写 API Key。", 422)
            self._write({"endpoint": endpoint, "protected_key": encrypted, "revision": uuid.uuid4().hex, "connection": "configured"})
        return self.status()

    def probe(self, confirmed: bool):
        if not confirmed:
            raise WorkflowError("请确认向所配置的 MCP 服务发送连接测试。", 422)
        data = self._read()
        if not data.get("protected_key"):
            raise WorkflowError("请先配置旧版兼容 MCP 端点与 API Key。", 422)
        key = protect(base64.b64decode(data["protected_key"]), decrypt=True).decode("utf-8")
        client = McpClient(data["endpoint"], key)
        try:
            inspection = client.inspect()
        finally:
            client.close()
        with FileLock(str(self.path) + ".lock", timeout=5):
            current = self._read()
            if current.get("revision") != data.get("revision"):
                raise WorkflowError("连接配置已变化，请重新测试。")
            current.update(inspection=inspection, checked_at=datetime.now(timezone.utc).isoformat(), connection="connected_readonly")
            self._write(current)
        return self.status()
