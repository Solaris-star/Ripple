"""Declared adapter scope is separate from observed account authorization."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import importlib.metadata
import importlib.util
import subprocess

X_BROWSER_ADAPTER = "x-browser"
BILIUP_VERSION = "1.2.4"
PLAYWRIGHT_VERSION = "1.62.0"
_BROWSER_LABELS = {"msedge": "Microsoft Edge", "chrome": "Google Chrome", "chromium": "Chromium"}

NATIVE = {
    "x": {"name": "X", "module": "x_browser", "profile": "XProfile", "adapter": X_BROWSER_ADAPTER, "formats": ["text", "images"], "title_limit": 200, "body_limit": 280, "max_images": 4, "home": "https://x.com/home"},
    "xiaohongshu": {"name": "小红书", "module": "xhs_publish", "profile": "XiaohongshuProfile", "formats": ["images", "video"], "title_limit": 20, "body_limit": 1000, "max_images": 9, "home": "https://creator.xiaohongshu.com/"},
    "douyin": {"name": "抖音", "module": "douyin_publish", "profile": "DouyinProfile", "formats": ["video"], "title_limit": 30, "body_limit": 1000, "max_images": 0, "home": "https://creator.douyin.com/"},
    "kuaishou": {"name": "快手", "module": "web_publisher", "profile": "KuaishouProfile", "formats": ["images", "video"], "title_limit": 30, "body_limit": 1000, "max_images": 1, "home": "https://cp.kuaishou.com/"},
    "weixin-channels": {"name": "微信视频号", "module": "web_publisher", "profile": "ChannelsProfile", "formats": ["video"], "title_limit": 200, "body_limit": 1000, "max_images": 0, "home": "https://channels.weixin.qq.com/"},
    "zhihu": {"name": "知乎", "module": "web_publisher", "profile": "ZhihuProfile", "formats": ["text"], "title_limit": 100, "body_limit": 50000, "max_images": 0, "home": "https://zhuanlan.zhihu.com/"},
    "bilibili": {"name": "Bilibili", "module": "bili_login", "profile": None, "formats": ["video"], "title_limit": 80, "body_limit": 2000, "max_images": 0, "home": "https://member.bilibili.com/"},
}
NAMES = {"x": "X", **{k: v["name"] for k, v in NATIVE.items()}, "wechat": "微信公众号", "tiktok": "TikTok", "blog": "个人 Blog"}
CONNECTION_METHODS = {
    "x": [
        {"id": "browser", "label": "浏览器登录", "status": "available", "execution_scope": "browser_node", "requirements": "Chrome / Edge；每个账号使用独立 Ripple Profile"},
        {"id": "api", "label": "官方 API", "status": "available", "execution_scope": "server", "requirements": "X Developer App OAuth 2.0 Client ID；权限和额度取决于 X 计划"},
    ],
    "wechat": [
        {"id": "official_api", "label": "官方 API", "status": "available", "execution_scope": "server", "requirements": "AppID + AppSecret；Ripple Server 出口 IP 需加入公众号 IP 白名单。基础能力写入草稿箱；账号具备 freepublish 权限时可提交发布。"},
    ],
    "tiktok": [
        {"id": "official_api", "label": "Content Posting API", "status": "planned", "execution_scope": "server", "requirements": "TikTok Developer App、用户授权与对应发布 scope；应用审核状态会影响可见性/发布能力"},
        {"id": "browser", "label": "浏览器登录", "status": "planned", "execution_scope": "browser_node", "requirements": "Browser Node 独立 Profile；接入前不会尝试网页自动发布"},
    ],
    "blog": [
        {"id": "openapi", "label": "Blog OpenAPI", "status": "available", "execution_scope": "server", "requirements": "兼容 Ripple Blog Connector 能力声明的 OpenAPI + Agent Token"},
        {"id": "export", "label": "Markdown / 素材导出", "status": "available", "execution_scope": "server", "requirements": "无需账号连接"},
    ],
}
for _platform, _spec in NATIVE.items():
    if _platform not in CONNECTION_METHODS:
        CONNECTION_METHODS[_platform] = [{
            "id": "local_uploader" if _platform == "bilibili" else "browser",
            "label": "本地上传程序" if _platform == "bilibili" else "浏览器登录",
            "status": "available", "execution_scope": "local" if _platform == "bilibili" else "browser_node",
            "requirements": "biliup 运行环境" if _platform == "bilibili" else "可用浏览器与独立 Ripple Profile",
        }]
RECEIPT_HOSTS = {
    "x": {"x.com", "www.x.com", "twitter.com", "www.twitter.com"},
    "xiaohongshu": {"www.xiaohongshu.com", "xiaohongshu.com", "xhslink.com"},
    "douyin": {"www.douyin.com", "douyin.com", "v.douyin.com"},
    "kuaishou": {"www.kuaishou.com", "kuaishou.com", "v.kuaishou.com"},
    "weixin-channels": {"channels.weixin.qq.com"},
    "zhihu": {"www.zhihu.com", "zhuanlan.zhihu.com"},
    "bilibili": {"www.bilibili.com", "bilibili.com", "b23.tv"},
    "wechat": {"mp.weixin.qq.com"}, "tiktok": {"www.tiktok.com", "tiktok.com"},
}


def biliup_candidates(executable: Path | None = None, *, windows: bool | None = None) -> list[Path]:
    """Only consider the biliup executable belonging to Ripple's active Python."""
    python = (executable or Path(sys.executable)).resolve()
    is_windows = windows if windows is not None else os.name == "nt"
    if is_windows:
        return [python.parent / "biliup.exe", python.parent / "Scripts" / "biliup.exe"]
    return [python.parent / "biliup"]


def biliup_binary() -> Path | None:
    return next((path for path in biliup_candidates() if path.is_file()), None)


def browser_executable(channel: str) -> Path | None:
    """Return an installed browser executable without launching it."""
    if os.name == "nt":
        candidates = {
            "msedge": [
                ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
                ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
                ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe"),
            ],
            "chrome": [
                ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
                ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
                ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
            ],
        }
        for variable, suffix in candidates.get(channel, []):
            root = os.environ.get(variable)
            if root:
                path = (Path(root) / suffix).resolve()
                if path.is_file():
                    return path
    return None


def interactive_browser_channels() -> list[str]:
    """Browsers suitable for first-party interactive SSO without automation."""
    return [channel for channel in ("msedge", "chrome") if browser_executable(channel)]


def available_browser_channels() -> list[str]:
    channels: list[str] = []
    if os.name == "nt":
        for channel in ("msedge", "chrome"):
            if browser_executable(channel):
                channels.append(channel)
    # Read package metadata only; probing must not spawn a Playwright driver or
    # create an event loop while the application is starting.
    try:
        import importlib.util
        import json
        spec = importlib.util.find_spec("playwright")
        if spec and spec.origin:
            package = Path(spec.origin).parent / "driver/package"
            manifest = json.loads((package / "browsers.json").read_text(encoding="utf-8"))
            revision = next(b["revision"] for b in manifest["browsers"] if b["name"] == "chromium")
            configured_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
            browser_root = package / ".local-browsers" if configured_root == "0" else Path(configured_root) if configured_root else Path(os.environ.get("LOCALAPPDATA", str(Path.home() / ".cache"))) / "ms-playwright"
            revision_root = browser_root / ("chromium-" + revision)
            for executable in ("chrome-win64/chrome.exe", "chrome-win/chrome.exe", "chrome-linux64/chrome", "chrome-linux/chrome"):
                if (revision_root / executable).is_file():
                    channels.append("chromium")
                    break
    except (OSError, ValueError, StopIteration, KeyError):
        pass
    return list(dict.fromkeys(channels))


def browser_channel(preferred: str | None = None) -> str | None:
    configured = os.environ.get("RIPPLE_BROWSER_CHANNEL", "")
    if configured in {"msedge", "chrome", "chromium"}:
        return configured
    available = available_browser_channels()
    if preferred in {"msedge", "chrome", "chromium"} and preferred in available:
        return preferred
    return available[0] if available else None


def browser_label(channel: str) -> str:
    return _BROWSER_LABELS.get(channel, channel)


def _package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return ""


def environment_capabilities() -> dict:
    environment = environment_probe()
    browsers = [browser_label(value) for value in environment["browsers"]]
    return {"items": [
        {"id": "browser", "label": "浏览器自动化", "ready": bool(environment["browser"]),
         "status": "ready" if environment["browser"] else "missing", "summary": "已就绪 · 随项目环境管理" if environment["browser"] else "未就绪 · 可修复安装",
         "installable": not bool(environment["browser"]), "browsers": browsers,
         "detail": "Ripple 会直接复用已检测到的系统浏览器；只有没有可用浏览器时才显示修复安装，无需每次启动重复安装。"},
        {"id": "bilibili", "label": "B站发布", "ready": bool(environment["biliup"]),
         "status": "ready" if environment["biliup"] else "missing", "summary": "已就绪 · 随项目环境管理" if environment["biliup"] else "未就绪 · 可修复安装",
         "installable": not bool(environment["biliup"]), "provider": "biliup", "provider_version": _package_version("biliup"),
         "third_party": True, "detail": "biliup 是 Ripple 项目运行依赖，由项目环境统一管理；它是第三方开源组件，非 B 站官方组件。仅缺失时需要修复安装。"},
    ]}


def _run_install(argv: list[str], timeout: int) -> None:
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"安装程序启动失败：{str(exc)[:180]}") from exc
    if result.returncode != 0:
        tail = " ".join(line.strip() for line in (result.stderr or result.stdout).splitlines() if line.strip())[-500:]
        raise RuntimeError("安装失败。" + (f" {tail}" if tail else ""))


def install_environment_component(component: str) -> dict:
    """Install one fixed Ripple-managed runtime dependency; arbitrary packages are never accepted."""
    if component == "bilibili":
        if not biliup_binary():
            _run_install([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                          f"biliup=={BILIUP_VERSION}"], 300)
    elif component == "browser":
        if not available_browser_channels():
            if importlib.util.find_spec("playwright") is None:
                _run_install([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--no-input",
                              f"playwright=={PLAYWRIGHT_VERSION}"], 300)
            _run_install([sys.executable, "-m", "playwright", "install", "chromium"], 900)
    else:
        raise ValueError("不支持安装该环境组件。")
    state = environment_capabilities()
    item = next(row for row in state["items"] if row["id"] == component)
    if not item["ready"]:
        raise RuntimeError("安装命令已完成，但重新检测后组件仍未就绪。")
    return item


def environment_probe() -> dict:
    available = available_browser_channels()
    return {"browser": browser_channel(), "browsers": available, "biliup": biliup_binary() is not None,
            "requires_extension": False, "checked_locally": True}
