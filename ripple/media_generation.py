"""Unified media generation service for Ripple UI, Agent tools and skills.

Credentials stay in Ripple's local configuration. Callers receive only sanitized
capabilities and output paths; API keys are never returned to the Agent/UI.
"""
from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import sys
import time
from typing import Any, Callable
import uuid

from .publishing import WorkflowError
from .media_connections import MediaConnectionStore


_SYSTEM_ENV = {
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
    "HOME", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
}


def _configured_value(key: dict[str, Any], env: dict[str, str]) -> str:
    for name in (key["env"], *key.get("aliases", ())):
        value = str(os.environ.get(name, "") or env.get(name, "") or "").strip()
        if value:
            return value
    return ""


class MediaGenerationService:
    """Execute the existing image/video adapters behind one stable service boundary."""

    def __init__(
        self,
        project_root: Path,
        outputs: Path,
        env_provider: Callable[[], dict[str, str]],
        registry_provider: Callable[[str], dict[str, Any]],
        connection_store: MediaConnectionStore | None = None,
    ):
        self.project_root = project_root.resolve()
        self.outputs = outputs.resolve()
        self.env_provider = env_provider
        self.registry_provider = registry_provider
        self.connection_store = connection_store
        self.scripts = self.project_root / "skills" / "shared" / "scripts"

    def _env(self) -> dict[str, str]:
        return self.env_provider()

    def _provider_status(self, group: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        spec = self.registry_provider(group)
        env = self._env()
        providers = []
        for provider in spec["providers"]:
            keys = provider.get("keys", [])
            required = [key for key in keys if key.get("required")]
            ready = all(_configured_value(key, env) for key in required)
            models = {
                key["env"]: _configured_value(key, env)
                for key in keys
                if not key.get("secret", True) and "MODEL" in key["env"] and _configured_value(key, env)
            }
            providers.append({"id": provider["id"], "name": provider["name"], "ready": ready, "models": models})
        return spec, providers

    def _named_capability(self, group: str) -> dict[str, Any] | None:
        if self.connection_store is None:
            return None
        cap = self.connection_store.capabilities(group)
        return {**cap, "label": {"image": "图片生成", "video": "视频生成", "music": "音乐生成", "voice": "语音 / 声音克隆"}.get(group, group)}

    def capabilities(self) -> dict[str, Any]:
        named_image = self._named_capability("image")
        named_video = self._named_capability("video")
        if named_image is not None and named_video is not None:
            # Model-level capabilities come from explicit metadata/probes. Do not infer
            # edit/i2v/audio support merely because a Provider route exists.
            return {"image": named_image, "video": named_video}

        image_spec, image_providers = self._provider_status("image")
        video_spec, video_providers = self._provider_status("video")
        env = self._env()
        image_ready = [provider for provider in image_providers if provider["ready"]]
        requested_video = str(os.environ.get("VIDEO_PROVIDER", "") or env.get("VIDEO_PROVIDER", "") or "").strip()
        ready_video = [provider for provider in video_providers if provider["ready"]]
        selected_video = next((provider for provider in ready_video if provider["id"] == requested_video), None)
        selected_video = selected_video or (ready_video[0] if ready_video else None)
        image_model = next(iter(image_ready[0]["models"].values()), "") if image_ready else ""
        video_model = next(iter(selected_video["models"].values()), "") if selected_video else ""
        return {
            "image": {"available": bool(image_ready), "provider": image_ready[0]["id"] if image_ready else None,
                      "provider_name": image_ready[0]["name"] if image_ready else None, "model": image_model,
                      "text_to_image": True, "image_to_image": True, "variations": True,
                      "providers": image_providers, "label": image_spec["label"]},
            "video": {"available": bool(selected_video), "provider": selected_video["id"] if selected_video else None,
                      "provider_name": selected_video["name"] if selected_video else None, "model": video_model,
                      "text_to_video": True, "image_to_video": True,
                      "providers": video_providers, "label": video_spec["label"]},
        }

    def _child_env(self, group: str, provider_id: str | None = None, model_id: str | None = None) -> dict[str, str]:
        spec = self.registry_provider(group)
        configured = self._env()
        names: set[str] = {"RIPPLE_ROOT", "RIPPLE_PROXY", "RIPPLE_ROOT", "RIPPLE_PROXY"}
        for key in spec.get("settings", []):
            names.add(key["env"]); names.update(key.get("aliases", ()))
        if self.connection_store is None:
            for provider in spec["providers"]:
                for key in provider.get("keys", []):
                    names.add(key["env"]); names.update(key.get("aliases", ()))
        env = {name: value for name, value in os.environ.items() if name.upper() in _SYSTEM_ENV or name in names}
        for name in names:
            value = str(os.environ.get(name, "") or configured.get(name, "") or "").strip()
            if value:
                env[name] = value
        if self.connection_store is not None:
            selected_env = self.connection_store.env_for(group, provider_id, model_id)
            mode = selected_env.get("RIPPLE_MEDIA_NETWORK", "system")
            if mode == "direct":
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
                    env.pop(key, None)
            elif mode == "custom":
                for key in ("ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"):
                    env.pop(key, None)
                for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
                    if selected_env.get(key):
                        env[key] = selected_env[key]
            env.update({name: value for name, value in selected_env.items() if value})
        env["RIPPLE_ROOT"] = str(self.project_root)
        env["RIPPLE_ROOT"] = str(self.project_root)  # legacy script compatibility
        return env

    def _output(self, kind: str, suffix: str) -> Path:
        folder = self.outputs / "AI媒体生成"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        return folder / f"{kind}-{stamp}-{uuid.uuid4().hex[:8]}{suffix}"

    def _relative(self, path: Path) -> str:
        resolved = path.resolve()
        if self.outputs not in resolved.parents:
            raise WorkflowError("生成结果路径越界。", 500)
        return resolved.relative_to(self.outputs).as_posix()

    def _input_image(self, relative: str) -> Path:
        value = PurePosixPath(relative)
        if not relative or "\\" in relative or ":" in relative or value.is_absolute() or ".." in value.parts:
            raise WorkflowError("输入图片路径无效。", 422)
        if not value.parts or value.parts[0].startswith((".", "_")):
            raise WorkflowError("不能使用系统或隐藏目录中的图片。", 422)
        path = self.outputs.joinpath(*value.parts).resolve()
        if self.outputs not in path.parents or not path.is_file() or path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise WorkflowError("输入图片不存在或格式不支持。", 422)
        return path

    def _run(self, group: str, script: str, args: list[str], *, timeout: int,
             provider_id: str | None = None, model_id: str | None = None) -> None:
        target = self.scripts / script
        if not target.is_file():
            raise WorkflowError("媒体生成组件缺失。", 503)
        try:
            # Capture raw bytes. On Windows the child may encode Chinese output with the active
            # console code page while the parent runs UTF-8 mode; decoding here is unnecessary
            # and could also expose provider responses in logs. Only the exit code is trusted.
            result = subprocess.run(
                [sys.executable, str(target), *args],
                cwd=str(self.project_root),
                env=self._child_env(group, provider_id, model_id),
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise WorkflowError("媒体生成超时。", 504) from None
        except OSError:
            raise WorkflowError("无法启动媒体生成组件。", 503) from None
        if result.returncode != 0:
            raise WorkflowError("媒体生成失败，请检查设置中的 API、协议、模型与额度。", 502)

    def generate_image(
        self,
        prompt: str,
        *,
        size: str = "1024x1024",
        resolution: str = "2k",
        input_image: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        if not prompt.strip() or len(prompt) > 12_000:
            raise WorkflowError("图片 Prompt 不能为空且不能超过 12000 字符。", 422)
        if not self.capabilities()["image"]["available"]:
            raise WorkflowError("图片生成尚未在「设置」中配置完成。", 409)
        if self.connection_store is not None:
            try:
                selection = self.connection_store.resolve_selection("image", provider_id, model)
            except ValueError as exc:
                raise WorkflowError(str(exc), 422) from exc
            provider_id = selection["provider"]["id"]
            model = selection["model"]["id"]
        if size not in {"1024x1024", "1536x1024", "1024x1536", "1:1", "3:2", "2:3", "4:3", "3:4", "16:9", "9:16"}:
            raise WorkflowError("图片尺寸不在允许范围内。", 422)
        if resolution not in {"1k", "2k", "4k"}:
            raise WorkflowError("图片分辨率档位无效。", 422)
        output = self._output("image", ".png")
        command = "img2img" if input_image else "text2img"
        args = [command, "--prompt", prompt.strip(), "--output", str(output), "--size", size, "--resolution", resolution, "--format", "png"]
        if input_image:
            args += ["--image", str(self._input_image(input_image))]
        self._run("image", "ai_image.py", args, timeout=240, provider_id=provider_id, model_id=model)
        produced = [output] if output.is_file() else []
        produced.extend(sorted(
            path for path in output.parent.glob(output.stem + "-*")
            if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
        ))
        produced = list(dict.fromkeys(produced))
        if not produced:
            # URL-based providers may replace the requested suffix with the remote image suffix.
            produced = sorted(
                path for path in output.parent.glob(output.stem + ".*")
                if path.is_file() and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}
            )
        if not produced:
            raise WorkflowError("图片生成完成但没有找到输出文件。", 502)
        selected = produced[0]
        try:
            from PIL import Image
            with Image.open(selected) as image:
                width, height = image.size
                image.verify()
        except Exception:
            raise WorkflowError("图片生成结果无效。", 502) from None
        return {
            "kind": "image", "path": self._relative(selected), "bytes": selected.stat().st_size,
            "width": width, "height": height, "provider_id": provider_id, "model": model,
            "alternatives": [self._relative(path) for path in produced[1:]],
        }

    def generate_video(
        self,
        prompt: str,
        *,
        ratio: str = "9:16",
        duration: int | None = None,
        input_image: str | None = None,
        provider_id: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        if not prompt.strip() or len(prompt) > 12_000:
            raise WorkflowError("视频 Prompt 不能为空且不能超过 12000 字符。", 422)
        cap = self.capabilities()["video"]
        if not cap["available"]:
            raise WorkflowError("视频生成尚未在「设置」中配置完成。", 409)
        selection = None
        if self.connection_store is not None:
            try:
                selection = self.connection_store.resolve_selection("video", provider_id, model)
            except ValueError as exc:
                raise WorkflowError(str(exc), 422) from exc
            provider_id = selection["provider"]["id"]
            model = selection["model"]["id"]
        if ratio not in {"16:9", "9:16", "1:1", "4:3", "3:4"}:
            raise WorkflowError("视频画幅不在允许范围内。", 422)
        if duration is not None and not 1 <= duration <= 60:
            raise WorkflowError("视频时长需为 1–60 秒。", 422)
        output = self._output("video", ".mp4")
        command = "image2video" if input_image else "text2video"
        adapter = _configured_value({"env": "VIDEO_PROVIDER", "aliases": ()}, self._env()) or str(cap.get("provider") or "")
        if selection is not None:
            adapter = self.connection_store.protocol("video", selection["route"]["protocol"])["adapter"]
        args = [command, "--provider", adapter, "--prompt", prompt.strip(), "--ratio", ratio, "--output", str(output)]
        if model:
            args += ["--model", model]
        if duration is not None:
            args += ["--duration", str(duration)]
        if input_image:
            args += ["--image", str(self._input_image(input_image))]
        self._run("video", "ai_video.py", args, timeout=960, provider_id=provider_id, model_id=model)
        if not output.is_file() or output.stat().st_size <= 0:
            raise WorkflowError("视频生成完成但没有找到输出文件。", 502)
        return {"kind": "video", "path": self._relative(output), "bytes": output.stat().st_size,
                "provider_id": provider_id, "model": model, "adapter": adapter}
