"""真实浏览器加载内存中的平台替身，全部网络请求均拦截，不访问真实账号。"""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright, Locator

from ripple import xhs_browser, xhs_reply, x_interactions_browser
from ripple.browser_submission import mark_submission

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'skills/shared/scripts'))


@pytest.fixture
def browser_page():
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True, channel=os.environ.get('RIPPLE_TEST_BROWSER', 'msedge' if os.name == 'nt' else 'chromium'))
        context = browser.new_context()
        page = context.new_page()
        # Preserve the adapter's response waits: receipt callbacks are async even
        # with intercepted network traffic, so a 50 ms cap races slower CI hosts.
        yield page
        context.close()
        browser.close()


def test_account_notes_ignore_nested_cards_and_recover_profile_links(browser_page, monkeypatch, tmp_path):
    page = browser_page
    def navigate(current, url, **kwargs):
        if 'note-manager' in url:
            current.set_content('''<div class="note-card" data-impression='{"note_id":"note123"}'>
                <div class="note-card-title">本人作品</div><div class="note-card-stats">1</div></div>
                <div class="note-card" data-impression='{"note_id":"note123"}'></div>
                <div class="note-card" data-impression='{"note_id":"note456"}'><div class="title">第二篇</div></div>''')
        elif '/user/profile/' in url:
            current.set_content('''<a href="https://www.xiaohongshu.com/explore/note123">未签名链接</a>
                <a href="https://www.xiaohongshu.com/user/profile/owner/note123?xsec_token=fixture">第一篇</a>
                <a href="https://www.xiaohongshu.com/user/profile/owner/note456?xsec_token=fixture2">第二篇</a>
                <a href="https://www.xiaohongshu.com/user/profile/another/note123?xsec_token=wrong">其他作者</a>''')
        else:
            current.set_content('<div class="main-container"><div class="user"><a href="/user/profile/owner">自己</a></div></div>')
    monkeypatch.setattr(xhs_browser, '_launch', lambda *a, **kw: (None, None, page))
    monkeypatch.setattr(xhs_browser, '_close', lambda *a: None)
    monkeypatch.setattr(xhs_browser, '_goto', navigate)
    result = xhs_browser.account_notes(tmp_path, 2, 'owner')
    assert [row['note_id'] for row in result['items']] == ['note123', 'note456']
    assert all('xsec_token' not in row['url'] for row in result['items'])
    assert all('xsec_token=' in row['url'] for row in result['_locators'])


XHS_HTML = '''<!doctype html><meta charset="utf-8">
<div class="main-container"><div class="user"><a href="/user/profile/owner">自己</a></div></div>
<div class="note-detail-mask"><div class="author-wrapper"><a href="/user/profile/owner">作者</a></div></div>
<div data-comment-id="c1"><p>评论问题</p><button>回复</button></div>
<textarea placeholder="回复"></textarea><button id="send">发送</button><p>完整回复</p>
<script>window.clicks = 0; document.querySelector('#send').onclick = async () => {
 window.clicks++; await fetch('https://edith.xiaohongshu.com/api/sns/web/v1/comment/post', {method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({note_id:'note123',target_comment_id:'c1',content:document.querySelector('textarea').value})});
};</script>'''


def test_delete_targets_exact_prefixed_id_and_never_replays(browser_page, monkeypatch, tmp_path):
    page = browser_page
    html = '''<meta charset="utf-8"><div class="main-container"><div class="user"><a href="/user/profile/owner">自己</a></div></div>
      <div class="note-detail-mask"><div class="author-wrapper"><a href="/user/profile/owner">作者</a></div></div>
      <div id="comment-parent"><span class="author">同名</span>原评论
        <div id="comment-child"><span class="author">同名</span>子评论<button class="more" onclick="document.querySelector('#delete').hidden=false">更多</button></div>
      </div><button id="delete" hidden onclick="window.deletes++; document.querySelector('#comment-child').remove();this.remove()">删除评论</button>
      <script>window.deletes=0;</script>'''
    page.route('**/*', lambda route: route.fulfill(body=html, content_type='text/html'))
    monkeypatch.setattr(xhs_browser, '_launch', lambda *a, **kw: (None, None, page))
    monkeypatch.setattr(xhs_browser, '_close', lambda *a: None)
    args = {'expected_account_remote_id': 'owner', 'submission_file': str(tmp_path / 'submission.json')}
    targets = [{'id': 'child', 'nickname': '同名', 'content': '子评论'}]
    result = xhs_browser.delete_comments(tmp_path, 'https://www.xiaohongshu.com/explore/note123', targets, **args)
    assert result['results'][0]['status'] == 'verified', result
    assert page.locator('#comment-parent').count() == 1 and page.locator('#comment-child').count() == 0
    assert page.evaluate('window.deletes') == 1
    replay = xhs_browser.delete_comments(tmp_path, 'https://www.xiaohongshu.com/explore/note123', targets, **args)
    assert replay['results'][0]['status'] == 'unknown_result'
    assert page.evaluate('window.deletes') == 0


def xhs_fixture(monkeypatch, page, *, valid_receipt=True, owner='owner'):
    requests = []
    def route(handler):
        request = handler.request
        if request.method == 'OPTIONS':
            handler.fulfill(status=204, headers={'access-control-allow-origin': '*', 'access-control-allow-headers': '*'}); return
        if request.method == 'POST':
            requests.append(request.post_data_json)
            value = {'success': True, 'data': {'comment': {'id': 'reply1', 'user_info': {'user_id': owner}, 'content': '完整回复'}}} if valid_receipt else {'success': True}
            handler.fulfill(json=value, headers={'access-control-allow-origin': '*'}); return
        handler.fulfill(body=XHS_HTML.replace('/owner', '/' + owner), content_type='text/html')
    page.route('**/*', route)
    monkeypatch.setattr(xhs_browser, '_launch', lambda *a, **kw: (None, None, page))
    monkeypatch.setattr(xhs_browser, '_close', lambda *a: None)
    return requests


@pytest.mark.parametrize('valid_receipt,expected', [(True, 'verified'), (False, 'unknown_result')])
def test_root_comment_opens_editor_and_requires_platform_receipt(browser_page, monkeypatch, tmp_path, valid_receipt, expected):
    page = browser_page
    requests = []
    html = XHS_HTML.replace('<textarea placeholder="回复"></textarea>', '<button class="not-active" onclick="document.querySelector(\'textarea\').hidden=false;this.remove()">说点什么</button><textarea hidden></textarea>')
    html = html.replace("target_comment_id:'c1'", "target_comment_id:''")
    def route(handler):
        if handler.request.method == 'OPTIONS':
            handler.fulfill(status=204, headers={'access-control-allow-origin': '*', 'access-control-allow-headers': '*'}); return
        if handler.request.method == 'POST':
            requests.append(handler.request.post_data_json)
            value = {'success': True, 'data': {'comment': {'id': 'root1', 'user_info': {'user_id': 'owner'}, 'content': '完整回复'}}} if valid_receipt else {'success': True}
            handler.fulfill(json=value, headers={'access-control-allow-origin': '*'}); return
        handler.fulfill(body=html, content_type='text/html')
    page.route('**/*', route)
    monkeypatch.setattr(xhs_browser, '_launch', lambda *a, **kw: (None, None, page))
    monkeypatch.setattr(xhs_browser, '_close', lambda *a: None)
    args = {'expected_account_remote_id': 'owner', 'submission_file': str(tmp_path / 'submission.json')}
    result = xhs_browser.post_comment(tmp_path, 'https://www.xiaohongshu.com/explore/note123', '完整回复', **args)
    assert result['status'] == expected, (result, requests)
    assert len(requests) == 1
    assert xhs_browser.post_comment(tmp_path, 'https://www.xiaohongshu.com/explore/note123', '完整回复', **args)['status'] == 'unknown_result'
    assert len(requests) == 1


@pytest.mark.parametrize('valid_receipt,expected', [(True, 'verified'), (False, 'unknown_result')])
def test_xhs_actual_click_needs_bound_receipt_not_visible_text(browser_page, monkeypatch, tmp_path, valid_receipt, expected):
    requests = xhs_fixture(monkeypatch, browser_page, valid_receipt=valid_receipt)
    result = xhs_reply.reply(tmp_path, 'https://www.xiaohongshu.com/explore/note123', [{'id': 'c1', 'reply': '完整回复'}],
                             expected_account_remote_id='owner', submission_file=str(tmp_path / 'submission.json'))
    assert result['results'][0]['status'] == expected, (result, requests)
    assert len(requests) == browser_page.evaluate('window.clicks') == 1
    assert browser_page.locator('textarea').input_value() == '完整回复'
    replay = xhs_reply.reply(tmp_path, 'https://www.xiaohongshu.com/explore/note123', [{'id': 'c1', 'reply': '完整回复'}],
                             expected_account_remote_id='owner', submission_file=str(tmp_path / 'submission.json'))
    assert replay['results'][0]['status'] == 'unknown_result'
    assert len(requests) == 1


def test_xhs_click_timeout_has_no_keyboard_or_fallback_submit(browser_page, monkeypatch, tmp_path):
    requests = xhs_fixture(monkeypatch, browser_page)
    original = Locator.click
    def timeout_after_click(locator, *args, **kwargs):
        original(locator, *args, **kwargs)
        if locator.get_attribute('id') == 'send':
            raise TimeoutError('点击已发生但调用超时')
    monkeypatch.setattr(Locator, 'click', timeout_after_click)
    result = xhs_reply.reply(tmp_path, 'https://www.xiaohongshu.com/explore/note123', [{'id': 'c1', 'reply': '完整回复'}],
                             expected_account_remote_id='owner', submission_file=str(tmp_path / 'submission.json'))
    browser_page.wait_for_timeout(50)
    assert result['results'][0]['status'] == 'unknown_result'
    assert browser_page.evaluate('window.clicks') == len(requests) == 1


@pytest.mark.parametrize('cid,owner', [('missing', 'owner'), ('c1', 'different')])
def test_xhs_wrong_account_or_comment_never_clicks(browser_page, monkeypatch, tmp_path, cid, owner):
    requests = xhs_fixture(monkeypatch, browser_page, owner=owner)
    result = xhs_reply.reply(tmp_path, 'https://www.xiaohongshu.com/explore/note123', [{'id': cid, 'reply': '完整回复'}],
                             expected_account_remote_id='owner', submission_file=str(tmp_path / 'submission.json'))
    assert result['results'][0]['status'] == 'not_submitted'
    assert requests == []


def tweet(pid, author, parent='', root='100', text='作品'):
    return {'__typename': 'Tweet', 'rest_id': pid, 'core': {'user_results': {'result': {'legacy': {'screen_name': author}}}},
            'legacy': {'conversation_id_str': root, 'in_reply_to_status_id_str': parent, 'full_text': text}}


X_HTML = '''<!doctype html><meta charset="utf-8"><a data-testid="AppTabBar_Profile_Link" href="/owner">自己</a>
<article data-testid="tweet"><a href="/reader/status/101"><time>今天</time></a><p>问题</p><button data-testid="reply" onclick="document.querySelector('[role=dialog]').hidden=false">回复</button></article>
<div role="dialog" hidden><div data-testid="tweetTextarea_0" contenteditable="true"></div><button id="send" data-testid="tweetButton">回复</button></div>
<script>window.clicks=0;
 fetch('/i/api/graphql/fixture/' + (location.pathname.includes('/status/') ? 'TweetDetail' : 'UserTweets'));
 document.querySelector('#send').onclick=()=>{window.clicks++; fetch('/i/api/graphql/fixture/CreateTweet',{method:'POST',headers:{'Content-Type':'application/json'},
 body:JSON.stringify({variables:{tweet_text:document.querySelector('[contenteditable]').textContent, reply:{in_reply_to_tweet_id:'101'}}})});};</script>'''


def x_fixture(page, *, comment_root='100', valid_receipt=True, logged_in=True):
    requests = []
    def route(handler):
        request = handler.request
        if request.url.endswith('/CreateTweet'):
            requests.append(request.post_data_json)
            handler.fulfill(json={'data': {'create_tweet': {'tweet_results': {'result': tweet('200', 'owner', '101', text='完整回复')}}}} if valid_receipt else {'data': {}})
        elif '/i/api/graphql/' in request.url:
            handler.fulfill(json={'data': {'timeline': {'instructions': [{'entries': [tweet('100', 'owner'), tweet('101', 'reader', comment_root, comment_root)]}]}}})
        else:
            handler.fulfill(body=X_HTML if logged_in else '<p>登录</p>', content_type='text/html')
    page.route('**/*', route)
    import x_browser
    adapter = SimpleNamespace(_launch=lambda *a, **kw: (None, None, page), _safe_close=lambda *a: None, identity_from_page=x_browser.identity_from_page)
    return adapter, requests


@pytest.mark.parametrize('valid_receipt,expected', [(True, 'verified'), (False, 'unknown_result')])
def test_x_browser_reply_uses_post_id_and_one_submission(browser_page, tmp_path, valid_receipt, expected):
    adapter, requests = x_fixture(browser_page, valid_receipt=valid_receipt)
    params = {'target_id': '100', 'expected_account_remote_id': 'x-web:owner', 'items': [{'id': '101', 'reply': '完整回复'}], 'submission_file': str(tmp_path / 'submission.json')}
    result = x_interactions_browser.run(adapter, None, 'reply', params)
    assert result['results'][0]['status'] == expected, (result, requests)
    assert len(requests) == 1
    assert requests[0]['variables']['reply']['in_reply_to_tweet_id'] == '101'
    replay = x_interactions_browser.run(adapter, None, 'reply', params)
    assert replay['results'][0]['status'] == 'unknown_result'
    assert len(requests) == 1


@pytest.mark.parametrize('comment_root,logged_in', [('999', True), ('100', False)])
def test_x_wrong_parent_or_expired_login_never_submits(browser_page, tmp_path, comment_root, logged_in):
    adapter, requests = x_fixture(browser_page, comment_root=comment_root, logged_in=logged_in)
    result = x_interactions_browser.run(adapter, None, 'reply', {'target_id': '100', 'expected_account_remote_id': 'x-web:owner',
                                                              'items': [{'id': '101', 'reply': '完整回复'}], 'submission_file': str(tmp_path / 'submission.json')})
    assert result['results'][0]['status'] == 'not_submitted'
    assert requests == []
