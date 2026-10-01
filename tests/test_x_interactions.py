from copy import deepcopy
import json
from pathlib import Path

import pytest

from ripple.accounts import AccountInput
from ripple import execution_nodes
from ripple.publishing import WorkflowError
from ripple.workspace import WorkspaceService
from ripple.x_interactions_browser import belongs_to, extract_posts, reply_evidence
from ripple.x_text import count_reply, validate_reply


def tweet(pid='100', author='owner', parent='', conversation='100', text='作品'):
    return {'__typename': 'Tweet', 'rest_id': pid,
            'core': {'user_results': {'result': {'legacy': {'screen_name': author}}}},
            'legacy': {'full_text': text, 'conversation_id_str': conversation, 'in_reply_to_status_id_str': parent}}


def test_real_ids_relationships_ignore_retweets_quotes_and_promoted_content():
    root, direct, nested = tweet(), tweet('101', 'reader', '100'), tweet('102', 'reader2', '101')
    retweet = tweet('900', 'owner', conversation='900')
    retweet['legacy']['retweeted_status_result'] = {'result': tweet('901')}
    payload = {'data': [root, direct, nested, retweet,
                        {'promotedMetadata': {'ad': True}, 'item': tweet('800')},
                        {'quoted_status_result': {'result': tweet('700')}}]}
    rows = extract_posts(payload)
    assert set(rows) == {'100', '101', '102'}
    assert belongs_to(rows['101'], '100', rows)
    assert belongs_to(rows['102'], '100', rows)
    assert not belongs_to(rows['102'], '100', {'102': rows['102']})
    assert not belongs_to({**rows['101'], 'conversation_id': '999'}, '100', rows)
    assert not belongs_to({**rows['101'], 'parent': '101'}, '100', rows)


def test_create_tweet_response_without_typename_keeps_media_separate_from_text():
    created = tweet('123', text='正文 https://t.co/photo123')
    created.pop('__typename')
    created['legacy']['extended_entities'] = {'media': [{
        'id_str': '456', 'url': 'https://t.co/photo123',
        'media_url_https': 'https://pbs.twimg.com/media/test.png',
    }]}
    payload = {'data': {'create_tweet': {'tweet_results': {'result': created}}}}
    rows = extract_posts(payload)
    assert rows['123']['author'] == 'owner'
    assert rows['123']['content'] == '正文'
    assert rows['123']['media'] == [{'id': '456', 'url': 'https://pbs.twimg.com/media/test.png'}]


@pytest.fixture
def work(tmp_path, monkeypatch):
    # These tests stub platform operations, not a real interactive login. Keep
    # the account prerequisite independent of browsers installed on the host.
    monkeypatch.setattr(execution_nodes, 'interactive_browser_channels', lambda: ['msedge'])
    service = WorkspaceService(tmp_path / 'outputs', private=tmp_path / 'private')
    account = service.accounts.create(AccountInput(platform='x', label='X 隔离账号', idempotency_key='x-replies-account'))
    with service.store.transaction() as state:
        state['accounts'][account['id']].update(status='connected', identity={'logged_in': True, 'remote_id': 'x-web:owner', 'name': 'owner'})
    yield service, account['id']
    service.close()


def test_x_workflow_reads_own_posts_replies_and_sends_with_bound_evidence(work, monkeypatch):
    service, account = work
    calls = []
    def worker(selected, operation, operation_id, **options):
        calls.append((operation, options))
        assert options['headed'] is False
        assert options['x_params']['expected_account_remote_id'] == 'x-web:owner'
        if options['x_action'] == 'contents':
            return {'state': 'success', 'data': {'items': [{'id': '100', 'title': '作品', 'url': 'https://x.com/owner/status/100'}]}}
        if options['x_action'] == 'comments':
            return {'state': 'success', 'data': {'comments': [extract_posts(tweet('101', 'reader', '100'))['101']]}}
        item = options['x_params']['items'][0]
        return {'state': 'success', 'data': {'results': [{'id': item['id'], 'status': 'verified', 'evidence': {
            'kind': 'platform_receipt', 'reply_id': '200', 'target_id': '100', 'target_comment_id': '101',
            'account_remote_id': 'x-web:owner', 'text': item['reply'],
        }}]}}
    monkeypatch.setattr(service.accounts, 'run', worker)
    assert service.interactions.remote_contents(account, 300)['items'][0]['id'] == '100'
    assert calls[0][1]['x_params']['limit'] == 30
    source = service.interactions.remote_comments(account_id=account, target_id='100', limit=1000)
    assert source['limit'] == 100 and source['comments'][0]['parent'] == '100'
    row = service.interactions.create_draft(platform='x', source_id=source['id'], kind='reply', items=[{'id': '101', 'reply': '中文 👨‍👩‍👧‍👦 https://example.com'}], idempotency_key='x-reply-draft-01')
    assert service.interactions.execute(row['id'], True, row['updated_at'])['status'] == 'verified'
    assert [operation for operation, _ in calls] == ['x_read', 'x_read', 'x_interact']


@pytest.mark.parametrize('field,value,reason', [('adapter', 'x-api', '官方 API'), ('execution_node_id', 'b' * 32, '远程 Browser Node')])
def test_unsupported_x_connection_is_account_specific_and_never_calls_worker(work, monkeypatch, field, value, reason):
    service, account = work
    with service.store.transaction() as state:
        state['accounts'][account][field] = value
    caps = next(row for row in service.interactions.capabilities()['items'] if row['platform'] == 'x')
    assert caps['experimental'] is True
    assert caps['accounts'][0]['reply'] is False
    assert reason in caps['accounts'][0]['reason']
    calls = []
    monkeypatch.setattr(service.accounts, 'run', lambda *a, **kw: calls.append(a))
    with pytest.raises(WorkflowError, match=reason):
        service.interactions.remote_contents(account)
    assert calls == []


def test_foreign_comment_rejected_before_draft(work):
    service, account = work
    with service.store.transaction() as state:
        state['interaction_sources'] = {'source': {'id': 'source', 'kind': 'remote', 'platform': 'x', 'account_id': account,
                                                   'target_id': '100', 'target_url': 'https://x.com/owner/status/100',
                                                   'comments': [extract_posts(tweet('101', 'reader', '999', '999'))['101']]}}
    with pytest.raises(WorkflowError, match='父级'):
        service.interactions.create_draft(platform='x', source_id='source', kind='reply', items=[{'id': '101', 'reply': '回复'}], idempotency_key='invalid-parent-draft')


def test_overlength_x_draft_can_be_saved_but_cannot_cross_send_boundary(work, monkeypatch):
    service, account = work
    with service.store.transaction() as state:
        state['interaction_sources'] = {'source': {'id': 'source', 'kind': 'remote', 'platform': 'x', 'account_id': account,
            'target_id': '100', 'label': '测试 Post', 'target_url': 'https://x.com/owner/status/100',
            'comments': [extract_posts(tweet('101', 'reader', '100'))['101']]}}
    calls = []
    monkeypatch.setattr(service.interactions, '_send_item', lambda *a, **kw: calls.append(a))
    draft = service.interactions.create_draft(platform='x', source_id='source', kind='reply', items=[{'id': '101', 'reply': '中' * 141}], idempotency_key='overlength-x-draft')
    updated = service.interactions.update_draft(draft['id'], expected_updated_at=draft['updated_at'], items=[{**draft['payload']['items'][0], 'reply': '中' * 142}])
    assert updated['payload']['items'][0]['reply'] == '中' * 142
    assert updated['target_label'] == '测试 Post'
    with pytest.raises(WorkflowError, match='280'):
        service.interactions.execute(updated['id'], True, updated['updated_at'])
    assert service.interactions._get_row(updated['id'])['attempts'] == 0
    assert calls == []


def test_missing_x_runtime_is_reported_in_capabilities_and_as_service_error(work, monkeypatch):
    service, account = work
    import builtins
    original_import = builtins.__import__
    def without_x_text(name, *args, **kwargs):
        if name == 'x_text':
            raise ModuleNotFoundError('twitter_text')
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', without_x_text)
    cap = next(row for row in service.interactions.capabilities()['items'] if row['platform'] == 'x')
    assert cap['availability'] == 'missing_dependency'
    assert cap['accounts'][0]['availability'] == 'missing_dependency'
    assert cap['accounts'][0]['reply'] is False
    with pytest.raises(WorkflowError, match='依赖未就绪'):
        service.interactions.remote_contents(account)
    with pytest.raises(WorkflowError, match='依赖未就绪') as error:
        service.interactions._validate_platform_payload('x', {'items': [{'reply': '合法回复'}]})
    assert error.value.status == 503


@pytest.mark.parametrize('case', json.loads((Path(__file__).parent / 'fixtures/x_text_cases.json').read_text(encoding='utf-8')))
def test_x_weighted_counts_match_shared_frontend_cases(case):
    text = case['text'] * case.get('repeat', 1) + case.get('suffix', '')
    assert count_reply(text) == {'weighted_length': case['length'], 'valid': case['valid']}
    if not case['valid']:
        with pytest.raises(WorkflowError):
            validate_reply(text)


def test_every_shared_emoji_and_url_boundary():
    data = json.loads((Path(__file__).resolve().parents[1] / 'ripple/data/x_emoji_sequences.json').read_text(encoding='utf-8'))
    for emoji in data['sequences']:
        expected = 1 if emoji in {'©', '®'} else 2
        assert count_reply(emoji)['weighted_length'] == expected, emoji
        assert count_reply(emoji + 'https://example.com')['weighted_length'] == expected + 23, emoji
    assert count_reply('🤏🏻' * 140) == {'weighted_length': 280, 'valid': True}
    assert count_reply('🤏🏻' * 141) == {'weighted_length': 282, 'valid': False}
    assert count_reply('https://example.com/' + 'a' * 1500) == {'weighted_length': 23, 'valid': True}
    assert count_reply('a\r\nb') == {'weighted_length': 3, 'valid': True}
