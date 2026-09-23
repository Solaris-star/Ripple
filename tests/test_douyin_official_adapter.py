from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
import uuid

import httpx
import pytest

from ripple import douyin_browser as browser, douyin_campaigns as parser, native_worker
from ripple.accounts import AccountInput
from ripple.ai_providers import AIProviderService
from ripple.campaign_sources import CampaignSourceService
from ripple.workspace import WorkspaceService
from web import app as upstream


def activity(identity="7681577746870326281", **fields):
    return {"activity_id": identity, "show_name": "创作活动", "show_start_time": 1788192000,
            "show_end_time": 1790783999, "jump_link": "", "jump_type": 1, "challenge_ids": [0], **fields}


def payload(*rows):
    return {"status_code": 0, "list": list(rows)}


@pytest.mark.parametrize("data", [
    {"status_code": 0, "query_tags": [{"id": i, "name": "摄影摄像"} for i in range(39)]},
    {"status_code": 0, "income_summary": {"total_num": 39}},
    {"status_code": 0, "list": [{"id": 415, "name": "摄影摄像"}]},
    {"status_code": 1, "list": [activity()]}, {"status_code": False, "list": []},
    {"list": []}, {"status_code": 0, "list": None},
])
def test_non_activity_responses_never_become_successful_empty_lists(data):
    with pytest.raises(parser.DouyinCampaignError):
        parser.parse_activity_list(data)


@pytest.mark.parametrize("url", [
    "https://creator.douyin.com/web/api/v2/creator/activity/tags/query",
    "https://evil.test/web/api/v2/creator/activity/pc/list",
    "https://creator.douyin.com.evil.test/web/api/v2/creator/activity/pc/list",
    "http://creator.douyin.com/web/api/v2/creator/activity/pc/list",
    "https://user@creator.douyin.com/web/api/v2/creator/activity/pc/list",
])
def test_only_observed_official_list_endpoint_is_admitted(url):
    assert not parser.official_list_url(url)


def test_actual_shape_ids_dates_and_response_scope():
    result = parser.parse_activity_list(payload(activity(), activity("7681577746870326282"), activity()))
    assert len(result["items"]) == 2
    assert result["source_count"] == 2
    assert [r["external_id"] for r in result["items"]] == ["7681577746870326281", "7681577746870326282"]
    row = result["items"][0]
    assert row["display_starts_at"] == "2026-09-01T00:00:00+08:00"
    assert row["display_ends_at"] == "2026-09-30T23:59:59+08:00"
    assert "starts_at" not in row and "submit_deadline" not in row
    assert row["url"] == "" and row["detail_status"] == "app_only"
    assert row["challenge_ids"] == []
    assert result["scope"] == "creator_calendar_window"


def test_valid_empty_cap_and_malformed_rows_are_distinct():
    assert parser.parse_activity_list(payload())["items"] == []
    result = parser.parse_activity_list(payload(*[activity(str(i)) for i in range(13)]), limit=10)
    assert len(result["items"]) == 10 and result["source_count"] == 13 and result["truncated"]
    result = parser.parse_activity_list(payload(activity(), {"name": "分类", "id": 415}))
    assert result["rejected_count"] == 1 and result["truncated"]
    assert parser.parse_activity_list({**payload(activity()), "has_more": True})["truncated"]


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/a", "https://localhost/a", "https://api.amemv.com.evil.test/a",
    "https://api.amemv.com@evil.test/a", "https://evil@api.amemv.com/magic/eco/runtime/release/a",
    "https://api.amemv.com:8443/magic/eco/runtime/release/a", "javascript:alert(1)",
    "file:///C:/secret", "https://api.amemv.com/not-allowed", "sslocal://webview?url=https%3A%2F%2F127.0.0.1%2F",
    "sslocal://webview?url=https://evil.test/", "sslocal://other?url=https://www.douyin.com/",
    "https://www.douyin.com/\\@evil.test/", "https://www.douyin.com/\n/a",
])
def test_detail_links_fail_closed(url):
    assert parser.detail_url(url) == ""


def test_webview_unwrap_keeps_page_identity_and_drops_transient_tokens():
    url = "https://api.amemv.com/magic/eco/runtime/release/abc?activity=event1&msToken=never-export&magic_page_no=1"
    target = parser.detail_url("sslocal://webview?url=" + quote(url, safe=""))
    assert target == "https://api.amemv.com/magic/eco/runtime/release/abc?activity=event1&magic_page_no=1"
    assert parser.detail_url("sslocal://webview?url=" + quote("sslocal://webview?url=" + quote("https://evil.test/", safe=""), safe="")) == ""


def test_public_detail_private_dns_and_external_redirect_are_rejected(monkeypatch):
    monkeypatch.setattr(parser.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    with pytest.raises(parser.DouyinCampaignError):
        parser._public_host("https://api.amemv.com/magic/eco/runtime/release/abc")
    monkeypatch.setattr(parser, "_public_host", lambda url: None)
    real_client = httpx.Client
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
    monkeypatch.setattr(parser.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(handler)))
    assert parser.fetch_public_detail("https://api.amemv.com/magic/eco/runtime/release/abc")["detail_status"] == "failed"
    assert len(requests) == 1


def test_rules_use_visible_explicit_sections_without_inventing_fields():
    html = '<script>参与条件：伪造秘密规则</script><h2>参与条件</h2><p>仅限已实名认证的创作者</p><h2>作品要求</h2><p>原创视频</p><h2>奖励设置</h2><p>一等奖 1000 元</p><h2>获奖条件</h2><p>按照官方评审结果选出</p><h2>活动时间</h2><p>投稿截止：2026-10-15</p>'
    rules = parser.parse_public_rules(html)
    assert rules["eligibility"] == ["仅限已实名认证的创作者"]
    assert rules["prizes"] == ["一等奖 1000 元"]
    assert rules["winning_conditions"] == ["按照官方评审结果选出"]
    assert rules["submit_deadline"] == "2026-10-15"
    assert "伪造" not in rules["rule_text"]
    assert parser.parse_public_rules('<p>抖音创作者大会</p>')["detail_status"] == "no_structured_rules"
    image_rules = parser.parse_public_rules('<img src="rules.png">')
    assert image_rules["detail_status"] == "needs_visual_review" and not image_rules["prizes"]


@pytest.fixture
def workspace(tmp_path):
    value = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    yield value
    value.close()


def connected_account(workspace):
    row = workspace.accounts.create(AccountInput(platform="douyin", label="测试", idempotency_key="douyin-official-test"))
    with workspace.store.transaction() as state:
        state["accounts"][row["id"]]["status"] = "connected"
    return workspace.accounts.get(row["id"])


def test_source_mapping_preserves_missing_details_and_precise_scope(workspace, monkeypatch):
    account = connected_account(workspace)
    data = parser.parse_activity_list(payload(activity(), activity("7681577746870326282")))
    data.update(window={"start_time": 1788192000, "end_time": 1790783999}, fetched_at=1790160000)
    calls = []
    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return {"state": "success", "data": data}
    monkeypatch.setattr(workspace.accounts, "run", run)
    service = CampaignSourceService(workspace, AIProviderService(workspace.private))
    rows = service._douyin_portal(service._state())
    assert len(rows) == 2
    assert all(r["source_url"] == "" and r["starts_at"] == "" and r["submit_deadline"] == "" for r in rows)
    assert all(r["qualification_state"] == "unknown" and not r["prizes"] for r in rows)
    assert all(r["account_id"] == account["id"] for r in rows)
    assert "2026-09-01～2026-09-30" in rows[0]["douyin_listing"]["scope_note"]
    assert rows[0]["evidence"]["kind"] == "platform_public_list"
    assert len(calls) == 1 and calls[0][0][1] == "campaign_read" and calls[0][1]["headed"] is False
    assert service._state()["douyin"].get("tikhub_enabled") is False


def test_stable_id_merge_does_not_merge_shared_urls_or_titles(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, "CAMPAIGNS_FILE", tmp_path / "campaigns.json")
    rows = []
    base = {"provider_id": "douyin_creator_portal", "platform": "douyin", "title": "同名官方活动",
            "source_url": "https://creator.douyin.com/", "source_type": "creator_activity_api_v2",
            "source_status": "verified", "account_id": "account-a", "evidence": {"kind": "platform_public_list"},
            "douyin_listing": {"detail_status": "app_only"}}
    first = upstream._merge_campaign_candidate(rows, {**base, "external_id": "douyin:111"})
    second = upstream._merge_campaign_candidate(rows, {**base, "external_id": "douyin:222"})
    assert len(rows) == 2 and first["id"] != second["id"]
    first["saved"] = True
    first["user_confirmed_fields"] = ["prizes"]
    first["prizes"] = ["用户已确认的奖励"]
    again = upstream._merge_campaign_candidate(rows, {**base, "external_id": "douyin:111", "prizes": ["另一个值"]})
    assert len(rows) == 2 and again["id"] == first["id"] and again["saved"]
    assert again["prizes"] == ["用户已确认的奖励"]
    assert second["last_verified_at"] == 0
    upstream._write_campaigns(rows)
    found = upstream._campaign_page_result(platform="douyin", account_id="account-a")
    assert found["total"] == 2 and all(r.get("douyin_listing") for r in found["items"])
    assert upstream._campaign_page_result(platform="douyin", account_id="another")["total"] == 0


def test_login_progress_rotation_and_terminal_qr_cleanup(workspace):
    account = connected_account(workspace)
    operation_id = uuid.uuid4().hex
    with workspace.store.transaction() as state:
        row = state["accounts"][account["id"]]
        row["operation"] = {"id": operation_id, "kind": "login", "state": "running"}
    directory = workspace.accounts.directory(account["id"]) / "operations" / operation_id
    progress = browser._LoginProgress(directory)
    progress.update("starting")
    assert "正在打开" in workspace.accounts.get(account["id"])["message"]
    progress.update("qr_ready", b"test-image-one")
    row = workspace.accounts.get(account["id"])
    assert row["qr_available"] and row["login_state"] == "qr_ready"
    revision = row["qr_revision"]
    progress.update("qr_ready", b"test-image-two")
    assert workspace.accounts.get(account["id"])["qr_revision"] != revision
    for state in ("scanned", "verifying", "expired", "success", "error"):
        progress.update(state)
        assert not (directory / "qr.png").exists()
        assert not workspace.accounts.get(account["id"])["qr_available"]
    progress.update("qr_ready")
    assert workspace.accounts.get(account["id"])["login_state"] == "waiting_user"


def test_worker_passes_operation_directory_to_login(tmp_path, monkeypatch):
    observed = {}
    def login(directory, **kwargs):
        observed.update(kwargs)
        return {"logged_in": True, "name": "测试", "remote_id": "12345"}
    monkeypatch.setattr(browser, "login", login)
    operation_id = uuid.uuid4().hex
    result = native_worker.execute({"platform": "douyin", "operation": "login", "operation_id": operation_id,
                                    "private_dir": str(tmp_path), "headed": True, "browser_channel": "msedge"})
    assert result["state"] == "connected"
    assert observed["run_dir"] == tmp_path / "operations" / operation_id


def test_identity_needs_positive_authenticated_response():
    page = SimpleNamespace(url=parser.HOME, locator=lambda _: SimpleNamespace(inner_text=lambda **kwargs: "加载中"))
    observer = browser._IdentityObserver()
    assert not browser._identity(page, observer=observer)["logged_in"]
    response = SimpleNamespace(url="https://creator.douyin.com/aweme/v1/creator/pc/user/info/", status=200,
                               json=lambda: {"status_code": 0, "uid": "12345"})
    observer.observe(response)
    assert browser._identity(page, observer=observer)["remote_id"] == "12345"
    page.locator = lambda _: SimpleNamespace(inner_text=lambda **kwargs: "扫码登录")
    assert not browser._identity(page, observer=observer)["logged_in"]


def test_events_skip_taxonomy_and_never_click_task_buttons(tmp_path, monkeypatch):
    handlers = []
    data = payload(activity(), activity("7681577746870326282"))
    class Page:
        url = parser.HOME
        def on(self, event, callback): handlers.append(callback)
        def goto(self, *args, **kwargs):
            responses = [
                SimpleNamespace(url=parser.HOME.replace("/creator-micro/home", "/web/api/v2/creator/activity/tags/query"), status=200, headers={"content-type": "application/json"}, body=lambda: json.dumps({"status_code": 0, "query_tags": [{"id": 415, "name": "摄影摄像"}]}).encode()),
                SimpleNamespace(url="https://creator.douyin.com/aweme/v1/creator/pc/user/info/", status=200, json=lambda: {"status_code": 0, "uid": "12345"}),
                SimpleNamespace(url="https://creator.douyin.com" + parser.LIST_PATH + "?start_time=1788192000&end_time=1790783999&msToken=not-exported", status=200, headers={"content-type": "application/json"}, body=lambda: json.dumps(data).encode()),
            ]
            for response in responses:
                for handler in handlers: handler(response)
        def wait_for_timeout(self, value): pass
        def locator(self, value): return SimpleNamespace(inner_text=lambda **kwargs: "创作者中心")
    monkeypatch.setattr(browser, "_launch", lambda *a, **k: (None, None, Page()))
    monkeypatch.setattr(browser, "_close", lambda *a: None)
    result = browser.events(tmp_path)
    assert len(result["items"]) == 2
    assert result["window"] == {"start_time": 1788192000, "end_time": 1790783999}
    assert "not-exported" not in json.dumps(result)
    assert not any(r["title"] == "摄影摄像" for r in result["items"])


def test_taxonomy_repair_is_exact_backed_up_and_preserves_other_platforms(tmp_path):
    import hashlib
    from scripts.douyin_taxonomy_repair import quarantine
    legacy = {"id": "a" * 12, "platform": "douyin", "source_type": "creator_activity_api",
              "source_url": "https://creator.douyin.com/", "external_ids": {"douyin_creator_portal": "douyin:415"},
              "title": "摄影摄像", "saved": False, "updated_at": 1, "last_seen_at": 1}
    other = {"id": "b" * 12, "platform": "xiaohongshu", "title": "保留"}
    path = tmp_path / "outputs/_campaigns.json"
    path.parent.mkdir()
    raw = json.dumps([legacy, other], ensure_ascii=False).encode()
    path.write_bytes(raw)
    sha = hashlib.sha256(raw).hexdigest()
    result = quarantine(path, legacy["id"], sha, path.parent, tmp_path / "private/backups")
    assert Path(result["backup"]).read_bytes() == raw
    assert json.loads(path.read_bytes()) == [other]


@pytest.mark.parametrize("change", [{"saved": True}, {"user_confirmed_fields": ["title"]},
                                    {"updated_at": 2}, {"source_type": "user_import"}, {"prizes": ["100元"]}])
def test_taxonomy_repair_refuses_user_content(change):
    from scripts.douyin_taxonomy_repair import proven_taxonomy
    row = {"platform": "douyin", "source_type": "creator_activity_api", "source_url": "https://creator.douyin.com/",
           "external_ids": {"douyin_creator_portal": "douyin:415"}, "title": "摄影摄像",
           "updated_at": 1, "last_seen_at": 1, **change}
    assert not proven_taxonomy(row)


def test_taxonomy_repair_refuses_references_and_changed_sha(tmp_path):
    from scripts.douyin_taxonomy_repair import inspect, quarantine
    row = {"id": "a" * 12, "platform": "douyin", "source_type": "creator_activity_api",
           "source_url": "https://creator.douyin.com/", "external_ids": {"douyin_creator_portal": "douyin:415"},
           "title": "摄影摄像", "updated_at": 1, "last_seen_at": 1}
    path = tmp_path / "_campaigns.json"
    path.write_text(json.dumps([row]), encoding="utf-8")
    with pytest.raises(ValueError, match="已变化"):
        quarantine(path, row["id"], "stale-sha", tmp_path, tmp_path.parent / "backups")
    (tmp_path / "_ideas.json").write_text(json.dumps([{"campaign_id": row["id"]}]), encoding="utf-8")
    with pytest.raises(ValueError, match="业务引用"):
        inspect(path, row["id"], tmp_path)


def test_qr_endpoint_only_serves_current_ready_operation(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ripple.api import install
    for key in ('RIPPLE_DEPLOYMENT_MODE', 'RIPPLE_PUBLIC_ORIGIN', 'RIPPLE_TRUSTED_HOSTS'):
        monkeypatch.delenv(key, raising=False)
    app = FastAPI()
    service = install(app, tmp_path / 'outputs', private=tmp_path / 'private')
    try:
        account = connected_account(service)
        op_id = uuid.uuid4().hex
        run = service.accounts.directory(account['id']) / 'operations' / op_id
        progress = browser._LoginProgress(run)
        with TestClient(app, base_url='http://localhost') as client:
            # Create this operation after startup recovery has reconciled old jobs.
            with service.store.transaction() as state:
                state['accounts'][account['id']]['operation'] = {'id': op_id, 'kind': 'login', 'state': 'running'}
            url = f"/api/ripple/accounts/{account['id']}/qr/{op_id}"
            progress.update('qr_ready', b'local-test-image')
            response = client.get(url)
            assert response.status_code == 200, response.text
            assert response.headers['cache-control'] == 'no-store'
            assert response.content == b'local-test-image'
            assert client.get(f"/api/ripple/accounts/{account['id']}/qr/{uuid.uuid4().hex}").status_code == 404
            progress.update('scanned')
            # A leftover image cannot revive an operation which is no longer qr_ready.
            (run / 'qr.png').write_bytes(b'old-image')
            assert client.get(url).status_code == 404
            with service.store.transaction() as state:
                state['accounts'][account['id']]['operation']['state'] = 'finished'
            assert client.get(url).status_code == 404
    finally:
        service.close()
