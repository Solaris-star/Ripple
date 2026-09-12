from __future__ import annotations

import json
from pathlib import Path

from PIL import Image
import pytest

from ripple.media_generation import MediaGenerationService
from ripple.publishing import WorkflowError


def registry(group: str):
    specs = {
        "image": {"label": "AI 生图", "settings": [], "providers": [{"id": "openai", "name": "OpenAI compatible", "keys": [
            {"env": "IMG_API_KEY", "required": True, "secret": True, "aliases": ()},
            {"env": "IMG_BASE_URL", "required": True, "secret": False, "aliases": ()},
            {"env": "IMG_MODEL", "required": True, "secret": False, "aliases": ()},
        ]}]},
        "video": {"label": "AI 视频", "settings": [{"env": "VIDEO_PROVIDER", "required": False, "secret": False, "aliases": ()}], "providers": [{"id": "openai-compatible", "name": "OpenAI videos", "keys": [
            {"env": "VIDEO_API_KEY", "required": True, "secret": True, "aliases": ()},
            {"env": "VIDEO_BASE_URL", "required": True, "secret": False, "aliases": ()},
            {"env": "VIDEO_MODEL", "required": False, "secret": False, "aliases": ()},
        ]}]},
    }
    return specs[group]


def service(tmp_path: Path, env: dict[str, str]) -> MediaGenerationService:
    root = tmp_path / "app"
    outputs = root / "outputs"
    outputs.mkdir(parents=True)
    return MediaGenerationService(root, outputs, lambda: dict(env), registry)


def image_env(secret: str = "fixture-image-secret") -> dict[str, str]:
    return {"IMG_API_KEY": secret, "IMG_BASE_URL": "https://images.example.invalid/v1", "IMG_MODEL": "fixture-image"}


def test_capabilities_are_sanitized_and_require_real_image_model(tmp_path):
    secret = "do-not-return-this-secret"
    media = service(tmp_path, image_env(secret))
    result = media.capabilities()
    assert result["image"]["available"] is True
    assert result["image"]["model"] == "fixture-image"
    assert secret not in json.dumps(result)

    missing_model = service(tmp_path / "missing", {"IMG_API_KEY": secret, "IMG_BASE_URL": "https://images.example.invalid/v1"})
    assert missing_model.capabilities()["image"]["available"] is False


def test_generate_image_uses_shared_service_and_creates_library_asset(tmp_path, monkeypatch):
    media = service(tmp_path, image_env())

    def fake_run(group: str, script: str, args: list[str], *, timeout: int, provider_id=None, model_id=None):
        assert group == "image" and script == "ai_image.py"
        output = Path(args[args.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        # Some OpenAI-compatible providers return more than one data item even when n=1;
        # ai_image.py preserves those candidates with a suffixed filename.
        Image.new("RGB", (32, 24), (10, 20, 30)).save(output.parent / f"{output.stem}-provider-01.png")
        Image.new("RGB", (32, 24), (40, 50, 60)).save(output.parent / f"{output.stem}-provider-02.png")

    monkeypatch.setattr(media, "_run", fake_run)
    result = media.generate_image("small test image", size="1024x1024", resolution="1k")
    assert result["kind"] == "image"
    assert result["width"] == 32 and result["height"] == 24
    assert result["path"].startswith("AI媒体生成/image-")
    assert len(result["alternatives"]) == 1
    assert (media.outputs / result["path"]).is_file()


def test_generate_video_uses_selected_configured_provider(tmp_path, monkeypatch):
    env = {
        "VIDEO_PROVIDER": "openai-compatible",
        "VIDEO_API_KEY": "video-secret",
        "VIDEO_BASE_URL": "https://video.example.invalid/v1",
        "VIDEO_MODEL": "fixture-video",
    }
    media = service(tmp_path, env)
    captured = {}

    def fake_run(group: str, script: str, args: list[str], *, timeout: int, provider_id=None, model_id=None):
        captured["args"] = args
        output = Path(args[args.index("--output") + 1])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"fake-mp4")

    monkeypatch.setattr(media, "_run", fake_run)
    result = media.generate_video("short clip", ratio="9:16", duration=5)
    assert result["adapter"] == "openai-compatible"
    assert captured["args"][captured["args"].index("--provider") + 1] == "openai-compatible"
    assert result["path"].startswith("AI媒体生成/video-")


def test_input_image_rejects_system_and_escape_paths(tmp_path):
    media = service(tmp_path, image_env())
    for path in ("../secret.png", "_inbox/a.png", "C:/secret.png"):
        with pytest.raises(WorkflowError):
            media._input_image(path)
