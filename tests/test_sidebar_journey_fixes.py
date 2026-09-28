import json
from copy import deepcopy

import pytest

from ripple.bilibili_options import upload_options
from ripple.library import MotherCreate
from ripple.publishing import CreateInput, WorkflowError
from ripple.variants import VariantBatchInput, VariantRevision
from ripple.workspace import WorkspaceService


@pytest.fixture
def work(tmp_path):
    service = WorkspaceService(tmp_path / 'outputs', private=tmp_path / 'private')
    yield service
    service.close()


@pytest.mark.parametrize('adapter', ['x-browser', 'x-native', 'aitoearn-rest'])
def test_x_weighted_limit_blocks_each_transport_before_approval(work, adapter, monkeypatch):
    # 替身只跳过各传输层的账号检查，公共正文校验必须仍然执行。
    from ripple.catalog import X_BROWSER_ADAPTER
    from ripple.x_adapter import ADAPTER
    name = {'x-browser': X_BROWSER_ADAPTER, 'x-native': ADAPTER, 'aitoearn-rest': 'aitoearn-rest'}[adapter]
    monkeypatch.setattr(work.x, 'problems', lambda *args: [])
    monkeypatch.setattr(work.rest, 'problems', lambda *args: [])
    with work.store.transaction() as state:
        state.setdefault('accounts', {})['qa-x'] = {'id': 'qa-x', 'platform': 'x', 'adapter': name, 'status': 'connected', 'operation': None}
    task = work.create(CreateInput(title='边界', body='测' * 141, platform='x', account_id='qa-x', mode='real', idempotency_key='weighted-limit'))
    result = work.preflight(task['id'], task['version_id'])
    assert result['ok'] is False
    assert any('282' in problem for problem in result['problems'])
    assert result['task']['preflight_problems'] == result['problems']
    assert result['task']['attempts'] == 0


@pytest.mark.parametrize('options', [{}, {'bilibili_tid': 36}, {'bilibili_tid': True, 'bilibili_copyright': 1}, {'bilibili_tid': 36, 'bilibili_copyright': 2}, {'bilibili_tid': 36, 'bilibili_copyright': 3}])
def test_bilibili_rejects_incomplete_or_invalid_parameters(options):
    with pytest.raises(WorkflowError):
        upload_options(options)


def test_bilibili_cli_parameters_come_from_reviewed_snapshot():
    assert upload_options({'bilibili_tid': 201, 'bilibili_copyright': 1}) == ['--tid', '201', '--copyright', '1']
    assert upload_options({'bilibili_tid': 17, 'bilibili_copyright': 2, 'bilibili_source': '原作者'}) == ['--tid', '17', '--copyright', '2', '--source', '原作者']


def test_failed_preflight_is_persisted_and_cleared_on_success(work, monkeypatch):
    task = work.create(CreateInput(title='检查记录', body='正文', platform='blog', mode='blog', idempotency_key='preflight-ledger'))
    monkeypatch.setattr(work, '_problems', lambda *args: ['需修正'])
    assert work.preflight(task['id'], task['version_id'])['task']['preflight_problems'] == ['需修正']
    monkeypatch.setattr(work, '_problems', lambda *args: [])
    checked = work.preflight(task['id'], task['version_id'])['task']
    assert checked['preflight_problems'] == []
    assert checked['status'] == 'review_ready'


def test_platform_ai_preview_apply_and_stale_revision_guard(work):
    mother = work.library.create(MotherCreate(title='主稿', body='通用正文', idempotency_key='platform-ai-source'))
    variant = work.variants.create_many(mother['id'], VariantBatchInput(expected_source_version=mother['version_id'], idempotency_key='platform-ai-target', targets=[{'platform': 'x'}]))['items'][0]
    work.operations.configure_model(lambda _: json.dumps({'revised_text': '简短平台正文', 'changes': ['压缩正文'], 'warnings': []}), lambda: True)
    result = work.operations.execute('text_polish', {'body': variant['content']['body'], 'goal': '缩短为 X 帖子'}, {'kind': 'platform_variant', 'ref': variant['id'], 'version': variant['version_id']})
    assert work.variants.get(variant['id']) == variant
    revision = VariantRevision(**{**deepcopy(variant['content']), 'body': result['output']['revised_text']}, expected_version=variant['version_id'], source_version_id=variant['source_version_id'])
    applied = work.variants.revise(variant['id'], revision)
    assert applied['content']['body'] == '简短平台正文'
    assert work.library.get(mother['id'])['content'] == mother['content']
    with pytest.raises(WorkflowError, match='已变化'):
        work.variants.revise(variant['id'], revision)
