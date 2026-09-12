#!/usr/bin/env python3
"""Ripple experimental X browser-session adapter.

This adapter intentionally uses a dedicated persistent Playwright profile rather
than X Developer API credentials or manually copied auth_token/ct0 values. It
only operates after explicit Ripple account/login/publish actions. X may restrict
or prohibit non-API website automation; the product UI must surface that risk.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import time
import uuid

import content_guard
import login_state

X_HOME = "https://x.com/home"
X_COMPOSE = "https://x.com/compose/post"
PROFILE_NAME = "XProfile"
MAX_IMAGES = 4
SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp"}
LAUNCH_ARGS = ["--no-first-run", "--no-default-browser-check"]


def _profile(a) -> Path:
    root = Path(a.profile_base).expanduser().resolve()
    path = (root / PROFILE_NAME).resolve()
    if root not in path.parents:
        raise RuntimeError("X profile path escaped account directory")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _write_profile_json(path: Path, value: dict) -> None:
    tmp = path.with_name(path.name + ".ripple-tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, path)


def _set_profile_name(profile: Path, label: str) -> None:
    safe = re.sub(r"[\x00-\x1f]+", " ", str(label or "")).strip()[:48]
    name = ("Ripple · X" + (" · " + safe if safe else ""))[:64]
    local_state = profile / "Local State"
    preferences = profile / "Default" / "Preferences"
    for path, kind in ((local_state, "local"), (preferences, "preferences")):
        try:
            if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                continue
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                continue
            profile_data = value.setdefault("profile", {})
            if kind == "local":
                info = profile_data.setdefault("info_cache", {}).get("Default")
                if not isinstance(info, dict):
                    continue
                info["name"] = name
                info["is_using_default_name"] = False
            else:
                profile_data["name"] = name
            _write_profile_json(path, value)
        except (OSError, ValueError, TypeError):
            continue


def _first_visible(page, selectors: list[str]):
    for selector in selectors:
        try:
            locator = page.locator(selector)
            for i in range(min(locator.count(), 8)):
                item = locator.nth(i)
                if item.is_visible():
                    return item
        except Exception:
            continue
    return None


def identity_from_page(page) -> dict:
    """Return a conservative identity derived only from the visible X web UI."""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if any(marker in url for marker in ("/i/flow/login", "/login", "/account/access", "/account/suspended")):
        return {"loggedIn": False, "name": "", "uid": ""}

    profile = _first_visible(page, [
        'a[data-testid="AppTabBar_Profile_Link"]',
        'a[aria-label="Profile"]',
        'a[aria-label="个人资料"]',
    ])
    if profile is not None:
        try:
            href = (profile.get_attribute("href") or "").strip()
            match = re.fullmatch(r"/@?([A-Za-z0-9_]{1,30})/?", href)
            if match:
                handle = match.group(1)
                return {"loggedIn": True, "name": handle, "uid": "x-web:" + handle.lower()}
        except Exception:
            pass

    switcher = _first_visible(page, ['[data-testid="SideNav_AccountSwitcher_Button"]'])
    if switcher is not None:
        try:
            text = switcher.inner_text() or ""
            match = re.search(r"@([A-Za-z0-9_]{1,30})", text)
            if match:
                handle = match.group(1)
                return {"loggedIn": True, "name": handle, "uid": "x-web:" + handle.lower()}
        except Exception:
            pass
    return {"loggedIn": False, "name": "", "uid": ""}


def _launch(a, *, headed: bool):
    from playwright.sync_api import sync_playwright
    manager = sync_playwright().start()
    profile = _profile(a)
    if headed and not (profile / "Local State").is_file():
        bootstrap = manager.chromium.launch_persistent_context(
            str(profile), headless=True, args=LAUNCH_ARGS,
            viewport={"width": 1365, "height": 900}, locale="en-US",
        )
        bootstrap.close()
    _set_profile_name(profile, getattr(a, "profile_label", ""))
    context = manager.chromium.launch_persistent_context(
        str(profile), headless=not headed, args=LAUNCH_ARGS,
        viewport={"width": 1365, "height": 900}, locale="en-US",
    )
    page = context.pages[0] if context.pages else context.new_page()
    return manager, context, page


def _safe_close(manager, context) -> None:
    try:
        context.close()
    except Exception:
        pass
    try:
        manager.stop()
    except Exception:
        pass


def cmd_login(a) -> int:
    status_file = Path(getattr(a, "status_file", ""))
    try:
        manager, context, page = _launch(a, headed=True)
    except Exception:
        login_state.write_status(status_file, "error", "无法打开 X 独立浏览器。")
        return 1
    try:
        login_state.write_status(status_file, "starting", "请在独立 X 窗口完成本人登录。")
        page.goto(X_HOME, wait_until="domcontentloaded", timeout=45_000)
        deadline = time.monotonic() + max(30, min(int(getattr(a, "timeout", 240)), 300))
        while time.monotonic() < deadline:
            identity = identity_from_page(page)
            if identity["loggedIn"]:
                login_state.write_status(status_file, "success", "X 登录状态已核验。")
                return 0
            page.wait_for_timeout(1000)
        login_state.write_status(status_file, "expired", "等待 X 登录超时。")
        return 1
    except Exception:
        login_state.write_status(status_file, "error", "X 登录窗口未完成核验。")
        return 1
    finally:
        _safe_close(manager, context)


def cmd_whoami(a) -> int:
    result = {"loggedIn": False, "name": "", "uid": ""}
    manager = context = None
    try:
        manager, context, page = _launch(a, headed=False)
        page.goto(X_HOME, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1200)
        result = identity_from_page(page)
    except Exception:
        result["error"] = "x_browser_probe_failed"
    finally:
        if manager is not None and context is not None:
            _safe_close(manager, context)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _atomic_submission(a) -> None:
    path = Path(getattr(a, "submission_file", ""))
    if not path.name:
        raise RuntimeError("missing submission boundary")
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "task_id": getattr(a, "task_id", None),
        "version_id": getattr(a, "version_id", None),
        "operation_id": getattr(a, "operation_id", None),
    }
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _fill_composer(page, text: str) -> None:
    composer = _first_visible(page, [
        '[data-testid="tweetTextarea_0"]',
        'div[role="textbox"][contenteditable="true"]',
    ])
    if composer is None:
        raise RuntimeError("X composer not found")
    composer.click()
    try:
        composer.fill(text)
    except Exception:
        page.keyboard.insert_text(text)
    try:
        current = composer.inner_text() or ""
    except Exception:
        current = ""
    if text and text[:20] not in current:
        raise RuntimeError("X composer did not accept content")


def _upload_images(page, paths: list[str]) -> None:
    if not paths:
        return
    if len(paths) > MAX_IMAGES:
        raise RuntimeError("X browser mode supports at most 4 images")
    files = []
    for raw in paths:
        path = Path(raw).resolve()
        if path.suffix.lower() not in SUPPORTED_IMAGES or not path.is_file():
            raise RuntimeError("unsupported X image")
        files.append(str(path))
    picker = _first_visible(page, [
        'input[data-testid="fileInput"]',
        'input[type="file"][accept*="image"]',
        'input[type="file"]',
    ])
    if picker is None:
        raise RuntimeError("X image picker not found")
    picker.set_input_files(files)
    # Upload UI varies; only wait for at least one preview when media was chosen.
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            if page.locator('[data-testid="attachments"], [data-testid="media"], img[src^="blob:"]').count() > 0:
                return
        except Exception:
            pass
        page.wait_for_timeout(500)
    raise RuntimeError("X image upload did not become ready")


def submit_prepared_post(page, a) -> bool:
    """Click the final submit exactly once, with a persisted ambiguity boundary."""
    button = _first_visible(page, [
        '[data-testid="tweetButton"]',
        '[data-testid="tweetButtonInline"]',
        'button:has-text("Post")',
        'button:has-text("发布")',
    ])
    if button is None:
        raise RuntimeError("X submit button not found")
    try:
        if button.is_disabled():
            raise RuntimeError("X submit button disabled")
    except AttributeError:
        pass
    _atomic_submission(a)
    button.click()

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if "/compose/post" not in (page.url or ""):
                return True
        except Exception:
            pass
        toast = _first_visible(page, ['[data-testid="toast"]', '[role="status"]'])
        if toast is not None:
            try:
                text = (toast.inner_text() or "").lower()
                if any(marker in text for marker in ("sent", "posted", "published", "已发送", "已发布", "发布成功")):
                    return True
            except Exception:
                pass
        composer = _first_visible(page, ['[data-testid="tweetTextarea_0"]'])
        if composer is None:
            return True
        page.wait_for_timeout(500)
    return False


def cmd_publish(a) -> int:
    text = (getattr(a, "desc", None) or getattr(a, "content", None) or "").strip()
    if not text:
        raise RuntimeError("X 正文不能为空")
    if len(text) > 280:
        raise RuntimeError("X 浏览器模式首版正文限制 280 字符")
    if getattr(a, "tags", ""):
        raise RuntimeError("X 话题需直接写入正文")
    content_guard.guard_or_die([text], exec_mode=True,
                               allow_unsafe=getattr(a, "allow_unsafe", False),
                               label="X 浏览器发布内容")
    images = list(getattr(a, "image_paths", None) or [])
    manager, context, page = _launch(a, headed=bool(getattr(a, "headed", True)))
    try:
        page.goto(X_COMPOSE, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(1200)
        identity = identity_from_page(page)
        if not identity["loggedIn"]:
            raise RuntimeError("X 登录态已失效")
        _fill_composer(page, text)
        _upload_images(page, images)
        if not submit_prepared_post(page, a):
            raise RuntimeError("X 点击发布后没有获得可确认的页面信号")
        return 0
    finally:
        _safe_close(manager, context)
