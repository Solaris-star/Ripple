from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json

import pytest

from ripple.publishing import WorkflowError
from ripple.remote_posts import RemoteActionExecuteInput, RemoteActionInput, RemoteSyncInput
from ripple.workspace import ReceiptInput, WorkspaceService


ACCOUNT = 'a' * 32
OWNER = '5fc0ed9400000000010068de'


@pytest.fixture
def service(tmp_path):
    work = WorkspaceService(tmp_path / 'outputs', private=tmp_path / 'private')
    with work.store.transaction() as state:
        state.setdefault('accounts', {})[ACCOUNT] = {
            'id': ACCOUNT, 'platform': 'xiaohongshu', 'label': '测试账号',
            'status': 'connected', 'adapter': 'native', 'auth_revision': 3,
            'identity': {'logged_in': True, 'name': '测试', 'remote_id': OWNER},
            'operation': None, 'execution_node_id': 'local',
        }
    yield work
    work.close()


def note(number: int, *, title: str = '同名作品', body: str = '', detailed: bool = False) -> dict:
    remote_id = f'{number:024x}'
    return {'platform': 'xiaohongshu', 'account_remote_id': OWNER, 'remote_id': remote_id,
            'version_ids': [remote_id], 'kind': 'image', 'title': title, 'body': body,
            'topics': [], 'media': [{'url': f'https://example.test/{number}.jpg'}], 'url': '',
            'remote_status': 'reviewing', 'visibility': 'unknown', 'checked_at': '2026-09-27T10:00:00+00:00',
            'detail_complete': detailed, 'edit_available': detailed}


def test_sync_pages_deduplicates_and_keeps_partial_state(service, monkeypatch):
    first = [note(i) for i in range(1, 32)]
    second = [note(31), note(32)]
    calls = []
    def worker(_account, operation, _operation_id, **kwargs):
        assert operation == 'remote_posts_read'
        cursor = kwargs['remote_params']['cursor']
        calls.append(cursor)
        return {'state': 'success', 'data': {'items': first if cursor == '0' else second,
                'account_remote_id': OWNER, 'complete': cursor != '0',
                'next_cursor': '31' if cursor == '0' else None}}
    monkeypatch.setattr(service.accounts, 'run', worker)
    first_result = service.remote_posts.sync(RemoteSyncInput(account_id=ACCOUNT, limit=31))
    assert first_result['sync']['complete'] is False
    assert first_result['sync']['next_cursor'] == '31'
    second_result = service.remote_posts.sync(RemoteSyncInput(account_id=ACCOUNT, cursor='31'))
    assert second_result['sync']['complete'] is True
    assert service.remote_posts.list(ACCOUNT, limit=100)['total'] == 32
    assert calls == ['0', '31']


def test_failed_sync_keeps_old_rows(service, monkeypatch):
    monkeypatch.setattr(service.accounts, 'run', lambda *_a, **_k: {'state': 'success', 'data': {
        'items': [note(1)], 'account_remote_id': OWNER, 'complete': True, 'next_cursor': None}})
    service.remote_posts.sync(RemoteSyncInput(account_id=ACCOUNT))
    monkeypatch.setattr(service.accounts, 'run', lambda *_a, **_k: {'state': 'read_failed', 'message': 'read_unconfirmed'})
    with pytest.raises(WorkflowError, match='read_unconfirmed'):
        service.remote_posts.sync(RemoteSyncInput(account_id=ACCOUNT))
    assert service.remote_posts.list(ACCOUNT)['total'] == 1


def test_normal_empty_sync_is_distinct_from_failure(service, monkeypatch):
    monkeypatch.setattr(service.accounts, 'run', lambda *_a, **_k: {'state': 'success', 'data': {
        'items': [], 'account_remote_id': OWNER, 'complete': True, 'next_cursor': None}})
    result = service.remote_posts.sync(RemoteSyncInput(account_id=ACCOUNT))
    assert result['items'] == [] and result['sync']['complete'] is True
    assert service.remote_posts.list(ACCOUNT)['total'] == 0


def test_link_uses_id_or_owned_url_and_never_equal_title(service):
    nid = note(1)['remote_id']
    with service.store.transaction() as state:
        for suffix, receipt in [('id', {'remote_id': nid}),
                                ('link', {'public_url': 'https://www.xiaohongshu.com/explore/' + nid}),
                                ('title', {})]:
            state['tasks'][suffix] = {'id': suffix, 'content': {'account_id': ACCOUNT, 'title': '同名作品'},
                                       'receipt': receipt}
    with service.store.transaction() as state:
        row = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], note(1))
    assert set(row['task_ids']) == {'id', 'link'}
    assert row['visibility'] == 'unknown'
    assert row['remote_status'] == 'reviewing'
    assert not row['url']


def test_wrong_account_response_is_rejected(service):
    bad = note(1)
    bad['account_remote_id'] = 'another-owner'
    with pytest.raises(WorkflowError, match='身份'):
        with service.store.transaction() as state:
            service.remote_posts._upsert(state, state['accounts'][ACCOUNT], bad)
    assert service.remote_posts.list(ACCOUNT)['total'] == 0


def test_account_write_uses_saved_browser_channel_after_operation_changes(service, monkeypatch):
    account = service.accounts.get(ACCOUNT)
    account['browser_channel'] = 'chrome'
    account['operation'] = {'id': 'f' * 32, 'kind': 'remote_post_action', 'state': 'running'}
    service.accounts.environment['browser'] = 'msedge'
    captured = {}
    class Input:
        data = b''
        def write(self, data):
            self.data += data
        def close(self):
            pass
    class Process:
        stdin = Input()
        def poll(self):
            return 0
    def launch(*_args, **_kwargs):
        captured['process'] = Process()
        return captured['process']
    monkeypatch.setattr('ripple.accounts.subprocess.Popen', launch)
    monkeypatch.setattr(service.accounts, 'read_result', lambda *_args: {'state': 'success'})
    assert service.accounts.run(account, 'remote_posts_write', 'd' * 32, confirmed=True)['state'] == 'success'
    assert json.loads(captured['process'].stdin.data)['browser_channel'] == 'chrome'


def test_delete_conflict_and_duplicate_execute(service, monkeypatch):
    target = note(1, body='原正文', detailed=True)
    target['remote_status'] = 'published'
    target['visibility'] = 'private'
    with service.store.transaction() as state:
        saved = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], target)
    count = {'write': 0}
    def worker(_account, operation, operation_id, **kwargs):
        if operation == 'remote_posts_read':
            return {'state': 'success', 'data': {'post': deepcopy(target)}}
        count['write'] += 1
        return {'state': 'success', 'data': {'state': 'verified', 'evidence': {
            'kind': 'platform_receipt', 'target_id': target['remote_id'],
            'account_remote_id': OWNER, 'operation_id': operation_id,
            'post_check': 'target_absent_after_reload', 'status': 200}}}
    monkeypatch.setattr(service.accounts, 'run', worker)
    with pytest.raises(WorkflowError, match='变化'):
        service.remote_posts.preview(saved['id'], RemoteActionInput(kind='delete', expected_version='0' * 64,
                                                                    idempotency_key='wrong-version'))
    preview = service.remote_posts.preview(saved['id'], RemoteActionInput(kind='delete',
        expected_version=saved['version'], idempotency_key='delete-once'))
    req = RemoteActionExecuteInput(confirmed=True, operation_id=preview['operation_id'],
                                   expected_version=preview['confirmed_version'], auth_revision=3)
    done = service.remote_posts.execute(preview['id'], req)
    assert done['status'] == 'verified'
    assert service.remote_posts.execute(preview['id'], req)['status'] == 'verified'
    assert count['write'] == 1
    assert service.remote_posts.get(saved['id'])['remote_status'] == 'deleted'


def test_delete_does_not_accept_generic_or_wrong_target_evidence(service):
    target = note(1, detailed=True)
    target['remote_status'] = 'published'
    with service.store.transaction() as state:
        post = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], target)
        action = {'id': 'd' * 32, 'post_id': post['id'], 'account_id': ACCOUNT,
                  'remote_id': target['remote_id'], 'operation_id': 'e' * 32,
                  'kind': 'delete', 'status': 'dispatching', 'snapshot': post,
                  'updated_at': ''}
        state.setdefault('remote_post_actions', {})[action['id']] = action
    result = service.remote_posts._settle_action('d' * 32, {'state': 'verified', 'evidence': {
        'kind': 'platform_receipt', 'target_id': note(2)['remote_id'], 'account_remote_id': OWNER,
        'operation_id': 'e' * 32, 'status': 200, 'post_check': 'target_absent_after_reload'}})
    assert result['status'] == 'unknown_result'
    assert service.remote_posts.get(post['id'])['remote_status'] == 'published'


def test_x_delete_request_without_response_is_not_verified(service):
    owner = 'x-web:owner'
    with service.store.transaction() as state:
        account = state['accounts'][ACCOUNT]
        account.update(platform='x', adapter='x-browser', identity={'remote_id': owner})
        post = service.remote_posts._upsert(state, account, {
            'platform': 'x', 'account_remote_id': owner, 'remote_id': '123',
            'version_ids': ['123'], 'kind': 'original', 'title': '', 'body': '测试',
            'topics': [], 'media': [], 'url': 'https://x.com/owner/status/123',
            'remote_status': 'published', 'visibility': 'unknown', 'detail_complete': True,
            'checked_at': '2026-09-28T08:00:00+00:00',
        })
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'post_id': post['id'], 'account_id': ACCOUNT,
            'remote_id': '123', 'operation_id': 'e' * 32, 'kind': 'delete',
            'status': 'dispatching', 'snapshot': deepcopy(post), 'updated_at': '',
        }
    evidence = {'target_id': '123', 'account_remote_id': owner, 'operation_id': 'e' * 32,
                'request_seen': True, 'post_check': 'target_absent_after_reload'}
    assert service.remote_posts._settle_action('d' * 32, {
        'state': 'verified', 'evidence': evidence})['status'] == 'unknown_result'
    assert service.remote_posts.get(post['id'])['remote_status'] == 'published'


def test_x_delete_marks_every_linked_version_deleted(service):
    owner = 'x-web:owner'
    def version(remote_id, ids):
        return {'platform': 'x', 'account_remote_id': owner, 'remote_id': remote_id,
                'version_ids': ids, 'kind': 'original', 'title': '', 'body': '测试',
                'topics': [], 'media': [], 'url': f'https://x.com/owner/status/{remote_id}',
                'remote_status': 'published', 'visibility': 'unknown', 'detail_complete': True,
                'checked_at': '2026-09-28T08:00:00+00:00'}
    with service.store.transaction() as state:
        account = state['accounts'][ACCOUNT]
        account.update(platform='x', adapter='x-browser', identity={'remote_id': owner})
        old = service.remote_posts._upsert(state, account, version('123', ['123']))
        new = service.remote_posts._upsert(state, account, version('124', ['123', '124']))
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'post_id': new['id'], 'account_id': ACCOUNT,
            'remote_id': '124', 'operation_id': 'e' * 32, 'kind': 'delete',
            'status': 'dispatching', 'snapshot': deepcopy(new), 'updated_at': '',
        }
    evidence = {'kind': 'platform_receipt', 'target_id': '124', 'account_remote_id': owner,
                'operation_id': 'e' * 32, 'status': 200, 'post_check': 'target_absent_after_reload'}
    assert service.remote_posts._settle_action('d' * 32, {'state': 'verified', 'evidence': evidence})['status'] == 'verified'
    for post_id, before in ((old['id'], old), (new['id'], new)):
        after = service.remote_posts.get(post_id)
        assert after['remote_status'] == 'deleted'
        assert after['visibility'] == 'none'
        assert after['version'] != before['version']


@pytest.mark.parametrize('deleted,expected', [(True, 'verified'), (False, 'unknown_result')])
def test_delete_query_uses_target_detail_code_not_list_absence(service, monkeypatch, deleted, expected):
    target = note(1, detailed=True)
    target['remote_status'] = 'published'
    with service.store.transaction() as state:
        post = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], target)
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'post_id': post['id'], 'account_id': ACCOUNT,
            'remote_id': target['remote_id'], 'operation_id': 'e' * 32,
            'kind': 'delete', 'status': 'unknown_result', 'snapshot': deepcopy(post),
            'evidence': {'responses': [{'target_id': target['remote_id'],
                'path': '/note/delete', 'status': 200, 'code': 0, 'success': True}]},
            'updated_at': ''}
        state['accounts'][ACCOUNT]['operation'] = {'id': 'e' * 32, 'state': 'recovery_required'}
    calls = []
    def worker(_account, operation, _operation_id, **kwargs):
        calls.append(kwargs['remote_action'])
        return {'state': 'success', 'data': {'target_id': target['remote_id'],
                'account_remote_id': OWNER, 'code': -9106 if deleted else 404,
                'deleted': deleted}}
    monkeypatch.setattr(service.accounts, 'run', worker)
    result = service.remote_posts.query('d' * 32)
    assert result['status'] == expected
    assert calls == ['deletion_check']


@pytest.mark.parametrize('target_on_second_page,expected', [(False, 'verified'), (True, 'unknown_result')])
def test_x_delete_query_checks_every_page_before_settling(service, monkeypatch, target_on_second_page, expected):
    owner = 'x-web:testowner'
    target = '1234567890123456789'
    with service.store.transaction() as state:
        state['accounts'][ACCOUNT].update(platform='x', identity={'remote_id': owner})
        post = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], {
            'platform': 'x', 'account_remote_id': owner, 'remote_id': target,
            'version_ids': [target], 'kind': 'original', 'title': '', 'body': '测试',
            'topics': [], 'media': [], 'url': f'https://x.com/testowner/status/{target}',
            'remote_status': 'published', 'visibility': 'unknown', 'detail_complete': True,
            'checked_at': '2026-09-27T10:00:00+00:00',
        })
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'post_id': post['id'], 'account_id': ACCOUNT,
            'remote_id': target, 'operation_id': 'e' * 32, 'kind': 'delete',
            'status': 'unknown_result', 'snapshot': deepcopy(post),
            'evidence': {'responses': [{'target_id': target, 'operation': 'DeleteTweet',
                'status': 200}]}, 'updated_at': '',
        }
        state['accounts'][ACCOUNT]['operation'] = {'id': 'e' * 32, 'state': 'recovery_required'}
    monkeypatch.setattr(service.accounts, 'read_result', lambda *_a: {})
    cursors = []
    def worker(_account, operation, _operation_id, **kwargs):
        assert operation == 'remote_posts_read'
        cursor = kwargs['remote_params']['cursor']
        cursors.append(cursor)
        return {'state': 'success', 'data': {
            'account_remote_id': owner,
            'items': [{'remote_id': target}] if cursor == '50' and target_on_second_page else [],
            'complete': cursor == '50', 'next_cursor': '50' if cursor == '0' else None,
        }}
    monkeypatch.setattr(service.accounts, 'run', worker)
    assert service.remote_posts.query('d' * 32)['status'] == expected
    assert cursors == ['0', '50']


def test_x_delete_without_receipt_records_target_readback_but_stays_unknown(service, monkeypatch):
    from ripple.remote_browser import _x_target_unavailable

    target = '1234567890123456789'
    unavailable = {'entryId': 'tweet-' + target, 'content': {'itemContent': {'tweet_results': {}}}}
    assert _x_target_unavailable({'data': [unavailable]}, target)
    assert not _x_target_unavailable({'data': [unavailable]}, '999')
    assert not _x_target_unavailable({'data': [{**unavailable, 'content': {
        'itemContent': {'tweet_results': {'result': {'rest_id': target}}}}}]}, target)
    owner = 'x-web:owner'
    with service.store.transaction() as state:
        account = state['accounts'][ACCOUNT]
        account.update(platform='x', adapter='x-browser', identity={'remote_id': owner})
        post = service.remote_posts._upsert(state, account, {
            'platform': 'x', 'account_remote_id': owner, 'remote_id': target,
            'version_ids': [target], 'kind': 'original', 'title': '', 'body': '测试',
            'topics': [], 'media': [], 'url': f'https://x.com/owner/status/{target}',
            'remote_status': 'published', 'visibility': 'unknown', 'detail_complete': True,
            'checked_at': '2026-09-28T08:00:00+00:00',
        })
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'post_id': post['id'], 'account_id': ACCOUNT,
            'remote_id': target, 'operation_id': 'e' * 32, 'kind': 'delete',
            'platform': 'x', 'status': 'unknown_result', 'snapshot': deepcopy(post),
            'evidence': {'target_id': target, 'responses': []}, 'updated_at': '',
        }
        account['operation'] = {'id': 'e' * 32, 'state': 'recovery_required'}
    monkeypatch.setattr(service.accounts, 'read_result', lambda *_args: {})
    monkeypatch.setattr(service.accounts, 'run', lambda *_args, **_kwargs: {'state': 'success', 'data': {
        'target_id': target, 'account_remote_id': owner, 'exists': False,
        'target_unavailable': True, 'checked_at': '2026-09-28T08:10:00+00:00'}})
    result = service.remote_posts.query('d' * 32)
    assert result['status'] == 'unknown_result'
    assert result['evidence']['readback']['target_unavailable'] is True
    assert service.accounts.get(ACCOUNT)['operation']['state'] == 'recovery_required'


@pytest.mark.parametrize('profile_complete,target_found,unlocked', [
    (True, False, True), (False, False, False), (True, True, False),
])
def test_x_delete_without_receipt_releases_only_after_complete_target_absence(
        service, monkeypatch, profile_complete, target_found, unlocked):
    owner = 'x-web:owner'
    target = '1234567890123456789'
    operation_id = 'e' * 32
    with service.store.transaction() as state:
        account = state['accounts'][ACCOUNT]
        account.update(platform='x', adapter='x-browser', identity={'remote_id': owner})
        post = service.remote_posts._upsert(state, account, {
            'platform': 'x', 'account_remote_id': owner, 'remote_id': target,
            'version_ids': ['122', target], 'kind': 'original', 'title': '', 'body': '测试',
            'topics': [], 'media': [], 'url': f'https://x.com/owner/status/{target}',
            'remote_status': 'not_found_in_sync', 'visibility': 'unknown', 'detail_complete': True,
            'checked_at': '2026-09-28T08:00:00+00:00',
        })
        linked = service.remote_posts._upsert(state, account, {
            'platform': 'x', 'account_remote_id': owner, 'remote_id': '122',
            'version_ids': ['122'], 'kind': 'original', 'title': '', 'body': '旧版本',
            'topics': [], 'media': [], 'url': 'https://x.com/owner/status/122',
            'remote_status': 'published', 'visibility': 'unknown', 'detail_complete': True,
            'checked_at': '2026-09-28T08:00:00+00:00',
        })
        other = service.remote_posts._upsert(state, account, {
            'platform': 'x', 'account_remote_id': owner, 'remote_id': '888',
            'version_ids': ['888'], 'kind': 'original', 'title': '', 'body': '其他作品',
            'topics': [], 'media': [], 'url': 'https://x.com/owner/status/888',
            'remote_status': 'published', 'visibility': 'unknown', 'detail_complete': True,
            'checked_at': '2026-09-28T08:00:00+00:00',
        })
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'post_id': post['id'], 'account_id': ACCOUNT,
            'remote_id': target, 'operation_id': operation_id, 'kind': 'delete',
            'platform': 'x', 'status': 'unknown_result', 'snapshot': deepcopy(post),
            'evidence': {'target_id': target, 'responses': []}, 'updated_at': '',
        }
        account['operation'] = {'id': operation_id, 'state': 'recovery_required'}
        for stored in state['remote_posts'].values():
            stored['capabilities'] = service.remote_posts._capabilities(stored, account)
    marker = service.accounts.directory(ACCOUNT) / 'operations' / operation_id / 'submission.json'
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({'operation_id': operation_id, 'target_id': target,
                                  'account_remote_id': owner}), encoding='utf-8')
    monkeypatch.setattr(service.accounts, 'read_result', lambda *_args: {})
    calls = []
    def worker(_account, operation, _operation_id, **kwargs):
        action = kwargs['remote_action']
        calls.append(action)
        if action == 'deletion_check':
            return {'state': 'success', 'data': {'target_id': target, 'account_remote_id': owner,
                    'exists': False, 'target_unavailable': True, 'checked_at': '2026-09-28T08:10:00+00:00'}}
        assert action == 'list'
        return {'state': 'success', 'data': {'account_remote_id': owner,
                'items': [{'remote_id': '999', 'version_ids': [target, '999']}] if target_found else [],
                'complete': profile_complete, 'next_cursor': None,
                'checked_at': '2026-09-28T08:11:00+00:00'}}
    monkeypatch.setattr(service.accounts, 'run', worker)
    result = service.remote_posts.query('d' * 32)
    assert calls == ['deletion_check', 'list']
    assert result['status'] == 'unknown_result'
    assert result['evidence']['profile_check']['complete'] is (profile_complete and not target_found)
    assert (service.accounts.get(ACCOUNT)['operation'] is None) is unlocked
    assert service.remote_posts.get(post['id'])['remote_status'] == 'not_found_in_sync'
    assert service.remote_posts.get(post['id'])['capabilities']['delete'] is False
    assert (service.remote_posts.get(linked['id'])['remote_status'] == 'not_found_in_sync') is unlocked
    assert service.remote_posts.get(linked['id'])['capabilities']['delete'] is False
    assert service.remote_posts.get(other['id'])['capabilities']['delete'] is unlocked


def test_x_edit_capability_expires_and_replies_are_not_editable(service):
    account = {'status': 'connected'}
    current = format_datetime(datetime.now(timezone.utc) - timedelta(minutes=5))
    row = {'platform': 'x', 'remote_status': 'published', 'kind': 'reply',
           'created_at': current, 'edit_available': True, 'edits_remaining': 5}
    assert service.remote_posts._capabilities(row, account)['edit'] is False
    row['kind'] = 'original'
    assert service.remote_posts._capabilities(row, account)['edit'] is True
    row['created_at'] = format_datetime(datetime.now(timezone.utc) - timedelta(hours=2))
    assert '1 小时' in service.remote_posts._capabilities(row, account)['edit_reason']


@pytest.mark.parametrize('media_retained,expected', [(True, 'verified'), (False, 'partial')])
def test_x_edit_query_finds_new_version_and_checks_original_media(service, monkeypatch, media_retained, expected):
    owner = 'x-web:owner'
    before_media = [{'id': '456', 'url': 'https://pbs.twimg.com/media/test.png'}]
    original = {'platform': 'x', 'account_remote_id': owner, 'remote_id': '123',
                'version_ids': ['123'], 'kind': 'original', 'title': '', 'body': '原正文',
                'topics': [], 'media': before_media, 'url': 'https://x.com/owner/status/123',
                'remote_status': 'published', 'visibility': 'unknown', 'detail_complete': True,
                'checked_at': '2026-09-28T08:00:00+00:00'}
    updated = {**original, 'remote_id': '124', 'version_ids': ['123', '124'],
               'body': '新正文', 'media': before_media if media_retained else [],
               'url': 'https://x.com/owner/status/124'}
    with service.store.transaction() as state:
        account = state['accounts'][ACCOUNT]
        account.update(platform='x', adapter='x-browser', identity={'remote_id': owner})
        saved = service.remote_posts._upsert(state, account, original)
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'post_id': saved['id'], 'account_id': ACCOUNT,
            'remote_id': '123', 'operation_id': 'e' * 32, 'kind': 'edit',
            'platform': 'x', 'status': 'unknown_result', 'snapshot': deepcopy(saved),
            'changes': {'body': '新正文'}, 'evidence': {'target_id': '123'},
            'updated_at': '',
        }
        account['operation'] = {'id': 'e' * 32, 'state': 'recovery_required'}
    monkeypatch.setattr(service.accounts, 'read_result', lambda *_args: {})
    calls = []
    def worker(_account, operation, _operation_id, **kwargs):
        assert operation == 'remote_posts_read'
        action = kwargs['remote_action']
        calls.append(action)
        return {'state': 'success', 'data': {'items': [updated]} if action == 'list' else {'post': updated}}
    monkeypatch.setattr(service.accounts, 'run', worker)
    result = service.remote_posts.query('d' * 32)
    assert result['status'] == expected
    assert result['evidence']['result_post_id'] == ACCOUNT + ':124'
    assert calls == ['list', 'detail']
    assert service.accounts.get(ACCOUNT)['operation'] is None


def test_xhs_review_label_overrides_generic_posted_tab():
    from ripple.remote_browser import _xhs_row
    from ripple import xhs_browser

    row = _xhs_row({'noteId': note(1)['remote_id'], 'title': '测试', 'text': '审核中\n测试'},
                   OWNER, xhs_browser, {'id': note(1)['remote_id'], 'tab_status': 1})
    assert row['remote_status'] == 'reviewing'
    assert row['visibility'] == 'unknown'


def test_x_timeline_cursor_keeps_partial_sync_partial():
    from ripple.remote_browser import _has_bottom_cursor

    assert _has_bottom_cursor({'instructions': [{'entries': [
        {'entryId': 'cursor-bottom-123', 'content': {'value': 'next'}}]}]}) is True
    assert _has_bottom_cursor({'instructions': [{'entries': [{'entryId': 'tweet-1'}]}]}) is False


def test_x_delete_requires_specific_mutation_response():
    from ripple.remote_browser import _x_delete_proof, _x_delete_response_valid

    assert _x_delete_response_valid({'data': {'delete_tweet': {}}}) is True
    assert _x_delete_response_valid({'data': {'delete_tweet': None}}) is False
    assert _x_delete_response_valid({'errors': [{'message': 'failed'}], 'data': {'delete_tweet': {}}}) is False
    assert _x_delete_response_valid({'data': {}}) is False
    assert _x_delete_response_valid({'data': {'delete_tweet': True}}) is False
    assert _x_delete_response_valid({'data': {'delete_other': {}}}) is False
    receipt = {'operation': 'DeleteTweet', 'target_id': '123',
               'account_remote_id': 'x-web:owner', 'status': 200,
               'kind': 'platform_receipt'}
    args = ('123', 'x-web:owner', 'e' * 32)
    assert _x_delete_proof([receipt], *args, identity_ok=True,
                           target_unavailable=True, article_present=False)['post_check'] == 'target_absent_after_reload'
    assert not _x_delete_proof([receipt], *args, identity_ok=True,
                               target_unavailable=False, article_present=False)
    assert not _x_delete_proof([receipt], *args, identity_ok=False,
                               target_unavailable=True, article_present=False)
    assert not _x_delete_proof([receipt], *args, identity_ok=True,
                               target_unavailable=True, article_present=True)
    assert not _x_delete_proof([{**receipt, 'target_id': '456'}], *args, identity_ok=True,
                               target_unavailable=True, article_present=False)


def test_x_edit_version_ids_are_read_from_nested_control():
    from ripple.x_interactions_browser import extract_posts

    payload = {'data': {'__typename': 'Tweet', 'rest_id': '101',
        'core': {'user_results': {'result': {'legacy': {'screen_name': 'owner'}}}},
        'legacy': {'conversation_id_str': '101', 'full_text': '更新后'},
        'edit_control': {'edit_control_initial': {'edit_tweet_ids': ['100', '101'],
                                                   'edits_remaining': 4}}}}
    assert extract_posts(payload)['101']['edit_ids'] == ['100', '101']


def test_xhs_video_media_reference_is_separate_from_cover():
    from ripple.remote_browser import _xhs_video_reference

    assert _xhs_video_reference('https://sns-video.xhscdn.com/stream/video.mp4?token=private') == {
        'kind': 'video', 'url': 'https://sns-video.xhscdn.com/stream/video.mp4'}
    assert _xhs_video_reference('https://unrelated.example/video.mp4') is None


def test_video_detail_without_original_reference_disables_edit(service):
    video = note(1, body='视频正文', detailed=True)
    video['remote_status'] = 'published'
    video['kind'] = 'video'
    video['media'].append({'kind': 'video', 'url': 'https://sns-video.xhscdn.com/stream/original.mp4'})
    with service.store.transaction() as state:
        complete = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], video)
        missing = {**video, 'detail_complete': False, 'detail_checked': True,
                   'media': video['media'][:1]}
        after = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], missing)
    assert complete['capabilities']['edit'] is True
    assert after['detail_complete'] is False
    assert after['capabilities']['edit'] is False


def test_xhs_create_response_extracts_only_one_explicit_note_id():
    import importlib.util
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / 'skills/shared/scripts/xhs_publish.py'
    spec = importlib.util.spec_from_file_location('xhs_publish_receipt_test', script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    remote_id = '6ab8fb3700000000140029d1'
    assert module._published_note_id({'result': 0, 'success': True, 'data': {'id': remote_id}}) == remote_id
    assert module._published_note_id({'result': {'noteId': remote_id}, 'success': True}) == remote_id
    assert module._published_note_id({'result': 1, 'success': False, 'data': {'id': remote_id}}) == ''
    assert module._published_note_id({'result': 1, 'success': True, 'data': {'id': remote_id}}) == ''
    assert module._published_note_id({'result': 0, 'success': True,
                                      'data': {'id': remote_id, 'note_id': '6ab8f2d4000000000a02053b'}}) == ''


def test_x_browser_checks_account_again_in_publish_page(monkeypatch):
    import importlib.util
    from pathlib import Path
    from types import SimpleNamespace

    script = Path(__file__).resolve().parents[1] / 'skills/shared/scripts/x_browser.py'
    monkeypatch.syspath_prepend(str(script.parent))
    spec = importlib.util.spec_from_file_location('x_browser_account_test', script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    class Page:
        def goto(self, *_args, **_kwargs):
            pass
        def wait_for_timeout(self, _value):
            pass
        def on(self, *_args):
            pass
    monkeypatch.setattr(module.content_guard, 'guard_or_die', lambda *_a, **_k: None)
    monkeypatch.setattr(module, '_launch', lambda *_a, **_k: (None, None, Page()))
    monkeypatch.setattr(module, '_safe_close', lambda *_a: None)
    monkeypatch.setattr(module, 'identity_from_page', lambda _page: {'loggedIn': True, 'uid': 'x-web:other'})
    monkeypatch.setattr(module, '_fill_composer', lambda *_a: pytest.fail('账号错误时不能输入正文'))
    with pytest.raises(RuntimeError, match='账号与所选账号不一致'):
        module.cmd_publish(SimpleNamespace(desc='hello', content=None, tags='', image_paths=[],
                                           headed=False, account_remote_id='x-web:expected'))


@pytest.mark.parametrize('remote_status,task_status', [('reviewing', 'accepted'), ('published', 'published')])
def test_manual_link_checks_owner_without_inventing_visibility(service, monkeypatch, remote_status, task_status):
    nid = note(1)['remote_id']
    task_id = 'task-manual-link'
    version = 'f' * 64
    with service.store.transaction() as state:
        state['tasks'][task_id] = {'id': task_id, 'version_id': version, 'status': 'accepted',
            'content': {'platform': 'xiaohongshu', 'account_id': ACCOUNT, 'mode': 'real'},
            'receipt': {'adapter': 'native', 'result': 'accepted', 'public_url': None},
            'events': [], 'attempts': 1, 'operation_id': 'c' * 32}
    service.accounts.directory(ACCOUNT).mkdir(parents=True)
    target = note(1)
    target['remote_status'] = remote_status
    monkeypatch.setattr(service.accounts, 'run', lambda *_a, **_k: {'state': 'success', 'data': {'post': target}})
    req = ReceiptInput(expected_version=version, confirmed=True,
                       public_url='https://www.xiaohongshu.com/explore/' + nid)
    result = service.confirm_receipt(task_id, req)
    assert result['status'] == task_status
    assert result['receipt']['remote_id'] == nid
    assert result['receipt']['remote_status'] == remote_status
    assert result['receipt']['visibility'] == 'unknown'

    wrong = ReceiptInput(expected_version=version, confirmed=True,
                         public_url='https://www.xiaohongshu.com/explore/' + note(2)['remote_id'])
    with pytest.raises(WorkflowError, match='无法从所选账号'):
        service.confirm_receipt(task_id, wrong)


def test_edit_readback_rejects_changed_media(service, monkeypatch):
    target = note(1, body='原正文', detailed=True)
    target['remote_status'] = 'published'
    with service.store.transaction() as state:
        post = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], target)
        action = {'id': 'f' * 32, 'post_id': post['id'], 'account_id': ACCOUNT,
                  'remote_id': target['remote_id'], 'operation_id': 'e' * 32,
                  'kind': 'edit', 'status': 'unknown_result', 'snapshot': deepcopy(post),
                  'changes': {'body': '新正文'}, 'evidence': None, 'updated_at': ''}
        state.setdefault('remote_post_actions', {})[action['id']] = action
    changed = deepcopy(target)
    changed['body'] = '新正文'
    changed['media'] = [{'url': 'https://example.test/replaced.jpg'}]
    monkeypatch.setattr(service.accounts, 'run', lambda *_a, **_k: {'state': 'success', 'data': {'post': changed}})
    assert service.remote_posts.query('f' * 32)['status'] == 'unknown_result'


def test_target_bound_edit_receipt_keeps_reviewing_distinct(service, monkeypatch):
    target = note(1, body='原正文', detailed=True)
    target['remote_status'] = 'reviewing'
    with service.store.transaction() as state:
        post = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], target)
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'post_id': post['id'], 'account_id': ACCOUNT,
            'remote_id': target['remote_id'], 'operation_id': 'e' * 32,
            'kind': 'edit', 'status': 'unknown_result', 'snapshot': deepcopy(post),
            'changes': {'body': '新正文'}, 'evidence': {'responses': [{
                'target_id': target['remote_id'], 'path': '/note/update', 'status': 200,
                'code': 0, 'success': True}]}, 'updated_at': '', 'platform': 'xiaohongshu'}
        state['accounts'][ACCOUNT]['operation'] = {'id': 'e' * 32, 'state': 'recovery_required'}
    monkeypatch.setattr(service.accounts, 'run', lambda *_a, **_k: {'state': 'success', 'data': {'post': target}})
    result = service.remote_posts.query('d' * 32)
    assert result['status'] == 'reviewing'
    assert service.accounts.get(ACCOUNT)['operation']['state'] == 'recovery_required'


def test_matching_text_during_review_is_not_final_success(service, monkeypatch):
    target = note(1, body='原正文', detailed=True)
    target['remote_status'] = 'reviewing'
    with service.store.transaction() as state:
        post = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], target)
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'post_id': post['id'], 'account_id': ACCOUNT,
            'remote_id': target['remote_id'], 'operation_id': 'e' * 32,
            'kind': 'edit', 'status': 'unknown_result', 'snapshot': deepcopy(post),
            'changes': {'body': '新正文'}, 'evidence': {'responses': [{
                'target_id': target['remote_id'], 'path': '/note/update', 'status': 200,
                'success': True}]}, 'updated_at': '', 'platform': 'xiaohongshu'}
        state['accounts'][ACCOUNT]['operation'] = {'id': 'e' * 32, 'state': 'recovery_required'}
    fresh = deepcopy(target)
    fresh['body'] = '新正文'
    monkeypatch.setattr(service.accounts, 'run', lambda *_a, **_k: {'state': 'success', 'data': {'post': fresh}})
    result = service.remote_posts.query('d' * 32)
    assert result['status'] == 'reviewing'
    assert '审核中' in result['reason']
    assert service.accounts.get(ACCOUNT)['operation'] is None


def test_later_verified_platform_snapshot_closes_review_after_deletion(service, monkeypatch):
    target = note(1, body='原正文', detailed=True)
    target['remote_status'] = 'reviewing'
    with service.store.transaction() as state:
        post = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], target)
        edited = {**post, 'body': '新正文', 'remote_status': 'published',
                  'checked_at': '2026-09-27T11:50:00+00:00'}
        state['remote_posts'][post['id']]['remote_status'] = 'deleted'
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'post_id': post['id'], 'account_id': ACCOUNT,
            'remote_id': target['remote_id'], 'operation_id': 'e' * 32,
            'kind': 'edit', 'status': 'reviewing', 'snapshot': post,
            'changes': {'body': '新正文'}, 'created_at': '2026-09-27T11:40:00+00:00'}
        state['remote_post_actions']['f' * 32] = {
            'id': 'f' * 32, 'post_id': post['id'], 'kind': 'delete',
            'snapshot': edited, 'created_at': '2026-09-27T11:51:00+00:00'}
    monkeypatch.setattr(service.accounts, 'run', lambda *_a, **_k: pytest.fail('无需重新访问已删除作品'))
    result = service.remote_posts.query('d' * 32)
    assert result['status'] == 'verified'
    assert result['evidence']['source_action_id'] == 'f' * 32


def test_partial_edit_readback_preserves_media_and_releases_account(service, monkeypatch):
    target = note(1, body='原正文', detailed=True)
    target['remote_status'] = 'published'
    with service.store.transaction() as state:
        post = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], target)
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'post_id': post['id'], 'account_id': ACCOUNT,
            'remote_id': target['remote_id'], 'operation_id': 'e' * 32,
            'kind': 'edit', 'status': 'unknown_result', 'snapshot': deepcopy(post),
            'changes': {'title': '新标题', 'body': '新正文'}, 'evidence': {'responses': [{
                'target_id': target['remote_id'], 'path': '/note/update', 'status': 200,
                'code': None, 'success': True}]}, 'created_at': '2026-01-01T00:00:00+00:00',
            'updated_at': '', 'platform': 'xiaohongshu'}
        state['accounts'][ACCOUNT]['operation'] = {'id': 'e' * 32, 'state': 'recovery_required'}
    changed = deepcopy(target)
    changed['body'] = '新正文'
    monkeypatch.setattr(service.accounts, 'run', lambda *_a, **_k: {'state': 'success', 'data': {'post': changed}})
    result = service.remote_posts.query('d' * 32)
    assert result['status'] == 'partial'
    assert result['evidence']['missing_fields'] == ['title']
    assert service.accounts.get(ACCOUNT)['operation'] is None


def test_recovery_keeps_uncertain_action(service):
    target = note(1, body='正文', detailed=True)
    target['remote_status'] = 'published'
    with service.store.transaction() as state:
        saved = service.remote_posts._upsert(state, state['accounts'][ACCOUNT], target)
        row = {'id': 'b' * 32, 'post_id': saved['id'], 'account_id': ACCOUNT,
               'remote_id': target['remote_id'], 'operation_id': 'c' * 32,
               'status': 'dispatching', 'kind': 'delete', 'snapshot': saved,
               'changes': {}, 'updated_at': ''}
        state.setdefault('remote_post_actions', {})[row['id']] = row
        state['accounts'][ACCOUNT]['operation'] = {'id': row['operation_id'], 'kind': 'remote_post_action', 'state': 'running'}
    assert service.remote_posts.recover() == 1
    assert service.remote_posts.get_action('b' * 32)['status'] == 'unknown_result'
    assert service.accounts.get(ACCOUNT)['operation']['state'] == 'recovery_required'


def test_new_routes_keep_server_auth_and_csrf(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ripple.api import install

    monkeypatch.setenv('RIPPLE_DEPLOYMENT_MODE', 'server')
    monkeypatch.setenv('RIPPLE_PUBLIC_ORIGIN', 'https://testserver')
    monkeypatch.setenv('RIPPLE_TRUSTED_HOSTS', 'testserver')
    monkeypatch.setenv('RIPPLE_BOOTSTRAP_CODE', 'fixture-bootstrap-code-1234567890')
    app = FastAPI()
    service = install(app, tmp_path / 'outputs', private=tmp_path / 'private')
    try:
        with TestClient(app, base_url='https://testserver') as client:
            assert client.get('/api/ripple/remote-posts').status_code == 401
            assert client.post('/api/ripple/remote-posts/sync', json={'account_id': ACCOUNT}).status_code == 401
            setup = client.post('/api/ripple/auth/setup', json={
                'username': 'owner', 'email': '', 'password': 'correct-horse-battery',
                'setup_code': 'fixture-bootstrap-code-1234567890',
                'workspace_name': 'Test', 'confirmed': True,
            })
            assert setup.status_code == 200
            assert client.get('/api/ripple/remote-posts').status_code == 200
            assert client.post('/api/ripple/remote-posts/sync', json={'account_id': ACCOUNT}).status_code == 403
            headers = {'X-Ripple-CSRF': client.cookies['ripple_csrf']}
            response = client.post('/api/ripple/remote-posts/sync', headers=headers, json={'account_id': ACCOUNT})
            assert response.status_code == 404
    finally:
        service.close()


def test_action_projection_does_not_expose_private_evidence_path(service):
    with service.store.transaction() as state:
        state.setdefault('remote_post_actions', {})['d' * 32] = {
            'id': 'd' * 32, 'status': 'verified', 'evidence': {
                'kind': 'platform_target_readback', 'target_id': note(1)['remote_id'],
                'private_evidence': 'operations/private/browser-evidence.json'}}
    public = service.remote_posts.get_action('d' * 32)
    assert 'private_evidence' not in public['evidence']
    assert public['evidence']['target_id'] == note(1)['remote_id']
