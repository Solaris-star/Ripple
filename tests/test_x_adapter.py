from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit
import uuid

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from ripple.publishing import ApprovalInput, CreateInput, WorkflowError
from ripple.api import install
from ripple.workspace import WorkspaceService
from ripple.x_adapter import XClient, XConfigInput, XConnectInput, _challenge, callback_url


class FakeX:
    def __init__(self):
        self.calls = []
        self.media = 0
        self.posts = 0
        self.refreshes = 0
        self.fail_post_transport = False
        self.fail_media = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path, dict(request.headers), request.content))
        path = request.url.path
        if path == '/2/oauth2/token':
            body = parse_qs(request.content.decode())
            if body.get('grant_type') == ['refresh_token']:
                self.refreshes += 1
                return httpx.Response(200, json={'access_token':'access-refresh','refresh_token':'refresh-next','expires_in':7200,'scope':' '.join(['tweet.read','tweet.write','users.read','media.write','offline.access'])})
            assert body['client_id'] == ['client-fixture-123']
            assert body['redirect_uri'] == [callback_url()]
            assert body['code_verifier'][0]
            return httpx.Response(200, json={'access_token':'access-initial','refresh_token':'refresh-initial','expires_in':7200})
        if path == '/2/users/me':
            assert request.headers.get('authorization', '').startswith('Bearer access-')
            return httpx.Response(200, json={'data':{'id':'42','username':'ripple_fixture','name':'Ripple Fixture'}})
        if path == '/2/media/upload':
            self.media += 1
            if self.fail_media:
                return httpx.Response(403, json={'error':'fixture secret should not be persisted'})
            body = json.loads(request.content)
            assert body['media_category'] == 'tweet_image'
            assert body['media_type'] == 'image/png'
            assert base64.b64decode(body['media'])
            return httpx.Response(200, json={'data':{'id':f'media-{self.media}'}})
        if path == '/2/tweets':
            self.posts += 1
            if self.fail_post_transport:
                raise httpx.ReadTimeout('ambiguous fixture')
            body = json.loads(request.content)
            assert 'title' not in body
            return httpx.Response(201, json={'data':{'id':'post-123','text':body['text']}})
        raise AssertionError(f'unexpected X request: {request.method} {request.url}')


def fake_protect(data: bytes, decrypt=False) -> bytes:
    if decrypt:
        assert data.startswith(b'fixture-protected:')
        return data[len(b'fixture-protected:'):]
    return b'fixture-protected:' + data


@pytest.fixture
def work(tmp_path, monkeypatch):
    from ripple import x_adapter
    monkeypatch.setattr(x_adapter, 'protect', fake_protect)
    service = WorkspaceService(tmp_path/'outputs', private=tmp_path/'private')
    fake = FakeX()
    service.x.client_factory = lambda: XClient(transport=httpx.MockTransport(fake))
    service.x.save(XConfigInput(client_id='client-fixture-123', confirmed=True))
    yield service, fake
    service.close()


def start_and_finish(work: WorkspaceService):
    result = work.x.connect_new(XConnectInput(label='我的 X', idempotency_key='x-connect-fixture', confirmed=True))
    parsed = urlsplit(result['authorize_url'])
    query = parse_qs(parsed.query)
    assert parsed.scheme == 'https' and parsed.netloc == 'x.com' and parsed.path == '/i/oauth2/authorize'
    assert query['redirect_uri'] == [callback_url()]
    assert query['code_challenge_method'] == ['S256']
    assert set(query['scope'][0].split()) == {'tweet.read','tweet.write','users.read','media.write','offline.access'}
    state = query['state'][0]
    pending = work.x._pending_path(state)
    raw = json.loads(pending.read_text())
    verifier = fake_protect(base64.b64decode(raw['protected_verifier']), decrypt=True).decode()
    assert query['code_challenge'] == [_challenge(verifier)]
    assert verifier not in pending.read_text()
    account = work.x.callback(state, 'one-time-code')
    return account, result


def test_delete_x_api_account_removes_pending_oauth_and_private_credentials(work):
    service, _ = work
    result = service.x.connect_new(XConnectInput(label='待删除 X', idempotency_key='x-delete-pending-fixture', confirmed=True))
    account = result['account']
    state = parse_qs(urlsplit(result['authorize_url']).query)['state'][0]
    pending = service.x._pending_path(state)
    directory = service.accounts.directory(account['id'])
    credential = directory / 'x-credentials.json'; credential.parent.mkdir(parents=True, exist_ok=True); credential.write_text('{"fixture":true}', encoding='utf-8')
    assert pending.exists() and credential.exists()
    deleted = service.delete_account(account['id'], True)
    assert deleted['deleted'] is True and deleted['pending_oauth_removed'] == 1
    assert not pending.exists() and not directory.exists()
    with pytest.raises(WorkflowError):
        service.accounts.get(account['id'])


def approve(work, account, *, body='Ripple native X fixture', media=None):
    task = work.create(CreateInput(title='本地任务名', body=body, platform='x', account_id=account['id'], mode='real',
                                   media=media or [], idempotency_key=uuid.uuid4().hex))
    checked = work.preflight(task['id'], task['version_id'])
    assert checked['ok'], checked['problems']
    return work.approve(task['id'], ApprovalInput(expected_version=task['version_id'], confirmed=True,
        real_publish_confirmed=True, expected_account_revision=account['auth_revision']))


def wait_done(work, task_id):
    deadline = time.monotonic()+5
    while time.monotonic() < deadline:
        result = work.get(task_id)
        if result['status'] != 'dispatching':
            return result
        time.sleep(.01)
    raise AssertionError('X publisher worker did not finish')


def test_pkce_callback_tokens_encrypted_and_identity_connected(work):
    service, _ = work
    account, _ = start_and_finish(service)
    assert account['status'] == 'connected'
    assert account['adapter'] == 'x-api'
    assert account['identity']['name'] == 'ripple_fixture'
    assert account['identity']['remote_id'] == '42'
    credential_path = service.x._credentials_path(account['id'])
    raw = credential_path.read_text()
    assert 'access-initial' not in raw and 'refresh-initial' not in raw
    assert 'access-initial' not in service.store.path.read_text(encoding='utf-8')
    public = json.dumps(service.x.status())
    assert 'access-initial' not in public and 'refresh-initial' not in public


def test_callback_state_is_single_use_and_disconnect_invalidates_pending(work):
    service, _ = work
    account, started = start_and_finish(service)
    state = parse_qs(urlsplit(started['authorize_url']).query)['state'][0]
    with pytest.raises(WorkflowError):
        service.x.callback(state, 'replay-code')
    again = service.x.reconnect(account['id'], True)
    state2 = parse_qs(urlsplit(again['authorize_url']).query)['state'][0]
    service.x.disconnect(account['id'], True)
    # Callback must not be able to revive a locally disconnected account.
    with service.store.transaction() as store:
        current = store['accounts'][account['id']]
        current_revision = current['auth_revision']
    with pytest.raises(WorkflowError):
        service.x.callback(state2, 'late-code')
    assert service.accounts.get(account['id'])['auth_revision'] == current_revision
    assert service.accounts.get(account['id'])['status'] == 'disconnected'


def test_expired_token_refreshes_before_probe(work):
    service, fake = work
    account, _ = start_and_finish(service)
    creds = service.x._credentials(account['id'])
    creds['expires_at'] = 1
    service.x._save_credentials(account['id'], creds)
    checked = service.x.probe(account['id'], True)
    assert checked['status'] == 'connected' and fake.refreshes == 1
    assert 'access-refresh' not in service.x._credentials_path(account['id']).read_text()


def test_native_x_text_post_is_once_and_keeps_manual_publication_boundary(work):
    service, fake = work
    account, _ = start_and_finish(service)
    task = approve(service, account)
    service.dispatch(task['id'], task['version_id'])
    result = wait_done(service, task['id'])
    assert result['status'] == 'accepted' and result['attempts'] == 1
    assert result['receipt']['post_id'] == 'post-123'
    assert result['receipt']['candidate_url'] == 'https://x.com/ripple_fixture/status/post-123'
    assert result['receipt']['public_url'] is None
    assert fake.posts == 1
    # Query reads the existing correlation and never submits another post.
    assert service.query(task['id'], task['version_id'])['status'] == 'accepted'
    assert fake.posts == 1


def test_native_x_image_upload_then_post(work):
    service, fake = work
    account, _ = start_and_finish(service)
    path = service.outputs/'ripple-media'/'x.png'; path.parent.mkdir(parents=True)
    Image.new('RGB',(3,3)).save(path)
    task = approve(service, account, media=['ripple-media/x.png'])
    service.dispatch(task['id'], task['version_id'])
    result = wait_done(service, task['id'])
    assert result['status'] == 'accepted'
    assert fake.media == 1 and fake.posts == 1
    tweet_body = json.loads(next(c[3] for c in fake.calls if c[1] == '/2/tweets'))
    assert tweet_body['media']['media_ids'] == ['media-1']


def test_post_transport_failure_is_unknown_and_never_blind_retried(work):
    service, fake = work
    account, _ = start_and_finish(service)
    task = approve(service, account)
    fake.fail_post_transport = True
    service.dispatch(task['id'], task['version_id'])
    result = wait_done(service, task['id'])
    assert result['status'] == 'unknown_result'
    assert result['receipt']['not_submitted'] is False
    with pytest.raises(WorkflowError):
        service.dispatch(task['id'], task['version_id'])
    assert fake.posts == 1


def test_media_failure_is_proven_before_post(work):
    service, fake = work
    account, _ = start_and_finish(service)
    path = service.outputs/'ripple-media'/'x.png'; path.parent.mkdir(parents=True)
    Image.new('RGB',(3,3)).save(path)
    task = approve(service, account, media=['ripple-media/x.png'])
    fake.fail_media = True
    service.dispatch(task['id'], task['version_id'])
    result = wait_done(service, task['id'])
    assert result['status'] == 'verification_required'
    assert result['receipt']['not_submitted'] is True
    assert fake.posts == 0


def test_x_preflight_scope_and_config_revision(work):
    service, _ = work
    account, _ = start_and_finish(service)
    too_long = service.create(CreateInput(title='x', body='a'*281, platform='x', account_id=account['id'], mode='real', idempotency_key='x-too-long-fixture'))
    check = service.preflight(too_long['id'], too_long['version_id'])
    assert not check['ok'] and any('280' in item for item in check['problems'])
    okay = approve(service, account)
    service.x.save(XConfigInput(client_id='different-client-456', confirmed=True))
    assert service.accounts.get(account['id'])['status'] == 'disconnected'
    assert service.get(okay['id'])['approval'] is None


def test_x_http_config_and_oauth_callback_boundary(tmp_path):
    app = FastAPI()
    install(app, tmp_path/'outputs', private=tmp_path/'private')
    with TestClient(app, base_url='http://localhost') as client:
        status = client.get('/api/ripple/x/config')
        assert status.status_code == 200
        assert status.json()['callback_url'].startswith('http://127.0.0.1:')
        assert status.json()['configured'] is False
        # A user may open Ripple from a link in another app/site; public GET navigation is safe.
        assert client.get('/not-an-api', headers={'sec-fetch-site':'cross-site'}).status_code == 404
        blocked = client.put('/api/ripple/x/config', json={'client_id':'client-fixture-123','confirmed':True}, headers={'origin':'https://evil.example'})
        assert blocked.status_code == 403
        # X navigation is the sole cross-site exception, but invalid state still fails closed.
        callback = client.get('/api/ripple/x/oauth/callback?state=invalid&code=secret-code-never-echo', headers={'sec-fetch-site':'cross-site'})
        assert callback.status_code == 400
        assert 'secret-code-never-echo' not in callback.text and 'invalid' not in callback.text
        assert callback.headers['cache-control'] == 'no-store'
        assert callback.headers['referrer-policy'] == 'no-referrer'


def test_x_channel_is_direct_adapter_without_external_bridge(work):
    service, _ = work
    channels = service.channels()
    x = next(row for row in channels if row['id'] == 'x')
    assert x['adapter_available'] is True and x['adapter'] == 'x-multi'
    assert x['connection_methods'] == ['browser', 'api']
    assert x['formats'] == ['text','images'] and x['status'] == 'not_connected'
    wechat = next(row for row in channels if row['id'] == 'wechat')
    tiktok = next(row for row in channels if row['id'] == 'tiktok')
    assert wechat['status'] == 'not_connected' and wechat['adapter_available'] is True and wechat['adapter'] == 'wechat-api'
    assert tiktok['status'] == 'not_available' and tiktok['adapter_available'] is False
    assert [item['id'] for item in wechat['connection_options']] == ['official_api']
    assert [item['status'] for item in wechat['connection_options']] == ['available']
    assert {item['id'] for item in tiktok['connection_options']} == {'official_api', 'browser'}
    assert {item['status'] for item in tiktok['connection_options']} == {'planned'}
