"""Current real-browser acceptance for Ripple Agent + shared persona UX.

Uses isolated Ripple outputs/private state and the operator's ignored local model
configuration. Browser traffic is loopback-only. It never logs into a social
account, approves a publish version, or dispatches publication.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PERSONA = "Mock-科技工具测试号"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    app_port = free_port()
    opencode_port = free_port()
    while opencode_port == app_port:
        opencode_port = free_port()
    url = f"http://127.0.0.1:{app_port}"
    process: subprocess.Popen | None = None
    report = {
        "passed": False,
        "checks": [],
        "browser_errors": [],
        "persona": PERSONA,
        "live_social_login": False,
        "live_publication": False,
        "model_used": True,
        "tool_test_idea_deleted": False,
    }

    with tempfile.TemporaryDirectory(prefix="ripple-agent-smoke-") as temp:
        artifacts = Path(temp) / "artifacts"
        artifacts.mkdir()
        outputs = Path(temp) / "outputs"
        env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "RIPPLE_PORT": str(app_port),
            "RIPPLE_OPENCODE_PORT": str(opencode_port),
            "RIPPLE_OUTPUTS_DIR": str(outputs),
            "RIPPLE_ENABLE_AI": "1",
        }
        log = (artifacts / "browser-server.log").open("w", encoding="utf-8")

        def request(path: str, data=None, method: str | None = None, timeout: int = 30):
            body = None if data is None else json.dumps(data, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url + path,
                data=body,
                method=method,
                headers={"Content-Type": "application/json"},
            )
            with OPENER.open(req, timeout=timeout) as response:
                raw = response.read()
                return json.loads(raw.decode("utf-8")) if raw else None

        def start() -> subprocess.Popen:
            child = subprocess.Popen(
                [sys.executable, "-X", "utf8", str(ROOT / "web/app.py")],
                cwd=ROOT,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
            deadline = time.monotonic() + 35
            last_error = ""
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise AssertionError("Ripple test server exited; inspect browser-server.log")
                try:
                    status = request("/api/status", timeout=25)
                    if status.get("agentRuntime") != "opencode":
                        raise AssertionError(f"Unexpected Agent runtime: {status.get('agentRuntime')!r}")
                    if not status.get("agentReady"):
                        last_error = str(status.get("agentDetail") or "OpenCode not healthy")
                        time.sleep(.5)
                        continue
                    if PERSONA not in {p.get("name") for p in status.get("personas", [])}:
                        raise AssertionError(f"Required ignored smoke persona is missing: {PERSONA}")
                    return child
                except (OSError, urllib.error.URLError) as exc:
                    last_error = str(exc)
                    time.sleep(.25)
            child.terminate()
            child.wait(timeout=10)
            raise AssertionError("Ripple/OpenCode startup timed out: " + last_error[:300])

        def stop() -> None:
            nonlocal process
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            process = None

        def assistant_text(page) -> str:
            bubbles = page.locator(".message-row.assistant .message-bubble.assistant")
            if bubbles.count() == 0:
                return ""
            return bubbles.last.inner_text().strip()

        def send_and_wait(page, text: str, expected: str | None = None, attempts: int = 3) -> str:
            """Send through the real UI; retry only bounded model-rate-limit failures."""
            for attempt in range(attempts):
                box = page.locator(".r2-content-ai-composer textarea")
                expect(box).to_be_visible()
                box.fill(text)
                page.get_by_title("发送", exact=True).click()
                # A streaming request is now active; wait for the normal send button to return.
                page.get_by_title("发送", exact=True).wait_for(state="visible", timeout=120_000)
                current = assistant_text(page)
                if "429" in current and attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                if expected:
                    assert expected in current, current
                return current
            raise AssertionError("Model remained rate-limited after bounded retries")

        smoke_title = f"[Ripple Agent 浏览器验收] {int(time.time())}"
        try:
            process = start()
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 1000})

                def loopback_only(route):
                    if route.request.url.startswith(url + "/"):
                        route.continue_()
                    else:
                        route.abort()

                context.route("**/*", loopback_only)
                page = context.new_page()
                chat_payloads: list[dict] = []
                def capture_request(req):
                    if req.url == url + "/api/chat/stream" and req.method == "POST":
                        try:
                            chat_payloads.append(req.post_data_json)
                        except Exception:
                            pass
                page.on("request", capture_request)
                page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
                page.goto(url, wait_until="networkidle", timeout=60_000)

                # Sidebar product IA: AI collaboration lives in the content workbench, not as a standalone nav entry.
                expect(page.get_by_role("heading", name="工作概览", exact=True)).to_be_visible()
                assert page.get_by_role("button", name="AI 对话", exact=True).count() == 0
                assert page.get_by_text("AI 对话", exact=True).count() == 0
                assert page.get_by_role("button", name="AI 助手", exact=True).count() == 0
                assert page.get_by_role("button", name="账号画像", exact=True).count() == 0
                report["checks"].append("Sidebar has no standalone AI conversation/assistant/profile entry")

                # Shared persona state: Dashboard -> sidebar -> Ideas, with no navigation on switch.
                dashboard_persona = page.get_by_label("首页账号画像", exact=True)
                expect(dashboard_persona).to_be_visible()
                dashboard_persona.select_option(label=PERSONA)
                expect(page.get_by_role("heading", name="工作概览", exact=True)).to_be_visible()
                expect(page.locator("select.persona-select")).to_have_value(PERSONA)
                expect(page.get_by_text(f"正在使用「{PERSONA}」", exact=False)).to_be_visible()

                page.get_by_role("button", name="选题中心", exact=True).click()
                page.locator(".subnav").get_by_role("button", name="选题库", exact=True).click()
                idea_persona = page.get_by_label("AI 推荐账号画像", exact=True)
                expect(idea_persona).to_have_value(PERSONA)
                report["checks"].append("Dashboard/sidebar/Ideas share current persona and switching does not force chat navigation")

                # Content workbench is a compact three-pane editor; creating content opens a fresh AI collaboration session.
                page.get_by_role("button", name="内容工作台", exact=True).click()
                assert page.get_by_role("heading", name="内容工作台", exact=True).count() == 0
                expect(page.get_by_text("全部内容", exact=False)).to_be_visible()
                new_content = page.get_by_role("button", name="新建内容", exact=True)
                expect(new_content).to_be_visible()
                assert page.get_by_text("需要润色、套模板、生图、生视频或改写平台风格时", exact=False).count() == 0
                title_input = page.get_by_label("内容标题", exact=True)
                assert title_input.get_attribute("placeholder") is None
                assert page.get_by_text("新内容", exact=True).count() == 0

                list_separator = page.get_by_role("separator", name="调整内容列表宽度", exact=True)
                ai_separator = page.get_by_role("separator", name="调整 AI 协作宽度", exact=True)
                expect(list_separator).to_be_visible(); expect(ai_separator).to_be_visible()
                list_before = page.locator(".r2-content-list").bounding_box()["width"]
                list_separator.press("ArrowRight"); page.wait_for_timeout(80)
                list_after = page.locator(".r2-content-list").bounding_box()["width"]
                assert list_after > list_before, (list_before, list_after)
                ai_before = page.locator(".r2-content-ai-wrap").bounding_box()["width"]
                ai_separator.press("ArrowLeft"); page.wait_for_timeout(80)
                ai_after = page.locator(".r2-content-ai-wrap").bounding_box()["width"]
                assert ai_after > ai_before, (ai_before, ai_after)
                assert page.evaluate("localStorage.getItem('ripple_content_list_width_v1')")
                assert page.evaluate("localStorage.getItem('ripple_content_ai_width_v1')")
                collapse_content = page.get_by_role("button", name="收起全部内容", exact=True)
                expect(collapse_content).to_be_visible()
                expanded_width = page.locator(".r2-content-list").bounding_box()["width"]
                collapse_content.click(); page.wait_for_timeout(80)
                collapsed_width = page.locator(".r2-content-list").bounding_box()["width"]
                assert collapsed_width < 60, (expanded_width, collapsed_width)
                expect(page.get_by_role("button", name="展开全部内容", exact=True)).to_be_visible()
                expect(new_content).to_be_hidden()
                expect(list_separator).to_be_hidden()
                assert page.evaluate("localStorage.getItem('ripple_content_list_collapsed_v1')") == '1'
                page.get_by_role("button", name="展开全部内容", exact=True).click(); page.wait_for_timeout(80)
                restored_width = page.locator(".r2-content-list").bounding_box()["width"]
                assert abs(restored_width - expanded_width) < 2, (expanded_width, restored_width)
                expect(new_content).to_be_visible()
                expect(list_separator).to_be_visible()
                assert page.evaluate("localStorage.getItem('ripple_content_list_collapsed_v1')") == '0'
                report["checks"].append("Content workbench removes the page header/helper copy and persists keyboard-resizable content/AI panes")

                new_content.click()
                expect(page.locator(".r2-content-ai-composer textarea")).to_be_visible()
                expect(page.get_by_role("button", name="保存", exact=True)).to_be_visible()
                agent_label = page.locator(".r2-content-ai .r2-agent-runtime-label")
                expect(agent_label).to_be_visible()
                assert "·" not in agent_label.inner_text(), agent_label.inner_text()
                assert page.locator('.r2-content-ai select[aria-label="Agent"]').count() == 0
                model_select = page.locator('.r2-content-ai select[aria-label="模型"]')
                expect(model_select).to_be_visible()
                expect(model_select.locator("option").first).to_be_attached(timeout=10_000)
                assert model_select.locator("option").count() >= 1
                report["checks"].append("Content workbench shows the bound Agent as a plain label and keeps model selection separate")
                reply = send_and_wait(
                    page,
                    "只回复 OPENCODE_BROWSER_OK，不调用任何工具。",
                    expected="OPENCODE_BROWSER_OK",
                )
                assert chat_payloads and chat_payloads[-1].get("persona") == PERSONA, chat_payloads[-1:]
                sessions = page.evaluate("JSON.parse(localStorage.getItem('ripple_sessions') || '[]')")
                remote = next((s.get("sessionKey") for s in sessions if s.get("sessionKey")), "")
                assert isinstance(remote, str) and remote.strip(), remote
                report["checks"].append("Content workbench AI persists the bound Agent session, streams a reply, and submits the selected persona with the turn")

                # Exercise explicit stop in the browser, then prove the same conversation can continue.
                box = page.locator(".r2-content-ai-composer textarea")
                box.fill("请写一个很长的 80 条编号清单，每条都解释两句，用于测试停止生成。")
                page.get_by_title("发送", exact=True).click()
                stop_button = page.get_by_title("停止", exact=True)
                stop_button.wait_for(state="visible", timeout=15_000)
                stop_button.click()
                page.get_by_title("发送", exact=True).wait_for(state="visible", timeout=15_000)
                followup = send_and_wait(page, "停止测试完成。现在只回复 STOP_RECOVERED。", attempts=3)
                assert "STOP_RECOVERED" in followup, followup
                report["checks"].append("Browser stop returns composer to usable state and the bound Agent conversation continues")

                # Controlled Ripple tool: add one clearly marked idea, verify actual Ripple data, then clean it.
                tool_reply = send_and_wait(
                    page,
                    f"请调用 Ripple 工具，把选题「{smoke_title}」加入选题库，备注写“自动浏览器验收，随后清理”。完成后明确说只是加入选题库，没有创建发布任务。",
                    attempts=3,
                )
                ideas = request("/api/ideas")
                match = next((item for item in ideas if item.get("title") == smoke_title), None)
                assert match and match.get("source") == "Ripple Agent", (tool_reply, match)
                request(f"/api/ideas/{match['id']}", method="DELETE")
                assert all(item.get("title") != smoke_title for item in request("/api/ideas"))
                report["tool_test_idea_deleted"] = True
                report["checks"].append("Controlled Ripple tool writes one idea; no publish action occurs; smoke data is deleted")

                accounts_icon = page.get_by_role("button", name="账号与平台", exact=True).locator("svg").first.evaluate("el => el.innerHTML")
                settings_button = page.get_by_role("button", name="设置", exact=True)
                settings_icon = settings_button.locator("svg").first.evaluate("el => el.innerHTML")
                assert accounts_icon != settings_icon
                settings_button.click()
                expect(page.get_by_role("heading", name="模型配置", exact=True)).to_be_visible()
                expect(page.get_by_role("heading", name="本地环境", exact=True)).to_be_visible()
                browser_capability = page.locator('[data-environment="browser"]')
                bilibili_capability = page.locator('[data-environment="bilibili"]')
                expect(browser_capability).to_contain_text("浏览器自动化")
                expect(bilibili_capability).to_contain_text("B站发布")
                expect(bilibili_capability).to_contain_text("已就绪 · 随项目环境管理")
                first_runtime = page.locator(".r2-agent-runtime-row").first
                status_label = first_runtime.locator(".r2-agent-state span").bounding_box()
                model_label = first_runtime.locator(".r2-agent-bound-model").nth(1).locator("span").bounding_box()
                status_value = first_runtime.locator(".r2-agent-state strong").bounding_box()
                model_value = first_runtime.locator(".r2-agent-bound-model").nth(1).locator("strong").bounding_box()
                assert status_label and model_label and abs(status_label["y"] - model_label["y"]) < 1
                assert status_value and model_value and abs(status_value["y"] - model_value["y"]) < 1
                runtime_rows = page.locator(".r2-agent-runtime-row")
                state_centers = []
                model_centers = []
                for index in range(runtime_rows.count()):
                    row = runtime_rows.nth(index)
                    state_box = row.locator(".r2-agent-state").bounding_box()
                    model_box = row.locator(".r2-agent-bound-model").nth(1).bounding_box()
                    actions_box = row.locator(".r2-agent-runtime-actions").bounding_box()
                    assert state_box and model_box and actions_box
                    assert abs(state_box["width"] - model_box["width"]) < 1
                    assert actions_box["width"] >= 96
                    state_centers.append(state_box["x"] + state_box["width"] / 2)
                    model_centers.append(model_box["x"] + model_box["width"] / 2)
                assert max(state_centers) - min(state_centers) < 1, state_centers
                assert max(model_centers) - min(model_centers) < 1, model_centers
                settings_text = page.locator("body").inner_text()
                assert "AiToEarn" not in settings_text
                assert "msedge" not in settings_text and "Bilibili 上传程序" not in settings_text
                assert "本机回环服务" not in settings_text and "本机回环地址" not in settings_text
                assert "Developer App Client ID" not in settings_text
                report["checks"].append("Settings aligns Agent facts, uses distinct nav icons, and exposes only actionable local environment capabilities")

                assert report["browser_errors"] == [], report["browser_errors"]
                report["passed"] = True
                context.close()
                browser.close()
        except Exception as exc:
            report["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            stop()
            log.close()
            print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
