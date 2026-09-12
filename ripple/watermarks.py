"""Optional integration for inspecting and regenerating image-level AI provenance marks.

The heavy invisible-watermark engine is deliberately optional. Inspection never treats
missing evidence as proof that an image is clean, and cleaning always writes a new file.
"""
from __future__ import annotations

from pathlib import Path
import os
import threading
import uuid
import urllib.error
import urllib.request

from PIL import Image

from .publishing import WorkflowError

SUPPORTED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


def _safe_signal(signal: object) -> str:
    """Return a bounded, path-free label from a third-party signal object."""
    if isinstance(signal, str):
        return signal[:120]
    if isinstance(signal, dict):
        values = [signal.get(key) for key in ("kind", "type", "provider", "name", "label")]
    else:
        values = [getattr(signal, key, None) for key in ("kind", "type", "provider", "name", "label")]
    text = " · ".join(str(value) for value in values if isinstance(value, (str, int, float)) and str(value))
    return (text or signal.__class__.__name__)[:120]


class WatermarkService:
    """Thin adapter around remove-ai-watermarks with lazy GPU initialization."""

    def __init__(self):
        self._engine = None
        self._engine_lock = threading.Lock()

    def capability(self) -> dict:
        try:
            from remove_ai_watermarks.identify import identify  # noqa: F401
        except Exception:
            return {
                "inspection_ready": False,
                "deep_clean_ready": False,
                "reason": "隐式水印组件未安装。",
            }
        try:
            from remove_ai_watermarks.invisible_engine import is_available
            if not is_available():
                return {
                    "inspection_ready": True,
                    "deep_clean_ready": False,
                    "reason": "深度清理组件未安装。",
                }
            import torch
            if not torch.cuda.is_available():
                return {
                    "inspection_ready": True,
                    "deep_clean_ready": False,
                    "reason": "深度清理需要可用的 NVIDIA CUDA GPU。",
                }
        except Exception:
            return {
                "inspection_ready": True,
                "deep_clean_ready": False,
                "reason": "深度清理运行环境不可用。",
            }
        return {"inspection_ready": True, "deep_clean_ready": True, "reason": ""}

    def inspect(self, source: Path, relative: str) -> dict:
        capability = self.capability()
        suffix = source.suffix.lower()
        if suffix not in SUPPORTED_IMAGE_EXTENSIONS:
            return {
                "path": relative,
                "status": "unsupported",
                "provider": None,
                "signals": [],
                "supported": False,
                **capability,
                "note": "当前仅支持 PNG/JPEG/WebP 静态图片的隐式水印处理。",
            }
        if not capability["inspection_ready"]:
            return {
                "path": relative,
                "status": "unavailable",
                "provider": None,
                "signals": [],
                "supported": True,
                **capability,
                "note": capability["reason"],
            }
        try:
            from remove_ai_watermarks.identify import identify
            report = identify(source)
            provider = getattr(report, "platform", None)
            provider = str(provider)[:80] if provider else None
            signals = [_safe_signal(item) for item in (getattr(report, "signals", None) or [])][:12]
            has_evidence = bool(provider or signals)
            return {
                "path": relative,
                "status": "evidence" if has_evidence else "unknown",
                "provider": provider,
                "signals": signals,
                "supported": True,
                **capability,
                "note": (
                    "发现可识别的 AI 来源/水印线索；仍需深度清理才能改写像素域信号。"
                    if has_evidence else
                    "未检出可识别线索；这不代表图片不存在私有或未知的隐式水印。"
                ),
            }
        except Exception:
            return {
                "path": relative,
                "status": "error",
                "provider": None,
                "signals": [],
                "supported": True,
                **capability,
                "note": "隐式水印检查未完成。",
            }

    @staticmethod
    def _model_cache_ready() -> bool:
        cache = Path(
            os.environ.get("HF_HUB_CACHE")
            or os.environ.get("HUGGINGFACE_HUB_CACHE")
            or (Path.home() / ".cache" / "huggingface" / "hub")
        )
        repos = (
            "models--stabilityai--stable-diffusion-xl-base-1.0",
            "models--xinsir--controlnet-canny-sdxl-1.0",
        )
        for repo in repos:
            snapshots = cache / repo / "snapshots"
            try:
                if not snapshots.is_dir() or not any(snapshots.iterdir()):
                    return False
            except OSError:
                return False
        return True

    def _ensure_model_source(self) -> None:
        """Fail quickly when first-run ControlNet weights cannot be reached."""
        if self._model_cache_ready():
            return
        if os.environ.get("HF_HUB_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}:
            raise WorkflowError("隐式水印模型尚未缓存，当前处于 Hugging Face 离线模式；原图未修改。", 503)
        endpoint = (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").strip().rstrip("/")
        request = urllib.request.Request(
            endpoint + "/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/model_index.json",
            method="HEAD",
            headers={"User-Agent": "Ripple-watermark/0.2"},
        )
        try:
            with urllib.request.urlopen(request, timeout=6) as response:
                if not 200 <= int(getattr(response, "status", 200)) < 400:
                    raise OSError()
        except (OSError, TimeoutError, urllib.error.URLError, urllib.error.HTTPError):
            raise WorkflowError(
                "隐式水印模型尚未缓存，且当前无法访问 Hugging Face 模型源。"
                "请先配置可访问的网络或 HF_ENDPOINT 后重试；原图未修改。",
                503,
            ) from None

    def _load_engine(self):
        capability = self.capability()
        if not capability["deep_clean_ready"]:
            raise WorkflowError(capability["reason"] or "隐式水印深度清理不可用。", 503)
        if self._engine is None:
            self._ensure_model_source()
            from remove_ai_watermarks.invisible_engine import InvisibleEngine
            # Use the upstream default ControlNet path. It is materially lighter than the
            # two-stage Qwen-Image-2512 + Z-Image stack and is the practical default for a
            # local 16 GB-class GPU while still regenerating pixel-domain watermark signals.
            self._engine = InvisibleEngine(pipeline="controlnet", device="cuda", cpu_offload=True)
        return self._engine

    def clean(self, source: Path, relative: str) -> dict:
        if source.suffix.lower() not in SUPPORTED_IMAGE_EXTENSIONS:
            raise WorkflowError("当前仅支持 PNG/JPEG/WebP 静态图片的隐式水印清理。", 422)
        suffix = source.suffix.lower()
        token = uuid.uuid4().hex[:8]
        target = source.with_name(f"{source.stem}.implicit-clean-{token}{suffix}")
        temporary = source.with_name(f".ripple-watermark-{uuid.uuid4().hex}{suffix}")
        try:
            with self._engine_lock:
                engine = self._load_engine()
                engine.remove_watermark(source, temporary)
            if not temporary.is_file() or temporary.stat().st_size == 0:
                raise WorkflowError("隐式水印清理没有生成有效图片。", 502)
            with Image.open(temporary) as image:
                image.verify()
            os.replace(temporary, target)
        except WorkflowError:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise
        except Exception:
            temporary.unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            raise WorkflowError("隐式水印深度清理失败；原始图片未修改。", 502) from None
        output_relative = str(Path(relative).with_name(target.name)).replace("\\", "/")
        return {"source": relative, "output": output_relative}
