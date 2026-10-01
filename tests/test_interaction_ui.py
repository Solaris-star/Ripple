"""以隔离服务数据驱动实际 React 组件，平台发送由替身记录。"""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import time
from urllib.request import urlopen

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from playwright.sync_api import sync_playwright, expect

from ripple.accounts import AccountInput
from ripple import execution_nodes
from ripple.api import install
from ripple.library import MotherCreate, MotherRevision
from ripple.publishing import WorkflowError
from ripple.variants import VariantBatchInput, VariantRevision

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope='module')
def frontend_server():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen([shutil.which('node'), 'node_modules/vite/bin/vite.js', '--host', '127.0.0.1', '--port', str(port), '--strictPort'],
                               cwd=ROOT / 'web/frontend', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    origin = f'http://127.0.0.1:{port}'
    try:
        for _ in range(80):
            try:
                with urlopen(origin, timeout=1):
                    break
            except OSError:
                time.sleep(.1)
        else:
            raise AssertionError('隔离前端服务未启动')
        yield origin
    finally:
        process.terminate()
        process.wait(timeout=10)


@pytest.fixture
def ui(tmp_path, monkeypatch, frontend_server):
    monkeypatch.delenv('RIPPLE_ENABLE_AI', raising=False)
    app = FastAPI()
    service = install(app, tmp_path / 'outputs', private=tmp_path / 'private')
    account = service.accounts.create(AccountInput(platform='xiaohongshu', label='隔离账号', idempotency_key='ui-safety-account'))
    source = {'id': 'd' * 32, 'kind': 'remote', 'platform': 'xiaohongshu', 'account_id': account['id'], 'account_label': '隔离账号',
              'target_id': 'note123', 'target_url': 'https://www.xiaohongshu.com/explore/note123', 'label': '隔离作品',
              'comments': [{'id': f'c{i}', 'nickname': f'读者{i}', 'content': f'问题{i}', 'like': '0', 'parent': ''} for i in range(21)],
              'count': 21, 'created_at': '2026-09-26T00:00:00Z', 'updated_at': '2026-09-26T00:00:00Z'}
    with service.store.transaction() as state:
        state['accounts'][account['id']].update(status='connected', identity={'logged_in': True, 'remote_id': 'owner', 'name': '自己'})
        state['interaction_sources'] = {source['id']: source}
    service.xhs_ops._save_locators(account['id'], [{'note_id': 'note123', 'url': source['target_url']}])
    sent = []
    def send(row, item, operation_id):
        sent.append(deepcopy(item))
        return {'state': 'verified', 'data': {'results': [{'id': item['id'], 'status': 'verified', 'evidence': {
            'kind': 'platform_receipt', 'reply_id': 'sent-' + item['id'], 'target_id': 'note123',
            'target_comment_id': item['id'], 'account_remote_id': 'owner', 'text': item['reply'],
        }}]}}
    monkeypatch.setattr(service.interactions, '_send_item', send)
    state = {'component': 'interactions'}
    pending_ai = []
    with TestClient(app, base_url='http://localhost') as client, sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel=os.environ.get('RIPPLE_TEST_BROWSER', 'msedge' if os.name == 'nt' else 'chromium'))
        page = browser.new_page(viewport={'width': 1440, 'height': 1100})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        def route(handler):
            request = handler.request
            path = request.url.removeprefix(frontend_server)
            if path == '/__fixture/state':
                handler.fulfill(json=state)
            elif path.startswith('/api/chat'):
                pending_ai.append(handler)
            elif path.startswith('/api/'):
                response = client.request(request.method, path, content=request.post_data, headers={'Content-Type': 'application/json'})
                handler.fulfill(status=response.status_code, body=response.content, content_type='application/json')
            elif path == '/' and request.is_navigation_request():
                # 测试入口将地址改为根路径；刷新仍使用隔离组件，避免误入主应用。
                handler.continue_(url=frontend_server + '/test/ui-harness.html')
            elif request.url.startswith(frontend_server):
                handler.continue_()
            else:
                handler.abort()
        page.route('**/*', route)
        def open_page():
            page.goto(frontend_server + '/test/ui-harness.html')
        yield SimpleUI(page, service, source, sent, state, pending_ai, open_page, errors, tmp_path)
        browser.close()
    service.close()


class SimpleUI:
    def __init__(self, page, service, source, sent, state, pending_ai, open_page, errors, tmp_path):
        self.page, self.service, self.source, self.sent, self.state = page, service, source, sent, state
        self.pending_ai, self.open, self.errors = pending_ai, open_page, errors
        self.artifacts = Path(os.environ.get('RIPPLE_QA_ARTIFACTS', str(tmp_path)))
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def screenshot(self, name):
        self.page.screenshot(path=str(self.artifacts / name), full_page=True)

    def draft(self):
        return self.service.interactions.create_draft(platform='xiaohongshu', source_id=self.source['id'], kind='reply',
                                                      items=[{'id': 'c0', 'reply': '原来保存的回复'}], idempotency_key='ui-reply-original')


def test_retry_flow_blocks_early_then_excludes_completed_retry(ui, monkeypatch):
    service = ui.service.interactions
    original_send = service._send_item
    row = service.create_draft(platform='xiaohongshu', source_id=ui.source['id'], kind='reply',
                               items=[{'id': 'c0', 'reply': '回复零'}, {'id': 'c1', 'reply': '回复一'}], idempotency_key='ui-retry-batch')
    monkeypatch.setattr(service, '_send_item', lambda *args: {'state': 'unknown_result'})
    service.execute(row['id'], True, row['updated_at'])
    monkeypatch.setattr(service, '_send_item', original_send)
    ui.open(); page = ui.page
    page.get_by_role('button', name='历史 1', exact=True).click()
    page.locator('.r2-task-card').click()
    page.get_by_role('button', name='为确认未发送项建立草稿（1 条）', exact=True).click()
    expect(page.get_by_role('alert')).to_contain_text('此账号有结果待核对的操作')
    expect(page.get_by_role('button', name='预览并发送', exact=True)).to_be_disabled()
    expect(page.get_by_role('button', name='保存修改', exact=True)).to_be_enabled()
    page.get_by_role('button', name='查看账号受限记录', exact=True).click()
    page.get_by_role('button', name='本条确认未发送', exact=True).click()
    page.get_by_role('button', name='确认记录', exact=True).click()
    page.get_by_role('button', name='关闭', exact=True).click()
    page.get_by_role('button', name='草稿 1', exact=True).click()
    page.locator('.r2-task-card').click()
    expect(page.get_by_role('button', name='预览并发送', exact=True)).to_be_enabled()
    page.get_by_role('button', name='预览并发送', exact=True).click()
    page.get_by_role('button', name='确认发送', exact=True).click()
    expect(page.get_by_role('dialog', name='确认发送内容')).to_have_count(0)
    expect(page.get_by_role('dialog', name='回复评论')).to_contain_text('评论 c1 · 已发送')
    page.get_by_role('button', name='关闭', exact=True).click()
    page.locator('.r2-task-card').filter(has_text='回复零').click()
    expect(page.get_by_role('dialog')).to_contain_text('已排除 1 条')
    page.get_by_role('button', name='为确认未发送项建立草稿（1 条）', exact=True).click()
    expect(page.get_by_role('textbox')).to_have_count(1)
    expect(page.get_by_role('textbox')).to_have_value('回复零')
    assert [item['id'] for item in ui.sent] == ['c1']
    assert ui.errors == []
    ui.screenshot('retry-excludes-completed.png')


def test_edit_then_send_confirms_and_sends_saved_complete_text(ui):
    ui.draft(); ui.open()
    page = ui.page
    page.get_by_role('button', name='草稿 1', exact=True).click()
    page.locator('.r2-task-card').click()
    page.get_by_role('textbox').fill('最后编辑的完整回复，不先点保存')
    page.get_by_role('button', name='预览并发送', exact=True).click()
    dialog = page.get_by_role('dialog', name='确认发送内容')
    expect(dialog).to_contain_text('最后编辑的完整回复，不先点保存')
    expect(dialog).to_contain_text('隔离账号')
    expect(dialog).to_contain_text('问题0')
    assert ui.sent == []
    ui.screenshot('reply-confirmation.png')
    dialog.get_by_role('button', name='确认发送', exact=True).click()
    expect(page.get_by_role('dialog').get_by_text('评论 c0 · 已发送')).to_be_visible()
    assert [item['reply'] for item in ui.sent] == ['最后编辑的完整回复，不先点保存']
    ui.screenshot('reply-result.png')
    assert ui.errors == []


@pytest.mark.parametrize('failure', ['save', 'version'])
def test_save_failure_or_version_conflict_never_sends(ui, monkeypatch, failure):
    draft = ui.draft(); ui.open()
    page = ui.page
    page.get_by_role('button', name='草稿 1', exact=True).click()
    page.locator('.r2-task-card').click()
    page.get_by_role('textbox').fill('准备发送的新内容')
    if failure == 'save':
        def fail(*args, **kwargs):
            raise WorkflowError('隔离保存失败', 503)
        monkeypatch.setattr(ui.service.interactions, 'update_draft', fail)
    page.get_by_role('button', name='预览并发送', exact=True).click()
    if failure == 'version':
        expect(page.get_by_role('dialog', name='确认发送内容')).to_be_visible()
        current = ui.service.interactions._get_row(draft['id'])
        ui.service.interactions.update_draft(draft['id'], expected_updated_at=current['updated_at'], items=[{**current['payload']['items'][0], 'reply': '另一页面修改'}])
        page.get_by_role('button', name='确认发送', exact=True).click()
        expect(page.get_by_text('草稿已在其他页面修改，请刷新并重新审阅完整回复。')).to_be_visible()
    else:
        expect(page.get_by_text('隔离保存失败')).to_be_visible()
    expect(page.get_by_role('dialog', name='确认发送内容')).to_have_count(0)
    assert ui.sent == []


def test_selection_limit_and_late_ai_preserve_shared_manual_reply(ui):
    ui.open(); page = ui.page
    boxes = page.locator('.r2-comment-select input')
    expect(boxes).to_have_count(21)
    for index in range(20):
        boxes.nth(index).check()
    expect(boxes.nth(20)).to_be_disabled()
    expect(page.get_by_text('已选 20 / 20')).to_be_visible()
    page.locator('.r2-comment-card').first.get_by_role('button', name='AI 拟回复').click()
    editor = page.get_by_role('dialog').get_by_role('textbox')
    editor.fill('等待 AI 时手工修改')
    assert len(ui.pending_ai) == 1
    ui.pending_ai.pop().fulfill(json={'response': '[{"id":"c0","reply":"迟到的 AI 建议"}]'})
    expect(page.get_by_role('button', name='重新生成', exact=True)).to_be_enabled()
    expect(editor).to_have_value('等待 AI 时手工修改')
    page.get_by_role('button', name='关闭详情').click()
    expect(page.locator('.r2-comment-card').first).to_contain_text('等待 AI 时手工修改')
    page.locator('.r2-comment-card').first.get_by_role('button', name='查看回复', exact=True).click()
    expect(page.get_by_role('dialog').get_by_role('textbox')).to_have_value('等待 AI 时手工修改')
    assert ui.errors == []


def test_empty_comments_normal_page(ui):
    with ui.service.store.transaction() as state:
        state['interaction_sources'][ui.source['id']].update(comments=[], count=0)
    ui.open()
    expect(ui.page.get_by_text('这个筛选下暂无评论')).to_be_visible()
    assert ui.errors == []
    ui.screenshot('empty-comments.png')


def test_variant_sync_is_selective_and_reversible(ui):
    mother = ui.service.library.create(MotherCreate(title='主稿原题', body='主稿原文', idempotency_key='ui-mother-source'))
    variant = ui.service.variants.create_many(mother['id'], VariantBatchInput(expected_source_version=mother['version_id'], idempotency_key='ui-mother-variant', targets=[{'platform': 'xiaohongshu', 'account_id': ui.source['account_id']}]))['items'][0]
    ui.service.variants.revise(variant['id'], VariantRevision(**{**variant['content'], 'title': '平台标题', 'body': '保留平台改写正文'}, expected_version=variant['version_id'], source_version_id=mother['version_id']))
    updated = ui.service.library.revise(mother['id'], MotherRevision(title='最新主稿标题', body='最新主稿正文', expected_version=mother['version_id']))
    ui.state.update(component='variant', variantId=variant['id'], mother=updated)
    ui.open(); page = ui.page
    page.get_by_role('button', name='查看同步差异').click()
    dialog = page.get_by_role('dialog', name='选择同步字段')
    expect(dialog.get_by_role('checkbox', checked=True)).to_have_count(0)
    expect(dialog).to_contain_text('保留平台改写正文')
    expect(dialog).to_contain_text('最新主稿正文')
    dialog.get_by_role('checkbox', name='同步素材').check()
    ui.screenshot('variant-sync-diff.png')
    dialog.get_by_role('button', name='应用所选字段').click()
    expect(page.locator(f'#variant-{variant["id"]}-body')).to_have_value('保留平台改写正文')
    page.locator(f'#variant-{variant["id"]}-body').fill('同步后继续手工改写正文')
    page.get_by_role('button', name='撤销本次同步').click()
    expect(page.get_by_role('button', name='保留平台稿继续发布')).to_be_visible()
    expect(page.locator(f'#variant-{variant["id"]}-body')).to_have_value('同步后继续手工改写正文')
    assert ui.errors == []


def test_x_accounts_keep_edits_separate_and_show_weighted_limit(ui, monkeypatch):
    # The UI uses isolated connected accounts; no real browser login is needed.
    monkeypatch.setattr(execution_nodes, 'interactive_browser_channels', lambda: ['msedge'])
    account_ids = []
    for index in range(2):
        account = ui.service.accounts.create(AccountInput(platform='x', label=f'X 隔离账号 {index}', idempotency_key=f'x-ui-account-{index}'))
        account_ids.append(account['id'])
        source = {**deepcopy(ui.source), 'id': str(index + 1) * 32, 'platform': 'x', 'account_id': account['id'], 'account_label': account['label'],
                  'target_id': str(100 + index), 'target_url': f'https://x.com/owner{index}/status/{100 + index}', 'count': 1,
                  'comments': [{'id': '201', 'nickname': '同一读者', 'content': '同 ID 的样本评论', 'parent': str(100 + index), 'conversation_id': str(100 + index)}]}
        with ui.service.store.transaction() as state:
            state['accounts'][account['id']].update(status='connected', identity={'logged_in': True, 'remote_id': f'x-web:owner{index}', 'name': f'owner{index}'})
            state['interaction_sources'][source['id']] = source
    ui.open(); page = ui.page
    page.get_by_role('navigation', name='互动平台').get_by_text('X', exact=True).click()
    page.get_by_role('combobox', name='互动账号').select_option(account_ids[0])
    page.locator('.r2-comment-card').get_by_role('button', name='回复', exact=True).click()
    editor = page.get_by_role('dialog').get_by_role('textbox')
    editor.fill('中' * 141)
    expect(page.get_by_role('button', name='预览并发送', exact=True)).to_be_disabled()
    expect(page.get_by_text('282 / 280 加权字符 · 还需减少 2 个加权字符；可先保存草稿')).to_be_visible()
    page.get_by_role('dialog').get_by_role('button', name='保存草稿', exact=True).click()
    expect(page.get_by_role('dialog')).to_contain_text('已保存待发送草稿')
    assert ui.service.interactions.list(platform='x')['items'][0]['payload']['items'][0]['reply'] == '中' * 141
    expect(page.get_by_role('button', name='预览并发送', exact=True)).to_be_disabled()
    editor.fill('第一个账号的手工稿')
    page.get_by_role('button', name='关闭详情').click()
    page.get_by_role('combobox', name='互动账号').select_option(account_ids[1])
    page.locator('.r2-comment-card').get_by_role('button', name='回复', exact=True).click()
    expect(page.get_by_role('dialog').get_by_role('textbox')).to_have_value('')
    page.get_by_role('dialog').get_by_role('textbox').fill('第二个账号的手工稿')
    page.get_by_role('button', name='关闭详情').click()
    page.get_by_role('combobox', name='互动账号').select_option(account_ids[0])
    page.locator('.r2-comment-card').get_by_role('button', name='查看回复', exact=True).click()
    expect(page.get_by_role('dialog').get_by_role('textbox')).to_have_value('第一个账号的手工稿')
    assert ui.sent == []
    assert ui.errors == []
    ui.screenshot('x-account-isolation.png')


def test_local_reply_survives_reload_and_can_be_discarded_without_platform_action(ui):
    ui.open(); page = ui.page
    page.locator('.r2-comment-card').first.get_by_role('button', name='回复', exact=True).click()
    page.get_by_role('textbox', name='回复内容').fill('无需手动保存也能恢复的编辑内容')
    expect(page.get_by_role('dialog')).to_contain_text('编辑内容已保存在此浏览器')
    page.reload()
    expect(page.locator('.r2-comment-card').first).to_contain_text('无需手动保存也能恢复的编辑内容')
    expect(page.get_by_role('button', name='草稿 0', exact=True)).to_be_visible()
    page.locator('.r2-comment-card').first.get_by_role('button', name='查看回复', exact=True).click()
    page.get_by_role('button', name='放弃本地修改').click()
    page.get_by_role('dialog', name='放弃草稿或修改').get_by_role('button', name='确认放弃').click()
    expect(page.get_by_role('textbox', name='回复内容')).to_have_value('')
    page.reload()
    expect(page.locator('.r2-comment-card').first).not_to_contain_text('无需手动保存也能恢复的编辑内容')
    assert ui.sent == [] and ui.service.interactions.list()['items'] == []


def test_account_change_scopes_counts_tasks_and_selection(ui):
    ui.draft()
    account = ui.service.accounts.create(AccountInput(platform='xiaohongshu', label='另一个账号', idempotency_key='ui-second-account'))
    second = {**deepcopy(ui.source), 'id': 'e' * 32, 'account_id': account['id'], 'account_label': account['label'],
              'label': '另一个账号的作品', 'comments': [ui.source['comments'][0]], 'count': 1}
    with ui.service.store.transaction() as state:
        state['accounts'][account['id']].update(status='connected', identity={'logged_in': True, 'remote_id': 'owner2'})
        state['interaction_sources'][second['id']] = second
    ui.open(); page = ui.page
    page.get_by_role('combobox', name='互动账号').select_option(ui.source['account_id'])
    expect(page.get_by_role('button', name='评论 21', exact=True)).to_be_visible()
    expect(page.get_by_role('button', name='草稿 1', exact=True)).to_be_visible()
    page.locator('.r2-comment-select input').first.check()
    page.get_by_role('combobox', name='互动账号').select_option(account['id'])
    expect(page.locator('.r2-current-source-meta')).to_contain_text('另一个账号的作品')
    expect(page.get_by_role('button', name='评论 1', exact=True)).to_be_visible()
    expect(page.get_by_text('已选 0 / 20')).to_be_visible()
    page.get_by_role('button', name='草稿 0', exact=True).click()
    expect(page.locator('.r2-task-card')).to_have_count(0)
    page.get_by_role('button', name='历史 0', exact=True).click()
    expect(page.get_by_text('暂无互动历史')).to_be_visible()
    page.reload()
    expect(page.get_by_role('combobox', name='互动账号')).to_have_value(account['id'])
    expect(page.get_by_role('button', name='草稿 0', exact=True)).to_be_visible()
    assert ui.errors == []


def test_work_change_loads_matching_cached_comments_and_clears_old_selection(ui, monkeypatch):
    second = {**deepcopy(ui.source), 'id': 'e' * 32, 'target_id': 'note456', 'label': '另一篇作品',
              'comments': [{**ui.source['comments'][0], 'id': 'other', 'content': '另一篇的评论'}], 'count': 1}
    with ui.service.store.transaction() as state:
        state['interaction_sources'][second['id']] = second
    calls = []
    monkeypatch.setattr(ui.service.interactions, 'remote_comments', lambda **kw: calls.append(kw) or second)
    ui.open(); page = ui.page
    page.get_by_role('combobox', name='选择作品').select_option('note123')
    expect(page.locator('.r2-comment-card')).to_have_count(21)
    page.locator('.r2-comment-select input').first.check()
    page.get_by_role('combobox', name='选择作品').select_option('note456')
    expect(page.locator('.r2-comment-card')).to_have_count(1)
    expect(page.locator('.r2-comment-card')).to_contain_text('另一篇的评论')
    expect(page.get_by_text('已选 0 / 20')).to_be_visible()
    page.reload()
    expect(page.get_by_role('combobox', name='选择作品')).to_have_value('note456')
    page.get_by_role('button', name='刷新评论', exact=True).click()
    expect(page.get_by_role('status')).to_contain_text('已同步 1')
    assert calls[0]['target_id'] == 'note456'
    assert calls[0]['target_label'] == '另一篇作品'
    assert ui.errors == []


def test_uncached_work_never_retains_old_comments(ui, monkeypatch):
    monkeypatch.setattr(ui.service.interactions, 'remote_contents', lambda *a, **kw: {'items': [
        {'id': 'note123', 'title': '隔离作品'}, {'id': 'new-work', 'title': '尚未同步作品'}]})
    ui.open(); page = ui.page
    page.get_by_role('button', name='读取作品', exact=True).click()
    page.locator('.r2-comment-select input').first.check()
    page.get_by_role('combobox', name='选择作品').select_option('new-work')
    expect(page.locator('.r2-comment-card')).to_have_count(0)
    expect(page.get_by_text('这篇作品尚未同步评论')).to_be_visible()
    assert ui.sent == []


def test_ai_error_is_next_to_editor_and_keeps_manual_text(ui):
    ui.open(); page = ui.page
    page.locator('.r2-comment-card').first.get_by_role('button', name='AI 拟回复').click()
    page.get_by_role('textbox', name='回复内容').fill('服务失败也要保留这段手工输入')
    ui.pending_ai.pop().fulfill(status=503, json={'detail': '模拟 AI 暂不可用'})
    expect(page.get_by_role('dialog').get_by_role('alert')).to_contain_text('模拟 AI 暂不可用')
    expect(page.get_by_role('textbox', name='回复内容')).to_have_value('服务失败也要保留这段手工输入')
    assert ui.sent == []


def test_manual_result_uses_in_page_confirmation_and_keeps_send_count(ui, monkeypatch):
    draft = ui.draft()
    monkeypatch.setattr(ui.service.interactions, '_send_item', lambda *a: {'state': 'unknown_result'})
    ui.service.interactions.execute(draft['id'], True, draft['updated_at'])
    ui.open(); page = ui.page
    page.get_by_role('button', name='历史 1', exact=True).click()
    page.locator('.r2-task-card').click()
    expect(page.get_by_role('link', name='打开作品核对评论 ↗')).to_have_attribute('href', ui.source['target_url'])
    page.get_by_role('button', name='本条已发送', exact=True).click()
    dialog = page.get_by_role('dialog', name='记录人工核对结果')
    expect(dialog).to_contain_text('隔离账号')
    expect(dialog).to_contain_text('问题0')
    assert ui.service.interactions._get_row(draft['id'])['status'] == 'unknown_result'
    dialog.get_by_role('button', name='返回', exact=True).click()
    expect(page.get_by_role('button', name='本条已发送')).to_be_visible()
    page.get_by_role('button', name='本条已发送').click()
    page.get_by_role('button', name='确认记录').click()
    expect(page.get_by_role('dialog')).to_contain_text('评论 c0 · 已发送')
    assert ui.service.interactions._get_row(draft['id'])['attempts'] == 1
    assert ui.errors == []


def test_delete_menu_and_final_confirmation_name_platform_comment(ui):
    ui.open(); page = ui.page
    page.locator('.r2-comment-select input').first.check()
    page.get_by_text('更多操作', exact=True).click()
    page.get_by_role('button', name='删除平台评论…', exact=True).click()
    expect(page.get_by_role('dialog')).to_contain_text('将删除以下平台评论（不可恢复）')
    page.get_by_role('button', name='预览删除').click()
    dialog = page.get_by_role('dialog', name='确认删除平台评论')
    expect(dialog).to_contain_text('隔离账号')
    expect(dialog).to_contain_text('隔离作品')
    expect(dialog).to_contain_text('问题0')
    assert ui.sent == []
    task = ui.service.interactions.list()['items'][0]
    assert task['kind'] == 'delete' and task['attempts'] == 0


def test_verification_is_available_in_context_and_never_opens_login_window(ui, monkeypatch):
    with ui.service.store.transaction() as state:
        state['accounts'][ui.source['account_id']]['identity'] = None
    checks = []
    def probe(account_id, kind, request):
        checks.append((kind, request.headed))
        with ui.service.store.transaction() as state:
            state['accounts'][account_id].update(identity={'logged_in': True, 'remote_id': 'owner', 'name': '自己'}, message='账号校验通过')
        return ui.service.accounts.get(account_id)
    monkeypatch.setattr(ui.service.accounts, 'start', probe)
    ui.open(); page = ui.page
    expect(page.locator('.r2-platform-workbench-title')).to_contain_text('需要重新校验')
    expect(page.get_by_role('button', name='读取作品', exact=True)).to_be_disabled()
    page.get_by_role('button', name='校验此账号').click()
    expect(page.get_by_role('button', name='读取作品', exact=True)).to_be_enabled()
    expect(page.locator('.r2-current-source-meta')).to_contain_text('隔离作品')
    assert checks == [('probe', False)]
    assert ui.errors == []


def test_late_source_response_cannot_replace_newly_selected_work(ui):
    second = {**deepcopy(ui.source), 'id': 'e' * 32, 'target_id': 'note456', 'label': '新的作品',
              'comments': [{**ui.source['comments'][0], 'id': 'other', 'content': '新的作品评论'}], 'count': 1}
    with ui.service.store.transaction() as state:
        state['interaction_sources'][second['id']] = second
    pending = []
    ui.page.route('**/api/ripple/interactions/sources/' + ui.source['id'], lambda route: pending.append(route))
    ui.open(); page = ui.page
    page.get_by_role('combobox', name='选择作品').select_option('note123')
    expect(page.get_by_text('正在读取所选作品的评论…')).to_be_visible()
    page.get_by_role('combobox', name='选择作品').select_option('note456')
    expect(page.locator('.r2-current-source-meta')).to_contain_text('新的作品')
    assert pending
    for route in pending:
        route.fulfill(json=ui.source)
    # 后续用户操作在旧响应后执行，列表和选择仍必须属于新的作品。
    page.locator('.r2-comment-select input').check()
    expect(page.locator('.r2-current-source-meta')).to_contain_text('新的作品')
    expect(page.locator('.r2-comment-card')).to_have_count(1)
    expect(page.locator('.r2-comment-card')).to_contain_text('新的作品评论')
    assert ui.errors == []


def test_storage_failure_is_visible_and_manual_save_remains_available(ui):
    ui.page.add_init_script("""const original = Storage.prototype.setItem;
      Storage.prototype.setItem = function (key, value) {
        if (key.includes('interaction_reply_edits')) throw new DOMException('配额不足', 'QuotaExceededError');
        return original.call(this, key, value);
      };""")
    ui.open(); page = ui.page
    page.locator('.r2-comment-card').first.get_by_role('button', name='回复', exact=True).click()
    page.get_by_role('textbox', name='回复内容').fill('存储失败时仍然保留的文字')
    expect(page.get_by_role('dialog')).to_contain_text('编辑中，尚未保存')
    page.get_by_role('dialog').get_by_role('button', name='保存草稿', exact=True).click()
    expect(page.get_by_role('dialog')).to_contain_text('已保存待发送草稿')
    assert ui.service.interactions.list()['items'][0]['payload']['items'][0]['reply'] == '存储失败时仍然保留的文字'
    assert ui.sent == []


def test_cancel_saved_reply_uses_modal_and_does_not_restore_local_edit(ui):
    ui.draft(); ui.open(); page = ui.page
    page.get_by_role('button', name='草稿 1', exact=True).click()
    page.locator('.r2-task-card').click()
    page.get_by_role('textbox').fill('准备放弃的本地修改')
    page.get_by_role('button', name='放弃回复草稿').click()
    expect(page.get_by_role('dialog', name='放弃草稿或修改')).to_contain_text('不会删除平台评论')
    page.get_by_role('button', name='确认放弃').click()
    expect(page.get_by_role('dialog')).to_contain_text('已取消')
    assert ui.service.interactions.list()['items'][0]['status'] == 'cancelled'
    page.reload()
    expect(page.locator('.r2-comment-card').first).not_to_contain_text('准备放弃的本地修改')
    assert ui.sent == [] and ui.errors == []
