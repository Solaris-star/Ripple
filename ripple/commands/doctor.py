"""Ripple doctor — 检查开发环境是否就绪。"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

# 项目根目录（Ripple/）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

GREEN = "\033[0;32m"
RED = "\033[0;31m"
YELLOW = "\033[0;33m"
NC = "\033[0m"


def _check(label: str, ok: bool, detail: str = "") -> bool:
    status = f"{GREEN}OK{NC}" if ok else f"{RED}FAIL{NC}"
    print(f"  {label:<40s} {status}")
    if not ok and detail:
        print(f"    └─ {detail}")
    return ok


def _optional(label: str, ok: bool, detail: str = "") -> None:
    status = f"{GREEN}OK{NC}" if ok else f"{YELLOW}OPTIONAL{NC}"
    print(f"  {label:<40s} {status}")
    if not ok and detail:
        print(f"    └─ {detail}")


def _node_version_ok() -> bool:
    """Check Node.js >= 22.19."""
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return False
        # e.g. "v22.19.0"
        m = re.match(r"v(\d+)\.(\d+)", result.stdout.strip())
        if not m:
            return False
        major, minor = int(m.group(1)), int(m.group(2))
        return (major, minor) >= (22, 19)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False


def _python_version_ok() -> bool:
    import sys
    return sys.version_info >= (3, 10)


def _module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def _venv_available() -> bool:
    return _module_available("venv")


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            return Path(playwright.chromium.executable_path).is_file()
    except (ImportError, OSError, RuntimeError):
        return False


def _env_key_valid() -> bool:
    """Check .env 配置了可用的认证。

    以下任一通道满足即可：
    - 标准 API key：ANTHROPIC_API_KEY
    - Ripple 兼容模型服务：RIPPLE_LLM_API_KEY + RIPPLE_LLM_BASE_URL

    ping 才是权威连通性测试；这里只做静态配置存在性检查。
    """
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return False

    # 认证变量 → 是否已填入非占位值
    auth_vars: dict[str, str] = {}
    try:
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key in (
                "ANTHROPIC_API_KEY", "RIPPLE_LLM_API_KEY", "RIPPLE_LLM_BASE_URL",
                "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                "OPENAI_API_KEY", "OPENAI_BASE_URL",
                "OPENAI_MAAS_API_KEY", "OPENAI_MAAS_ENDPOINT",
            ):
                auth_vars[key] = value
    except OSError:
        return False

    def _set(name: str) -> bool:
        v = auth_vars.get(name, "")
        return bool(v) and "REPLACE_ME" not in v

    # 标准 key 通道
    if _set("ANTHROPIC_API_KEY"):
        return True
    # Ripple compatible-model service: key + base_url must both be present.
    if _set("RIPPLE_LLM_API_KEY") and _set("RIPPLE_LLM_BASE_URL"):
        return True
    if _set("ANTHROPIC_AUTH_TOKEN") and _set("ANTHROPIC_BASE_URL"):
        return True
    if _set("OPENAI_API_KEY"):
        return True
    if _set("OPENAI_MAAS_API_KEY") and _set("OPENAI_MAAS_ENDPOINT"):
        return True
    return False


def cmd_doctor(_args) -> int:
    print("Ripple — 环境检查\n")
    all_ok = True

    # 1. Runtime prerequisites
    all_ok &= _check("Python >= 3.10", _python_version_ok(),
                      "请安装 Python 3.10 或更高版本")
    all_ok &= _check("Python venv module", _venv_available(),
                      "Debian/Ubuntu 请安装 python3-venv")
    has_node = shutil.which("node") is not None
    node_ok = _node_version_ok()
    node_detail = ("请安装 Node.js >= 22.19: https://nodejs.org/" if not has_node
                   else "Node.js 版本过低，请升级到 >= 22.19: https://nodejs.org/")
    all_ok &= _check("Node.js >= 22.19", node_ok, node_detail)
    _optional("FFmpeg", shutil.which("ffmpeg") is not None,
              "未安装时仅视频/音频处理能力受限。")

    # Agent runtimes are user-owned and optional at install time. Ripple detects
    # whichever supported clients already exist without modifying their config.
    detected_agents = [name for name in ("opencode", "claude", "codex", "hermes") if shutil.which(name)]
    _optional("Local Agent", bool(detected_agents),
              "未检测到 OpenCode / Claude Code / Codex / Hermes；可稍后安装后在设置中扫描。")
    if detected_agents:
        print(f"    └─ 已检测: {', '.join(detected_agents)}")

    for module in ("fastapi", "uvicorn", "sse_starlette", "multipart"):
        all_ok &= _check(f"Python package: {module}", _module_available(module),
                          "运行 pip install -e . 安装 Ripple 运行依赖")

    frontend_ready = (PROJECT_ROOT / "web" / "frontend" / "dist" / "index.html").is_file()
    all_ok &= _check("Web frontend build", frontend_ready,
                      "运行 cd web/frontend && npm ci && npm run build")
    _optional("Playwright Chromium", _chromium_available(),
              "未安装时浏览器账号适配器不可用；运行 python -m playwright install chromium。")

    # Model/API keys can live in Ripple Settings or an already configured local
    # Agent, so a project .env is useful but no longer a core readiness gate.
    _optional("Project .env model/API config", _env_key_valid(),
              "可在 Ripple 设置中配置媒体/API，或直接复用已配置的本地 Agent。")

    # Core repository files.
    key_files = [
        ("Skill library", PROJECT_ROOT / "skills" / "ripple"),
        ("Web app", PROJECT_ROOT / "web" / "app.py"),
    ]
    for label, path in key_files:
        all_ok &= _check(label, path.exists())

    print()
    if all_ok:
        print(f"{GREEN}✓ Ripple 核心环境就绪{NC}")
    else:
        print(f"{YELLOW}⚠ 有未满足项{NC} — 请按上述提示修复后重试")

    return 0 if all_ok else 1
