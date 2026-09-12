"""Legacy REST v2 compatibility transport. No implicit retry or auth redirects.

Public API reference checked 2026-09-08. An uploaded asset's object-storage URL
never receives the API key. Untrusted server error bodies are not exposed.
"""
from __future__ import annotations

import ipaddress
import json
import mimetypes
from pathlib import Path
import re
import socket
import time
from urllib.parse import urlsplit

import httpx
from .publishing import WorkflowError

ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
PLATFORM_MAP = {"twitter": "x", "wxGzh": "wechat", "tiktok": "tiktok"}


def identifier(value: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise WorkflowError("服务返回无效的资源标识。", 502)
    return value


def api_base(value: str) -> str:
    try:
        p = urlsplit(value.strip())
        if not p.hostname or p.username or p.password or p.query or p.fragment or p.port == 0:
            raise ValueError()
        if p.scheme != "https" and not (p.scheme == "http" and p.hostname in {"127.0.0.1", "localhost", "::1"}):
            raise ValueError()
        if any(part in {".", ".."} for part in p.path.split("/")):
            raise ValueError()
        if p.path.rstrip("/").endswith(("/api", "/mcp", "/api/v2")):
            raise ValueError()
    except (ValueError, TypeError):
        raise WorkflowError("填写受信任的服务根地址，使用 HTTPS（本机可 HTTP）；不要附加 /api、MCP 路径或密钥。", 422) from None
    return value.strip().rstrip("/")


def host_name(value: str) -> str:
    h = value.strip().lower().rstrip(".")
    if len(h) > 253 or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", h) or "." not in h or ".." in h or h.endswith((".local", ".localhost")):
        raise WorkflowError("素材上传域名应为明确的完整主机名，不能使用通配符、路径或端口。", 422)
    try:
        ipaddress.ip_address(h)
    except ValueError:
        return h
    raise WorkflowError("素材上传仅允许明确的域名，不接受 IP 地址。", 422)


def public_media_url(value, allowed_hosts, *, resolver=socket.getaddrinfo):
    try:
        p = urlsplit(value)
        if p.scheme != "https" or p.hostname not in allowed_hosts or p.username or p.password or p.fragment or p.port not in {None, 443}:
            raise ValueError()
        entries = resolver(p.hostname, 443, type=socket.SOCK_STREAM)
        if not entries or any(not ipaddress.ip_address(item[4][0]).is_global for item in entries):
            raise ValueError()
    except (ValueError, TypeError, OSError):
        raise WorkflowError("素材目标不在批准的公网上传域名内，已停止传输。请核对服务的对象存储配置。", 422) from None
    return value


class RestError(WorkflowError):
    def __init__(self, message, *, outcome_unknown=False, status=502):
        super().__init__(message, status)
        self.outcome_unknown = outcome_unknown


class RestClient:
    def __init__(self, base: str, key: str, *, transport=None, resolver=socket.getaddrinfo):
        self.base = api_base(base)
        self.key = key
        self.transport = transport
        self.resolver = resolver
        self.http = httpx.Client(timeout=httpx.Timeout(30, connect=8), trust_env=False,
                                 follow_redirects=False, transport=transport)

    def close(self):
        self.http.close()

    def request(self, method, path, payload=None):
        if not path.startswith("/api/") or ".." in path or "?" in path or "#" in path:
            raise WorkflowError("无效的固定服务接口。", 422)
        try:
            with self.http.stream(method, self.base + path, json=payload if payload is not None else None,
                headers={"X-Api-Key": self.key, "Accept": "application/json"}) as response:
                if not 200 <= response.status_code < 300:
                    # A backend can commit before returning an error; mutating
                    # requests are conservatively ambiguous, never auto-retried.
                    raise RestError(f"服务返回 HTTP {response.status_code}，请检查端点、认证和权限。",
                                    outcome_unknown=method != "GET", status=422 if response.status_code in {401,403} else 502)
                raw = bytearray()
                deadline = time.monotonic() + 45
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES or time.monotonic() > deadline:
                        raise RestError("服务响应超过大小或等待限制。", outcome_unknown=method != "GET")
                data = json.loads(raw)
                if not isinstance(data, dict) or type(data.get("code")) is not int or data["code"] != 0 or "data" not in data:
                    raise RestError("服务业务状态未确认成功，请先核对结果。", outcome_unknown=method != "GET")
                return data["data"]
        except (httpx.HTTPError, ValueError, TypeError):
            raise RestError("服务连接中断或响应格式不匹配；未执行自动重试。", outcome_unknown=method != "GET") from None

    def platforms(self):
        result = self.request("GET", "/api/v2/channels/platforms")
        if not isinstance(result, list) or len(result) > 100:
            raise RestError("平台元数据格式不匹配。")
        return result

    def accounts(self):
        result = self.request("GET", "/api/v2/channels/accounts")
        if not isinstance(result, dict) or not isinstance(result.get("list"), list) or type(result.get("total")) is not int:
            raise RestError("账号列表格式不匹配。")
        if len(result["list"]) > 1000:
            raise RestError("远端账号过多，请在服务端缩小 API Key 的授权范围。")
        return result

    def account(self, remote_id):
        result = self.request("GET", "/api/v2/channels/accounts/" + identifier(remote_id))
        if not isinstance(result, dict) or result.get("id") != remote_id:
            raise RestError("远端账号标识不匹配。")
        auth = self.request("GET", "/api/v2/channels/accounts/" + remote_id + "/auth-status")
        if not isinstance(auth, dict) or type(auth.get("status")) is not int:
            raise RestError("授权状态响应无效。")
        return result, auth["status"]

    def upload(self, file: Path, hosts: list[str]):
        if not hosts:
            raise WorkflowError("尚未批准对象存储上传域名，未上传素材。", 422)
        info = self.request("POST", "/api/assets/uploadSign", {"filename": file.name, "type": "publishMedia", "size": file.stat().st_size})
        if not isinstance(info, dict):
            raise RestError("上传签名格式不匹配。")
        asset_id = identifier(info.get("id"))
        destination = public_media_url(info.get("uploadUrl"), hosts, resolver=self.resolver)
        try:
            # Separate client: no API-key headers, API cookies or environment proxy.
            with httpx.Client(timeout=httpx.Timeout(180, connect=10), follow_redirects=False,
                              trust_env=False, transport=self.transport) as storage, file.open("rb") as stream:
                result = storage.put(destination, content=stream,
                    headers={"Content-Type": mimetypes.guess_type(file.name)[0] or "application/octet-stream",
                             "Content-Length": str(file.stat().st_size)})
                if not 200 <= result.status_code < 300:
                    raise RestError("对象存储上传未成功；未创建发布任务。")
        except httpx.HTTPError:
            raise RestError("素材上传中断；未创建发布任务。") from None
        confirmed = self.request("POST", "/api/assets/" + asset_id + "/confirm")
        if not isinstance(confirmed, dict) or confirmed.get("id") != asset_id or confirmed.get("status") != "confirmed":
            raise RestError("素材服务尚未确认上传，未创建发布任务。")
        # CDN URL may differ from the presigned upload host. It is sent only to the
        # same explicitly authorized publishing service and is never fetched here.
        link = confirmed.get("url", "")
        try:
            p = urlsplit(link)
            valid = p.scheme == "https" and bool(p.hostname) and not p.username and not p.password and not p.fragment
        except ValueError:
            valid = False
        if not valid:
            raise RestError("素材服务没有返回有效的 HTTPS 资源地址。")
        return {"id": asset_id, "url": link}
