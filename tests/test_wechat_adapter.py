from __future__ import annotations

import base64
import json
from pathlib import Path
import time
import uuid

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from ripple.publishing import ApprovalInput, CreateInput, WorkflowError
from ripple.api import install
from ripple.library import MotherCreate
from ripple.variants import VariantBatchInput, VariantRevision, VariantTarget, VariantTaskCreate
from ripple.wechat_adapter import WeChatClient, WeChatConnectInput, WeChatReconnectInput
from ripple.workspace import WorkspaceService


APP_ID = "wx1234567890abcdef"
APP_SECRET = "0123456789abcdef0123456789abcdef"


class FakeWeChat:
    def __init__(self):
        self.calls: list[tuple[str, str, bytes]] = []
        self.draft_allowed = True
        self.publish_allowed = True
        self.publish_status = 1
        self.token_error: int | None = None
        self.fail_draft_transport = False
        self.fail_submit_transport = False
        self.empty_draft_get = False
        self.cover_uploads = 0
        self.body_uploads = 0
        self.draft_adds = 0
        self.submit_calls = 0
        self.last_article: dict | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = request.content
        self.calls.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/cgi-bin/stable_token":
            payload = json.loads(body)
            assert payload["appid"] == APP_ID and payload["secret"] == APP_SECRET
            if self.token_error:
                return httpx.Response(200, json={"errcode": self.token_error, "errmsg": "fixture error"})
            return httpx.Response(200, json={"access_token": "wechat-token", "expires_in": 7200})
        assert request.url.params.get("access_token") == "wechat-token"
        if path == "/cgi-bin/get_api_domain_ip":
            return httpx.Response(200, json={"ip_list": ["1.1.1.1"]})
        if path == "/cgi-bin/draft/count":
            if not self.draft_allowed:
                return httpx.Response(200, json={"errcode": 48001, "errmsg": "api unauthorized"})
            return httpx.Response(200, json={"total_count": 0})
        if path == "/cgi-bin/freepublish/batchget":
            if not self.publish_allowed:
                return httpx.Response(200, json={"errcode": 48001, "errmsg": "api unauthorized"})
            return httpx.Response(200, json={"total_count": 0, "item_count": 0, "item": []})
        if path == "/cgi-bin/material/add_material":
            assert request.url.params.get("type") == "thumb"
            assert b'image/jpeg' in body
            self.cover_uploads += 1
            return httpx.Response(200, json={"media_id": f"cover-{self.cover_uploads}"})
        if path == "/cgi-bin/media/uploadimg":
            assert b'filename=' in body
            self.body_uploads += 1
            return httpx.Response(200, json={"url": f"https://mmbiz.qpic.cn/fixture/{self.body_uploads}"})
        if path == "/cgi-bin/draft/add":
            self.draft_adds += 1
            if self.fail_draft_transport:
                raise httpx.ReadTimeout("fixture ambiguous draft")
            payload = json.loads(body)
            article = payload["articles"][0]
            self.last_article = article
            assert article["article_type"] == "news"
            assert article["thumb_media_id"].startswith("cover-")
            return httpx.Response(200, json={"media_id": "draft-1"})
        if path == "/cgi-bin/draft/get":
            payload = json.loads(body)
            assert payload["media_id"] == "draft-1"
            if self.empty_draft_get:
                return httpx.Response(200, json={})
            return httpx.Response(200, json={"news_item": [{"title": "fixture", "url": "https://mp.weixin.qq.com/s/draft-preview"}]})
        if path == "/cgi-bin/freepublish/submit":
            self.submit_calls += 1
            if not self.publish_allowed:
                return httpx.Response(200, json={"errcode": 48001, "errmsg": "api unauthorized"})
            if self.fail_submit_transport:
                raise httpx.ReadTimeout("fixture ambiguous publish")
            payload = json.loads(body)
            assert payload["media_id"] == "draft-1"
            return httpx.Response(200, json={"publish_id": 987654321})
        if path == "/cgi-bin/freepublish/get":
            payload = json.loads(body)
            assert payload["publish_id"] == 987654321
            result = {"publish_id": 987654321, "publish_status": self.publish_status}
            if self.publish_status == 0:
                result["article_detail"] = {"count": 1, "item": [{"idx": 1, "article_url": "https://mp.weixin.qq.com/s/public-fixture"}]}
            return httpx.Response(200, json=result)
        raise AssertionError(f"unexpected WeChat request {request.method} {request.url}")


def fake_protect(data: bytes, decrypt=False) -> bytes:
    prefix = b"wechat-fixture-protected:"
    if decrypt:
        assert data.startswith(prefix)
        return data[len(prefix):]
    return prefix + data


@pytest.fixture
def work(tmp_path, monkeypatch):
    from ripple import wechat_adapter
    monkeypatch.setattr(wechat_adapter, "protect", fake_protect)
    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    fake = FakeWeChat()
    service.wechat.client_factory = lambda: WeChatClient(transport=httpx.MockTransport(fake))
    yield service, fake
    service.close()


def connect(service: WorkspaceService, *, key: str | None = None):
    return service.wechat.connect_new(WeChatConnectInput(
        label="我的公众号", app_id=APP_ID, app_secret=APP_SECRET,
        idempotency_key=key or uuid.uuid4().hex, confirmed=True,
    ))


def create_image(service: WorkspaceService, name="cover.png") -> str:
    path = service.outputs / "wechat-media" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (640, 360), "white").save(path, format="PNG")
    return f"wechat-media/{name}"


def approve(service: WorkspaceService, account: dict, *, action="draft", body="正文 **加粗**", media=None):
    media = media or [create_image(service)]
    task = service.create(CreateInput(
        title="微信公众号测试文章", body=body, platform="wechat", account_id=account["id"], mode="real",
        media=media, options={"wechat_action": action, "wechat_author": "Ripple", "wechat_digest": "摘要"},
        idempotency_key=uuid.uuid4().hex,
    ))
    checked = service.preflight(task["id"], task["version_id"])
    assert checked["ok"], checked["problems"]
    return service.approve(task["id"], ApprovalInput(
        expected_version=task["version_id"], confirmed=True, real_publish_confirmed=True,
        expected_account_revision=account["auth_revision"],
    ))


def wait_done(service: WorkspaceService, task_id: str):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = service.get(task_id)
        if task["status"] != "dispatching":
            return task
        time.sleep(.01)
    raise AssertionError("WeChat publisher did not finish")


def test_wechat_connection_encrypts_secret_and_exposes_real_capabilities(work):
    service, _fake = work
    before = next(row for row in service.channels() if row["id"] == "wechat")
    assert before["adapter_available"] is True and before["adapter"] == "wechat-api"
    assert before["connection_methods"] == ["official_api"]
    assert before["connection_options"][0]["status"] == "available"

    account = connect(service, key="wechat-connect-fixture")
    assert account["status"] == "connected" and account["adapter"] == "wechat-api"
    assert set(account["capabilities"]) == {"access_token", "draft", "freepublish"}
    assert account["identity"]["remote_id"] == APP_ID
    credential = service.wechat._credentials_path(account["id"])
    raw = credential.read_text(encoding="utf-8")
    assert APP_SECRET not in raw
    assert APP_SECRET not in service.store.path.read_text(encoding="utf-8")
    channel = next(row for row in service.channels() if row["id"] == "wechat")
    assert channel["connected"] is True and channel["direct_publish"] is True


def test_wechat_missing_freepublish_still_connects_for_draft(work):
    service, fake = work
    fake.publish_allowed = False
    account = connect(service)
    assert account["status"] == "connected"
    assert set(account["capabilities"]) == {"access_token", "draft"}
    assert "发布接口权限" in account["message"]
    draft = approve(service, account, action="draft")
    check = service.preflight(draft["id"], draft["version_id"])
    assert check["ok"] is True
    publish_task = service.create(CreateInput(
        title="发布权限检查", body="正文", platform="wechat", account_id=account["id"], mode="real",
        media=[create_image(service, "publish.png")], options={"wechat_action": "publish"}, idempotency_key=uuid.uuid4().hex,
    ))
    blocked = service.preflight(publish_task["id"], publish_task["version_id"])
    assert not blocked["ok"] and any("freepublish" in item for item in blocked["problems"])


def test_wechat_draft_uploads_cover_body_images_and_never_publicly_submits(work):
    service, fake = work
    account = connect(service)
    cover = create_image(service, "cover.png")
    body_image = create_image(service, "body.png")
    task = approve(service, account, action="draft", body=f"第一段\n\n![正文图]({body_image})", media=[cover, body_image])
    service.dispatch(task["id"], task["version_id"])
    result = wait_done(service, task["id"])
    assert result["status"] == "accepted"
    assert result["receipt"]["draft_media_id"] == "draft-1"
    assert result["receipt"]["draft_only"] is True
    assert result["receipt"]["public_url"] is None
    assert fake.cover_uploads == 1 and fake.body_uploads == 1
    assert fake.draft_adds == 1 and fake.submit_calls == 0
    assert fake.last_article is not None
    assert "https://mmbiz.qpic.cn/fixture/1" in fake.last_article["content"]
    assert body_image not in fake.last_article["content"]
    # Query only verifies the existing draft and never turns it into a publish request.
    again = service.query(task["id"], task["version_id"])
    assert again["status"] == "accepted" and fake.submit_calls == 0


def test_wechat_freepublish_waits_for_official_success_before_published(work):
    service, fake = work
    account = connect(service)
    task = approve(service, account, action="publish")
    service.dispatch(task["id"], task["version_id"])
    accepted = wait_done(service, task["id"])
    assert accepted["status"] == "accepted"
    assert accepted["receipt"]["publish_id"] == "987654321"
    assert accepted["receipt"]["public_url"] is None
    assert fake.submit_calls == 1
    pending = service.query(task["id"], task["version_id"])
    assert pending["status"] == "accepted"
    fake.publish_status = 0
    published = service.query(task["id"], task["version_id"])
    assert published["status"] == "published"
    assert published["receipt"]["public_url"] == "https://mp.weixin.qq.com/s/public-fixture"
    assert fake.submit_calls == 1


def test_wechat_mother_variant_task_pipeline_preserves_official_api_options(work):
    service, fake = work
    account = connect(service)
    cover = create_image(service, "variant-cover.png")
    mother = service.library.create(MotherCreate(
        title="母版公众号文章", body="母版正文", media=[cover], idempotency_key="wechat-mother-fixture",
    ))
    variant = service.variants.create_many(mother["id"], VariantBatchInput(
        expected_source_version=mother["version_id"], idempotency_key="wechat-variant-fixture",
        targets=[VariantTarget(platform="wechat")],
    ))["items"][0]
    content = variant["content"]
    saved = service.variants.revise(variant["id"], VariantRevision(
        project_id=content["project_id"], title=content["title"], body=content["body"], media=content["media"],
        tags=content["tags"], target_id=account["id"], delivery="remote", scheduled_local=None,
        timezone=content["timezone"], fold=None,
        options={"wechat_action": "draft", "wechat_author": "Ripple", "wechat_cover": cover},
        expected_version=variant["version_id"], source_version_id=mother["version_id"],
    ))
    task = service.variants.create_task(saved["id"], VariantTaskCreate(
        expected_version=saved["version_id"], idempotency_key="wechat-task-fixture",
    ))
    assert task["content"]["options"]["wechat_action"] == "draft"
    assert task["content"]["options"]["wechat_cover"] == cover
    checked = service.preflight(task["id"], task["version_id"])
    assert checked["ok"] is True
    approved = service.approve(task["id"], ApprovalInput(
        expected_version=task["version_id"], confirmed=True, real_publish_confirmed=True,
        expected_account_revision=account["auth_revision"],
    ))
    assert "草稿箱" in approved["events"][-1]["note"]
    service.dispatch(task["id"], task["version_id"])
    result = wait_done(service, task["id"])
    assert result["status"] == "accepted" and result["receipt"]["draft_only"] is True
    assert fake.submit_calls == 0


def test_wechat_preflight_requires_cover_and_title_limits(work):
    service, _fake = work
    account = connect(service)
    no_cover = service.create(CreateInput(
        title="无封面", body="正文", platform="wechat", account_id=account["id"], mode="real",
        media=[], options={"wechat_action": "draft"}, idempotency_key=uuid.uuid4().hex,
    ))
    checked = service.preflight(no_cover["id"], no_cover["version_id"])
    assert not checked["ok"] and any("封面" in item for item in checked["problems"])
    too_long = service.create(CreateInput(
        title="标" * 33, body="正文", platform="wechat", account_id=account["id"], mode="real",
        media=[create_image(service, "long.png")], options={"wechat_action": "draft"}, idempotency_key=uuid.uuid4().hex,
    ))
    checked = service.preflight(too_long["id"], too_long["version_id"])
    assert not checked["ok"] and any("32" in item for item in checked["problems"])


def test_wechat_ip_whitelist_error_is_actionable_and_secret_not_persisted(work):
    service, fake = work
    fake.token_error = 40164
    with pytest.raises(WorkflowError, match="IP 白名单"):
        connect(service)
    assert not [row for row in service.accounts.list() if row["platform"] == "wechat"]
    assert APP_SECRET not in service.store.path.read_text(encoding="utf-8")


def test_wechat_ambiguous_draft_creation_is_unknown_and_not_retried(work):
    service, fake = work
    account = connect(service)
    task = approve(service, account, action="draft")
    fake.fail_draft_transport = True
    service.dispatch(task["id"], task["version_id"])
    result = wait_done(service, task["id"])
    assert result["status"] == "unknown_result"
    assert result["receipt"]["not_submitted"] is False
    assert fake.draft_adds == 1
    with pytest.raises(WorkflowError):
        service.dispatch(task["id"], task["version_id"])
    assert fake.draft_adds == 1


def test_wechat_draft_get_must_return_real_article_data(work):
    service, fake = work
    account = connect(service)
    task = approve(service, account, action="draft")
    fake.empty_draft_get = True
    service.dispatch(task["id"], task["version_id"])
    result = wait_done(service, task["id"])
    assert result["status"] == "verification_required"
    assert result["receipt"]["draft_media_id"] == "draft-1"
    assert result["receipt"]["not_submitted"] is False


def test_wechat_durable_progress_failure_keeps_remote_draft_identity(work):
    service, fake = work
    account = connect(service)
    task = approve(service, account, action="publish")
    original_write = service.wechat._write_progress

    def fail_after_remote_draft(account_id, operation_id, value):
        if value.get("draft_media_id") and not value.get("publish_id"):
            raise OSError("fixture disk failure")
        return original_write(account_id, operation_id, value)

    service.wechat._write_progress = fail_after_remote_draft
    service.dispatch(task["id"], task["version_id"])
    result = wait_done(service, task["id"])
    assert result["status"] == "verification_required"
    assert result["receipt"]["draft_media_id"] == "draft-1"
    assert result["receipt"]["not_submitted"] is False
    assert fake.draft_adds == 1 and fake.submit_calls == 0


def test_wechat_reconnect_refuses_active_publish_operation(work):
    service, _fake = work
    account = connect(service)
    with service.store.transaction() as state:
        state["accounts"][account["id"]]["operation"] = {"id": "fixture-op", "kind": "publish", "state": "running"}
    with pytest.raises(WorkflowError, match="不能更新连接凭据"):
        service.wechat.reconnect(account["id"], WeChatReconnectInput(app_id=APP_ID, app_secret=APP_SECRET, confirmed=True))


def test_wechat_verified_draft_does_not_block_local_account_deletion(work):
    service, fake = work
    account = connect(service)
    task = approve(service, account, action="draft")
    service.dispatch(task["id"], task["version_id"])
    result = wait_done(service, task["id"])
    assert result["status"] == "accepted" and result["receipt"]["draft_only"] is True
    deleted = service.delete_account(account["id"], True)
    assert deleted["deleted"] is True
    assert fake.submit_calls == 0


def test_wechat_http_connect_route_never_echoes_app_secret(tmp_path, monkeypatch):
    from ripple import wechat_adapter
    monkeypatch.setattr(wechat_adapter, "protect", fake_protect)
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    fake = FakeWeChat()
    service.wechat.client_factory = lambda: WeChatClient(transport=httpx.MockTransport(fake))
    try:
        with TestClient(app, base_url="http://localhost") as client:
            response = client.post("/api/ripple/wechat/connect", json={
                "label": "API 路由公众号", "app_id": APP_ID, "app_secret": APP_SECRET,
                "idempotency_key": "wechat-http-connect", "confirmed": True,
            })
            assert response.status_code == 201
            payload = response.json()
            assert payload["adapter"] == "wechat-api" and payload["status"] == "connected"
            assert APP_SECRET not in response.text
            assert APP_SECRET not in service.store.path.read_text(encoding="utf-8")
            account_id = payload["id"]
            checked = client.post(f"/api/ripple/accounts/{account_id}/probe", json={"confirmed": True, "headed": False})
            assert checked.status_code == 200 and checked.json()["status"] == "connected"
            invalid = client.post("/api/ripple/wechat/connect", json={
                "label": "错误密钥", "app_id": APP_ID, "app_secret": "visible invalid!",
                "idempotency_key": "wechat-http-invalid", "confirmed": True,
            })
            assert invalid.status_code == 422 and "visible invalid!" not in invalid.text
    finally:
        service.close()


def test_wechat_reconnect_and_disconnect_clear_local_secret(work):
    service, fake = work
    account = connect(service)
    updated = service.wechat.reconnect(account["id"], WeChatReconnectInput(app_id=APP_ID, app_secret=APP_SECRET, confirmed=True))
    assert updated["auth_revision"] == account["auth_revision"] + 1
    path = service.wechat._credentials_path(account["id"])
    assert path.exists()
    disconnected = service.wechat.disconnect(account["id"], True)
    assert disconnected["status"] == "disconnected" and not path.exists()
    fake.publish_allowed = True
