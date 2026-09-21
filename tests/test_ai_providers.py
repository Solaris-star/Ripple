from __future__ import annotations

import json

import pytest

from ripple.ai_providers import AIProviderService
from ripple.publishing import WorkflowError


def fake_protect(data: bytes, decrypt: bool = False) -> bytes:
    prefix = b"protected:"
    if decrypt:
        assert data.startswith(prefix)
        return data[len(prefix):]
    return prefix + data


def test_legacy_model_config_migrates_to_provider_routes_without_leaking_key(tmp_path, monkeypatch):
    from ripple import ai_providers
    monkeypatch.setattr(ai_providers, "protect", fake_protect)
    service = AIProviderService(tmp_path / "private")
    service.ensure_legacy(
        enabled=True,
        base_url="https://gateway.example/v1",
        api_key="secret-fixture",
        models=[{"id": "deepseek-v4-flash", "name": "DeepSeek", "effort_levels": ["auto", "high"],
                 "supports_reasoning": True, "source": "discovered"}],
        default_model="deepseek-v4-flash",
    )
    public = service.public_state()
    assert len(public["providers"]) == 1
    provider = public["providers"][0]
    assert provider["api_key_set"] is True
    assert provider["models"][0]["effort_levels"] == ["auto", "high"]
    assert provider["models"][0]["supports_reasoning"] is True
    assert provider["models"][0]["source"] == "discovered"
    assert public["routes"]["default_agent"] == {"provider_id": provider["id"], "model_id": "deepseek-v4-flash"}
    assert public["routes"]["idea_generation"] == {"provider_id": provider["id"], "model_id": "deepseek-v4-flash"}
    assert "secret-fixture" not in json.dumps(public)
    assert "secret-fixture" not in service.path.read_text(encoding="utf-8")
    assert service.resolved("idea_generation")["api_key"] == "secret-fixture"


def test_multiple_ai_providers_and_purpose_routes_are_independent(tmp_path, monkeypatch):
    from ripple import ai_providers
    monkeypatch.setattr(ai_providers, "protect", fake_protect)
    service = AIProviderService(tmp_path / "private")
    one = service.upsert(
        name="Default Gateway", kind="openai-compatible", base_url="https://one.example/v1",
        api_key="one-secret", models=["model-a"], default_model="model-a",
    )
    one_id = one["providers"][0]["id"]
    two = service.upsert(
        name="xAI", kind="xai", base_url="https://api.x.ai/v1",
        api_key="xai-secret", models=["grok-4.6"], default_model="grok-4.6",
    )
    xai = next(row for row in two["providers"] if row["name"] == "xAI")
    service.set_route("default_agent", one_id, "model-a")
    state = service.set_route("x_campaign_discovery", xai["id"], "grok-4.6")
    assert state["routes"]["default_agent"]["provider_id"] == one_id
    assert state["routes"]["x_campaign_discovery"]["provider_id"] == xai["id"]
    assert service.resolved("research")["provider_id"] == one_id
    assert service.resolved("x_campaign_discovery", fallback_to_default=False)["provider_id"] == xai["id"]


def test_openai_compatible_grok_gateway_can_verify_and_use_x_search(tmp_path, monkeypatch):
    from ripple import ai_providers
    monkeypatch.setattr(ai_providers, "protect", fake_protect)
    service = AIProviderService(tmp_path / "private")
    state = service.upsert(
        name="Generic Grok Gateway", kind="openai-compatible", base_url="https://gateway.example/v1",
        api_key="secret", models=["grok-4.6"], default_model="grok-4.6",
    )
    provider = state["providers"][0]
    service.set_route("x_campaign_discovery", provider["id"], "grok-4.6")
    seen = []

    class Response:
        content = b'{"output":[{"content":[{"text":"RIPPLE_OK"}]}]}'
        def raise_for_status(self): return None
        def json(self): return {"output": [{"content": [{"text": "RIPPLE_OK"}]}]}

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, headers=None, json=None):
            seen.append((url, json))
            return Response()

    monkeypatch.setattr(ai_providers.httpx, "Client", Client)
    result = service.probe(provider["id"], "grok-4.6", "x_search")
    assert result["ok"] is True
    assert service.public_state()["providers"][0]["capabilities"]["x_search"] == "verified"
    search = service.x_search("creator challenge")
    assert search["text"] == "RIPPLE_OK"
    assert all(url == "https://gateway.example/v1/responses" for url, _ in seen)
    assert all(call[1]["tools"] == [{"type": "x_search"}] for call in seen)


def test_x_search_probe_timeout_reports_specific_error(tmp_path, monkeypatch):
    from ripple import ai_providers
    monkeypatch.setattr(ai_providers, "protect", fake_protect)
    service = AIProviderService(tmp_path / "private")
    state = service.upsert(
        name="Slow Grok Gateway", kind="openai-compatible", base_url="https://slow.example/v1",
        api_key="secret", models=["grok-4.5-search"], default_model="grok-4.5-search",
    )
    provider = state["providers"][0]

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def post(self, url, headers=None, json=None):
            request = ai_providers.httpx.Request("POST", url)
            raise ai_providers.httpx.ReadTimeout("slow", request=request)

    monkeypatch.setattr(ai_providers.httpx, "Client", Client)
    with pytest.raises(WorkflowError, match="60 秒") as exc:
        service.probe(provider["id"], "grok-4.5-search", "x_search")
    assert exc.value.status == 504
    assert service.public_state()["providers"][0]["capabilities"]["x_search"] == "failed"


def test_discover_existing_provider_prefers_current_editor_base_url(tmp_path, monkeypatch):
    from ripple import ai_providers
    monkeypatch.setattr(ai_providers, "protect", fake_protect)
    service = AIProviderService(tmp_path / "private")
    state = service.upsert(
        name="Gateway", kind="openai-compatible", base_url="https://old.example/v1",
        api_key="secret", models=["model-a"], default_model="model-a",
    )
    provider_id = state["providers"][0]["id"]
    seen = []

    class Response:
        content = b'{"data":[{"id":"model-b"}]}'
        def raise_for_status(self): return None
        def json(self): return {"data": [{"id": "model-b"}]}

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def get(self, url):
            seen.append(url)
            return Response()

    monkeypatch.setattr(ai_providers.httpx, "Client", Client)
    rows = service.discover(provider_id=provider_id, base_url="https://new.example/v1")
    assert seen == ["https://new.example/v1/models"]
    assert rows[0]["id"] == "model-b"


def test_discover_timeout_reports_specific_error(tmp_path, monkeypatch):
    from ripple import ai_providers
    service = AIProviderService(tmp_path / "private")

    class Client:
        def __init__(self, *args, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def get(self, url):
            request = ai_providers.httpx.Request("GET", url)
            raise ai_providers.httpx.ReadTimeout("slow", request=request)

    monkeypatch.setattr(ai_providers.httpx, "Client", Client)
    with pytest.raises(WorkflowError, match="20 秒") as exc:
        service.discover(base_url="https://slow.example/v1", api_key="secret")
    assert exc.value.status == 504


def test_provider_rejects_insecure_public_endpoint(tmp_path, monkeypatch):
    from ripple import ai_providers
    monkeypatch.setattr(ai_providers, "protect", fake_protect)
    service = AIProviderService(tmp_path / "private")
    with pytest.raises(WorkflowError, match="HTTPS"):
        service.upsert(
            name="bad", kind="openai-compatible", base_url="http://evil.example/v1",
            api_key="secret", models=["m"], default_model="m",
        )
