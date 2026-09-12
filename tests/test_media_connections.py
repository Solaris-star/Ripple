from __future__ import annotations

import json
from pathlib import Path
import urllib.error

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from ripple.media_connections import MediaConnectionStore
from ripple.media_generation import MediaGenerationService
from skills.shared.scripts.model_registry import model_group


def _group(state: dict, name: str) -> dict:
    return next(item for item in state["groups"] if item["group"] == name)


def _provider_payload(**overrides):
    payload = {
        "name": "m365", "protocol": "openai-images", "base_url": "https://images.example/v1",
        "api_key": "secret-a", "network_mode": "system", "models": ["image-a", "image-b"],
        "default_model": "image-a", "set_default": True,
    }
    payload.update(overrides)
    return payload


def test_schema1_migrates_to_provider_models_and_direct_network(tmp_path):
    secret = "legacy-secret"
    path = tmp_path / "media-models.json"
    path.write_text(json.dumps({
        "schema": 1,
        "groups": {
            "image": {
                "default_id": "old-image",
                "connections": [{
                    "id": "old-image", "name": "gpt-image-2", "protocol": "openai-images",
                    "values": {"api_key": secret, "base_url": "https://m365.example/v1", "model": "gpt-image-2", "no_proxy": True},
                }],
            }
        },
    }), encoding="utf-8")
    store = MediaConnectionStore(path, lambda: {})

    state = store.public_state()
    image = _group(state, "image")
    assert state["schema"] == 2 and image["configured"] is True
    assert len(image["providers"]) == 1
    provider = image["providers"][0]
    assert provider["name"] == "m365.example"
    assert provider["network"]["mode"] == "direct"
    assert [item["id"] for item in provider["models"]] == ["gpt-image-2"]
    assert image["default"]["model_id"] == "gpt-image-2"
    assert provider["secrets"]["api_key"] is True
    assert secret not in json.dumps(state)
    assert path.with_name("media-models.schema1.backup.json").is_file()


def test_legacy_env_migrates_once_without_exposing_secret(tmp_path):
    secret = "env-secret"
    env = {"IMG_API_KEY": secret, "IMG_BASE_URL": "https://image.example/v1", "IMG_MODEL": "image-env", "IMG_NO_PROXY": "1"}
    store = MediaConnectionStore(tmp_path / "media-models.json", lambda: dict(env))
    state = store.public_state()
    provider = _group(state, "image")["providers"][0]
    assert provider["network"]["mode"] == "direct"
    assert provider["models"][0]["id"] == "image-env"
    assert secret not in json.dumps(state)


def test_one_provider_can_serve_multiple_groups_and_models(tmp_path):
    store = MediaConnectionStore(tmp_path / "media-models.json", lambda: {})
    first = store.upsert_provider("image", _provider_payload())
    image = _group(first, "image")
    provider_id = image["providers"][0]["id"]
    revision = first["revision"]

    second = store.upsert_provider("video", {
        "provider_id": provider_id, "expected_revision": revision,
        "name": "m365", "protocol": "openai-videos", "base_url": "https://images.example/v1",
        "api_key": "", "network_mode": "system", "models": ["video-a", "video-b"],
        "default_model": "video-b", "set_default": True,
    })
    assert len(second["providers"]) == 1
    assert _group(second, "image")["providers"][0]["id"] == provider_id
    assert _group(second, "video")["providers"][0]["id"] == provider_id
    assert _group(second, "video")["default"] == {"provider_id": provider_id, "model_id": "video-b"}
    assert store.env_for("image", provider_id, "image-b")["IMG_MODEL"] == "image-b"
    assert store.env_for("video", provider_id, "video-a")["VIDEO_MODEL"] == "video-a"
    assert store.env_for("video", provider_id, "video-a")["VIDEO_API_KEY"] == "secret-a"


def test_default_binding_is_provider_plus_model_and_revision_is_optimistic(tmp_path):
    store = MediaConnectionStore(tmp_path / "media-models.json", lambda: {})
    state = store.upsert_provider("image", _provider_payload(set_default=False))
    provider = _group(state, "image")["providers"][0]
    changed = store.set_default("image", provider["id"], "image-b", state["revision"])
    assert _group(changed, "image")["default"] == {"provider_id": provider["id"], "model_id": "image-b"}
    with pytest.raises(ValueError, match="刷新"):
        store.set_default("image", provider["id"], "image-a", state["revision"])


def test_editing_provider_host_requires_new_secret(tmp_path):
    store = MediaConnectionStore(tmp_path / "media-models.json", lambda: {})
    state = store.upsert_provider("image", _provider_payload())
    provider = _group(state, "image")["providers"][0]
    with pytest.raises(ValueError, match="重新填写密钥"):
        store.upsert_provider("image", {
            "provider_id": provider["id"], "expected_revision": state["revision"], "name": "m365",
            "protocol": "openai-images", "base_url": "https://other.example/v1", "api_key": "",
            "network_mode": "system", "models": ["image-a"],
        })


def test_network_modes_are_provider_scoped(tmp_path):
    store = MediaConnectionStore(tmp_path / "media-models.json", lambda: {})
    direct = store.upsert_provider("image", _provider_payload(network_mode="direct"))
    provider_id = _group(direct, "image")["providers"][0]["id"]
    env = store.env_for("image", provider_id, "image-a")
    assert env["RIPPLE_MANAGED_MEDIA"] == "1" and env["RIPPLE_MEDIA_DIRECT"] == "1"
    assert "HTTP_PROXY" not in env

    store2 = MediaConnectionStore(tmp_path / "custom.json", lambda: {})
    custom = store2.upsert_provider("image", _provider_payload(network_mode="custom", proxy_url="http://127.0.0.1:7890"))
    pid = _group(custom, "image")["providers"][0]["id"]
    env2 = store2.env_for("image", pid, "image-a")
    assert env2["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert env2["HTTPS_PROXY"] == "http://127.0.0.1:7890"

    store3 = MediaConnectionStore(tmp_path / "socks.json", lambda: {})
    socks = store3.upsert_provider("image", _provider_payload(network_mode="custom", proxy_url="socks5://127.0.0.1:1080"))
    socks_id = _group(socks, "image")["providers"][0]["id"]
    env3 = store3.env_for("image", socks_id, "image-a")
    assert env3["RIPPLE_MEDIA_PROXY"] == "socks5://127.0.0.1:1080"
    assert env3["HTTPS_PROXY"] == "socks5://127.0.0.1:1080"


class _Response:
    def __init__(self, payload: bytes): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *_args): return False
    def read(self, _limit=None): return self.payload


def test_model_discovery_uses_saved_secret_and_returns_sanitized_models(tmp_path, monkeypatch):
    store = MediaConnectionStore(tmp_path / "media-models.json", lambda: {})
    state = store.upsert_provider("image", _provider_payload())
    provider = _group(state, "image")["providers"][0]
    seen = {}

    class FakeOpener:
        def open(self, request, timeout=0):
            seen["auth"] = request.headers.get("Authorization")
            seen["url"] = request.full_url
            return _Response(json.dumps({"data": [{"id": "gpt-image-2"}, {"id": "gpt-image-1"}, {"id": "gpt-image-2"}]}).encode())

    monkeypatch.setattr("ripple.media_connections.urllib.request.build_opener", lambda *_handlers: FakeOpener())
    result = store.discover_models("image", {
        "provider_id": provider["id"], "protocol": "openai-images", "base_url": provider["base_url"],
        "api_key": "", "network_mode": "system", "proxy_url": "",
    })
    assert result["supported"] is True
    assert [item["id"] for item in result["models"]] == ["gpt-image-2", "gpt-image-1"]
    assert seen["auth"] == "Bearer secret-a" and seen["url"].endswith("/v1/models")
    assert "secret-a" not in json.dumps(result)


def test_nonstandard_protocol_allows_manual_models(tmp_path):
    store = MediaConnectionStore(tmp_path / "media-models.json", lambda: {})
    result = store.discover_models("video", {
        "protocol": "async-content-video", "base_url": "https://video.example/v1",
        "api_key": "secret", "network_mode": "direct", "proxy_url": "",
    })
    assert result["supported"] is False and result["models"] == []


def test_custom_proxy_discovery_is_explicit_and_ignores_environment(monkeypatch, tmp_path):
    store = MediaConnectionStore(tmp_path / "media-models.json", lambda: {})
    seen = {}

    class Stream:
        status_code = 200
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def iter_bytes(self): yield b'{"data":[{"id":"image-proxy"}]}'

    class Client:
        def __init__(self, *, proxy, trust_env, timeout, follow_redirects):
            seen.update(proxy=proxy, trust_env=trust_env, timeout=timeout, follow_redirects=follow_redirects)
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def stream(self, method, url, headers):
            seen.update(method=method, url=url, auth=headers.get("Authorization"))
            return Stream()

    monkeypatch.setattr("ripple.media_connections.httpx.Client", Client)
    result = store.discover_models("image", {
        "protocol": "openai-images", "base_url": "https://image.example/v1", "api_key": "secret",
        "network_mode": "custom", "proxy_url": "http://127.0.0.1:7890",
    })
    assert result["models"] == [{"id": "image-proxy", "name": "image-proxy"}]
    assert seen["proxy"] == "http://127.0.0.1:7890"
    assert seen["trust_env"] is False and seen["follow_redirects"] is False
    assert seen["auth"] == "Bearer secret"


def test_media_provider_http_api_is_sanitized_and_supports_multi_model_default(tmp_path, monkeypatch):
    from web import app as webapp

    store = MediaConnectionStore(tmp_path / "media-models.json", lambda: {})
    outputs = tmp_path / "outputs"; outputs.mkdir()
    service = MediaGenerationService(tmp_path, outputs, lambda: {}, model_group, store)
    monkeypatch.setattr(webapp, "_MEDIA_CONNECTIONS", store)
    monkeypatch.setattr(webapp, "_MEDIA_GENERATION", service)

    with TestClient(webapp.app, base_url="http://localhost") as client:
        initial = client.get("/api/media-model-config").json()
        created = client.post("/api/media-model-config/image/providers", json={
            "expected_revision": initial["revision"], "name": "主力图像", "protocol": "openai-images",
            "base_url": "https://image.example/v1", "api_key": "http-secret", "network_mode": "direct",
            "models": ["image-a", "image-b"], "default_model": "image-b", "set_default": True,
        })
        assert created.status_code == 200
        body = created.json(); image = _group(body, "image"); provider = image["providers"][0]
        assert image["default"] == {"provider_id": provider["id"], "model_id": "image-b"}
        assert provider["secrets"]["api_key"] is True and "http-secret" not in created.text

        switched = client.post("/api/media-model-config/image/default", json={
            "expected_revision": body["revision"], "provider_id": provider["id"], "model_id": "image-a",
        })
        assert switched.status_code == 200
        assert _group(switched.json(), "image")["default"]["model_id"] == "image-a"

        removed = client.request("DELETE", f"/api/media-model-config/image/providers/{provider['id']}", json={"expected_revision": switched.json()["revision"]})
        assert removed.status_code == 200 and _group(removed.json(), "image")["providers"] == []
        assert "http-secret" not in removed.text


def test_media_skill_status_uses_v2_provider_models(monkeypatch, tmp_path):
    from web import app as webapp

    empty = MediaConnectionStore(tmp_path / "empty.json", lambda: {})
    monkeypatch.setattr(webapp, "_MEDIA_CONNECTIONS", empty)
    assert webapp._skill_api_configured("ai-image-gen", {}) is False

    ready = MediaConnectionStore(tmp_path / "ready.json", lambda: {})
    ready.upsert_provider("image", _provider_payload())
    monkeypatch.setattr(webapp, "_MEDIA_CONNECTIONS", ready)
    assert webapp._skill_api_configured("ai-image-gen", {}) is True
    assert webapp._skill_api_configured("short-drama", {}) is True


def test_generation_service_uses_explicit_provider_model_and_excludes_stale_secrets(tmp_path, monkeypatch):
    root = tmp_path / "app"; outputs = root / "outputs"; outputs.mkdir(parents=True)
    store = MediaConnectionStore(tmp_path / "media-models.json", lambda: {})
    image_state = store.upsert_provider("image", _provider_payload(name="图片服务", models=["image-a", "image-b"]))
    image_provider = _group(image_state, "image")["providers"][0]
    video_state = store.upsert_provider("video", {
        "name": "视频服务", "protocol": "async-content-video", "base_url": "https://video.example/v1",
        "api_key": "video-secret", "network_mode": "direct", "models": ["video-a"], "set_default": True,
    })
    video_provider = _group(video_state, "video")["providers"][0]
    service = MediaGenerationService(root, outputs, lambda: {"VIDEO_API_KEY": "stale-secret", "HTTP_PROXY": "http://old-proxy:8080", "ALL_PROXY": "http://all-proxy:8080", "NO_PROXY": "video.example"}, model_group, store)
    captured = []

    def fake_run(group, script, args, *, timeout, provider_id=None, model_id=None):
        captured.append((group, list(args), provider_id, model_id, service._child_env(group, provider_id, model_id)))
        output = Path(args[args.index("--output") + 1]); output.parent.mkdir(parents=True, exist_ok=True)
        if group == "image": Image.new("RGB", (20, 20)).save(output)
        else: output.write_bytes(b"video")

    monkeypatch.setattr(service, "_run", fake_run)
    image = service.generate_image("test", resolution="1k", provider_id=image_provider["id"], model="image-b")
    video = service.generate_video("test", provider_id=video_provider["id"], model="video-a")
    assert image["provider_id"] == image_provider["id"] and image["model"] == "image-b"
    assert video["provider_id"] == video_provider["id"] and video["model"] == "video-a"
    video_call = captured[1]
    assert video_call[1][video_call[1].index("--provider") + 1] == "ark"
    assert video_call[4]["ARK_API_KEY"] == "video-secret"
    assert "VIDEO_API_KEY" not in video_call[4]
    assert "HTTP_PROXY" not in video_call[4]
    assert "ALL_PROXY" not in video_call[4]
    assert "NO_PROXY" not in video_call[4]
    capability_blob = json.dumps(service.capabilities(), ensure_ascii=False)
    assert "video-secret" not in capability_blob and "https://video.example" not in capability_blob
