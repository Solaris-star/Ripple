"""Private media Provider/model configuration for Ripple.

A Provider stores credentials, base URL and network routing once. Media-specific
protocol routes and enabled models hang off that Provider. Public projections
never expose credentials, proxy secrets, or Agent-internal adapter identifiers.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid

import httpx


GROUP_LABELS = {
    "image": "图片生成",
    "video": "视频生成",
    "music": "音乐生成",
    "voice": "语音 / 声音克隆",
}
MAX_PROVIDERS = 30
MAX_MODELS_PER_PROVIDER = 200
MAX_DISCOVERED_MODELS = 500
MAX_DISCOVERY_BODY = 2 * 1024 * 1024


def _field(key: str, label: str, *, required: bool = False, kind: str = "text",
           choices: tuple[str, ...] = (), placeholder: str = "") -> dict[str, Any]:
    return {
        "key": key, "label": label, "required": required, "kind": kind,
        "choices": list(choices), "placeholder": placeholder,
    }


# Protocols are request adapters. Provider identity and credentials are not tied
# to a vendor brand, so one Provider can expose more than one media route.
PROTOCOLS: dict[str, list[dict[str, Any]]] = {
    "image": [{
        "id": "openai-images", "label": "OpenAI Images 兼容", "adapter": "openai-images",
        "auth": ("api_key",), "discover": "openai-models", "default_base": "",
        "fields": [],
        "env": {"api_key": "IMG_API_KEY", "base_url": "IMG_BASE_URL", "model": "IMG_MODEL"},
    }],
    "video": [
        {"id": "openai-videos", "label": "OpenAI Videos 兼容", "adapter": "openai-compatible",
         "auth": ("api_key",), "discover": "openai-models", "default_base": "",
         "fields": [], "env": {"api_key": "VIDEO_API_KEY", "base_url": "VIDEO_BASE_URL", "model": "VIDEO_MODEL"}},
        {"id": "async-input-video", "label": "异步任务 · input/parameters", "adapter": "dashscope",
         "auth": ("api_key",), "discover": None, "default_base": "https://dashscope.aliyuncs.com/api/v1",
         "fields": [], "env": {"api_key": "DASHSCOPE_API_KEY", "base_url": "DASHSCOPE_BASE_URL", "model": "DASHSCOPE_VIDEO_MODEL"}},
        {"id": "async-content-video", "label": "异步任务 · content", "adapter": "ark",
         "auth": ("api_key",), "discover": None, "default_base": "https://ark.cn-beijing.volces.com/api/v3",
         "fields": [], "env": {"api_key": "ARK_API_KEY", "base_url": "ARK_BASE_URL", "model": "ARK_MODEL"}},
        {"id": "jwt-video", "label": "JWT 签名任务", "adapter": "kling",
         "auth": ("access_key", "secret_key"), "discover": None, "default_base": "https://api.klingai.com",
         "fields": [], "env": {"access_key": "KLING_ACCESS_KEY", "secret_key": "KLING_SECRET_KEY", "base_url": "KLING_BASE_URL"}},
        {"id": "async-media-video", "label": "兼容异步任务 · media/parameters", "adapter": "xhs-maas",
         "auth": ("api_key",), "discover": None, "default_base": "",
         "fields": [_field("resolution", "默认分辨率")],
         "env": {"api_key": "XHS_MAAS_API_KEY", "base_url": "XHS_MAAS_VIDEO_BASE", "resolution": "XHS_MAAS_RESOLUTION"}},
        {"id": "custom-poll-video", "label": "创建 + 自定义轮询", "adapter": "agnes",
         "auth": ("api_key",), "discover": "openai-models", "default_base": "https://apihub.agnes-ai.com/v1",
         "fields": [_field("poll_base", "轮询端点"), _field("size", "默认分辨率")],
         "env": {"api_key": "AGNES_API_KEY", "base_url": "AGNES_BASE_URL", "poll_base": "AGNES_POLL_BASE", "size": "AGNES_SIZE"}},
    ],
    "music": [
        {"id": "async-music", "label": "异步音乐任务", "adapter": "dashscope",
         "auth": ("api_key",), "discover": None, "default_base": "https://dashscope.aliyuncs.com/api/v1",
         "fields": [], "env": {"api_key": "DASHSCOPE_API_KEY", "base_url": "DASHSCOPE_BASE_URL", "model": "DASHSCOPE_MUSIC_MODEL"}},
        {"id": "suno-compatible", "label": "Suno 兼容", "adapter": "suno-compatible",
         "auth": ("api_key",), "discover": None, "default_base": "",
         "fields": [], "env": {"api_key": "MUSIC_API_KEY", "base_url": "MUSIC_BASE_URL", "model": "MUSIC_MODEL"}},
    ],
    "voice": [
        {"id": "openai-speech", "label": "OpenAI Speech 兼容", "adapter": "openai-compatible",
         "auth": ("api_key",), "discover": "openai-models", "default_base": "",
         "fields": [_field("instruct_mode", "情感指令模式", kind="select", choices=("field", "inline")),
                    _field("instruct_delim", "内联指令分隔符")],
         "env": {"api_key": "VOICE_API_KEY", "base_url": "VOICE_BASE_URL", "model": "VOICE_MODEL",
                 "instruct_mode": "VOICE_INSTRUCT_MODE", "instruct_delim": "VOICE_INSTRUCT_DELIM"}},
        {"id": "tts-input", "label": "TTS · input/parameters", "adapter": "dashscope",
         "auth": ("api_key",), "discover": None, "default_base": "https://dashscope.aliyuncs.com/api/v1",
         "fields": [], "env": {"api_key": "DASHSCOPE_API_KEY", "base_url": "DASHSCOPE_BASE_URL", "model": "DASHSCOPE_TTS_MODEL"}},
        {"id": "tts-group", "label": "TTS · Group ID 协议", "adapter": "minimax",
         "auth": ("api_key",), "discover": None, "default_base": "",
         "fields": [_field("group_id", "Group ID", required=True)],
         "env": {"api_key": "MINIMAX_API_KEY", "base_url": "MINIMAX_BASE_URL", "model": "MINIMAX_MODEL", "group_id": "MINIMAX_GROUP_ID"}},
        {"id": "tts-reference", "label": "TTS · Reference 协议", "adapter": "fish-audio",
         "auth": ("api_key",), "discover": None, "default_base": "",
         "fields": [], "env": {"api_key": "FISH_API_KEY", "base_url": "FISH_BASE_URL"}},
        {"id": "gemini-tts", "label": "TTS · Gemini 协议", "adapter": "gemini",
         "auth": ("api_key",), "discover": None, "default_base": "",
         "fields": [_field("voice", "预置音色"), _field("rate", "采样率")],
         "env": {"api_key": "GEMINI_API_KEY", "base_url": "GEMINI_BASE_URL", "model": "GEMINI_TTS_MODEL",
                 "voice": "GEMINI_VOICE", "rate": "GEMINI_TTS_RATE"}},
    ],
}


LEGACY_ENV: dict[str, list[tuple[str, dict[str, str]]]] = {
    "image": [("openai-images", {"api_key": "IMG_API_KEY", "base_url": "IMG_BASE_URL", "model": "IMG_MODEL", "direct": "IMG_NO_PROXY"})],
    "video": [
        ("openai-videos", {"api_key": "VIDEO_API_KEY", "base_url": "VIDEO_BASE_URL", "model": "VIDEO_MODEL"}),
        ("async-input-video", {"api_key": "DASHSCOPE_API_KEY", "base_url": "DASHSCOPE_BASE_URL", "model": "DASHSCOPE_VIDEO_MODEL"}),
        ("async-content-video", {"api_key": "ARK_API_KEY", "base_url": "ARK_BASE_URL", "model": "ARK_MODEL"}),
        ("jwt-video", {"access_key": "KLING_ACCESS_KEY", "secret_key": "KLING_SECRET_KEY", "base_url": "KLING_BASE_URL"}),
        ("async-media-video", {"api_key": "XHS_MAAS_API_KEY", "base_url": "XHS_MAAS_VIDEO_BASE", "model": "XHS_MAAS_T2V_MODEL", "model2": "XHS_MAAS_I2V_MODEL", "resolution": "XHS_MAAS_RESOLUTION"}),
        ("custom-poll-video", {"api_key": "AGNES_API_KEY", "base_url": "AGNES_BASE_URL", "model": "AGNES_MODEL", "poll_base": "AGNES_POLL_BASE", "size": "AGNES_SIZE"}),
    ],
    "music": [
        ("async-music", {"api_key": "DASHSCOPE_API_KEY", "base_url": "DASHSCOPE_BASE_URL", "model": "DASHSCOPE_MUSIC_MODEL"}),
        ("suno-compatible", {"api_key": "MUSIC_API_KEY", "base_url": "MUSIC_BASE_URL", "model": "MUSIC_MODEL"}),
    ],
    "voice": [
        ("openai-speech", {"api_key": "VOICE_API_KEY", "base_url": "VOICE_BASE_URL", "model": "VOICE_MODEL"}),
        ("tts-input", {"api_key": "DASHSCOPE_API_KEY", "base_url": "DASHSCOPE_BASE_URL", "model": "DASHSCOPE_TTS_MODEL"}),
        ("tts-group", {"api_key": "MINIMAX_API_KEY", "base_url": "MINIMAX_BASE_URL", "model": "MINIMAX_MODEL", "group_id": "MINIMAX_GROUP_ID"}),
        ("tts-reference", {"api_key": "FISH_API_KEY", "base_url": "FISH_BASE_URL"}),
        ("gemini-tts", {"api_key": "GEMINI_API_KEY", "base_url": "GEMINI_BASE_URL", "model": "GEMINI_TTS_MODEL", "voice": "GEMINI_VOICE", "rate": "GEMINI_TTS_RATE"}),
    ],
}


def _protocol(group: str, protocol_id: str) -> dict[str, Any]:
    for protocol in PROTOCOLS.get(group, []):
        if protocol["id"] == protocol_id:
            return protocol
    raise ValueError("不支持的媒体 API 协议。")


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _valid_endpoint(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    local = host in {"localhost", "127.0.0.1", "::1"}
    return bool(host) and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and (
        parsed.scheme == "https" or (local and parsed.scheme == "http")
    )


def _valid_proxy(value: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return False
    return bool(parsed.hostname) and parsed.scheme in {"http", "https", "socks5"} and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment


def _hostname(value: str) -> str:
    try:
        return urllib.parse.urlsplit(value).hostname or "API Provider"
    except ValueError:
        return "API Provider"


def _safe_proxy(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError:
        return ""
    if not parsed.hostname:
        return ""
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.hostname}{port}"


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def system_proxy_info() -> dict[str, Any]:
    try:
        proxies = urllib.request.getproxies()
    except Exception:
        proxies = {}
    http = _safe_proxy(str(proxies.get("http") or ""))
    https = _safe_proxy(str(proxies.get("https") or ""))
    return {"available": bool(http or https), "http": http, "https": https}


class MediaConnectionStore:
    def __init__(self, path: Path, env_provider: Callable[[], dict[str, str]]):
        self.path = path.resolve()
        self.env_provider = env_provider

    def _empty(self) -> dict[str, Any]:
        return {"schema": 2, "revision": 0, "providers": [], "defaults": {group: None for group in GROUP_LABELS}}

    def _write(self, state: dict[str, Any]) -> None:
        state["revision"] = int(state.get("revision") or 0) + 1
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            state = self._empty()
            self._migrate_env(state)
            if state["providers"]:
                self._write(state)
            return state
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise ValueError("媒体模型配置文件损坏，拒绝覆盖。") from None
        if not isinstance(raw, dict):
            raise ValueError("媒体模型配置格式不支持。")
        if raw.get("schema") == 1:
            backup = self.path.with_name(self.path.stem + ".schema1.backup.json")
            if not backup.exists():
                backup.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            state = self._migrate_v1(raw)
            self._write(state)
            return state
        if raw.get("schema") != 2 or not isinstance(raw.get("providers"), list) or not isinstance(raw.get("defaults"), dict):
            raise ValueError("媒体模型配置格式不支持。")
        for group in GROUP_LABELS:
            raw["defaults"].setdefault(group, None)
        raw.setdefault("revision", 0)
        return raw

    def _assert_revision(self, state: dict[str, Any], expected: Any) -> None:
        if expected is None:
            return
        try:
            value = int(expected)
        except (TypeError, ValueError):
            raise ValueError("配置版本无效，请刷新后重试。") from None
        if value != int(state.get("revision") or 0):
            raise ValueError("媒体模型配置已被其他操作更新，请刷新后重试。")

    def _find_provider(self, state: dict[str, Any], provider_id: str) -> dict[str, Any] | None:
        return next((item for item in state["providers"] if item.get("id") == provider_id), None)

    def _route_models(self, provider: dict[str, Any], group: str) -> list[dict[str, Any]]:
        return [item for item in provider.get("models", []) if item.get("enabled", True) and group in item.get("groups", [])]

    def _route_configured(self, provider: dict[str, Any], group: str) -> bool:
        route = (provider.get("routes") or {}).get(group)
        if not route:
            return False
        try:
            protocol = _protocol(group, str(route.get("protocol") or ""))
        except ValueError:
            return False
        if not _valid_endpoint(str(provider.get("base_url") or "")):
            return False
        credentials = provider.get("credentials") or {}
        if any(not str(credentials.get(key) or "").strip() for key in protocol.get("auth", ())):
            return False
        settings = route.get("settings") or {}
        for field in protocol.get("fields", []):
            if field.get("required") and not str(settings.get(field["key"]) or "").strip():
                return False
        return bool(self._route_models(provider, group))

    def _migrate_v1(self, old: dict[str, Any]) -> dict[str, Any]:
        state = self._empty()
        old_groups = old.get("groups") if isinstance(old.get("groups"), dict) else {}
        old_to_provider: dict[tuple[str, str], tuple[str, str | None]] = {}
        for group in GROUP_LABELS:
            data = old_groups.get(group) if isinstance(old_groups.get(group), dict) else {}
            connections = data.get("connections") if isinstance(data.get("connections"), list) else []
            for conn in connections:
                if not isinstance(conn, dict):
                    continue
                protocol_id = str(conn.get("protocol") or "")
                try:
                    protocol = _protocol(group, protocol_id)
                except ValueError:
                    continue
                values = conn.get("values") if isinstance(conn.get("values"), dict) else {}
                base_url = str(values.get("base_url") or protocol.get("default_base") or "").strip().rstrip("/")
                if not base_url or not _valid_endpoint(base_url):
                    continue
                credentials = {key: str(values.get(key) or "").strip() for key in ("api_key", "access_key", "secret_key") if str(values.get(key) or "").strip()}
                network = {"mode": "direct" if _truthy(values.get("no_proxy")) else "system", "proxy_url": ""}
                signature = (base_url.casefold(), json.dumps(credentials, sort_keys=True))
                provider = next((p for p in state["providers"] if p.get("_signature") == signature), None)
                model_ids = [str(values.get(key) or "").strip() for key in ("model", "t2v_model", "i2v_model") if str(values.get(key) or "").strip()]
                if provider is None:
                    old_name = str(conn.get("name") or "").strip()
                    name = _hostname(base_url) if old_name in model_ids or not old_name else old_name
                    provider = {
                        "id": uuid.uuid4().hex, "name": name[:60], "base_url": base_url,
                        "credentials": credentials, "network": network, "routes": {}, "models": [],
                        "_signature": signature,
                    }
                    state["providers"].append(provider)
                settings = {}
                for field in protocol.get("fields", []):
                    value = str(values.get(field["key"]) or "").strip()
                    if value:
                        settings[field["key"]] = value
                provider["routes"][group] = {"protocol": protocol_id, "settings": settings}
                for model_id in model_ids:
                    model = next((m for m in provider["models"] if m["id"] == model_id), None)
                    if model:
                        if group not in model["groups"]:
                            model["groups"].append(group)
                    else:
                        provider["models"].append({"id": model_id, "name": model_id, "groups": [group], "enabled": True, "source": "migrated", "capabilities": {}})
                primary = model_ids[0] if model_ids else None
                old_to_provider[(group, str(conn.get("id") or ""))] = (provider["id"], primary)
            default_id = str(data.get("default_id") or "")
            binding = old_to_provider.get((group, default_id))
            if binding and binding[1]:
                state["defaults"][group] = {"provider_id": binding[0], "model_id": binding[1]}
        for provider in state["providers"]:
            provider.pop("_signature", None)
        return state

    def _migrate_env(self, state: dict[str, Any]) -> None:
        env = self.env_provider()
        for group, candidates in LEGACY_ENV.items():
            for protocol_id, mapping in candidates:
                protocol = _protocol(group, protocol_id)
                credentials = {}
                for key in protocol.get("auth", ()):
                    env_name = mapping.get(key)
                    value = str(env.get(env_name, "") or "").strip() if env_name else ""
                    if value:
                        credentials[key] = value
                if any(not credentials.get(key) for key in protocol.get("auth", ())):
                    continue
                base_url = str(env.get(mapping.get("base_url", ""), "") or protocol.get("default_base") or "").strip().rstrip("/")
                if not base_url or not _valid_endpoint(base_url):
                    continue
                model_ids = []
                for key in ("model", "model2"):
                    env_name = mapping.get(key)
                    value = str(env.get(env_name, "") or "").strip() if env_name else ""
                    if value and value not in model_ids:
                        model_ids.append(value)
                settings = {}
                for field in protocol.get("fields", []):
                    env_name = mapping.get(field["key"])
                    value = str(env.get(env_name, "") or "").strip() if env_name else ""
                    if value:
                        settings[field["key"]] = value
                direct_name = mapping.get("direct")
                network = {"mode": "direct" if direct_name and _truthy(env.get(direct_name)) else "system", "proxy_url": ""}
                provider = {
                    "id": uuid.uuid4().hex, "name": _hostname(base_url)[:60], "base_url": base_url,
                    "credentials": credentials, "network": network,
                    "routes": {group: {"protocol": protocol_id, "settings": settings}},
                    "models": [{"id": model, "name": model, "groups": [group], "enabled": True, "source": "migrated", "capabilities": {}} for model in model_ids],
                }
                state["providers"].append(provider)
                if model_ids and not state["defaults"].get(group):
                    state["defaults"][group] = {"provider_id": provider["id"], "model_id": model_ids[0]}
                break

    def _public_provider(self, state: dict[str, Any], group: str, provider: dict[str, Any]) -> dict[str, Any]:
        route = provider["routes"][group]
        protocol = _protocol(group, route["protocol"])
        binding = state["defaults"].get(group) or {}
        network = provider.get("network") or {"mode": "system", "proxy_url": ""}
        return {
            "id": provider["id"], "name": provider["name"], "base_url": provider["base_url"],
            "protocol": route["protocol"], "protocol_label": protocol["label"],
            "configured": self._route_configured(provider, group),
            "is_default": binding.get("provider_id") == provider["id"],
            "default_model": binding.get("model_id") if binding.get("provider_id") == provider["id"] else None,
            "network": {"mode": network.get("mode", "system"), "proxy_url": _safe_proxy(str(network.get("proxy_url") or ""))},
            "secrets": {key: bool((provider.get("credentials") or {}).get(key)) for key in ("api_key", "access_key", "secret_key")},
            "route_settings": deepcopy(route.get("settings") or {}),
            "models": [deepcopy(model) for model in self._route_models(provider, group)],
        }

    def public_state(self) -> dict[str, Any]:
        state = self._read()
        groups = []
        for group, label in GROUP_LABELS.items():
            providers = [self._public_provider(state, group, item) for item in state["providers"] if group in (item.get("routes") or {})]
            groups.append({
                "group": group, "label": label, "configured": any(item["configured"] for item in providers),
                "default": deepcopy(state["defaults"].get(group)),
                "protocols": [{
                    "id": protocol["id"], "label": protocol["label"], "discoverable": bool(protocol.get("discover")),
                    "auth": list(protocol.get("auth", ())), "fields": deepcopy(protocol.get("fields", [])),
                    "default_base": protocol.get("default_base", ""),
                } for protocol in PROTOCOLS[group]],
                "providers": providers,
            })
        reusable = [{
            "id": item["id"], "name": item["name"], "base_url": item["base_url"],
            "groups": sorted((item.get("routes") or {}).keys()),
            "network": {"mode": (item.get("network") or {}).get("mode", "system"), "proxy_url": _safe_proxy(str((item.get("network") or {}).get("proxy_url") or ""))},
            "secrets": {key: bool((item.get("credentials") or {}).get(key)) for key in ("api_key", "access_key", "secret_key")},
        } for item in state["providers"]]
        return {"schema": 2, "revision": int(state.get("revision") or 0), "groups": groups, "providers": reusable, "system_proxy": system_proxy_info()}

    def _merge_credentials(self, existing: dict[str, Any] | None, payload: dict[str, Any], protocol: dict[str, Any], base_url: str) -> dict[str, Any]:
        previous = deepcopy((existing or {}).get("credentials") or {})
        old_host = _hostname(str((existing or {}).get("base_url") or "")).casefold()
        new_host = _hostname(base_url).casefold()
        changed_host = bool(existing and old_host and new_host and old_host != new_host)
        credentials = previous
        fresh_secret = False
        for key in ("api_key", "access_key", "secret_key"):
            if key in payload:
                value = str(payload.get(key) or "").strip()
                if value:
                    credentials[key] = value
                    fresh_secret = True
        if changed_host and any(previous.values()) and not fresh_secret:
            raise ValueError("API 域名已变化。为避免把旧密钥发送到新地址，请重新填写密钥。")
        if any(not str(credentials.get(key) or "").strip() for key in protocol.get("auth", ())):
            # Incomplete Provider may be saved, but it cannot become a default until complete.
            pass
        return credentials

    def upsert_provider(self, group: str, payload: dict[str, Any]) -> dict[str, Any]:
        if group not in GROUP_LABELS:
            raise ValueError("未知媒体模型组。")
        state = self._read()
        self._assert_revision(state, payload.get("expected_revision"))
        provider_id = str(payload.get("provider_id") or "").strip()
        existing = self._find_provider(state, provider_id) if provider_id else None
        if provider_id and not existing:
            raise ValueError("API Provider 不存在。")
        if not existing and len(state["providers"]) >= MAX_PROVIDERS:
            raise ValueError(f"最多保存 {MAX_PROVIDERS} 个 API Provider。")
        protocol_id = str(payload.get("protocol") or ((existing or {}).get("routes") or {}).get(group, {}).get("protocol") or "").strip()
        protocol = _protocol(group, protocol_id)
        name = str(payload.get("name") or (existing or {}).get("name") or "").strip()
        if not name or len(name) > 60:
            raise ValueError("Provider 名称不能为空且不能超过 60 字符。")
        if any(item.get("id") != provider_id and str(item.get("name") or "").casefold() == name.casefold() for item in state["providers"]):
            raise ValueError("Provider 名称不能重复。")
        base_url = str(payload.get("base_url") or (existing or {}).get("base_url") or protocol.get("default_base") or "").strip().rstrip("/")
        if not base_url or not _valid_endpoint(base_url):
            raise ValueError("API 根地址必须使用 HTTPS；只有本机 loopback 可以使用 HTTP，且不能包含凭据、query 或 fragment。")
        credentials = self._merge_credentials(existing, payload, protocol, base_url)
        network_mode = str(payload.get("network_mode") or ((existing or {}).get("network") or {}).get("mode") or "system").strip()
        if network_mode not in {"system", "direct", "custom"}:
            raise ValueError("网络路径必须是系统代理、直连或自定义代理。")
        proxy_url = str(payload.get("proxy_url") or ((existing or {}).get("network") or {}).get("proxy_url") or "").strip()
        if network_mode == "custom":
            if not proxy_url or not _valid_proxy(proxy_url):
                raise ValueError("自定义代理当前支持不含凭据的 HTTP/HTTPS/SOCKS5 代理地址，且不能包含 query 或 fragment。")
        else:
            proxy_url = ""
        route_values = payload.get("route_settings") if isinstance(payload.get("route_settings"), dict) else {}
        previous_route = ((existing or {}).get("routes") or {}).get(group) or {}
        previous_settings = previous_route.get("settings") if previous_route.get("protocol") == protocol_id else {}
        settings = {}
        allowed_fields = {field["key"]: field for field in protocol.get("fields", [])}
        if set(route_values) - set(allowed_fields):
            raise ValueError("包含当前协议不支持的高级配置项。")
        for key, field in allowed_fields.items():
            value = str(route_values.get(key, previous_settings.get(key, "")) or "").strip()
            if len(value) > 2048:
                raise ValueError(f"{field['label']} 内容过长。")
            if key.endswith("_base") and value and not _valid_endpoint(value):
                raise ValueError(f"{field['label']} 地址无效。")
            if value:
                settings[key] = value
        raw_models = payload.get("models") if isinstance(payload.get("models"), list) else []
        model_ids: list[str] = []
        for item in raw_models:
            model_id = str(item.get("id") if isinstance(item, dict) else item or "").strip()
            if not model_id or len(model_id) > 200 or any(ord(ch) < 32 for ch in model_id):
                raise ValueError("模型 ID 不能为空、不能超过 200 字符，且不能包含控制字符。")
            if model_id not in model_ids:
                model_ids.append(model_id)
        if len(model_ids) > MAX_MODELS_PER_PROVIDER:
            raise ValueError(f"每个 Provider 最多启用 {MAX_MODELS_PER_PROVIDER} 个模型。")
        provider = deepcopy(existing) if existing else {
            "id": uuid.uuid4().hex, "name": name, "base_url": base_url, "credentials": {},
            "network": {"mode": "system", "proxy_url": ""}, "routes": {}, "models": [],
        }
        provider["name"] = name
        provider["base_url"] = base_url
        provider["credentials"] = credentials
        provider["network"] = {"mode": network_mode, "proxy_url": proxy_url}
        provider.setdefault("routes", {})[group] = {"protocol": protocol_id, "settings": settings}
        current_models = deepcopy(provider.get("models") or [])
        for model in current_models:
            groups = [value for value in model.get("groups", []) if value != group]
            model["groups"] = groups
        for model_id in model_ids:
            model = next((item for item in current_models if item.get("id") == model_id), None)
            if model:
                if group not in model["groups"]:
                    model["groups"].append(group)
                model["enabled"] = True
            else:
                current_models.append({"id": model_id, "name": model_id, "groups": [group], "enabled": True, "source": "manual", "capabilities": {}})
        provider["models"] = [item for item in current_models if item.get("groups")]
        if existing:
            state["providers"][state["providers"].index(existing)] = provider
        else:
            state["providers"].append(provider)
        default_model = str(payload.get("default_model") or "").strip()
        if default_model and default_model not in model_ids:
            raise ValueError("默认模型必须属于当前 Provider 已启用的模型。")
        configured = self._route_configured(provider, group)
        if payload.get("set_default"):
            if not configured:
                raise ValueError("Provider 配置尚未完整，不能设为默认。")
            chosen = default_model or model_ids[0]
            state["defaults"][group] = {"provider_id": provider["id"], "model_id": chosen}
        else:
            binding = state["defaults"].get(group) or {}
            if binding.get("provider_id") == provider["id"] and binding.get("model_id") not in model_ids:
                state["defaults"][group] = {"provider_id": provider["id"], "model_id": (default_model or (model_ids[0] if model_ids else ""))} if configured and model_ids else None
            elif not state["defaults"].get(group) and configured and model_ids:
                state["defaults"][group] = {"provider_id": provider["id"], "model_id": default_model or model_ids[0]}
        self._write(state)
        return self.public_state()

    def detach_provider(self, group: str, provider_id: str, expected_revision: Any = None) -> dict[str, Any]:
        state = self._read()
        self._assert_revision(state, expected_revision)
        provider = self._find_provider(state, provider_id)
        if not provider or group not in (provider.get("routes") or {}):
            raise ValueError("API Provider 不存在于当前媒体类型。")
        provider["routes"].pop(group, None)
        for model in provider.get("models", []):
            model["groups"] = [item for item in model.get("groups", []) if item != group]
        provider["models"] = [item for item in provider.get("models", []) if item.get("groups")]
        if (state["defaults"].get(group) or {}).get("provider_id") == provider_id:
            state["defaults"][group] = None
        if not provider["routes"]:
            state["providers"].remove(provider)
        self._write(state)
        return self.public_state()

    def set_default(self, group: str, provider_id: str, model_id: str, expected_revision: Any = None) -> dict[str, Any]:
        state = self._read()
        self._assert_revision(state, expected_revision)
        provider = self._find_provider(state, provider_id)
        if not provider or group not in (provider.get("routes") or {}) or not self._route_configured(provider, group):
            raise ValueError("该 Provider 尚未完整配置，不能设为默认。")
        if model_id not in {item["id"] for item in self._route_models(provider, group)}:
            raise ValueError("默认模型不属于该 Provider。")
        state["defaults"][group] = {"provider_id": provider_id, "model_id": model_id}
        self._write(state)
        return self.public_state()

    def resolve_selection(self, group: str, provider_id: str | None = None, model_id: str | None = None) -> dict[str, Any]:
        state = self._read()
        providers = [item for item in state["providers"] if group in (item.get("routes") or {}) and self._route_configured(item, group)]
        provider = None
        if provider_id:
            provider = next((item for item in providers if item["id"] == provider_id), None)
            if not provider:
                raise ValueError("指定的媒体 Provider 不存在、未启用或配置不完整。")
        else:
            binding = state["defaults"].get(group) or {}
            provider = next((item for item in providers if item["id"] == binding.get("provider_id")), None)
            if not provider and model_id:
                matches = [item for item in providers if model_id in {m["id"] for m in self._route_models(item, group)}]
                if len(matches) == 1:
                    provider = matches[0]
                elif len(matches) > 1:
                    raise ValueError("多个 Provider 都包含该模型，请同时指定 Provider。")
            provider = provider or (providers[0] if providers else None)
        if not provider:
            raise ValueError("当前媒体类型没有可用的 Provider。")
        models = self._route_models(provider, group)
        chosen = None
        if model_id:
            chosen = next((item for item in models if item["id"] == model_id), None)
            if not chosen:
                raise ValueError("指定模型不属于当前 Provider。")
        else:
            binding = state["defaults"].get(group) or {}
            if binding.get("provider_id") == provider["id"]:
                chosen = next((item for item in models if item["id"] == binding.get("model_id")), None)
            chosen = chosen or (models[0] if models else None)
        if not chosen:
            raise ValueError("当前 Provider 没有启用的模型。")
        return {"provider": deepcopy(provider), "route": deepcopy(provider["routes"][group]), "model": deepcopy(chosen)}

    def env_for(self, group: str, provider_id: str | None = None, model_id: str | None = None) -> dict[str, str]:
        selected = self.resolve_selection(group, provider_id, model_id)
        provider, route, model = selected["provider"], selected["route"], selected["model"]
        protocol = _protocol(group, route["protocol"])
        env: dict[str, str] = {"RIPPLE_MANAGED_MEDIA": "1", "RIPPLE_MEDIA_NETWORK": str((provider.get("network") or {}).get("mode") or "system")}
        credentials = provider.get("credentials") or {}
        settings = route.get("settings") or {}
        mapping = protocol.get("env", {})
        for key in protocol.get("auth", ()):
            if mapping.get(key) and credentials.get(key):
                env[mapping[key]] = str(credentials[key])
        if mapping.get("base_url"):
            env[mapping["base_url"]] = provider["base_url"]
        if mapping.get("model"):
            env[mapping["model"]] = model["id"]
        for key, value in settings.items():
            if mapping.get(key) and value:
                env[mapping[key]] = str(value)
        if group in {"video", "music", "voice"}:
            env[{"video": "VIDEO_PROVIDER", "music": "MUSIC_PROVIDER", "voice": "VOICE_PROVIDER"}[group]] = protocol["adapter"]
        network = provider.get("network") or {}
        mode = network.get("mode") or "system"
        if mode == "direct":
            env["RIPPLE_MEDIA_DIRECT"] = "1"
        elif mode == "custom":
            proxy = str(network.get("proxy_url") or "")
            env["RIPPLE_MEDIA_PROXY"] = proxy
            env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "http_proxy": proxy, "https_proxy": proxy})
        return env

    def protocol(self, group: str, protocol_id: str) -> dict[str, Any]:
        return deepcopy(_protocol(group, protocol_id))

    def capabilities(self, group: str) -> dict[str, Any]:
        state = self._read()
        binding = state["defaults"].get(group) or {}
        providers = []
        for provider in state["providers"]:
            if group not in (provider.get("routes") or {}):
                continue
            route = provider["routes"][group]
            models = self._route_models(provider, group)
            providers.append({
                "id": provider["id"], "name": provider["name"], "protocol": route["protocol"],
                "configured": self._route_configured(provider, group),
                "models": [{"id": model["id"], "name": model.get("name") or model["id"], "capabilities": deepcopy(model.get("capabilities") or {})} for model in models],
            })
        return {
            "available": any(item["configured"] for item in providers),
            "default_provider_id": binding.get("provider_id"), "default_model": binding.get("model_id"),
            "providers": providers,
        }

    def _discovery_context(self, group: str, payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        state = self._read()
        provider_id = str(payload.get("provider_id") or "").strip()
        existing = self._find_provider(state, provider_id) if provider_id else None
        protocol_id = str(payload.get("protocol") or ((existing or {}).get("routes") or {}).get(group, {}).get("protocol") or "").strip()
        protocol = _protocol(group, protocol_id)
        base_url = str(payload.get("base_url") or (existing or {}).get("base_url") or protocol.get("default_base") or "").strip().rstrip("/")
        if not base_url or not _valid_endpoint(base_url):
            raise ValueError("请先填写有效的 API 根地址。")
        credentials = self._merge_credentials(existing, payload, protocol, base_url)
        if any(not str(credentials.get(key) or "").strip() for key in protocol.get("auth", ())):
            raise ValueError("请先填写当前协议所需的 API 凭据。")
        mode = str(payload.get("network_mode") or ((existing or {}).get("network") or {}).get("mode") or "system")
        proxy_url = str(payload.get("proxy_url") or ((existing or {}).get("network") or {}).get("proxy_url") or "").strip()
        if mode not in {"system", "direct", "custom"}:
            raise ValueError("网络路径无效。")
        if mode == "custom" and (not proxy_url or not _valid_proxy(proxy_url)):
            raise ValueError("自定义代理当前支持不含凭据的 HTTP/HTTPS/SOCKS5 代理地址。")
        return protocol, {"base_url": base_url, "credentials": credentials, "network": {"mode": mode, "proxy_url": proxy_url}}

    def discover_models(self, group: str, payload: dict[str, Any]) -> dict[str, Any]:
        protocol, context = self._discovery_context(group, payload)
        if protocol.get("discover") != "openai-models":
            return {"supported": False, "models": [], "message": "当前协议没有标准模型列表接口，可手动添加模型 ID。"}
        api_key = str(context["credentials"].get("api_key") or "")
        endpoint = context["base_url"].rstrip("/") + "/models"
        headers = {"Authorization": f"Bearer {api_key}", "User-Agent": "Ripple-model-discovery/1"}
        network = context["network"]
        try:
            if network["mode"] == "custom":
                # Explicit proxy routing must not inherit NO_PROXY or silently fall back to direct.
                with httpx.Client(proxy=network["proxy_url"], trust_env=False, timeout=15, follow_redirects=False) as client:
                    with client.stream("GET", endpoint, headers=headers) as response:
                        status = response.status_code
                        if status in {401, 403}:
                            raise ValueError("模型列表鉴权失败，请检查 API Key。")
                        if status == 404:
                            return {"supported": False, "models": [], "message": "该服务没有标准 /models 接口，可手动添加模型 ID。"}
                        if not 200 <= status < 300:
                            raise ValueError(f"模型列表请求失败（HTTP {status}）。")
                        chunks = []
                        total = 0
                        for chunk in response.iter_bytes():
                            total += len(chunk)
                            if total > MAX_DISCOVERY_BODY:
                                raise ValueError("模型列表响应过大，已拒绝处理。")
                            chunks.append(chunk)
                        raw = b"".join(chunks)
            else:
                request = urllib.request.Request(endpoint, headers=headers, method="GET")
                handlers: list[Any] = [_NoRedirectHandler()]
                if network["mode"] == "direct":
                    handlers.insert(0, urllib.request.ProxyHandler({}))
                opener = urllib.request.build_opener(*handlers)
                with opener.open(request, timeout=15) as response:
                    raw = response.read(MAX_DISCOVERY_BODY + 1)
        except ValueError:
            raise
        except urllib.error.HTTPError as exc:
            if exc.code in {401, 403}:
                raise ValueError("模型列表鉴权失败，请检查 API Key。") from None
            if exc.code == 404:
                return {"supported": False, "models": [], "message": "该服务没有标准 /models 接口，可手动添加模型 ID。"}
            raise ValueError(f"模型列表请求失败（HTTP {exc.code}）。") from None
        except (urllib.error.URLError, httpx.HTTPError, TimeoutError, OSError):
            raise ValueError("模型列表请求失败或超时，请检查网络路径与 API 地址。") from None
        if len(raw) > MAX_DISCOVERY_BODY:
            raise ValueError("模型列表响应过大，已拒绝处理。")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("模型列表接口没有返回有效 JSON。") from None
        items: Any
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            items = data["data"]
        elif isinstance(data, dict) and isinstance(data.get("models"), list):
            items = data["models"]
        elif isinstance(data, list):
            items = data
        else:
            raise ValueError("模型列表响应格式无法识别。")
        models: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in items:
            model_id = str(item.get("id") or item.get("name") or "").strip() if isinstance(item, dict) else str(item or "").strip()
            if not model_id or len(model_id) > 200 or model_id in seen or any(ord(ch) < 32 for ch in model_id):
                continue
            seen.add(model_id)
            models.append({"id": model_id, "name": model_id})
            if len(models) >= MAX_DISCOVERED_MODELS:
                break
        return {"supported": True, "models": models, "count": len(models)}
