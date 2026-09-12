from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import time
import uuid
from types import SimpleNamespace

from PIL import Image
import pytest

from ripple.accounts import AccountInput, ConnectionAction
from ripple.catalog import X_BROWSER_ADAPTER
from ripple import execution_nodes
from ripple.execution_nodes import LOCAL_NODE_ID
from ripple.publishing import ApprovalInput, CreateInput
from ripple.workspace import WorkspaceService

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/shared/scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
_spec = importlib.util.spec_from_file_location("ripple_test_x_browser", SCRIPTS / "x_browser.py")
assert _spec and _spec.loader
x_browser = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(x_browser)


@pytest.fixture
def work(tmp_path, monkeypatch):
    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    service.accounts.environment = {"browser": "msedge", "browsers": ["msedge", "chrome"], "biliup": True, "requires_extension": False}
    monkeypatch.setattr(service.execution_nodes, "local", lambda: {
        "id": LOCAL_NODE_ID, "name": "Fixture PC", "kind": "local", "platform": "nt", "online": True,
        "capabilities": ["browser.interactive", "browser.automation", "browser.edge", "browser.chrome", "x.login", "x.probe", "x.publish"],
        "browsers": ["msedge", "chrome"], "interactive_browsers": ["msedge", "chrome"], "last_seen": "2026-09-12T00:00:00+00:00",
    })
    yield service
    service.close()


def connected_browser_x(work: WorkspaceService):
    account = work.accounts.create(AccountInput(platform="x", label="X 浏览器测试", idempotency_key=uuid.uuid4().hex))
    with work.store.transaction() as state:
        row = state["accounts"][account["id"]]
        row.update(status="connected", auth_revision=2,
                   identity={"name": "fixture_x", "remote_id": "x-web:fixture_x", "logged_in": True})
    return work.accounts.get(account["id"])


def wait_done(work: WorkspaceService, task_id: str):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        task = work.get(task_id)
        if task["status"] != "dispatching":
            return task
        time.sleep(.01)
    raise AssertionError("browser X publisher did not finish")


def test_x_browser_account_is_local_native_method(work):
    account = work.accounts.create(AccountInput(platform="x", label="浏览器 X", idempotency_key="x-browser-create-fixture"))
    assert account["adapter"] == X_BROWSER_ADAPTER
    assert account["platform"] == "x"
    channel = next(row for row in work.channels() if row["id"] == "x")
    assert channel["adapter"] == "x-multi"
    assert channel["connection_methods"] == ["browser", "api"]
    assert channel["adapter_available"] is True
    assert "auth_token" in channel["scope_note"]


def test_x_browser_login_uses_native_browser_and_keeps_profile_isolated(work, monkeypatch):
    account = work.accounts.create(AccountInput(platform="x", label="浏览器选择", idempotency_key="x-browser-choice-fixture"))
    launched = []
    monkeypatch.setattr(work.execution_nodes, "launch_native_x_login", lambda row, directory, channel: launched.append((row["id"], directory, channel)))
    monkeypatch.setattr(type(work.accounts), "_connect", lambda *args, **kwargs: pytest.fail("first X login must not enter Playwright worker"))
    started = work.accounts.start(account["id"], "login", ConnectionAction(confirmed=True, headed=True, browser_channel="chrome", execution_node_id="local"))
    assert started["operation"]["state"] == "waiting_user"
    assert started["operation"]["browser_channel"] == "chrome"
    assert started["execution_node_id"] == "local"
    assert started["profile_id"] == account["id"]
    assert launched == [(account["id"], work.accounts.directory(account["id"]), "chrome")]
    assert work.accounts.directory(account["id"]) / "browser" != Path.home()


def test_x_browser_profile_cookie_survives_workspace_restart(tmp_path, monkeypatch):
    from playwright.sync_api import sync_playwright
    # Account creation checks the local node's interactive-login capability. This
    # persistence test supplies that prerequisite explicitly so Linux CI does not
    # depend on host browser discovery.
    monkeypatch.setattr(execution_nodes, "interactive_browser_channels", lambda: ["msedge"])
    outputs, private = tmp_path / "outputs", tmp_path / "private"
    first = WorkspaceService(outputs, private=private)
    account = first.accounts.create(AccountInput(platform="x", label="持久登录", idempotency_key="x-persist-restart-fixture"))
    profile = first.accounts.directory(account["id"]) / "browser" / "XProfile"
    first.close()
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), channel="msedge", headless=True)
        context.add_cookies([{"name": "ripple_fixture_session", "value": "persisted", "domain": "x.com", "path": "/", "expires": time.time() + 86400}])
        context.close()
    second = WorkspaceService(outputs, private=private)
    try:
        assert second.accounts.get(account["id"])["id"] == account["id"]
        assert second.accounts.directory(account["id"]) / "browser" / "XProfile" == profile
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(str(profile), channel="msedge", headless=True)
            cookies = context.cookies("https://x.com")
            context.close()
        assert any(c["name"] == "ripple_fixture_session" and c["value"] == "persisted" for c in cookies)
    finally:
        second.close()


def test_x_browser_private_profile_gets_ripple_display_name(tmp_path):
    from playwright.sync_api import sync_playwright
    profile = tmp_path / "browser" / "XProfile"
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), channel="msedge", headless=True)
        context.close()
    x_browser._set_profile_name(profile, "个人账号")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile), channel="msedge", headless=True)
        context.close()
    local = json.loads((profile / "Local State").read_text(encoding="utf-8"))
    prefs = json.loads((profile / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert local["profile"]["info_cache"]["Default"]["name"] == "Ripple · X · 个人账号"
    assert local["profile"]["info_cache"]["Default"]["is_using_default_name"] is False
    assert prefs["profile"]["name"] == "Ripple · X · 个人账号"


def test_x_browser_preflight_supports_text_images_but_rejects_tags_gif(work):
    account = connected_browser_x(work)
    good = work.create(CreateInput(title="本地标题", body="hello x", platform="x", account_id=account["id"], mode="real",
                                   idempotency_key=uuid.uuid4().hex))
    assert work.preflight(good["id"], good["version_id"])["ok"] is True

    tagged = work.create(CreateInput(title="本地标题", body="hello #x", tags="x", platform="x", account_id=account["id"], mode="real",
                                     idempotency_key=uuid.uuid4().hex))
    problems = work.preflight(tagged["id"], tagged["version_id"])["problems"]
    assert any("话题" in problem for problem in problems)

    gif = work.outputs / "ripple-media" / "fixture.gif"
    gif.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (3, 3)).save(gif, format="GIF")
    media = work.create(CreateInput(title="本地标题", body="hello", media=["ripple-media/fixture.gif"], platform="x",
                                    account_id=account["id"], mode="real", idempotency_key=uuid.uuid4().hex))
    problems = work.preflight(media["id"], media["version_id"])["problems"]
    assert any("PNG/JPEG/WebP" in problem for problem in problems)


def test_x_browser_dispatch_uses_existing_approval_and_account_runner(work, monkeypatch):
    account = connected_browser_x(work)
    task = work.create(CreateInput(title="只在本地显示的标题", body="browser mode fixture", platform="x", account_id=account["id"],
                                   mode="real", idempotency_key=uuid.uuid4().hex))
    checked = work.preflight(task["id"], task["version_id"])
    assert checked["ok"]
    approved = work.approve(task["id"], ApprovalInput(expected_version=task["version_id"], confirmed=True,
        real_publish_confirmed=True, expected_account_revision=account["auth_revision"]))
    calls = []
    def fake_run(row, operation, operation_id, **kwargs):
        calls.append((row["adapter"], operation, kwargs["content"]["body"], kwargs["content"]["title"]))
        return {"state": "accepted", "operation_id": operation_id, "task_id": kwargs["task_id"],
                "version_id": kwargs["version_id"], "message": "fixture browser signal", "evidence": "fixture"}
    monkeypatch.setattr(work.accounts, "run", fake_run)
    work.dispatch(approved["id"], approved["version_id"])
    result = wait_done(work, approved["id"])
    assert result["status"] == "accepted" and result["receipt"]["adapter"] == X_BROWSER_ADAPTER
    assert calls == [(X_BROWSER_ADAPTER, "publish", "browser mode fixture", "只在本地显示的标题")]


def test_x_browser_identity_comes_from_visible_profile_link():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page()
        page.set_content('<a data-testid="AppTabBar_Profile_Link" href="/fixture_user">Profile</a>')
        assert x_browser.identity_from_page(page) == {
            "loggedIn": True, "name": "fixture_user", "uid": "x-web:fixture_user"
        }
        browser.close()


def test_x_browser_post_marks_boundary_only_immediately_before_click(tmp_path):
    from playwright.sync_api import sync_playwright
    submission = tmp_path / "submission.json"
    args = SimpleNamespace(submission_file=str(submission), task_id="task", version_id="version", operation_id="op")
    html = '''<!doctype html><html><body>
      <a data-testid="AppTabBar_Profile_Link" href="/fixture_user">Profile</a>
      <div data-testid="tweetTextarea_0" role="textbox" contenteditable="true"></div>
      <input data-testid="fileInput" type="file" multiple>
      <div data-testid="attachments"></div>
      <button data-testid="tweetButton" onclick="history.pushState({},'', '/home')">Post</button>
    </body></html>'''
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.route("http://fixture.local/**", lambda route: route.fulfill(status=200, content_type="text/html", body=html))
        page.goto("http://fixture.local/compose/post")
        x_browser._fill_composer(page, "hello")
        assert not submission.exists()
        assert x_browser.submit_prepared_post(page, args) is True
        assert submission.exists()
        context.close(); browser.close()


def test_x_browser_pre_submit_failure_keeps_not_submitted_boundary_absent(tmp_path):
    from playwright.sync_api import sync_playwright
    submission = tmp_path / "submission.json"
    args = SimpleNamespace(submission_file=str(submission), task_id="task", version_id="version", operation_id="op")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge", headless=True)
        page = browser.new_page()
        page.set_content('<div data-testid="tweetTextarea_0" contenteditable="true"></div>')
        x_browser._fill_composer(page, "hello")
        with pytest.raises(RuntimeError, match="submit button"):
            x_browser.submit_prepared_post(page, args)
        assert not submission.exists()
        browser.close()
