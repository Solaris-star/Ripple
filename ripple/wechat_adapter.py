"""Official WeChat Official Account (微信公众号) API adapter.

This adapter uses only documented server-side MP APIs. AppSecret stays encrypted in
Ripple's private account directory. Remote writes are version-bound by the normal
Ripple publish workflow and are never retried implicitly after an ambiguous mutation.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import html
import io
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid

import httpx
import markdown
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from .secrets import protect
from .publishing import WorkflowError, fingerprint
from .tenancy import DEFAULT_WORKSPACE_ID

ADAPTER = "wechat-api"
API_BASE = "https://api.weixin.qq.com"
MAX_RESPONSE = 2 * 1024 * 1024
MAX_BODY_HTML_CHARS = 20_000
MAX_BODY_HTML_BYTES = 1024 * 1024
MAX_BODY_IMAGE_BYTES = 1024 * 1024
MAX_COVER_BYTES = 64 * 1024
APP_ID_RE = re.compile(r"^wx[A-Za-z0-9]{8,64}$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")

_ERROR_MESSAGES = {
    40001: "access_token 无效或已失效，请重新检查公众号凭据。",
    40007: "微信返回素材 media_id 无效，请重新生成平台版本后再试。",
    40013: "AppID 无效，请从微信公众平台复制正确的 AppID。",
    40005: "微信拒绝了素材文件类型，请使用 JPG/PNG 图片。",
    40006: "微信拒绝了素材大小，请压缩图片后重试。",
    40125: "AppSecret 无效，请在微信公众平台核对或重置 AppSecret。",
    40164: "当前 Ripple Server 的出口 IP 未加入公众号 IP 白名单。请在微信公众平台的开发配置中加入该服务器公网出口 IP。",
    42001: "access_token 已过期，Ripple 会在下次检查时重新获取。",
    45009: "公众号 API 今日调用次数已达上限，请稍后再试。",
    48001: "当前公众号没有此 API 权限。请检查公众号类型、认证状态和接口权限。",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _read(path: Path, limit: int = 256_000) -> dict:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


class WeChatConnectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=80)
    app_id: str = Field(min_length=10, max_length=66)
    app_secret: SecretStr
    idempotency_key: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    confirmed: bool = False

    @field_validator("app_id")
    @classmethod
    def valid_app_id(cls, value: str) -> str:
        if not APP_ID_RE.fullmatch(value):
            raise ValueError("invalid app id")
        return value

    @field_validator("app_secret")
    @classmethod
    def valid_secret(cls, value: SecretStr) -> SecretStr:
        if not SECRET_RE.fullmatch(value.get_secret_value()):
            raise ValueError("invalid app secret")
        return value


class WeChatReconnectInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    app_id: str = Field(min_length=10, max_length=66)
    app_secret: SecretStr
    confirmed: bool = False

    @field_validator("app_id")
    @classmethod
    def valid_app_id(cls, value: str) -> str:
        if not APP_ID_RE.fullmatch(value):
            raise ValueError("invalid app id")
        return value

    @field_validator("app_secret")
    @classmethod
    def valid_secret(cls, value: SecretStr) -> SecretStr:
        if not SECRET_RE.fullmatch(value.get_secret_value()):
            raise ValueError("invalid app secret")
        return value


class WeChatApiError(WorkflowError):
    def __init__(self, message: str, *, errcode: int | None = None, status: int = 502, outcome_unknown: bool = False):
        super().__init__(message, status)
        self.errcode = errcode
        self.outcome_unknown = outcome_unknown


class WeChatClient:
    def __init__(self, *, transport=None):
        self.http = httpx.Client(
            base_url=API_BASE,
            timeout=httpx.Timeout(30, connect=8),
            trust_env=False,
            follow_redirects=False,
            transport=transport,
            headers={"Accept": "application/json"},
        )

    def close(self) -> None:
        self.http.close()

    @staticmethod
    def _api_error(value: dict, *, mutation=False) -> WeChatApiError | None:
        raw = value.get("errcode", 0)
        try:
            code = int(raw or 0)
        except (TypeError, ValueError):
            code = -1
        if code == 0:
            return None
        friendly = _ERROR_MESSAGES.get(code)
        raw_message = str(value.get("errmsg") or "").strip().replace("\n", " ")[:160]
        detail = friendly or (f"微信 API 返回错误 {code}" + (f"：{raw_message}" if raw_message else "。"))
        status = 422 if code in {40001, 40005, 40006, 40007, 40013, 40125, 40164, 42001, 45009, 48001} else 502
        return WeChatApiError(f"{detail}（errcode {code}）", errcode=code, status=status, outcome_unknown=False if mutation else False)

    def _decode(self, response: httpx.Response, *, mutation=False, ambiguous_5xx=False) -> dict:
        if not 200 <= response.status_code < 300:
            unknown = bool(mutation and ambiguous_5xx and response.status_code >= 500)
            status = 422 if response.status_code in {400, 401, 403, 404, 409, 422, 429} else 502
            raise WeChatApiError(f"微信 API 返回 HTTP {response.status_code}。请检查公众号接口权限和网络。",
                                 status=status, outcome_unknown=unknown)
        raw = response.content
        if len(raw) > MAX_RESPONSE:
            raise WeChatApiError("微信 API 响应超过 2 MiB 安全上限。", outcome_unknown=mutation)
        try:
            value = response.json()
        except (ValueError, TypeError):
            raise WeChatApiError("微信 API 返回了无法解析的响应。", outcome_unknown=mutation) from None
        if not isinstance(value, dict):
            raise WeChatApiError("微信 API 响应结构不匹配。", outcome_unknown=mutation)
        error = self._api_error(value, mutation=mutation)
        if error:
            raise error
        return value

    def _request(self, method: str, path: str, *, params=None, json_body=None, files=None,
                 mutation=False, ambiguous_5xx=False) -> dict:
        try:
            response = self.http.request(method, path, params=params, json=json_body, files=files)
        except httpx.HTTPError:
            raise WeChatApiError("微信 API 连接中断；Ripple 未自动重试。", outcome_unknown=mutation) from None
        return self._decode(response, mutation=mutation, ambiguous_5xx=ambiguous_5xx)

    def stable_token(self, app_id: str, app_secret: str) -> tuple[str, int]:
        value = self._request("POST", "/cgi-bin/stable_token", json_body={
            "grant_type": "client_credential", "appid": app_id, "secret": app_secret, "force_refresh": False,
        })
        token = str(value.get("access_token") or "")
        if not token or len(token) > 4096:
            raise WeChatApiError("微信没有返回有效 access_token。")
        try:
            expires = max(300, min(int(value.get("expires_in") or 7200), 7200))
        except (TypeError, ValueError):
            expires = 7200
        return token, expires

    def api_domain_ips(self, token: str) -> list[str]:
        value = self._request("GET", "/cgi-bin/get_api_domain_ip", params={"access_token": token})
        values = value.get("ip_list") or []
        return [str(item)[:128] for item in values[:100] if isinstance(item, str)] if isinstance(values, list) else []

    def draft_count(self, token: str) -> int:
        value = self._request("GET", "/cgi-bin/draft/count", params={"access_token": token})
        try:
            return max(0, int(value.get("total_count") or 0))
        except (TypeError, ValueError):
            return 0

    def freepublish_probe(self, token: str) -> None:
        self._request("POST", "/cgi-bin/freepublish/batchget", params={"access_token": token},
                      json_body={"offset": 0, "count": 1, "no_content": 1})

    def add_cover(self, token: str, path: Path) -> str:
        raw = path.read_bytes()
        if not 0 < len(raw) <= MAX_COVER_BYTES or path.suffix.lower() not in {".jpg", ".jpeg"}:
            raise WeChatApiError("公众号封面需转换为不超过 64 KiB 的 JPG。", status=422)
        value = self._request("POST", "/cgi-bin/material/add_material",
                              params={"access_token": token, "type": "thumb"},
                              files={"media": (path.name, raw, "image/jpeg")}, mutation=True)
        media_id = str(value.get("media_id") or "")
        if not media_id or len(media_id) > 256:
            raise WeChatApiError("微信封面素材回执缺少有效 media_id。", outcome_unknown=True)
        return media_id

    def upload_body_image(self, token: str, path: Path) -> str:
        raw = path.read_bytes()
        if not 0 < len(raw) < MAX_BODY_IMAGE_BYTES or path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise WeChatApiError("公众号正文图片需为 JPG/PNG 且小于 1 MiB。", status=422)
        content_type = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
        value = self._request("POST", "/cgi-bin/media/uploadimg", params={"access_token": token},
                              files={"media": (path.name, raw, content_type)}, mutation=True)
        url = str(value.get("url") or "")
        if not url.startswith("https://") or len(url) > 4096:
            raise WeChatApiError("微信正文图片上传回执缺少有效 HTTPS URL。", outcome_unknown=True)
        return url

    def draft_add(self, token: str, article: dict) -> str:
        value = self._request("POST", "/cgi-bin/draft/add", params={"access_token": token},
                              json_body={"articles": [article]}, mutation=True, ambiguous_5xx=True)
        media_id = str(value.get("media_id") or "")
        if not media_id or len(media_id) > 256:
            raise WeChatApiError("微信创建草稿后的回执无法关联，请到公众号后台核对草稿箱。", outcome_unknown=True)
        return media_id

    def draft_get(self, token: str, media_id: str) -> dict:
        value = self._request("POST", "/cgi-bin/draft/get", params={"access_token": token},
                              json_body={"media_id": media_id})
        news = value.get("news_item")
        if not isinstance(news, list) or not news or not all(isinstance(item, dict) for item in news):
            raise WeChatApiError("微信 draft/get 回执缺少有效 news_item，不能确认草稿。")
        return value

    def freepublish_submit(self, token: str, media_id: str) -> str:
        value = self._request("POST", "/cgi-bin/freepublish/submit", params={"access_token": token},
                              json_body={"media_id": media_id}, mutation=True, ambiguous_5xx=True)
        raw_publish_id = value.get("publish_id")
        try:
            publish_number = int(raw_publish_id)
        except (TypeError, ValueError):
            raise WeChatApiError("微信提交发布后的回执缺少有效 publish_id，请到公众号后台核对。", outcome_unknown=True) from None
        if publish_number <= 0:
            raise WeChatApiError("微信提交发布后的回执缺少有效 publish_id，请到公众号后台核对。", outcome_unknown=True)
        return str(publish_number)

    def freepublish_get(self, token: str, publish_id: str) -> dict:
        try:
            publish_number = int(publish_id)
        except (TypeError, ValueError):
            raise WeChatApiError("本地 publish_id 无效，不能安全查询发布状态。", status=422) from None
        return self._request("POST", "/cgi-bin/freepublish/get", params={"access_token": token},
                             json_body={"publish_id": publish_number})


class WeChatService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store
        self.private = workspace.private
        self.client_factory = WeChatClient
        self._tokens: dict[str, tuple[str, float]] = {}

    def _credentials_path(self, account_id: str) -> Path:
        return self.workspace.accounts.directory(account_id) / "wechat-credentials.json"

    def _save_credentials(self, account_id: str, app_id: str, app_secret: str) -> None:
        raw = json.dumps({"app_id": app_id, "app_secret": app_secret}, ensure_ascii=False,
                         separators=(",", ":")).encode("utf-8")
        encrypted = base64.b64encode(protect(raw)).decode("ascii")
        _atomic(self._credentials_path(account_id), {"protected": encrypted, "updated_at": _now()})

    def _credentials(self, account_id: str) -> dict:
        row = _read(self._credentials_path(account_id), 64_000)
        try:
            value = json.loads(protect(base64.b64decode(row["protected"], validate=True), decrypt=True).decode("utf-8"))
            if not isinstance(value, dict) or not APP_ID_RE.fullmatch(str(value.get("app_id") or "")) or not SECRET_RE.fullmatch(str(value.get("app_secret") or "")):
                raise ValueError()
            return value
        except (KeyError, ValueError, UnicodeError, TypeError):
            raise WorkflowError("微信公众号本地凭据无法读取，请重新连接。", 503) from None

    def _probe_remote(self, app_id: str, app_secret: str) -> dict:
        client = self.client_factory()
        try:
            token, expires = client.stable_token(app_id, app_secret)
            client.api_domain_ips(token)
            capabilities = ["access_token"]
            draft_count = None
            try:
                draft_count = client.draft_count(token)
                capabilities.append("draft")
            except WeChatApiError as exc:
                if exc.errcode != 48001:
                    raise
            try:
                client.freepublish_probe(token)
                capabilities.append("freepublish")
            except WeChatApiError as exc:
                if exc.errcode != 48001:
                    raise
            return {"token": token, "expires": expires, "capabilities": capabilities, "draft_count": draft_count}
        finally:
            client.close()

    def _cache_token(self, account_id: str, token: str, expires: int) -> None:
        self._tokens[account_id] = (token, time.time() + max(60, expires - 300))

    def _token(self, account_id: str) -> str:
        cached = self._tokens.get(account_id)
        if cached and cached[1] > time.time():
            return cached[0]
        creds = self._credentials(account_id)
        client = self.client_factory()
        try:
            token, expires = client.stable_token(creds["app_id"], creds["app_secret"])
        finally:
            client.close()
        self._cache_token(account_id, token, expires)
        return token

    @staticmethod
    def _identity(app_id: str, label: str) -> dict:
        return {"name": label[:80], "display_name": label[:120], "remote_id": app_id, "logged_in": True}

    def connect_new(self, req: WeChatConnectInput) -> dict:
        if not req.confirmed:
            raise WorkflowError("请确认允许 Ripple 使用 AppID/AppSecret 连接微信公众号官方 API。", 422)
        app_secret = req.app_secret.get_secret_value()
        checked = self._probe_remote(req.app_id, app_secret)
        digest = fingerprint({"platform": "wechat", "label": req.label, "app_id": req.app_id, "adapter": ADAPTER})
        with self.store.transaction() as state:
            accounts = state.setdefault("accounts", {})
            for item in accounts.values():
                if item.get("creation_key") == req.idempotency_key:
                    if item.get("creation_digest") != digest:
                        raise WorkflowError("账号创建键已用于其他输入。")
                    return self.workspace.accounts.project(item)
            if len(accounts) >= 100:
                raise WorkflowError("最多保存 100 个账号。", 422)
            account_id = uuid.uuid4().hex
            caps = checked["capabilities"]
            account = {"id": account_id, "workspace_id": DEFAULT_WORKSPACE_ID, "platform": "wechat", "label": req.label,
                       "status": "connected", "auth_revision": 1, "identity": self._identity(req.app_id, req.label),
                       "checked_at": _now(), "operation": None, "created_at": _now(),
                       "message": self._capability_message(caps), "creation_key": req.idempotency_key,
                       "creation_digest": digest, "adapter": ADAPTER, "live_verified": True,
                       "capabilities": caps}
            accounts[account_id] = account
            projected = self.workspace.accounts.project(account)
        try:
            self._save_credentials(account_id, req.app_id, app_secret)
            self._cache_token(account_id, checked["token"], checked["expires"])
        except Exception:
            with self.store.transaction() as state:
                state.get("accounts", {}).pop(account_id, None)
            self._credentials_path(account_id).unlink(missing_ok=True)
            raise
        return projected

    @staticmethod
    def _capability_message(capabilities: list[str]) -> str:
        if "draft" not in capabilities:
            return "AppID/AppSecret 已验证，但当前公众号没有草稿箱 API 权限。"
        if "freepublish" in capabilities:
            return "微信公众号官方 API 已连接：草稿箱与发布接口可用。"
        return "微信公众号官方 API 已连接：草稿箱可用；当前账号没有发布接口权限。"

    def reconnect(self, account_id: str, req: WeChatReconnectInput) -> dict:
        if not req.confirmed:
            raise WorkflowError("请确认更新微信公众号 AppID/AppSecret。", 422)
        with self.store.transaction(write=False) as state:
            current = self.workspace.accounts._account(state, account_id)
            if current.get("adapter") != ADAPTER or current.get("platform") != "wechat":
                raise WorkflowError("这不是微信公众号官方 API 账号。", 422)
            op = current.get("operation") or {}
            if op.get("kind") == "publish" and op.get("state") in {"running", "recovery_required"}:
                raise WorkflowError("微信公众号发布操作仍在进行或待核对，不能更新连接凭据。", 409)
            label = str(current.get("label") or "微信公众号")
        secret = req.app_secret.get_secret_value()
        checked = self._probe_remote(req.app_id, secret)
        with self.store.transaction() as state:
            account = self.workspace.accounts._account(state, account_id)
            op = account.get("operation") or {}
            if op.get("kind") == "publish" and op.get("state") in {"running", "recovery_required"}:
                raise WorkflowError("微信公众号发布操作状态已变化；请先完成核对再更新连接凭据。", 409)
            account.update(status="connected", auth_revision=account.get("auth_revision", 0) + 1,
                           identity=self._identity(req.app_id, label), checked_at=_now(), operation=None,
                           live_verified=True, capabilities=checked["capabilities"],
                           message=self._capability_message(checked["capabilities"]))
            self.workspace.accounts.invalidate_tasks(state, account_id, "微信公众号凭据已更新，原审批失效。")
            projected = self.workspace.accounts.project(account)
        self._save_credentials(account_id, req.app_id, secret)
        self._cache_token(account_id, checked["token"], checked["expires"])
        return projected

    def probe(self, account_id: str, confirmed: bool) -> dict:
        if not confirmed:
            raise WorkflowError("请确认检查微信公众号 API 状态。", 422)
        creds = self._credentials(account_id)
        try:
            checked = self._probe_remote(creds["app_id"], creds["app_secret"])
        except Exception:
            with self.store.transaction() as state:
                account = self.workspace.accounts._account(state, account_id)
                account.update(status="expired", checked_at=_now(), auth_revision=account.get("auth_revision", 0) + 1,
                               message="微信公众号凭据或接口访问不可用，请检查 AppSecret、IP 白名单与公众号权限。")
                self.workspace.accounts.invalidate_tasks(state, account_id, "微信公众号 API 检查失败，原审批失效。")
            raise
        with self.store.transaction() as state:
            account = self.workspace.accounts._account(state, account_id)
            old_caps = list(account.get("capabilities") or [])
            new_caps = checked["capabilities"]
            account.update(status="connected", identity=self._identity(creds["app_id"], account.get("label") or "微信公众号"),
                           checked_at=_now(), live_verified=True, capabilities=new_caps,
                           message=self._capability_message(new_caps))
            if old_caps != new_caps:
                account["auth_revision"] = account.get("auth_revision", 0) + 1
                self.workspace.accounts.invalidate_tasks(state, account_id, "微信公众号接口权限发生变化，原审批失效。")
            projected = self.workspace.accounts.project(account)
        self._cache_token(account_id, checked["token"], checked["expires"])
        return projected

    def disconnect(self, account_id: str, confirmed: bool) -> dict:
        if not confirmed:
            raise WorkflowError("请确认断开微信公众号。", 422)
        with self.store.transaction() as state:
            account = self.workspace.accounts._account(state, account_id)
            if account.get("adapter") != ADAPTER:
                raise WorkflowError("这不是微信公众号官方 API 账号。", 422)
            op = account.get("operation") or {}
            if op.get("kind") == "publish" and op.get("state") in {"running", "recovery_required"}:
                raise WorkflowError("微信公众号发布操作仍在进行或待核对，不能断开。")
            account.update(status="disconnected", identity=None, checked_at=None,
                           auth_revision=account.get("auth_revision", 0) + 1, operation=None,
                           message="已从 Ripple 断开微信公众号并清除本地 AppSecret。")
            self.workspace.accounts.invalidate_tasks(state, account_id, "微信公众号已断开，原审批失效。")
            projected = self.workspace.accounts.project(account)
        self._tokens.pop(account_id, None)
        self._credentials_path(account_id).unlink(missing_ok=True)
        return projected

    def purge(self, account_id: str) -> None:
        self._tokens.pop(account_id, None)

    @staticmethod
    def _option(content: dict, key: str, default=""):
        options = content.get("options") or {}
        return options.get(key, default) if isinstance(options, dict) else default

    def problems(self, task: dict, state: dict) -> list[str]:
        content = task["content"]
        account = state.get("accounts", {}).get(content.get("account_id"))
        errors: list[str] = []
        if not account or account.get("adapter") != ADAPTER or account.get("platform") != "wechat":
            return ["请选择已连接的微信公众号官方 API 账号。"]
        if account.get("status") != "connected":
            errors.append("微信公众号当前未连接，请检查 AppID/AppSecret、IP 白名单与接口权限。")
        if (account.get("operation") or {}).get("state") in {"running", "recovery_required"}:
            errors.append("微信公众号有未结束或待核对的发布操作。")
        capabilities = set(account.get("capabilities") or [])
        if "draft" not in capabilities:
            errors.append("当前公众号没有草稿箱 API 权限（常见 errcode 48001）。请检查公众号类型、认证状态和接口权限。")
        action = str(self._option(content, "wechat_action", "draft") or "draft")
        if action not in {"draft", "publish"}:
            errors.append("微信公众号执行方式无效，请重新选择草稿箱或提交发布。")
        if action == "publish" and "freepublish" not in capabilities:
            errors.append("当前公众号没有 freepublish 发布接口权限；可改为“保存到草稿箱”，或在微信公众平台确认发布接口权限。")
        title = str(content.get("title") or "")
        if not title.strip():
            errors.append("微信公众号文章标题不能为空。")
        if len(title) > 32:
            errors.append("微信公众号图文标题最多 32 个字符。")
        body = str(content.get("body") or "")
        if not body.strip():
            errors.append("微信公众号文章正文不能为空。")
        author = str(self._option(content, "wechat_author", "") or "")
        digest = str(self._option(content, "wechat_digest", "") or "")
        if len(author) > 16:
            errors.append("微信公众号作者最多 16 个字符。")
        if len(digest) > 128:
            errors.append("微信公众号摘要最多 128 个字符。")
        source_url = str(self._option(content, "wechat_source_url", "") or "")
        if source_url:
            try:
                parsed = urlsplit(source_url)
                if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.fragment or len(source_url) > 1024:
                    raise ValueError()
            except ValueError:
                errors.append("微信公众号“阅读原文”链接必须是完整 HTTP/HTTPS URL，且不超过 1KB。")
        media = list(content.get("media") or [])
        if not media:
            errors.append("微信公众号图文需要至少一张本地图片作为封面。请先添加封面图片。")
        cover = str(self._option(content, "wechat_cover", "") or "") or (media[0] if media else "")
        if cover and cover not in media:
            errors.append("所选微信公众号封面不在当前平台版本素材中。")
        for item in content.get("media_snapshot") or []:
            suffix = Path(item["path"]).suffix.lower()
            if suffix not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                errors.append("微信公众号首版只接收本地图片；请移除视频素材。")
            try:
                actual = self.workspace.media_info(item["path"])
                if actual["sha256"] != item.get("sha256") or actual["bytes"] != item.get("bytes"):
                    errors.append("微信公众号素材已变化，请保存新版本。")
            except (WorkflowError, OSError):
                errors.append("微信公众号素材无法读取或已失效。")
        try:
            preview = self._render_html(body, {})
            if len(preview) > MAX_BODY_HTML_CHARS or len(preview.encode("utf-8")) > MAX_BODY_HTML_BYTES:
                errors.append("微信公众号正文转换为 HTML 后超过 2 万字符或 1 MiB，请精简正文。")
        except Exception:
            errors.append("微信公众号正文无法转换为安全 HTML。")
        scheduled = content.get("scheduled_at")
        if scheduled and datetime.fromisoformat(scheduled) <= self.workspace.clock():
            errors.append("微信公众号排期已过期，请调整时间。")
        return list(dict.fromkeys(errors))

    @staticmethod
    def _jpeg_under(image: Image.Image, output: Path, maximum: int, *, max_side: int, start_quality: int = 88) -> Path:
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        quality = start_quality
        while True:
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
            raw = buffer.getvalue()
            if len(raw) < maximum:
                output.write_bytes(raw)
                return output
            if quality > 52:
                quality -= 8
                continue
            width, height = image.size
            if width <= 240 or height <= 120:
                raise WorkflowError("图片无法压缩到微信公众号接口大小限制，请换用更简单或更小的图片。", 422)
            image = image.resize((max(1, int(width * .82)), max(1, int(height * .82))), Image.Resampling.LANCZOS)
            quality = 80

    def _prepare_cover(self, source: Path, directory: Path) -> Path:
        target = directory / "wechat-cover.jpg"
        try:
            with Image.open(source) as image:
                return self._jpeg_under(image.copy(), target, MAX_COVER_BYTES, max_side=900, start_quality=86)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("微信公众号封面图片无法处理，请使用有效的 PNG/JPEG/WebP/GIF。", 422) from None

    def _prepare_body_image(self, source: Path, directory: Path, index: int) -> Path:
        suffix = source.suffix.lower()
        if suffix == ".png" and source.stat().st_size < MAX_BODY_IMAGE_BYTES:
            target = directory / f"wechat-body-{index}.png"
            target.write_bytes(source.read_bytes())
            return target
        if suffix in {".jpg", ".jpeg"} and source.stat().st_size < MAX_BODY_IMAGE_BYTES:
            target = directory / f"wechat-body-{index}.jpg"
            target.write_bytes(source.read_bytes())
            return target
        target = directory / f"wechat-body-{index}.jpg"
        try:
            with Image.open(source) as image:
                return self._jpeg_under(image.copy(), target, MAX_BODY_IMAGE_BYTES, max_side=2400, start_quality=90)
        except WorkflowError:
            raise
        except Exception:
            raise WorkflowError("微信公众号正文图片无法转换为 JPG/PNG。", 422) from None

    @staticmethod
    def _render_html(body: str, image_urls: dict[str, str]) -> str:
        escaped = html.escape(body, quote=False)
        rendered = markdown.markdown(escaped, extensions=["extra", "sane_lists"])
        used: set[str] = set()
        image_pattern = re.compile(r"<img\b[^>]*\bsrc=([\"'])([^\"']+)\1[^>]*>", re.I)

        def replace_image(match: re.Match) -> str:
            source = html.unescape(match.group(2))
            candidate = image_urls.get(source) or image_urls.get(source.lstrip("./")) or image_urls.get(Path(source).name)
            if not candidate:
                return ""
            used.add(candidate)
            alt_match = re.search(r"\balt=([\"'])([^\"']*)\1", match.group(0), re.I)
            alt = html.escape(html.unescape(alt_match.group(2)) if alt_match else "", quote=True)
            return f'<img src="{html.escape(candidate, quote=True)}" alt="{alt}" />'

        rendered = image_pattern.sub(replace_image, rendered)
        # Raw HTML was escaped before Markdown conversion. Remove dangerous link schemes
        # that may still be introduced through Markdown link syntax.
        rendered = re.sub(r"href=([\"'])(?:javascript:|data:|file:)[^\"']*\1", 'href="#"', rendered, flags=re.I)
        for url in dict.fromkeys(image_urls.values()):
            if url not in used:
                rendered += f'\n<p><img src="{html.escape(url, quote=True)}" alt="" /></p>'
        return rendered

    def _progress_path(self, account_id: str, operation_id: str) -> Path:
        return self.workspace.accounts.directory(account_id) / "operations" / operation_id / "wechat-progress.json"

    def _write_progress(self, account_id: str, operation_id: str, value: dict) -> None:
        _atomic(self._progress_path(account_id, operation_id), value)

    def _read_progress(self, account_id: str, operation_id: str) -> dict:
        return _read(self._progress_path(account_id, operation_id), 64_000)

    def publish(self, task: dict, account: dict, paths: list[str]) -> dict:
        content = task["content"]
        action = str(self._option(content, "wechat_action", "draft") or "draft")
        media_rels = list(content.get("media") or [])
        cover_rel = str(self._option(content, "wechat_cover", "") or "") or (media_rels[0] if media_rels else "")
        if not cover_rel or cover_rel not in media_rels or len(paths) != len(media_rels):
            return {"state": "verification_required", "not_submitted": True, "message": "微信公众号封面或素材快照不完整，未创建草稿。"}
        staged = {rel: Path(path) for rel, path in zip(media_rels, paths)}
        run_dir = self.workspace.accounts.directory(account["id"]) / "operations" / task["operation_id"] / "wechat"
        run_dir.mkdir(parents=True, exist_ok=True)
        progress = {"task_id": task["id"], "version_id": task["version_id"], "operation_id": task["operation_id"],
                    "action": action, "at": _now(), "payload_digest": fingerprint({"title": content.get("title"), "body": content.get("body"), "media": media_rels, "options": content.get("options") or {}})}
        self._write_progress(account["id"], task["operation_id"], progress)
        draft_media_id: str | None = None
        publish_id: str | None = None
        progress_saved = True
        try:
            token = self._token(account["id"])
            client = self.client_factory()
            try:
                cover_path = self._prepare_cover(staged[cover_rel], run_dir)
                cover_id = client.add_cover(token, cover_path)
                image_urls: dict[str, str] = {}
                raw_body = str(content.get("body") or "")
                for index, rel in enumerate(media_rels):
                    # The cover is a separate permanent thumb. Upload it to the body
                    # image endpoint only when the article actually references it.
                    cover_referenced = rel in raw_body or Path(rel).name in raw_body
                    if rel == cover_rel and not cover_referenced:
                        continue
                    prepared = self._prepare_body_image(staged[rel], run_dir, index)
                    url = client.upload_body_image(token, prepared)
                    image_urls[rel] = url
                    image_urls[Path(rel).name] = url
                body_html = self._render_html(str(content.get("body") or ""), image_urls)
                if len(body_html) > MAX_BODY_HTML_CHARS or len(body_html.encode("utf-8")) > MAX_BODY_HTML_BYTES:
                    return {"state": "verification_required", "not_submitted": True,
                            "message": "微信公众号正文转换并回写图片后超过 2 万字符或 1 MiB，未创建草稿。"}
                article = {"article_type": "news", "title": str(content.get("title") or "")[:32],
                           "content": body_html, "thumb_media_id": cover_id,
                           "need_open_comment": 0, "only_fans_can_comment": 0}
                author = str(self._option(content, "wechat_author", "") or "")
                digest = str(self._option(content, "wechat_digest", "") or "")
                source_url = str(self._option(content, "wechat_source_url", "") or "")
                if author:
                    article["author"] = author
                if digest:
                    article["digest"] = digest
                if source_url:
                    article["content_source_url"] = source_url
                try:
                    draft_media_id = client.draft_add(token, article)
                except WeChatApiError as exc:
                    if exc.outcome_unknown:
                        return {"state": "unknown_result", "not_submitted": False,
                                "message": "微信公众号草稿创建响应丢失，草稿可能已经创建；请先到公众号后台核对，Ripple 不会自动重试。"}
                    return {"state": "verification_required", "not_submitted": True, "message": str(exc)}
                progress.update(draft_media_id=draft_media_id, draft_created_at=_now())
                try:
                    self._write_progress(account["id"], task["operation_id"], progress)
                except OSError:
                    progress_saved = False
                # Read the draft back before claiming verified success. If this read
                # fails, the draft id is already authoritative: never create another.
                try:
                    client.draft_get(token, draft_media_id)
                except WeChatApiError as exc:
                    return {"state": "verification_required", "not_submitted": False, "adapter": ADAPTER,
                            "draft_media_id": draft_media_id, "draft_only": action == "draft",
                            "message": "微信公众号草稿已返回 media_id，但 draft/get 核对失败；不会重复创建草稿。" + str(exc)}
                if action == "draft":
                    return {"state": "accepted", "not_submitted": False, "adapter": ADAPTER,
                            "draft_media_id": draft_media_id, "draft_only": True,
                            "message": "微信公众号草稿已创建并通过 draft/get 核对；尚未公开发布。",
                            "evidence": "wechat-draft-get"}
                if not progress_saved:
                    return {"state": "verification_required", "not_submitted": False, "adapter": ADAPTER,
                            "draft_media_id": draft_media_id, "draft_only": False,
                            "message": "微信公众号草稿已创建，但本地发布进度写盘失败；为避免丢失关联，Ripple 没有继续提交公开发布。"}
                try:
                    publish_id = client.freepublish_submit(token, draft_media_id)
                except WeChatApiError as exc:
                    if exc.outcome_unknown:
                        return {"state": "unknown_result", "not_submitted": False, "draft_media_id": draft_media_id,
                                "message": "微信公众号发布提交响应丢失，可能已经进入发布队列；请先核对，Ripple 不会自动重试。"}
                    return {"state": "verification_required", "not_submitted": False, "draft_media_id": draft_media_id,
                            "message": str(exc)}
                progress.update(publish_id=publish_id, publish_submitted_at=_now())
                progress_note = ""
                try:
                    self._write_progress(account["id"], task["operation_id"], progress)
                except OSError:
                    progress_note = " 本地辅助进度文件写入失败，但 publish_id 已返回并会记录到发布任务回执。"
                return {"state": "accepted", "not_submitted": False, "adapter": ADAPTER,
                        "draft_media_id": draft_media_id, "publish_id": publish_id, "draft_only": False,
                        "message": "微信公众号已接收发布任务；等待 freepublish/get 返回最终结果。" + progress_note,
                        "evidence": "wechat-publish-id"}
            finally:
                client.close()
        except WeChatApiError as exc:
            return {"state": "verification_required", "not_submitted": True, "message": str(exc)}
        except WorkflowError as exc:
            return {"state": "verification_required", "not_submitted": True, "message": str(exc)}
        except Exception:
            if publish_id:
                return {"state": "unknown_result", "not_submitted": False, "adapter": ADAPTER,
                        "draft_media_id": draft_media_id, "publish_id": publish_id, "draft_only": False,
                        "message": "微信公众号已经返回 publish_id，但本地收尾失败；请按 publish_id 核对，Ripple 不会自动重发。"}
            if draft_media_id:
                return {"state": "verification_required", "not_submitted": False, "adapter": ADAPTER,
                        "draft_media_id": draft_media_id, "draft_only": action == "draft",
                        "message": "微信公众号已经返回草稿 media_id，但本地收尾失败；草稿可能已存在，Ripple 不会自动重建。"}
            return {"state": "verification_required", "not_submitted": True,
                    "message": "微信公众号发布准备失败，未确认创建草稿。"}

    @staticmethod
    def _public_url(value: dict) -> str | None:
        detail = value.get("article_detail")
        items = detail.get("item") if isinstance(detail, dict) else None
        if not isinstance(items, list):
            return None
        for item in items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("article_url") or "")
            try:
                parsed = urlsplit(url)
                if parsed.scheme == "https" and parsed.hostname in {"mp.weixin.qq.com", "mp.weixin.qq.com.cn"}:
                    return url
            except ValueError:
                continue
        return None

    def query(self, task: dict, account: dict) -> dict | None:
        receipt = task.get("receipt") or {}
        operation_id = str(task.get("operation_id") or "")
        progress = self._read_progress(account["id"], operation_id) if operation_id else {}
        draft_media_id = str(receipt.get("draft_media_id") or progress.get("draft_media_id") or "")
        publish_id = str(receipt.get("publish_id") or progress.get("publish_id") or "")
        action = str(progress.get("action") or self._option(task["content"], "wechat_action", "draft") or "draft")
        if not draft_media_id and not publish_id:
            return None
        try:
            token = self._token(account["id"])
            client = self.client_factory()
            try:
                if action == "draft":
                    if not draft_media_id:
                        return None
                    client.draft_get(token, draft_media_id)
                    return {"state": "accepted", "not_submitted": False, "adapter": ADAPTER,
                            "draft_media_id": draft_media_id, "draft_only": True,
                            "message": "微信公众号草稿仍存在；当前任务没有执行公开发布。",
                            "evidence": "wechat-draft-get"}
                if not publish_id:
                    if draft_media_id:
                        client.draft_get(token, draft_media_id)
                    state = "unknown_result" if task.get("status") == "unknown_result" else "verification_required"
                    return {"state": state, "not_submitted": False, "adapter": ADAPTER,
                            "draft_media_id": draft_media_id or None, "draft_only": False,
                            "message": "公众号草稿已存在，但没有可安全关联的 publish_id；Ripple 不会再次提交发布，请到微信公众平台核对。"}
                value = client.freepublish_get(token, publish_id)
            finally:
                client.close()
        except WeChatApiError as exc:
            return {"state": "verification_required", "not_submitted": False,
                    "draft_media_id": draft_media_id or None, "publish_id": publish_id or None,
                    "message": str(exc)}
        try:
            status = int(value.get("publish_status"))
        except (TypeError, ValueError):
            return {"state": "unknown_result", "not_submitted": False, "draft_media_id": draft_media_id,
                    "publish_id": publish_id, "message": "微信发布状态响应缺少 publish_status，请稍后再次核对。"}
        if status == 0:
            url = self._public_url(value)
            return {"state": "published", "not_submitted": False, "draft_media_id": draft_media_id,
                    "publish_id": publish_id, "publish_status": status, "public_url": url,
                    "message": "微信 freepublish/get 已确认发布成功。",
                    "evidence": "wechat-freepublish-success"}
        if status == 1:
            return {"state": "accepted", "not_submitted": False, "draft_media_id": draft_media_id,
                    "publish_id": publish_id, "publish_status": status,
                    "message": "微信公众号仍在处理发布任务。", "evidence": "wechat-freepublish-status"}
        failure_messages = {
            2: "微信返回原创声明失败。",
            3: "微信返回常规发布失败。",
            4: "微信平台审核未通过。",
            5: "文章发布成功后已被用户删除。",
            6: "文章发布成功后被系统封禁。",
        }
        if status in failure_messages:
            return {"state": "failed_terminal", "not_submitted": False, "draft_media_id": draft_media_id,
                    "publish_id": publish_id, "publish_status": status,
                    "message": failure_messages[status], "evidence": "wechat-freepublish-status"}
        return {"state": "unknown_result", "not_submitted": False, "draft_media_id": draft_media_id,
                "publish_id": publish_id, "publish_status": status,
                "message": f"微信返回未知 publish_status={status}，请人工核对。"}
