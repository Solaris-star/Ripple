"""REST and batch acceptance using only in-memory HTTP and private fixtures."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import socket
import time
import uuid

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest
from pydantic import ValidationError

from ripple.accounts import AccountInput
from ripple.api import install
from ripple.batches import BatchInput, derive_batch
from ripple.library import MotherCreate, MotherRevision
from ripple.publishing import ApprovalInput, CreateInput, WorkflowError
from ripple.rest_bridge import RestConfig, RemoteImport
from ripple.rest_client import RestClient, RestError, api_base, public_media_url
from ripple.workspace import WorkspaceService


class FakeRemote:
    """All outbound requests are intercepted by httpx.MockTransport."""
    def __init__(self):
        self.calls = []
        self.flows = 0
        self.uploads = []
        self.fail_submit = False
        self.bad_business_code = False
        self.upload_host = 'uploads.example'
        self.remote_status = 0
        self.account_status = 1
        self.platform_supported = True
        self.flow_response_mismatch = False
        self.query_response_mismatch = False
        self.rows = [dict(id='remote-'+p, type=p, nickname='隔离账号-'+p, uid='uid-'+p, status=1) for p in ('twitter','wxGzh','tiktok')]

    def __call__(self, request):
        self.calls.append((request.method, request.url.host, request.url.path, dict(request.headers)))
        path = request.url.path
        if request.url.host == self.upload_host:
            assert request.method == 'PUT'
            assert 'x-api-key' not in request.headers and 'authorization' not in request.headers and 'cookie' not in request.headers
            self.uploads.append(request.read())
            return httpx.Response(200)
        assert request.url.host == 'service.example'
        assert request.headers.get('x-api-key') == 'fixture-api-key'
        if self.bad_business_code:
            return httpx.Response(200, json={'code': 19001, 'message': 'fixture-api-key should never leak', 'data': None})
        if path == '/api/v2/channels/platforms':
            data = [dict(platform=p, displayName={'zh-CN': p}, authType='oauth2', status='available',
                         contentLimits={'maxBodyLength':280,'maxImages':4,'maxVideos':1}, mediaRules={'maxImageSize':20000000},
                         capabilities={'publish':{'supported':self.platform_supported}}, optionSchema={}) for p in ('twitter','wxGzh','tiktok')]
        elif path == '/api/v2/channels/accounts':
            data = {'total': len(self.rows), 'list': self.rows}
        elif path.endswith('/auth-status'):
            data = {'status':self.account_status}
        elif path.startswith('/api/v2/channels/accounts/'):
            data = next(r for r in self.rows if r['id'] == path.rsplit('/',1)[1])
        elif path == '/api/assets/uploadSign':
            body = json.loads(request.content)
            assert body['type'] == 'publishMedia' and body['size'] > 0
            data = {'id': 'asset-1', 'path': 'media/x', 'url': 'https://cdn.example/x',
                    'uploadUrl': f'https://{self.upload_host}/signed?signature=fixture-only'}
        elif path == '/api/assets/asset-1/confirm':
            assert request.method == 'POST'
            data = {'id':'asset-1', 'status':'confirmed', 'url':'https://cdn.example/image.png'}
        elif path == '/api/v2/channels/publish/flows':
            assert request.method == 'POST'
            self.flows += 1
            self.last_payload = json.loads(request.content)
            assert 'title' not in self.last_payload['content']
            assert datetime.fromisoformat(self.last_payload['publishAt']) > datetime.now(timezone.utc)
            if self.fail_submit:
                raise httpx.ReadTimeout('fixture-api-key must not appear in logs')
            data = {'flowId':'flow-1', 'tasks':[{'id':'task-remote-1','accountId':'remote-twitter','platform':'twitter','status':0}]}
            if self.flow_response_mismatch:
                data['tasks'][0]['accountId'] = 'unrelated-account'
        elif path == '/api/v2/channels/publish/flows/flow-1':
            assert request.method == 'GET'
            data = {'flowId':'flow-1', 'tasks':[{'id':'task-remote-1','accountId':'remote-twitter', 'platform':'twitter',
                                              'status':self.remote_status, 'platformWorkId':'fixture-post', 'workLink':'https://x.com/fixture/status/123'}]}
            if self.query_response_mismatch:
                data['tasks'][0]['accountId'] = 'unrelated-account'
        else:
            raise AssertionError('Unexpected request: '+str(request.url))
        return httpx.Response(200, json={'code':0,'data':data}, headers={'Set-Cookie':'api-session=fixture; Path=/'})


def public_resolver(*args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))]


@pytest.fixture
def work(tmp_path, monkeypatch):
    monkeypatch.delenv('RIPPLE_ENABLE_AI', raising=False)
    service = WorkspaceService(tmp_path/'outputs', private=tmp_path/'private')
    yield service
    service.close()


@pytest.fixture
def remote(work):
    fake = FakeRemote()
    transport = httpx.MockTransport(fake)
    work.rest.client_factory = lambda base,key: RestClient(base,key,transport=transport,resolver=public_resolver)
    work.rest.save(RestConfig(base_url='https://service.example', api_key='fixture-api-key', upload_hosts=['uploads.example'], confirmed=True))
    return fake


def connect(work, platform='twitter'):
    info = work.rest.discover(True)
    return work.rest.import_accounts(RemoteImport(discovery_id=info['discovery']['id'], account_ids=['remote-'+platform], confirmed=True))['items'][0]


def source(work):
    return work.library.create(MotherCreate(title='统一内容', body='隔离测试正文', idempotency_key=uuid.uuid4().hex))


def draft(work, account):
    return work.create(CreateInput(title='不发送到 X 的任务名称', body='A fixture-only X post.', platform=account['platform'], account_id=account['id'], mode='real', idempotency_key=uuid.uuid4().hex))


def approve(work, account, task):
    checked=work.preflight(task['id'],task['version_id'])
    assert checked['ok'], checked['problems']
    return work.approve(task['id'],ApprovalInput(expected_version=task['version_id'],confirmed=True,real_publish_confirmed=True,
        expected_account_revision=account['auth_revision'], external_service_confirmed=True))


def wait(work, task_id):
    deadline=time.monotonic()+5
    while time.monotonic()<deadline:
        result=work.get(task_id)
        if result['status']!='dispatching':return result
        time.sleep(.01)
    raise AssertionError('worker did not finish')


def test_batch_creates_target_free_platform_variants_atomically(work):
    m=source(work)
    req=BatchInput(expected_source_version=m['version_id'],idempotency_key='batch-create-001',targets=[
        {'platform':'zhihu'},{'platform':'blog'}])
    result=derive_batch(work,m['id'],req)
    assert len(result['items'])==2
    assert {v['platform'] for v in result['items']}=={'zhihu','blog'}
    assert all(v['source_id']==m['id'] and v['content']['target_id']=='' for v in result['items'])
    assert len(work.library.list()[0]['variants'])==2
    assert work.list()['total']==0


def test_batch_replay_across_threads(work):
    m=source(work)
    req=BatchInput(expected_source_version=m['version_id'],idempotency_key='batch-concurrent-001',targets=[{'platform':'blog'}])
    with ThreadPoolExecutor(6) as pool:
        results=list(pool.map(lambda _:derive_batch(work,m['id'],req),range(6)))
    assert len({r['items'][0]['id'] for r in results})==1
    assert work.list()['total']==0
    assert len(work.variants.list_for_source(m['id']))==1


def test_batch_bad_platform_is_rejected_before_any_variant_is_written(work):
    m=source(work)
    with pytest.raises(ValidationError):
        BatchInput(expected_source_version=m['version_id'],idempotency_key='batch-bad-target',targets=[{'platform':'blog'},{'platform':'unknown-platform'}])
    assert work.variants.list_for_source(m['id'])==[]
    assert work.list()['total']==0


def test_batch_capacity_checked_before_creation(work,monkeypatch):
    from ripple import variants
    monkeypatch.setattr(variants,'MAX_VARIANTS',1)
    m=source(work)
    req=BatchInput(expected_source_version=m['version_id'],idempotency_key='batch-full-store',targets=[{'platform':'blog'},{'platform':'x'}])
    with pytest.raises(WorkflowError):derive_batch(work,m['id'],req)
    assert work.variants.list_for_source(m['id'])==[]
    assert work.list()['total']==0


def test_batch_stale_source_and_duplicate_targets_rejected(work):
    m=source(work)
    work.library.revise(m['id'],MotherRevision(title='updated',expected_version=m['version_id']))
    req=BatchInput(expected_source_version=m['version_id'],idempotency_key='batch-stale-source',targets=[{'platform':'blog'}])
    with pytest.raises(WorkflowError):derive_batch(work,m['id'],req)
    with pytest.raises(ValidationError):
        BatchInput(expected_source_version=m['version_id'],idempotency_key='batch-duplicates',targets=[{'platform':'x'}]*2)


def test_rest_save_does_not_connect_and_secret_never_returned(work,remote):
    assert not remote.calls
    status=work.rest.status()
    assert status['configured'] and not status['connected']
    assert 'fixture-api-key' not in json.dumps(status)
    assert 'fixture-api-key' not in work.rest.path.read_text()


def test_discovery_read_only_and_import_is_explicit(work,remote):
    with pytest.raises(WorkflowError):work.rest.discover(False)
    info=work.rest.discover(True)
    assert all(c[0]=='GET' for c in remote.calls)
    assert len(info['discovery']['accounts'])==3
    assert work.accounts.list()==[]
    a=work.rest.import_accounts(RemoteImport(discovery_id=info['discovery']['id'],account_ids=['remote-twitter'],confirmed=True))['items'][0]
    assert a['status']=='connected' and a['adapter']=='aitoearn-rest' and a['external_id']=='remote-twitter'
    assert a['identity']['remote_id']=='uid-twitter'
    assert 'fixture-api-key' not in work.store.path.read_text(encoding='utf-8')
    assert all(c[0]=='GET' for c in remote.calls)


def test_remote_expired_account_does_not_look_connected(work,remote):
    remote.account_status=0
    a=connect(work)
    assert a['status']=='expired'
    assert not work.preflight(draft(work,a)['id'],work.list()['items'][0]['version_id'])['ok']


def test_account_import_cannot_select_undiscovered_identity(work,remote):
    info=work.rest.discover(True)
    with pytest.raises(WorkflowError):work.rest.import_accounts(RemoteImport(discovery_id=info['discovery']['id'],account_ids=['unknown-id'],confirmed=True))
    assert work.accounts.list()==[]


def test_remote_creation_requires_separate_external_consent(work,remote):
    a=connect(work);t=draft(work,a)
    assert work.preflight(t['id'],t['version_id'])['ok']
    with pytest.raises(WorkflowError):
        work.approve(t['id'],ApprovalInput(expected_version=t['version_id'],confirmed=True,real_publish_confirmed=True,expected_account_revision=a['auth_revision']))
    assert not remote.flows


def test_real_x_bridge_sends_once_and_queries_original_flow(work,remote):
    a=connect(work);t=approve(work,a,draft(work,a))
    with ThreadPoolExecutor(5) as pool:list(pool.map(lambda _:work.dispatch(t['id'],t['version_id']),range(5)))
    result=wait(work,t['id'])
    assert remote.flows==1 and result['attempts']==1
    assert result['status']=='accepted' and result['receipt']['flow_id']=='flow-1'
    assert result['receipt']['public_url'] is None
    checked=work.query(t['id'],t['version_id'])
    assert checked['receipt']['candidate_url']=='https://x.com/fixture/status/123'
    assert checked['status']=='accepted' and remote.flows==1
    assert remote.last_payload['content']['body']==t['content']['body']
    assert remote.last_payload['items'][0]['accountId']=='remote-twitter'


def test_upload_put_excludes_api_key_and_uses_confirmed_url(work,remote):
    a=connect(work)
    path=work.outputs/'ripple-media'/'cover.png';path.parent.mkdir(parents=True)
    Image.new('RGB',(2,2)).save(path)
    t=work.create(CreateInput(title='fixture',body='With an image',media=['ripple-media/cover.png'],platform='x',account_id=a['id'],mode='real',idempotency_key='image-publication-fixture'))
    approve(work,a,t);work.dispatch(t['id'],t['version_id']);result=wait(work,t['id'])
    assert result['status']=='accepted'
    assert remote.uploads==[path.read_bytes()]
    assert remote.last_payload['content']['media']==[{'url':'https://cdn.example/image.png'}]
    assert [c[2] for c in remote.calls if c[0]=='POST'][-3:]==['/api/assets/uploadSign','/api/assets/asset-1/confirm','/api/v2/channels/publish/flows']


def test_lost_create_response_is_unknown_and_never_retried(work,remote):
    a=connect(work);t=approve(work,a,draft(work,a));remote.fail_submit=True
    work.dispatch(t['id'],t['version_id']);result=wait(work,t['id'])
    assert result['status']=='unknown_result' and result['receipt']['not_submitted'] is False
    with pytest.raises(WorkflowError):work.dispatch(t['id'],t['version_id'])
    assert work.query(t['id'],t['version_id'])['status']=='unknown_result'
    assert remote.flows==1 and 'fixture-api-key' not in work.store.path.read_text(encoding='utf-8')
    with pytest.raises(WorkflowError):work.rest.save(RestConfig(base_url='https://other.example',api_key='different-fixture-key',confirmed=True))


def test_flow_and_account_receipts_must_match(work,remote):
    a=connect(work);t=approve(work,a,draft(work,a));remote.flow_response_mismatch=True
    work.dispatch(t['id'],t['version_id']);result=wait(work,t['id'])
    assert result['status']=='unknown_result' and result['receipt']['flow_id']=='flow-1'
    remote.query_response_mismatch=True
    with pytest.raises(WorkflowError):work.query(t['id'],t['version_id'])
    assert work.get(t['id'])['status']=='unknown_result'
    assert remote.flows==1


def test_platform_capability_is_not_inferred_from_account_presence(work,remote):
    remote.platform_supported=False
    a=connect(work);t=draft(work,a)
    assert not work.preflight(t['id'],t['version_id'])['ok']
    assert not next(c for c in work.channels() if c['id']=='x')['direct_publish']


@pytest.mark.parametrize('platform',['wxGzh','tiktok'])
def test_missing_publish_options_remain_blocked(work,remote,platform):
    a=connect(work,platform)
    assert a['status']=='connected'
    t=draft(work,a)
    assert not work.preflight(t['id'],t['version_id'])['ok']
    with pytest.raises(WorkflowError):work.dispatch(t['id'],t['version_id'])
    assert not remote.flows


def test_http_200_business_failure_is_not_success(work,remote):
    remote.bad_business_code=True
    with pytest.raises(RestError) as exc:work.rest.discover(True)
    assert 'fixture-api-key' not in str(exc.value)
    assert not work.rest.status()['connected']


@pytest.mark.parametrize('url',['http://external.example','https://u:p@service.example','https://service.example?token=x','https://service.example/api','file:///tmp'])
def test_rest_base_url_constraints(url):
    with pytest.raises(WorkflowError):api_base(url)


def test_upload_host_and_private_dns_checks():
    with pytest.raises(WorkflowError):public_media_url('https://unapproved.example/x',['uploads.example'],resolver=public_resolver)
    def private(*args,**kwargs):return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',443))]
    with pytest.raises(WorkflowError):public_media_url('https://uploads.example/x',['uploads.example'],resolver=private)
    with pytest.raises(WorkflowError):public_media_url('https://user:pass@uploads.example/x',['uploads.example'],resolver=public_resolver)


def test_config_update_invalidates_pending_approvals(work,remote):
    a=connect(work);t=approve(work,a,draft(work,a))
    work.rest.save(RestConfig(base_url='https://service.example',confirmed=True))
    assert work.get(t['id'])['approval'] is None
    assert work.accounts.get(a['id'])['status']=='disconnected'
    with pytest.raises(WorkflowError):work.dispatch(t['id'],t['version_id'])


def test_remote_cancelled_status_not_optimistic_published(work,remote):
    a=connect(work);t=approve(work,a,draft(work,a))
    work.dispatch(t['id'],t['version_id']);wait(work,t['id']);remote.remote_status=9
    result=work.query(t['id'],t['version_id'])
    assert result['status']=='failed_terminal' and result['receipt']['public_url'] is None


def test_batch_and_rest_http_endpoints_are_guarded(tmp_path,monkeypatch):
    monkeypatch.delenv('RIPPLE_ENABLE_AI',raising=False)
    app=FastAPI();install(app,tmp_path/'outputs',private=tmp_path/'private')
    with TestClient(app,base_url='http://localhost') as client:
        status=client.get('/api/ripple/integrations/aitoearn-rest')
        assert status.status_code==200 and status.json()['configured'] is False
        assert client.post('/api/ripple/integrations/aitoearn-rest/discover',json={'confirmed':False}).status_code==422
        m=client.post('/api/ripple/contents',json={'title':'HTTP mother','idempotency_key':'http-mother-001'}).json()
        result=client.post('/api/ripple/contents/'+m['id']+'/variants',json={'expected_source_version':m['version_id'],'idempotency_key':'http-batch-001','targets':[{'platform':'blog'}]})
        assert result.status_code==201
        variant=result.json()['items'][0]
        assert variant['platform']=='blog' and variant['content']['target_id']==''
        assert client.get('/api/ripple/tasks').json()['total']==0
        blocked=client.post('/api/ripple/integrations/aitoearn-rest/discover',json={'confirmed':True},headers={'origin':'https://evil.example'})
        assert blocked.status_code==403


def test_account_refresh_cannot_revive_submitted_verification_task(work,remote):
    a=connect(work);t=approve(work,a,draft(work,a))
    work.dispatch(t['id'],t['version_id']);wait(work,t['id'])
    remote.remote_status=8
    assert work.query(t['id'],t['version_id'])['status']=='verification_required'
    connect(work)
    current=work.get(t['id'])
    assert current['status']=='verification_required' and current['approval'] is None
    with pytest.raises(WorkflowError):work.dispatch(t['id'],t['version_id'])
    assert remote.flows==1


def test_approval_guard_still_blocks_attempted_task_after_invalid_state_change(work,remote):
    a=connect(work);t=approve(work,a,draft(work,a))
    work.dispatch(t['id'],t['version_id']);wait(work,t['id'])
    with work.store.transaction() as state:state['tasks'][t['id']]['status']='review_ready'
    with pytest.raises(WorkflowError):
        work.approve(t['id'],ApprovalInput(expected_version=t['version_id'],confirmed=True,real_publish_confirmed=True,
            expected_account_revision=a['auth_revision'],external_service_confirmed=True))
    with work.store.transaction() as state:state['tasks'][t['id']]['status']='approved'
    with pytest.raises(WorkflowError):work.dispatch(t['id'],t['version_id'])
    assert remote.flows==1


def test_capability_change_after_approval_stops_before_upload(work,remote):
    a=connect(work);t=approve(work,a,draft(work,a))
    remote.platform_supported=False
    work.dispatch(t['id'],t['version_id']);result=wait(work,t['id'])
    assert result['status']=='verification_required' and result['receipt']['not_submitted'] is True
    assert '能力已变化' in result['events'][-1]['note']
    assert remote.flows==0 and not remote.uploads
