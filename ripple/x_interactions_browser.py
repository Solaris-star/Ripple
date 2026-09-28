"""X 网页会话互动，仅观察页面自身响应并通过页面控件操作。"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from .browser_submission import mark_submission


class XInteractionError(RuntimeError):
    pass


POST_ID = re.compile(r'^[0-9]{1,30}$')
ARTICLE_JS = r"""(id) => {
  const rows = [...document.querySelectorAll('article[data-testid="tweet"]')].filter(article => {
    const time = article.querySelector('time');
    const href = time?.closest('a')?.getAttribute('href') || '';
    return href.match(/\/status\/(\d+)(?:[/?#]|$)/)?.[1] === id;
  });
  return rows.length === 1 ? rows[0] : null;
}"""


def extract_posts(payload: dict) -> dict[str, dict]:
    """只保留真实 Post ID 与明确父级，不采纳转帖、广告和引用帖内嵌对象。"""
    posts = {}
    def walk(value, depth=0):
        if depth > 35:
            return
        if isinstance(value, list):
            for node in value[:500]:
                walk(node, depth + 1)
            return
        if not isinstance(value, dict) or value.get('promotedMetadata') or value.get('promoted_metadata'):
            return
        if (value.get('__typename') == 'Tweet'
                or (value.get('rest_id') and isinstance(value.get('legacy'), dict)
                    and isinstance((value.get('core') or {}).get('user_results'), dict))):
            legacy = value.get('legacy') or {}
            identity = ((value.get('core') or {}).get('user_results') or {}).get('result') or {}
            author = str((identity.get('core') or {}).get('screen_name') or (identity.get('legacy') or {}).get('screen_name') or '').lower()
            pid = str(value.get('rest_id') or '')
            conversation = str(legacy.get('conversation_id_str') or '')
            parent = str(legacy.get('in_reply_to_status_id_str') or '')
            if POST_ID.fullmatch(pid) and POST_ID.fullmatch(conversation) and author and not legacy.get('retweeted_status_result'):
                media = (legacy.get('extended_entities') or legacy.get('entities') or {}).get('media') or []
                content = str(legacy.get('full_text') or '')
                for item in media:
                    if isinstance(item, dict) and item.get('url'):
                        content = re.sub(r'(?<!\S)' + re.escape(str(item['url'])) + r'(?=\s|$)', '', content).rstrip()
                edit = value.get('edit_control') or {}
                edit_rules = edit.get('edit_control_initial') if isinstance(edit.get('edit_control_initial'), dict) else edit
                edit_ids = edit.get('edit_tweet_ids') or edit_rules.get('edit_tweet_ids') or []
                edit_ids = edit_ids if isinstance(edit_ids, list) else []
                posts[pid] = {'id': pid, 'author': author, 'nickname': author, 'content': content,
                              'conversation_id': conversation, 'parent': parent,
                              'url': f'https://x.com/{author}/status/{pid}', 'time_str': str(legacy.get('created_at') or ''),
                              'time': 0, 'like': str(legacy.get('favorite_count') or 0),
                              'quoted': bool(legacy.get('is_quote_status')),
                              'media': [{'id': str(item.get('id_str') or ''), 'url': str(item.get('media_url_https') or '')}
                                        for item in media if isinstance(item, dict)],
                              'edit_ids': [str(item) for item in edit_ids],
                              'edits_remaining': edit_rules.get('edits_remaining'),
                              'editable_until_msecs': edit_rules.get('editable_until_msecs')}
            return
        for key, child in value.items():
            if key not in {'quoted_status_result', 'retweeted_status_result', 'recommendations', 'promotedMetadata'}:
                walk(child, depth + 1)
    walk(payload)
    return posts


def belongs_to(post: dict, root_id: str, posts: dict[str, dict]) -> bool:
    if post.get('id') == root_id or post.get('conversation_id') != root_id:
        return False
    seen = {post['id']}
    parent = post.get('parent')
    while parent and parent not in seen:
        if parent == root_id:
            return True
        seen.add(parent)
        node = posts.get(parent)
        if not node or node.get('conversation_id') != root_id:
            return False
        parent = node.get('parent')
    return False


def _has_timeline(value, depth=0) -> bool:
    if depth > 25:
        return False
    if isinstance(value, dict):
        if isinstance(value.get('instructions'), list):
            return True
        return any(_has_timeline(child, depth + 1) for child in value.values())
    if isinstance(value, list):
        return any(_has_timeline(child, depth + 1) for child in value[:500])
    return False


def _identity(page, adapter, expected: str) -> str:
    if any(marker in page.url for marker in ('/account/access', '/account/suspended', '/i/flow/challenge')):
        raise XInteractionError('verification_required')
    identity = adapter.identity_from_page(page)
    if not identity.get('loggedIn'):
        raise XInteractionError('login_required')
    if not expected or identity.get('uid') != expected:
        raise XInteractionError('account_mismatch')
    return expected.removeprefix('x-web:')


def _owned_root(posts: dict, root: str, handle: str) -> None:
    record = posts.get(root)
    if not record or record.get('author') != handle or record.get('parent') or record.get('conversation_id') != root:
        raise XInteractionError('target_owner_unconfirmed')


def reply_evidence(response, root: str, cid: str, handle: str, text: str) -> dict | None:
    parsed = urlsplit(response.url)
    if parsed.hostname not in {'x.com', 'www.x.com'} or not parsed.path.endswith('/CreateTweet') or response.status != 200 or response.request.method != 'POST':
        return None
    request = response.request.post_data_json
    variables = request.get('variables', {}) if isinstance(request, dict) else {}
    if variables.get('tweet_text') != text or (variables.get('reply') or {}).get('in_reply_to_tweet_id') != cid:
        return None
    payload = response.json()
    if payload.get('errors'):
        return None
    records = extract_posts(payload)
    matches = [item for item in records.values() if item['parent'] == cid and item['conversation_id'] == root and item['author'] == handle and item['content'] == text]
    if len(matches) != 1:
        return None
    return {'kind': 'platform_receipt', 'target_id': root, 'target_comment_id': cid,
            'account_remote_id': 'x-web:' + handle, 'reply_id': matches[0]['id'], 'text': text}


def run(adapter, options, action: str, params: dict) -> dict:
    if action not in {'contents', 'comments', 'reply'}:
        raise XInteractionError('unsupported_action')
    expected = str(params.get('expected_account_remote_id') or '')
    root = str(params.get('target_id') or '')
    if action != 'contents' and not POST_ID.fullmatch(root):
        raise XInteractionError('invalid_post_id')
    entries = params.get('items') or []
    if action == 'reply':
        if len(entries) != 1 or not POST_ID.fullmatch(str(entries[0].get('id') or '')):
            raise XInteractionError('invalid_comment_id')
        from .x_text import validate_reply
        validate_reply(str(entries[0].get('reply') or ''))
    manager, context, page = adapter._launch(options, headed=False)
    posts, receipts = {}, []
    valid_responses = 0
    submitted = False
    cid = str(entries[0]['id']) if entries else ''
    text = str(entries[0].get('reply') or '').strip() if entries else ''
    handle = expected.removeprefix('x-web:')
    def capture(response):
        nonlocal valid_responses
        try:
            parsed = urlsplit(response.url)
            if parsed.hostname not in {'x.com', 'www.x.com'}:
                return
            operation = parsed.path.rsplit('/', 1)[-1]
            allowed = {'UserTweets', 'UserTweetsAndReplies'} if action == 'contents' else {'TweetDetail'}
            if operation in allowed and response.status == 200:
                value = response.json()
                if isinstance(value.get('data'), dict) and not value.get('errors') and _has_timeline(value['data']):
                    valid_responses += 1
                    posts.update(extract_posts(value))
            if submitted and operation == 'CreateTweet':
                evidence = reply_evidence(response, root, cid, handle, text)
                if evidence:
                    receipts.append(evidence)
        except Exception:
            pass
    page.on('response', capture)
    try:
        page.goto('https://x.com/home', wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(1200)
        handle = _identity(page, adapter, expected)
        page.goto(f'https://x.com/{handle}' if action == 'contents' else f'https://x.com/{handle}/status/{root}', wait_until='domcontentloaded', timeout=30000)
        limit = max(1, min(int(params.get('limit') or (30 if action == 'contents' else 100)), 30 if action == 'contents' else 100))
        for _ in range(6):
            page.wait_for_timeout(800)
            _identity(page, adapter, expected)
            selected = [p for p in posts.values() if p['author'] == handle and not p['parent'] and p['conversation_id'] == p['id']] if action == 'contents' else [p for p in posts.values() if belongs_to(p, root, posts)]
            if len(selected) >= limit or action == 'reply' and cid in posts:
                break
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        if not valid_responses:
            raise XInteractionError('read_unconfirmed')
        if action == 'contents':
            items = [{'id': p['id'], 'title': p['content'][:200], 'url': p['url'], 'metrics': {}} for p in selected[:limit]]
            return {'items': items, 'count': len(items), 'limit': limit, 'sample_scope': '当前账号页面可读取的原创作品，不含转帖与推荐内容'}
        _owned_root(posts, root, handle)
        selected = [p for p in posts.values() if belongs_to(p, root, posts)]
        if action == 'comments':
            return {'target_id': root, 'comments': selected[:limit], 'count': len(selected[:limit]), 'limit': limit,
                    'sample_scope': '当前作品页面已读取且父级关系可确认的回复，最多 100 条'}
        target = posts.get(cid)
        if not target or not belongs_to(target, root, posts):
            raise XInteractionError('comment_ownership_unconfirmed')
        page.goto(target['url'], wait_until='domcontentloaded', timeout=30000)
        page.wait_for_timeout(1200)
        _identity(page, adapter, expected)
        container = page.evaluate_handle(ARTICLE_JS, cid).as_element()
        if container is None:
            raise XInteractionError('comment_target_ambiguous')
        reply_buttons = container.query_selector_all('[data-testid="reply"]')
        if len(reply_buttons) != 1:
            raise XInteractionError('reply_control_ambiguous')
        reply_buttons[0].click()
        dialog = page.get_by_role('dialog')
        dialog.wait_for(state='visible', timeout=5000)
        if dialog.count() != 1:
            raise XInteractionError('reply_dialog_ambiguous')
        editor = dialog.locator('[data-testid="tweetTextarea_0"][contenteditable="true"]')
        if editor.count() != 1:
            raise XInteractionError('reply_input_missing')
        editor.fill(text)
        send = dialog.locator('[data-testid="tweetButton"]')
        if send.count() != 1 or not send.is_enabled():
            raise XInteractionError('send_control_missing')
        _identity(page, adapter, expected)
        mark_submission(str(params.get('submission_file') or ''), {'target_id': root, 'comment_id': cid, 'account_remote_id': expected})
        submitted = True
        send.click()
        page.wait_for_timeout(1600)
        _identity(page, adapter, expected)
        return {'results': [{'id': cid, 'status': 'verified' if len(receipts) == 1 else 'unknown_result',
                             'evidence': receipts[0] if len(receipts) == 1 else {},
                             'reason': '' if len(receipts) == 1 else '未取得目标与账号匹配的回复回执，请人工核对。'}]}
    except Exception as exc:
        if action != 'reply':
            raise
        code = str(exc)
        return {'results': [{'id': cid, 'status': 'unknown_result' if submitted or isinstance(exc, FileExistsError) else 'not_submitted',
                             'reason': error_message(code), 'stop_batch': code in {'login_required', 'verification_required', 'account_mismatch'}}]}
    finally:
        adapter._safe_close(manager, context)


def error_message(code: str) -> str:
    return {'login_required': 'X 登录已失效，请主动打开登录窗口处理。',
            'verification_required': 'X 要求平台验证，请主动打开登录窗口处理。',
            'account_mismatch': 'X 浏览器身份与所选账号不一致，请重新校验账号。',
            'target_owner_unconfirmed': '无法确认作品属于当前 X 账号。',
            'comment_ownership_unconfirmed': '无法确认评论属于当前作品，已停止发送。',
            'read_unconfirmed': '未取得有效的 X 页面数据，同步失败。'}.get(code, 'X 页面操作未完成，请查看逐条结果；未知结果不能重发。')
