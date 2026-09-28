from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from ripple.api import install
from ripple.interactions import _analyze
from ripple.publishing import WorkflowError
from tests.test_interactions import service_with_account, seed_remote_source, seed_xhs_locator


@pytest.fixture
def work(tmp_path):
    service, account = service_with_account(tmp_path, connected=True)
    seed_xhs_locator(service, account)
    source = seed_remote_source(service, account)
    yield service, account, source
    service.close()


def draft(work, count=2, key='safety-draft-01'):
    service, account, source = work
    return service.interactions.create_draft(platform='xiaohongshu', account_id=account, source_id=source['id'], kind='reply',
                                              items=[{'id': item['id'], 'reply': '最终回复 ' + item['id']} for item in source['comments'][:count]], idempotency_key=key)


def receipt(row, item):
    return {'state': 'verified', 'data': {'results': [{'id': item['id'], 'status': 'verified', 'evidence': {
        'kind': 'platform_receipt', 'target_id': row['target_id'], 'target_comment_id': item['id'],
        'account_remote_id': 'u1', 'reply_id': 'sent-' + item['id'], 'text': item['reply'],
    }}]}}


def test_empty_comments_are_zero_statistics():
    report = _analyze([])
    assert report['total'] == report['unique_authors'] == 0
    assert report['sentiment']['ratio'] == {'positive': 0, 'neutral': 0, 'negative': 0}
    assert report['keywords'] == []


def test_version_required_and_conflict_never_sends(work, monkeypatch):
    service, _, _ = work
    original = draft(work)
    calls = []
    monkeypatch.setattr(service.interactions, '_send_item', lambda *args: calls.append(args))
    with pytest.raises(WorkflowError, match='缺少草稿版本'):
        service.interactions.execute(original['id'], True)
    updated = service.interactions.update_draft(original['id'], expected_updated_at=original['updated_at'], items=[{**item, 'reply': '新回复'} for item in original['payload']['items']])
    with pytest.raises(WorkflowError, match='重新审阅'):
        service.interactions.execute(original['id'], True, original['updated_at'])
    assert calls == []
    assert service.interactions._get_row(original['id'])['payload'] == updated['payload']


def test_saved_snapshot_and_item_intent_precede_each_send(work, monkeypatch):
    service, _, _ = work
    original = draft(work)
    updated = service.interactions.update_draft(original['id'], expected_updated_at=original['updated_at'], items=[{**item, 'reply': '编辑后确认的完整回复'} for item in original['payload']['items']])
    calls = []
    def send(row, item, operation_id):
        stored = service.interactions._get_row(row['id'])
        assert stored['execution_snapshot']['payload'] == updated['payload']
        target = next(record for record in stored['item_results'] if record['id'] == item['id'])
        assert target['status'] == 'submitting' and target['operation_id'] == operation_id
        calls.append(item['reply'])
        return receipt(row, item)
    monkeypatch.setattr(service.interactions, '_send_item', send)
    result = service.interactions.execute(updated['id'], True, updated['updated_at'])
    assert result['status'] == 'verified'
    assert calls == ['编辑后确认的完整回复'] * 2
    assert service.interactions.execute(updated['id'], True, updated['updated_at']) == result
    assert len(calls) == 2


def test_double_click_returns_inflight_then_original_result(work, monkeypatch):
    service, _, _ = work
    row = draft(work, count=1)
    entered, release = Event(), Event()
    calls = []
    def send(current, item, operation_id):
        calls.append(operation_id)
        entered.set()
        assert release.wait(3)
        return receipt(current, item)
    monkeypatch.setattr(service.interactions, '_send_item', send)
    with ThreadPoolExecutor() as pool:
        future = pool.submit(service.interactions.execute, row['id'], True, row['updated_at'])
        try:
            assert entered.wait(3)
            duplicate = service.interactions.execute(row['id'], True, row['updated_at'])
            assert duplicate['status'] == 'dispatching'
        finally:
            release.set()
        assert future.result()['status'] == 'verified'
    assert len(calls) == 1


def test_unknown_stops_batch_and_only_confirmed_unsent_can_retry(work, monkeypatch):
    service, account, source = work
    source['comments'].append({'id': 'c3', 'content': '第三条', 'nickname': '丙'})
    with service.store.transaction() as state:
        state['interaction_sources'][source['id']] = source
    row = draft(work, count=3)
    calls = []
    def send(current, item, operation_id):
        calls.append(item['id'])
        if item['id'] == 'c1':
            return receipt(current, item)
        raise TimeoutError('模拟点击超时')
    monkeypatch.setattr(service.interactions, '_send_item', send)
    result = service.interactions.execute(row['id'], True, row['updated_at'])
    assert calls == ['c1', 'c2']
    assert [item['status'] for item in result['item_results']] == ['verified', 'unknown_result', 'not_submitted']
    assert service.accounts.get(account)['operation']['state'] == 'recovery_required'
    for ids in [['c1'], ['c2'], ['c1', 'c3']]:
        with pytest.raises(WorkflowError, match='确认未发送'):
            service.interactions.retry_draft(row['id'], expected_updated_at=result['updated_at'], item_ids=ids)
    retry = service.interactions.retry_draft(row['id'], expected_updated_at=result['updated_at'], item_ids=['c3'])
    assert [item['id'] for item in retry['payload']['items']] == ['c3']
    assert service.interactions.retry_draft(row['id'], expected_updated_at=result['updated_at'], item_ids=['c3'])['id'] == retry['id']
    resolved = service.interactions.resolve_unknown(row['id'], item_id='c2', result='not_submitted', confirmed=True, expected_updated_at=result['updated_at'])
    assert resolved['status'] == 'partial'
    assert resolved['item_results'][0]['status'] == 'verified'
    assert service.accounts.get(account)['operation'] is None
    assert service.interactions.execute(row['id'], True, row['updated_at'])['status'] == 'partial'
    assert calls == ['c1', 'c2']


@pytest.mark.parametrize('response', [
    {'state': 'success', 'data': ['invalid-result']},
    {'state': 'verified', 'data': {'results': [{'id': 'c1', 'status': 'verified'}]}},
    {'state': 'verification_required', 'not_submitted': True},
    {'state': 'login_required', 'not_submitted': True},
])
def test_missing_evidence_or_login_verification_stops_rest(work, monkeypatch, response):
    service, _, _ = work
    row = draft(work)
    calls = []
    monkeypatch.setattr(service.interactions, '_send_item', lambda *args: calls.append(args) or response)
    result = service.interactions.execute(row['id'], True, row['updated_at'])
    assert len(calls) == 1
    assert result['item_results'][1]['status'] == 'not_submitted'
    assert result['item_results'][0]['status'] != 'verified'


def test_restart_keeps_sent_and_marks_only_inflight_unknown(work, monkeypatch):
    service, _, source = work
    source['comments'].append({'id': 'c3', 'content': '第三条', 'nickname': '丙'})
    with service.store.transaction() as state:
        state['interaction_sources'][source['id']] = source
    row = draft(work, count=3)
    def send(current, item, operation_id):
        if item['id'] == 'c1':
            return receipt(current, item)
        raise KeyboardInterrupt()
    monkeypatch.setattr(service.interactions, '_send_item', send)
    with pytest.raises(KeyboardInterrupt):
        service.interactions.execute(row['id'], True, row['updated_at'])
    service.interactions.recover()
    result = service.interactions.execute(row['id'], True, row['updated_at'])
    assert [item['status'] for item in result['item_results']] == ['verified', 'unknown_result', 'not_submitted']


def test_twenty_allowed_twenty_one_rejected(work, monkeypatch):
    service, account, source = work
    source['comments'] = [{'id': f'c{i}', 'nickname': '读者', 'content': str(i)} for i in range(21)]
    with service.store.transaction() as state:
        state['interaction_sources'][source['id']] = source
    with pytest.raises(WorkflowError, match='1–20'):
        draft(work, count=21)
    row = draft(work, count=20)
    monkeypatch.setattr(service.interactions, '_send_item', lambda current, item, operation_id: receipt(current, item))
    assert len(service.interactions.execute(row['id'], True, row['updated_at'])['item_results']) == 20
    other = draft(work, count=1, key='second-draft-key')
    with pytest.raises(WorkflowError, match='已有发送记录'):
        service.interactions.execute(other['id'], True, other['updated_at'])


def test_wrong_receipt_account_or_target_is_unknown(work, monkeypatch):
    service, _, _ = work
    row = draft(work, count=1)
    def wrong(current, item, operation_id):
        response = receipt(current, item)
        response['data']['results'][0]['evidence']['account_remote_id'] = 'another-account'
        return response
    monkeypatch.setattr(service.interactions, '_send_item', wrong)
    assert service.interactions.execute(row['id'], True, row['updated_at'])['status'] == 'unknown_result'


def test_old_and_generic_execute_routes_require_version(tmp_path, monkeypatch):
    app = FastAPI()
    service = install(app, tmp_path / 'outputs', private=tmp_path / 'private')
    calls = []
    monkeypatch.setattr(service.accounts, 'run', lambda *a, **k: calls.append(a))
    with TestClient(app, base_url='http://localhost') as client:
        for prefix in ['interactions', 'xiaohongshu/interactions']:
            response = client.post(f'/api/ripple/{prefix}/not-created/execute', json={'confirmed': True})
            assert response.status_code == 422
            assert '刷新' in response.text
    assert calls == []


def test_failed_sync_preserves_previous_comments_and_is_not_zero(work, monkeypatch):
    service, account, source = work
    monkeypatch.setattr(service.xhs_ops, 'comments', lambda *a, **kw: {})
    with pytest.raises(WorkflowError, match='同步失败'):
        service.interactions.remote_comments(account_id=account, target_id='note123')
    assert service.interactions.get_source(source['id'])['comments'] == source['comments']


def test_retry_uses_original_payload_after_source_sample_removed(work, monkeypatch):
    service, _, source = work
    row = draft(work, count=1)
    monkeypatch.setattr(service.interactions, '_send_item', lambda *a: {'state': 'not_submitted', 'not_submitted': True})
    result = service.interactions.execute(row['id'], True, row['updated_at'])
    with service.store.transaction() as state:
        state['interaction_sources'].pop(source['id'])
    retry = service.interactions.retry_draft(row['id'], expected_updated_at=result['updated_at'], item_ids=['c1'])
    assert retry['payload'] == row['payload']
    assert retry['retry_of'] == row['id']


def test_retry_excludes_later_success_and_keeps_original_history(work, monkeypatch):
    service, account, _ = work
    original = draft(work)
    monkeypatch.setattr(service.interactions, '_send_item', lambda *args: {'state': 'unknown_result'})
    stopped = service.interactions.execute(original['id'], True, original['updated_at'])
    retry = service.interactions.retry_draft(original['id'], expected_updated_at=stopped['updated_at'], item_ids=['c2'])
    resolved = service.interactions.resolve_unknown(original['id'], item_id='c1', result='not_submitted', confirmed=True, expected_updated_at=stopped['updated_at'])
    monkeypatch.setattr(service.interactions, '_send_item', lambda row, item, op: receipt(row, item))
    assert service.interactions.execute(retry['id'], True, retry['updated_at'])['status'] == 'verified'
    latest = next(row for row in service.interactions.list(account_id=account)['items'] if row['id'] == original['id'])
    assert latest['retryable_item_ids'] == ['c1']
    assert latest['retry_exclusions'] == [{'id': 'c2', 'interaction_id': retry['id'], 'status': 'verified'}]
    assert all(item['status'] == 'not_submitted' for item in latest['item_results'])
    # 旧页面提交过时的完整集合，后端仍须排除后续已发送项。
    next_retry = service.interactions.retry_draft(original['id'], expected_updated_at=resolved['updated_at'], item_ids=['c1', 'c2'])
    assert [item['id'] for item in next_retry['payload']['items']] == ['c1']
    assert service.interactions.execute(next_retry['id'], True, next_retry['updated_at'])['status'] == 'verified'
    with pytest.raises(WorkflowError, match='没有可重试'):
        service.interactions.retry_draft(original['id'], expected_updated_at=resolved['updated_at'], item_ids=['c1', 'c2'])


@pytest.mark.parametrize('later_status', ['verified', 'unknown_result', 'dispatching'])
def test_retry_excludes_later_results_beyond_list_limit(work, monkeypatch, later_status):
    service, account, _ = work
    original = draft(work)
    monkeypatch.setattr(service.interactions, '_send_item', lambda *args: {'state': 'not_submitted', 'not_submitted': True})
    stopped = service.interactions.execute(original['id'], True, original['updated_at'])
    with service.store.transaction() as state:
        other = deepcopy(stopped)
        other.update(id='later', status=later_status, created_at='1900-01-01', item_results=[{'id': 'c2', 'status': 'submitting' if later_status == 'dispatching' else later_status}])
        other['payload']['items'] = [other['payload']['items'][1]]
        state['interactions']['later'] = other
    listed = service.interactions.list(account_id=account, limit=1)['items'][0]
    assert listed['id'] == original['id'] and listed['retryable_item_ids'] == ['c1']
    retry = service.interactions.retry_draft(original['id'], expected_updated_at=stopped['updated_at'], item_ids=['c1', 'c2'])
    assert [item['id'] for item in retry['payload']['items']] == ['c1']


def test_cancelled_retry_can_be_recreated_idempotently(work, monkeypatch):
    service, _, _ = work
    original = draft(work, count=1)
    monkeypatch.setattr(service.interactions, '_send_item', lambda *args: {'state': 'not_submitted', 'not_submitted': True})
    stopped = service.interactions.execute(original['id'], True, original['updated_at'])
    args = {'expected_updated_at': stopped['updated_at'], 'item_ids': ['c1']}
    retry = service.interactions.retry_draft(original['id'], **args)
    service.interactions.cancel_draft(retry['id'], expected_updated_at=retry['updated_at'])
    fresh = service.interactions.retry_draft(original['id'], **args)
    assert fresh['status'] == 'draft' and fresh['id'] != retry['id']
    assert service.interactions.retry_draft(original['id'], **args)['id'] == fresh['id']


def test_public_source_link_never_overwrites_signed_note_locator(work):
    service, account, source = work
    signed = 'https://www.xiaohongshu.com/explore/note123?xsec_token=private-locator&xsec_source=pc_user'
    service.xhs_ops._save_locators(account, [{'note_id': 'note123', 'url': signed}])
    row = draft(work, count=1)
    assert 'xsec_token' not in row['target_url']
    assert service.xhs_ops._locator(account, note_id='note123')[1] == signed
    assert service.xhs_ops._locator(account, url=source['target_url'])[1] == signed


@pytest.mark.parametrize('evidence', [{}, {'kind': 'platform_receipt', 'target_id': 'another-note', 'target_comment_id': '',
    'account_remote_id': 'u1', 'reply_id': 'comment1', 'text': '测试顶层评论'}])
def test_root_comment_without_bound_receipt_remains_unknown(work, monkeypatch, evidence):
    service, account, source = work
    row = service.interactions.create_draft(platform='xiaohongshu', account_id=account, source_id=source['id'],
        kind='comment', text='测试顶层评论', idempotency_key='root-receipt-test')
    monkeypatch.setattr(service.interactions, '_send_item', lambda *args: {'state': 'verified', 'data': {'status': 'verified', 'evidence': evidence}})
    assert service.interactions.execute(row['id'], True, row['updated_at'])['status'] == 'unknown_result'


@pytest.mark.parametrize('kind,item_id', [('comment', 'comment'), ('delete', 'c1')])
def test_retry_completion_is_shared_by_original_comment_and_delete_tasks(work, monkeypatch, kind, item_id):
    service, account, source = work
    row = service.interactions.create_draft(platform='xiaohongshu', account_id=account, source_id=source['id'],
        kind=kind, text='测试评论' if kind == 'comment' else '', items=[{'id': 'c1'}] if kind == 'delete' else [],
        idempotency_key='retry-completion-' + kind)
    monkeypatch.setattr(service.interactions, '_send_item', lambda *args: {'state': 'not_submitted', 'not_submitted': True})
    stopped = service.interactions.execute(row['id'], True, row['updated_at'])
    retry = service.interactions.retry_draft(row['id'], expected_updated_at=stopped['updated_at'], item_ids=[item_id])
    monkeypatch.setattr(service.interactions, '_send_item', lambda *args: {'state': 'verified', 'data': {'status': 'verified', 'evidence': {
        'kind': 'platform_receipt', 'target_id': 'note123', 'target_comment_id': '',
        'account_remote_id': 'u1', 'reply_id': 'published-root', 'text': '测试评论',
    }}})
    assert service.interactions.execute(retry['id'], True, retry['updated_at'])['status'] == 'verified'
    original = next(item for item in service.interactions.list()['items'] if item['id'] == row['id'])
    assert original['retryable_item_ids'] == []
    with pytest.raises(WorkflowError, match='没有可重试'):
        service.interactions.retry_draft(row['id'], expected_updated_at=stopped['updated_at'], item_ids=[item_id])
