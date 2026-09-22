from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pytest
from filelock import FileLock
from pydantic import ValidationError

from ripple.ai_providers import AIProviderService
from ripple.campaign_schedule import CampaignPaidSchedule, daily_slot, next_daily_at
from ripple.campaign_sources import CampaignSourceService
from ripple.publishing import WorkflowError
from ripple.workspace import WorkspaceService
from web import app as upstream


def ts(clock: str) -> int:
    return int(datetime.fromisoformat('2026-09-22T' + clock + '+08:00').timestamp())


@pytest.fixture
def service(tmp_path, monkeypatch):
    workspace = WorkspaceService(tmp_path / 'outputs', private=tmp_path / 'private')
    result = CampaignSourceService(workspace, AIProviderService(workspace.private))
    monkeypatch.setattr('time.time', lambda: ts('10:00:00'))
    yield result
    workspace.close()


@pytest.mark.parametrize('clock, expected', [
    ('08:59:59', None), ('09:00:00', '09:00'), ('09:00:59', '09:00'),
    ('09:01:00', None), ('13:59:59', None), ('14:00:00', '14:00'),
    ('20:00:00', '20:00'), ('20:01:00', None), ('23:59:59', None),
])
def test_daily_admission_boundaries(clock, expected):
    value = daily_slot(ts(clock))
    assert (value[0].split('T')[1] if value else None) == expected


def test_timezone_and_next_day():
    assert daily_slot(ts('09:00:00'))[1] == int(datetime.fromisoformat('2026-09-22T01:00:00+00:00').timestamp())
    assert next_daily_at(ts('09:00:00')) == ts('14:00:00')
    assert next_daily_at(ts('20:00:00')) == ts('09:00:00') + 86400


def test_paid_claim_survives_restart_and_concurrency(tmp_path):
    path = tmp_path / 'slots.json'
    def claim(_):
        return CampaignPaidSchedule(path).claim('x:discovery', ts('09:00:00'))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(claim, range(4)))
    assert results.count(True) == 1
    restarted = CampaignPaidSchedule(path)
    assert not restarted.claim('x:discovery', ts('09:00:30'))
    assert restarted.next_at('x:discovery', ts('09:00:30')) == ts('14:00:00')
    assert restarted.claim('x:discovery', ts('14:00:00'))
    assert not restarted.claim('x:discovery', ts('19:00:00'))


def test_corrupt_ledger_fails_closed(tmp_path):
    path = tmp_path / 'slots.json'
    path.write_text('{bad json', encoding='utf-8')
    schedule = CampaignPaidSchedule(path)
    assert not schedule.available('x:discovery', ts('09:00:00'))
    with pytest.raises(ValueError):
        schedule.claim('x:discovery', ts('09:00:00'))


@pytest.mark.parametrize('claims', [{'x:discovery': []}, {'x:discovery': None}, {'x:discovery': {'slot': 'bad'}}])
def test_malformed_claims_fail_closed(tmp_path, claims):
    path = tmp_path / 'slots.json'
    path.write_text(json.dumps({'version': 1, 'claims': claims}), encoding='utf-8')
    schedule = CampaignPaidSchedule(path)
    assert not schedule.available('x:discovery', ts('09:00:00'))
    with pytest.raises(ValueError):
        schedule.claim('x:discovery', ts('09:00:00'))


def ready_x(service, monkeypatch):
    monkeypatch.setattr(service, '_x_method_state', lambda *args: ('ready', 'fixture'))
    state = service._state(); state['x']['method'] = 'prompt'; service._write(state)


def test_x_runs_three_slots_only_and_failure_not_replayed(service, monkeypatch):
    ready_x(service, monkeypatch)
    calls = []
    def fail(*args):
        calls.append(1)
        raise WorkflowError('fixture failure', 502)
    monkeypatch.setattr(service, '_x_method', fail)
    assert 'x' not in service.due_platforms()
    assert service.refresh(['x'])['results'][0]['status'] == 'cached'
    for clock in ('09:00:00', '14:00:00', '20:00:00'):
        monkeypatch.setattr('time.time', lambda clock=clock: ts(clock))
        assert 'x' in service.due_platforms()
        assert service.refresh(['x'])['results'][0]['status'] == 'error'
        assert service.refresh(['x'])['results'][0]['status'] == 'cached'
        assert 'x' not in service.due_platforms()
    assert len(calls) == 3
    monkeypatch.setattr('time.time', lambda: ts('21:00:00'))
    assert 'x' not in service.due_platforms()
    cap = next(r for r in service.public_state()['items'] if r['platform'] == 'x')
    assert cap['next_sync_at'] == ts('09:00:00') + 86400
    assert cap['last_sync']['next_run_at'] == cap['schedule']['next_run_at']


def test_manual_x_extra_run_does_not_move_fixed_schedule(service, monkeypatch):
    ready_x(service, monkeypatch)
    monkeypatch.setattr(service, '_x_method', lambda *args: [])
    value = service.refresh(['x'], force=True, allow_paid=True)['results'][0]
    assert value['status'] == 'fresh'
    assert service.next_sync_at('x') == ts('14:00:00')
    assert not service.refresh(['x'])['results'][0].get('rules_allowed')


def test_free_bili_refresh_does_not_allow_agent_outside_paid_slots(service, monkeypatch):
    monkeypatch.setattr(service, '_bilibili', lambda **kwargs: [])
    first = service.refresh(['bilibili'], force=True)['results'][0]
    assert first['status'] == 'fresh' and not first['rules_allowed']
    assert 'bilibili' not in service.due_platforms()
    monkeypatch.setattr('time.time', lambda: ts('10:31:00'))
    assert 'bilibili' in service.due_platforms()
    assert not service.refresh(['bilibili'])['results'][0]['rules_allowed']
    monkeypatch.setattr('time.time', lambda: ts('14:00:00'))
    assert service.refresh(['bilibili'])['results'][0]['rules_allowed']
    assert service.refresh(['bilibili'])['results'][0]['status'] == 'cached'


def test_serial_batch_uses_admission_time_for_all_platforms(service, monkeypatch):
    ready_x(service, monkeypatch)
    clock = [ts('09:00:00')]
    monkeypatch.setattr('time.time', lambda: clock[0])
    def slow_free(**kwargs):
        clock[0] += 180
        return []
    monkeypatch.setattr(service, '_bilibili', slow_free)
    calls = []
    monkeypatch.setattr(service, '_x_method', lambda *args: calls.append('x') or [])
    values = service.refresh(['bilibili', 'x'])['results']
    assert all(row['status'] == 'fresh' for row in values)
    assert calls == ['x']


def test_douyin_fallback_never_piggybacks_on_hourly_or_free_manual(service, monkeypatch):
    state = service._state(); state['douyin']['tikhub_enabled'] = True; service._write(state)
    monkeypatch.setattr(service, '_tikhub_key', lambda: 'fixture-not-a-token')
    monkeypatch.setattr(service, '_effective_account', lambda p, *args, **kwargs: ({'id': 'douyin-test', 'label': 'test'}, 'ready') if p == 'douyin' else (None, 'needs_config'))
    monkeypatch.setattr(service, '_douyin_portal', lambda *_: (_ for _ in ()).throw(WorkflowError('primary failed', 502)))
    billed = []
    monkeypatch.setattr(service, '_tikhub', lambda: billed.append(1) or [])
    assert not service.refresh(['douyin'])['results'][0].get('fallback_used')
    assert not service.refresh(['douyin'], force=True)['results'][0].get('fallback_used')
    assert billed == []
    monkeypatch.setattr('time.time', lambda: ts('14:00:00'))
    assert service.refresh(['douyin'])['results'][0]['fallback_used']
    service.refresh(['douyin'])
    assert billed == [1]


def test_unconfigured_platforms_and_invalid_scopes_make_no_requests(service, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('unrelated platform called')
    for name in ('_bilibili', '_xiaohongshu', '_x_method', '_tikhub'):
        monkeypatch.setattr(service, name, forbidden)
    values = service.refresh(['douyin'], force=True)['results']
    assert len(values) == 1 and values[0]['platform'] == 'douyin'
    assert values[0]['status'] == 'needs_config'
    wechat_calls = []
    monkeypatch.setattr(service, '_wechat_public', lambda platform: wechat_calls.append(platform) or [])
    for name in ('wechat', 'weixin-channels'):
        values = service.refresh([name], force=True)['results']
        assert values == [{'platform': name, 'status': 'fresh', 'items': [], 'count': 0, 'provider': 'wechat_public_rules', 'rules_allowed': False, 'fallback_used': False}]
    assert wechat_calls == ['wechat', 'weixin-channels']
    for invalid in ([], ['unknown'], ['all']):
        with pytest.raises(WorkflowError) as exc:
            service.refresh(invalid, force=True)
        assert exc.value.status == 422


def test_duplicate_scope_and_inflight_lock_do_not_queue_requests(service, monkeypatch):
    calls = []
    monkeypatch.setattr(service, '_bilibili', lambda **kwargs: calls.append('bili') or [])
    assert len(service.refresh(['bilibili', 'bilibili'], force=True)['results']) == 1
    service._refresh_lock_path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(service._refresh_lock_path), timeout=0):
        assert service.refresh(['bilibili'], force=True)['results'][0]['status'] == 'busy'
    assert calls == ['bili']


def test_http_refresh_scope_and_no_other_platform_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(upstream, 'CAMPAIGNS_FILE', tmp_path / 'campaigns.json')
    monkeypatch.setattr(upstream, '_ensure_ai_provider_migration', lambda: None)
    calls = []
    def scoped(platforms, *, force=False, allow_paid=False):
        calls.append((platforms, force, allow_paid))
        return {'results': [{'platform': 'xiaohongshu', 'status': 'fresh', 'items': [], 'rules_allowed': False}], 'sources': {}}
    monkeypatch.setattr(upstream._CAMPAIGN_SOURCES, 'refresh', scoped)
    monkeypatch.setattr(upstream, '_start_x_enrichment_batch', lambda *_: pytest.fail('X rule call from XHS refresh'))
    monkeypatch.setattr(upstream, '_start_campaign_agent_batch', lambda *a, **k: pytest.fail('Bili Agent from XHS refresh'))
    asyncio.run(upstream.api_campaign_refresh(upstream.CampaignRefreshInput(platforms=['xiaohongshu'], force=True)))
    assert calls == [(['xiaohongshu'], True, False)]
    with pytest.raises(ValidationError):
        upstream.CampaignRefreshInput(platforms=[])


def test_paid_rule_dispatch_requires_matching_successful_admission(monkeypatch):
    started = []
    monkeypatch.setattr(upstream, '_x_enrichment_candidates', lambda **kw: [{'id': 'x1'}])
    monkeypatch.setattr(upstream, '_campaign_enrichment_candidates', lambda **kw: [{'id': 'b1'}])
    monkeypatch.setattr(upstream, '_start_x_enrichment_batch', lambda ids: started.append(('x', ids)))
    monkeypatch.setattr(upstream, '_start_campaign_agent_batch', lambda ids, **kw: started.append(('bili', ids)))
    for platform, status, allowed in [('xiaohongshu', 'fresh', False), ('bilibili', 'fresh', False), ('x', 'error', True), ('x', 'cached', True)]:
        upstream._campaign_run_scoped_rules({'results': [{'platform': platform, 'status': status, 'rules_allowed': allowed}]})
    assert not started
    upstream._campaign_run_scoped_rules({'results': [{'platform': 'x', 'status': 'fresh', 'rules_allowed': True}]})
    assert started == [('x', ['x1'])]
