"""Douyin login and read-only creator activity discovery in a Ripple profile."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit
import uuid

from .douyin_campaigns import (HOME, MAX_RESPONSE, DouyinCampaignError, fetch_public_detail,
                               official_list_url, parse_activity_list)


class DouyinBrowserError(RuntimeError):
    pass


def _launch(directory: Path, *, headed: bool = False, browser_channel: str | None = None):
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    profile = directory / "browser" / "DouyinProfile"
    profile.mkdir(parents=True, exist_ok=True)
    kwargs: dict[str, Any] = {
        "headless": not headed, "locale": "zh-CN",
        "args": ["--no-first-run", "--no-default-browser-check"], "chromium_sandbox": True,
    }
    if browser_channel in {"msedge", "chrome", "chromium"}:
        kwargs["channel"] = browser_channel
    context = None
    try:
        context = p.chromium.launch_persistent_context(str(profile), **kwargs)
        if context is None:
            raise DouyinBrowserError("browser_launch_failed")
        page = context.pages[0] if context.pages else context.new_page()
        return p, context, page
    except Exception:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass
        p.stop()
        raise


def _close(p, context) -> None:
    try:
        context.close()
    finally:
        p.stop()


def _body(page) -> str:
    try:
        return (page.locator("body").inner_text(timeout=800) or "")[:5000]
    except Exception:
        return ""


def _login_required(page) -> bool:
    return "login" in urlsplit(page.url or "").path.lower() or bool(re.search(r"扫码登录|验证码登录|密码登录", _body(page)))


class _IdentityObserver:
    """Positive authenticated API evidence; an unloaded page is never logged in."""
    def __init__(self):
        self.value: dict[str, Any] = {"logged_in": False, "name": "", "remote_id": ""}

    def observe(self, response) -> None:
        parsed = urlsplit(response.url)
        paths = {"/aweme/v1/creator/pc/user/info/", "/web/api/media/user/info/"}
        if parsed.hostname != "creator.douyin.com" or parsed.path not in paths or response.status != 200:
            return
        try:
            data = response.json()
            if not isinstance(data, dict) or type(data.get("status_code")) is not int or data["status_code"] != 0:
                return
            user = data.get("user") if isinstance(data.get("user"), dict) else data
            uid = user.get("uid")
            if isinstance(uid, bool) or not isinstance(uid, (str, int)) or not re.fullmatch(r"[1-9][0-9]{0,24}", str(uid)):
                return
            self.value = {"logged_in": True, "remote_id": str(uid),
                          "name": str(user.get("nickname") or self.value.get("name") or "")[:80]}
        except Exception:
            return


def _identity(page, fallback: str = "", observer: _IdentityObserver | None = None) -> dict[str, Any]:
    if (urlsplit(page.url or "").hostname != "creator.douyin.com" or _login_required(page)
            or observer is None or not observer.value.get("logged_in")):
        return {"logged_in": False, "name": "", "remote_id": ""}
    return {**observer.value, "name": observer.value.get("name") or str(fallback or "抖音创作者")[:80]}


LOGIN_MESSAGES = {
    "starting": "正在打开抖音登录窗口并读取二维码…",
    "qr_ready": "请使用抖音 App 扫码，并在手机上确认登录。",
    "scanned": "已扫码，请在手机上完成登录确认。",
    "verifying": "抖音要求额外验证，请到独立浏览器窗口完成。",
    "waiting_user": "平台暂未显示可读取的二维码，请在已打开的独立窗口选择扫码登录或完成验证。",
    "success": "抖音登录成功，账号身份已核验。",
    "expired": "二维码或登录等待已超时，请在平台窗口刷新二维码，或重新打开登录。",
    "error": "抖音登录未完成，请检查独立浏览器窗口或网络。",
}


class _LoginProgress:
    def __init__(self, directory: Path | None):
        self.directory = directory
        self.last: tuple[str, str] | None = None

    def update(self, state: str, image: bytes | None = None) -> None:
        if self.directory is None:
            return
        if state not in LOGIN_MESSAGES:
            state = "error"
        if state == "qr_ready" and not image:
            state = "waiting_user"
        revision = hashlib.sha256(image).hexdigest()[:16] if image and state == "qr_ready" else ""
        if self.last == (state, revision):
            return
        self.directory.mkdir(parents=True, exist_ok=True)
        qr = self.directory / "qr.png"
        if revision:
            temp = qr.with_name(uuid.uuid4().hex + ".png.tmp")
            temp.write_bytes(image)
            os.replace(temp, qr)
        else:
            qr.unlink(missing_ok=True)
        value = {"state": state, "message": LOGIN_MESSAGES[state], "qr_revision": revision}
        temp = self.directory / (uuid.uuid4().hex + ".json.tmp")
        temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(temp, self.directory / "status.json")
        self.last = state, revision


def _qr_image(page) -> bytes | None:
    """Capture only a QR element, validated geometrically; never a full login page."""
    import cv2
    import numpy as np
    nodes = page.locator('[class*="login-card"] img, [class*="login-card"] canvas, '
                         '[class*="qrcode"] img, [class*="qrcode"] canvas, [class*="qr-code"] img')
    for i in range(min(nodes.count(), 12)):
        node = nodes.nth(i)
        try:
            box = node.bounding_box(timeout=500)
            if not box or not 100 <= box["width"] <= 400 or not 100 <= box["height"] <= 400:
                continue
            if abs(box["width"] - box["height"]) > 12 or not node.is_visible():
                continue
            raw = node.screenshot(type="png", timeout=1200)
            if len(raw) > 512 * 1024:
                continue
            pixels = cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_GRAYSCALE)
            valid, points = cv2.QRCodeDetector().detect(pixels)
            if valid and points is not None:
                return raw
        except Exception:
            continue
    return None


def login(directory: Path, *, headed: bool = True, browser_channel: str | None = None,
          timeout: int = 240, fallback_name: str = "", run_dir: Path | None = None) -> dict[str, Any]:
    progress = _LoginProgress(run_dir)
    progress.update("starting")
    p = context = None
    try:
        p, context, page = _launch(directory, headed=headed, browser_channel=browser_channel)
        observer = _IdentityObserver()
        page.on("response", observer.observe)
        page.goto(HOME, wait_until="domcontentloaded", timeout=30000)
        deadline = time.monotonic() + max(5, min(timeout, 240))
        while time.monotonic() < deadline:
            page.wait_for_timeout(1000)
            identity = _identity(page, fallback_name, observer)
            if identity["logged_in"]:
                progress.update("success")
                return identity
            text = _body(page)
            if re.search(r"扫码成功|扫描成功|已扫码|请在手机.{0,15}确认", text):
                progress.update("scanned")
            elif re.search(r"二维码.{0,8}(?:过期|失效)|登录已超时", text):
                progress.update("expired")
            elif re.search(r"安全验证|拖动滑块|请完成验证|请输入短信验证码", text):
                progress.update("verifying")
            else:
                image = _qr_image(page)
                progress.update("qr_ready" if image else "waiting_user", image)
        progress.update("expired")
        raise DouyinBrowserError("login_required")
    except Exception:
        if progress.last is None or progress.last[0] != "expired":
            progress.update("error")
        raise
    finally:
        if p is not None and context is not None:
            _close(p, context)


def probe(directory: Path, *, browser_channel: str | None = None, fallback_name: str = "") -> dict[str, Any]:
    p, context, page = _launch(directory, headed=False, browser_channel=browser_channel)
    try:
        observer = _IdentityObserver()
        page.on("response", observer.observe)
        page.goto(HOME, wait_until="domcontentloaded", timeout=30000)
        for _ in range(20):
            page.wait_for_timeout(500)
            identity = _identity(page, fallback_name, observer)
            if identity["logged_in"] or _login_required(page):
                return identity
        raise DouyinBrowserError("identity_unconfirmed")
    finally:
        _close(p, context)


def _candidates(value: Any, *, limit: int = 200) -> list[dict[str, Any]]:
    return parse_activity_list(value, limit=limit)["items"]


def events(directory: Path, limit: int = 200, *, browser_channel: str | None = None) -> dict[str, Any]:
    p, context, page = _launch(directory, browser_channel=browser_channel)
    payloads: list[dict[str, Any]] = []
    failures: list[str] = []
    observer = _IdentityObserver()
    try:
        def on_response(response):
            if not official_list_url(response.url):
                return
            try:
                if response.status != 200 or "json" not in response.headers.get("content-type", "").lower():
                    raise DouyinCampaignError("抖音活动接口未返回可用的 JSON 响应。")
                raw = response.body()
                if len(raw) > MAX_RESPONSE:
                    raise DouyinCampaignError("抖音活动响应超过安全上限。")
                # Python parses raw JSON so long numeric IDs are not rounded by JavaScript.
                result = parse_activity_list(json.loads(raw), limit=limit)
                query = parse_qs(urlsplit(response.url).query)
                result["window"] = {k: int(query[k][0]) for k in ("start_time", "end_time")
                                    if len(query.get(k, [])) == 1 and query[k][0].isdigit()}
                if len(payloads) < 4:
                    payloads.append(result)
            except (ValueError, TypeError, KeyError):
                failures.append("抖音官方活动列表响应无效，未回退到分类或首页文本。")
            except Exception:
                failures.append("抖音官方活动列表读取失败。")

        page.on("response", observer.observe)
        page.on("response", on_response)
        page.goto(HOME, wait_until="domcontentloaded", timeout=30000)
        for _ in range(40):
            page.wait_for_timeout(500)
            if _login_required(page):
                raise DouyinBrowserError("login_required")
            if observer.value["logged_in"] and (payloads or failures):
                break
        if not _identity(page, observer=observer)["logged_in"]:
            raise DouyinBrowserError("identity_unconfirmed")
        if not payloads:
            raise DouyinBrowserError(failures[-1] if failures else "未观察到抖音官方活动列表响应，请稍后重试。")
        result = payloads[-1]
        result["page_url"] = HOME
        result["fetched_at"] = int(time.time())
    finally:
        _close(p, context)
    # Public details use no account credentials. Bound work and keep list rows on failure.
    deadline, attempted = time.monotonic() + 45, 0
    for row in result["items"]:
        if row["url"] and attempted < 8 and time.monotonic() < deadline:
            row.update(fetch_public_detail(row["url"]))
            attempted += 1
    result["detail_attempted"] = attempted
    return result


def run(action: str, directory: Path, params: dict[str, Any]) -> dict[str, Any]:
    if action != "events":
        raise DouyinBrowserError("unsupported_action")
    limit = max(1, min(int(params.get("limit") or 200), 500))
    return events(directory, limit, browser_channel=str(params.get("browser_channel") or "") or None)
