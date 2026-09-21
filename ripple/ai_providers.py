"""Multi-provider AI configuration for Ripple.

The default Agent route is mirrored into Ripple's legacy environment by web/app.py
so existing Agent runtimes keep working. Extra providers and task routes stay in
Ripple's private directory and never expose credentials through public state.
"""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit
import uuid

import httpx

from .publishing import WorkflowError
from .secrets import protect


PURPOSES = {
    "default_agent": "默认 Ripple Agent",
    "idea_generation": "AI 选题生成",
    "research": "联网资料核验",
    "x_campaign_discovery": "X 活动发现",
}
KINDS = {"openai-compatible", "xai"}
MAX_PROVIDERS = 20
MAX_MODELS = 200
MAX_RESPONSE = 2 * 1024 * 1024
MODEL_ID = re.compile(r"^[A-Za-z0-9._:/-]{1,200}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_endpoint(value: str) -> str:
    raw = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(raw)
    except ValueError:
        raise WorkflowError("AI Provider Base URL 无效。", 422) from None
    host = (parsed.hostname or "").lower().rstrip(".")
    local = host in {"localhost", "127.0.0.1", "::1"}
    if (parsed.scheme != "https" and not (local and parsed.scheme == "http")):
        raise WorkflowError("公网 AI Provider 必须使用 HTTPS。", 422)
    if not host or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise WorkflowError("AI Provider URL 不能包含凭据、query 或 fragment。", 422)
    return raw


def _atomic(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with tmp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read(path: Path, limit: int = 2 * 1024 * 1024) -> dict:
    try:
        if not path.is_file() or path.stat().st_size > limit:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _model_rows(values: list[Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in values[:MAX_MODELS]:
        if isinstance(item, dict):
            model_id = str(item.get("id") or "").strip()
            name = str(item.get("name") or model_id).strip()
            effort_levels = [str(x) for x in item.get("effort_levels", [])
                             if str(x) in {"auto", "low", "medium", "high", "extra_high"}][:5]
            source = str(item.get("source") or "manual")[:24]
            supports_reasoning = bool(item.get("supports_reasoning"))
        else:
            model_id = str(item or "").strip()
            name = model_id
            effort_levels = ["auto"]
            source = "manual"
            supports_reasoning = False
        if not MODEL_ID.fullmatch(model_id) or model_id in seen:
            continue
        seen.add(model_id)
        if "auto" not in effort_levels:
            effort_levels.insert(0, "auto")
        result.append({
            "id": model_id, "name": name[:200] or model_id,
            "effort_levels": effort_levels or ["auto"], "source": source,
            "supports_reasoning": supports_reasoning,
        })
    return result


def _extract_response_text(data: dict[str, Any]) -> str:
    direct = data.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    chunks: list[str] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if isinstance(content, list):
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    text = part.get("text") or part.get("output_text")
                    if isinstance(text, str) and text.strip():
                        chunks.append(text.strip())
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                chunks.append(text.strip())
    return "\n".join(chunks).strip()


class AIProviderService:
    def __init__(self, private: Path):
        self.private = private
        self.path = private / "integrations" / "ai-providers.json"
        self.secret_dir = private / "integrations" / "ai-provider-secrets"

    def _empty(self) -> dict[str, Any]:
        return {"schema": 2, "revision": 0, "providers": [], "routes": {}, "updated_at": _now()}

    def _state(self) -> dict[str, Any]:
        raw = _read(self.path)
        if not raw:
            return self._empty()
        providers = raw.get("providers") if isinstance(raw.get("providers"), list) else []
        routes = raw.get("routes") if isinstance(raw.get("routes"), dict) else {}
        return {
            "schema": 2,
            "revision": max(0, int(raw.get("revision") or 0)),
            "providers": [p for p in providers if isinstance(p, dict)],
            "routes": {k: v for k, v in routes.items() if k in PURPOSES and isinstance(v, dict)},
            "updated_at": str(raw.get("updated_at") or _now()),
        }

    def _write(self, state: dict[str, Any]) -> None:
        state = deepcopy(state)
        state["schema"] = 2
        state["revision"] = int(state.get("revision") or 0) + 1
        state["updated_at"] = _now()
        _atomic(self.path, state)

    def _secret_path(self, provider_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{32}", provider_id):
            raise WorkflowError("AI Provider 标识无效。", 422)
        return self.secret_dir / (provider_id + ".secret")

    def _save_key(self, provider_id: str, api_key: str) -> None:
        raw = api_key.strip()
        if not raw:
            return
        protected = base64.b64encode(protect(raw.encode("utf-8"))).decode("ascii")
        _atomic(self._secret_path(provider_id), {"protected": protected, "updated_at": _now()})

    def _key(self, provider_id: str) -> str:
        row = _read(self._secret_path(provider_id), 64_000)
        try:
            blob = base64.b64decode(row["protected"], validate=True)
            return protect(blob, decrypt=True).decode("utf-8")
        except (KeyError, ValueError, UnicodeError, TypeError):
            return ""

    def _provider(self, state: dict[str, Any], provider_id: str) -> dict[str, Any]:
        provider = next((p for p in state["providers"] if p.get("id") == provider_id), None)
        if not provider:
            raise WorkflowError("AI Provider 不存在。", 404)
        return provider

    def ensure_legacy(self, *, enabled: bool, base_url: str, api_key: str,
                      models: list[Any], default_model: str) -> None:
        state = self._state()
        if state["providers"] or not (enabled and base_url and api_key and default_model):
            return
        provider_id = uuid.uuid4().hex
        provider = {
            "id": provider_id,
            "name": (urlsplit(base_url).hostname or "Legacy Provider")[:80],
            "kind": "openai-compatible",
            "base_url": _valid_endpoint(base_url),
            "models": _model_rows(models or [default_model]),
            "default_model": default_model,
            "enabled": True,
            "capabilities": {"chat": "legacy", "reasoning": "unknown", "responses": "unknown",
                             "web_search": "unknown", "x_search": "unknown"},
            "created_at": _now(),
            "updated_at": _now(),
            "migrated_from_legacy": True,
        }
        if not any(m["id"] == default_model for m in provider["models"]):
            provider["models"].insert(0, {"id": default_model, "name": default_model})
        state["providers"].append(provider)
        state["routes"]["default_agent"] = {"provider_id": provider_id, "model_id": default_model}
        state["routes"]["idea_generation"] = {"provider_id": provider_id, "model_id": default_model}
        self._save_key(provider_id, api_key)
        self._write(state)

    def public_state(self) -> dict[str, Any]:
        state = self._state()
        providers = []
        for provider in state["providers"]:
            public = deepcopy(provider)
            public["api_key_set"] = bool(self._key(str(provider.get("id") or "")))
            providers.append(public)
        return {
            "schema": 2,
            "revision": state["revision"],
            "providers": providers,
            "routes": deepcopy(state["routes"]),
            "purposes": [{"id": key, "label": label} for key, label in PURPOSES.items()],
            "updated_at": state["updated_at"],
        }

    def upsert(self, *, provider_id: str = "", name: str, kind: str, base_url: str,
               api_key: str, models: list[Any], default_model: str, enabled: bool = True) -> dict[str, Any]:
        state = self._state()
        if kind not in KINDS:
            raise WorkflowError("不支持的 AI Provider 类型。", 422)
        base = _valid_endpoint(base_url)
        model_rows = _model_rows(models)
        if not model_rows:
            raise WorkflowError("至少配置一个模型。", 422)
        default_model = str(default_model or "").strip()
        if not any(row["id"] == default_model for row in model_rows):
            raise WorkflowError("默认模型必须属于当前 Provider。", 422)
        if provider_id:
            provider = self._provider(state, provider_id)
        else:
            if len(state["providers"]) >= MAX_PROVIDERS:
                raise WorkflowError("最多保存 20 个 AI Provider。", 422)
            provider_id = uuid.uuid4().hex
            provider = {"id": provider_id, "created_at": _now(), "capabilities": {}}
            state["providers"].append(provider)
        if not api_key.strip() and not self._key(provider_id):
            raise WorkflowError("首次保存 Provider 需要 API Key。", 422)
        previous_capabilities = provider.get("capabilities") if isinstance(provider.get("capabilities"), dict) else {}
        provider.update({
            "name": str(name or "").strip()[:80] or (urlsplit(base).hostname or "AI Provider"),
            "kind": kind,
            "base_url": base,
            "models": model_rows,
            "default_model": default_model,
            "enabled": bool(enabled),
            "capabilities": {
                "chat": previous_capabilities.get("chat", "unknown"),
                "reasoning": previous_capabilities.get("reasoning", "unknown"),
                "responses": previous_capabilities.get("responses", "unknown"),
                "web_search": previous_capabilities.get("web_search", "unknown"),
                "x_search": previous_capabilities.get("x_search", "unknown"),
            },
            "updated_at": _now(),
        })
        if api_key.strip():
            self._save_key(provider_id, api_key)
        self._write(state)
        return self.public_state()

    def remove(self, provider_id: str) -> dict[str, Any]:
        state = self._state()
        self._provider(state, provider_id)
        state["providers"] = [p for p in state["providers"] if p.get("id") != provider_id]
        state["routes"] = {
            purpose: route for purpose, route in state["routes"].items()
            if route.get("provider_id") != provider_id
        }
        self._write(state)
        try:
            self._secret_path(provider_id).unlink(missing_ok=True)
        except OSError:
            pass
        return self.public_state()

    def set_route(self, purpose: str, provider_id: str, model_id: str) -> dict[str, Any]:
        if purpose not in PURPOSES:
            raise WorkflowError("未知的 AI 用途路由。", 422)
        state = self._state()
        if not provider_id:
            state["routes"].pop(purpose, None)
            self._write(state)
            return self.public_state()
        provider = self._provider(state, provider_id)
        if not provider.get("enabled", True):
            raise WorkflowError("该 AI Provider 已停用。", 422)
        if not any(m.get("id") == model_id for m in provider.get("models", [])):
            raise WorkflowError("所选模型不属于该 AI Provider。", 422)
        state["routes"][purpose] = {"provider_id": provider_id, "model_id": model_id}
        self._write(state)
        return self.public_state()

    def resolved(self, purpose: str, *, fallback_to_default: bool = True) -> dict[str, Any] | None:
        state = self._state()
        route = state["routes"].get(purpose)
        if not route and fallback_to_default and purpose != "default_agent":
            route = state["routes"].get("default_agent")
        if not route:
            return None
        try:
            provider = self._provider(state, str(route.get("provider_id") or ""))
        except WorkflowError:
            return None
        if not provider.get("enabled", True):
            return None
        model_id = str(route.get("model_id") or "")
        if not any(m.get("id") == model_id for m in provider.get("models", [])):
            return None
        key = self._key(provider["id"])
        if not key:
            return None
        return {
            "provider_id": provider["id"],
            "provider_name": provider["name"],
            "kind": provider["kind"],
            "base_url": provider["base_url"],
            "api_key": key,
            "model": model_id,
            "capabilities": deepcopy(provider.get("capabilities") or {}),
        }

    def discover(self, *, provider_id: str = "", kind: str = "openai-compatible",
                 base_url: str = "", api_key: str = "") -> list[dict[str, str]]:
        state = self._state()
        if provider_id:
            provider = self._provider(state, provider_id)
            base = _valid_endpoint(base_url) if str(base_url or "").strip() else str(provider.get("base_url") or "")
            key = api_key.strip() or self._key(provider_id)
        else:
            if kind not in KINDS:
                raise WorkflowError("不支持的 AI Provider 类型。", 422)
            base = _valid_endpoint(base_url)
            key = api_key.strip()
        if not key:
            raise WorkflowError("发现模型需要 API Key。", 422)
        try:
            with httpx.Client(timeout=httpx.Timeout(20, connect=8), trust_env=False,
                              follow_redirects=False, headers={"Authorization": "Bearer " + key,
                              "Accept": "application/json"}) as client:
                response = client.get(base.rstrip("/") + "/models")
                response.raise_for_status()
                if len(response.content) > MAX_RESPONSE:
                    raise WorkflowError("模型列表超过安全上限。", 502)
                data = response.json()
        except WorkflowError:
            raise
        except httpx.TimeoutException:
            raise WorkflowError("Provider /models 请求超时（20 秒）；请检查该接口是否可用，或先手工添加模型 ID。", 504) from None
        except httpx.HTTPStatusError as exc:
            raise WorkflowError(f"Provider /models 返回 HTTP {exc.response.status_code}；请检查接口权限与 Base URL。", 502) from None
        except httpx.ConnectError:
            raise WorkflowError("无法连接 Provider /models；请检查 Base URL、DNS 与网络连通性。", 502) from None
        except httpx.HTTPError:
            raise WorkflowError("Provider /models 请求失败；请检查网络与接口兼容性。", 502) from None
        except (ValueError, TypeError):
            raise WorkflowError("Provider /models 返回的不是有效 JSON 模型列表；可以手工添加模型 ID。", 502) from None
        values = data.get("data", data.get("models", [])) if isinstance(data, dict) else []
        rows = _model_rows(values if isinstance(values, list) else [])
        return rows[:MAX_MODELS]

    def _update_capability(self, provider_id: str, capability: str, value: str) -> None:
        state = self._state()
        provider = self._provider(state, provider_id)
        capabilities = provider.setdefault("capabilities", {})
        capabilities[capability] = value
        provider["updated_at"] = _now()
        self._write(state)

    def probe(self, provider_id: str, model_id: str, capability: str) -> dict[str, Any]:
        state = self._state()
        provider = self._provider(state, provider_id)
        if not any(m.get("id") == model_id for m in provider.get("models", [])):
            raise WorkflowError("测试模型不属于该 Provider。", 422)
        key = self._key(provider_id)
        if not key:
            raise WorkflowError("AI Provider 尚未保存 API Key。", 422)
        if capability not in {"chat", "x_search", "web_search"}:
            raise WorkflowError("不支持测试该能力。", 422)
        try:
            if capability == "chat":
                payload = {"model": model_id, "messages": [{"role": "user", "content": "只回复 RIPPLE_OK"}],
                           "temperature": 0, "max_tokens": 20}
                with httpx.Client(timeout=httpx.Timeout(30, connect=8), trust_env=False,
                                  follow_redirects=False) as client:
                    response = client.post(provider["base_url"].rstrip("/") + "/chat/completions",
                                           headers={"Authorization": "Bearer " + key}, json=payload)
                    response.raise_for_status()
                    ok = bool(response.json())
            else:
                tool = "x_search" if capability == "x_search" else "web_search"
                payload = {"model": model_id, "input": [{"role": "user", "content": "返回一句简短测试结果。"}],
                           "tools": [{"type": tool}]}
                with httpx.Client(timeout=httpx.Timeout(45, connect=8), trust_env=False,
                                  follow_redirects=False) as client:
                    response = client.post(provider["base_url"].rstrip("/") + "/responses",
                                           headers={"Authorization": "Bearer " + key}, json=payload)
                    response.raise_for_status()
                    body = response.json()
                    ok = bool(_extract_response_text(body) or body.get("output"))
        except WorkflowError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            self._update_capability(provider_id, capability, "failed")
            raise WorkflowError("Provider 能力测试失败，请检查模型权限、API 兼容性和额度。", 502) from None
        self._update_capability(provider_id, capability, "verified" if ok else "failed")
        return {"ok": bool(ok), "capability": capability, "provider_id": provider_id, "model_id": model_id}

    def x_search(self, prompt: str, *, from_date: str = "", to_date: str = "",
                 allowed_handles: list[str] | None = None, max_handles: int = 20) -> dict[str, Any]:
        cfg = self.resolved("x_campaign_discovery", fallback_to_default=False)
        if not cfg:
            raise WorkflowError("尚未配置 X 活动发现的 X Search Provider 路由。", 409)
        if cfg["capabilities"].get("x_search") != "verified":
            raise WorkflowError("所选 Provider / Model 尚未通过 X Search 能力测试。", 409)
        tool: dict[str, Any] = {"type": "x_search"}
        handles = [str(x).lstrip("@")[:50] for x in (allowed_handles or []) if str(x).strip()][:max_handles]
        if handles:
            tool["allowed_x_handles"] = handles
        if from_date:
            tool["from_date"] = from_date[:10]
        if to_date:
            tool["to_date"] = to_date[:10]
        payload = {
            "model": cfg["model"],
            "input": [{"role": "user", "content": prompt}],
            "tools": [tool],
        }
        try:
            with httpx.Client(timeout=httpx.Timeout(60, connect=8), trust_env=False,
                              follow_redirects=False) as client:
                response = client.post(cfg["base_url"].rstrip("/") + "/responses",
                    headers={"Authorization": "Bearer " + cfg["api_key"]}, json=payload)
                response.raise_for_status()
                if len(response.content) > MAX_RESPONSE:
                    raise WorkflowError("xAI X Search 响应超过安全上限。", 502)
                data = response.json()
        except WorkflowError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise WorkflowError("X Search 请求失败，请检查模型权限、Provider 兼容性、额度与 /responses 工具支持。", 502) from None
        return {"text": _extract_response_text(data), "raw": data,
                "provider_id": cfg["provider_id"], "model": cfg["model"]}
