"""读取 Ripple 独立浏览器中的本人平台作品。"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

from .x_interactions_browser import ARTICLE_JS, extract_posts, _has_timeline
from .xhs_browser import XhsBrowserError
from .browser_submission import mark_submission


class RemoteBrowserError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _x_row(value: dict, account_remote_id: str) -> dict:
    kind = 'reply' if value.get('parent') else 'quote' if value.get('quoted') else 'original'
    return {'platform': 'x', 'account_remote_id': account_remote_id, 'remote_id': value['id'],
            'version_ids': value.get('edit_ids') or [value['id']], 'kind': kind,
            'title': '', 'body': value.get('content') or '', 'topics': [],
            'media': value.get('media') or [], 'url': value.get('url') or '',
            'remote_status': 'published', 'visibility': 'unknown', 'checked_at': _now(),
            'created_at': value.get('time_str') or '', 'edits_remaining': value.get('edits_remaining'),
            'editable_until_msecs': value.get('editable_until_msecs'),
            'detail_complete': True}


def _has_bottom_cursor(value, depth: int = 0) -> bool:
    if depth > 30:
        return False
    if isinstance(value, list):
        return any(_has_bottom_cursor(item, depth + 1) for item in value[:500])
    if isinstance(value, dict):
        if value.get('cursorType') == 'Bottom' or str(value.get('entryId') or '').startswith('cursor-bottom'):
            return True
        return any(_has_bottom_cursor(item, depth + 1) for item in value.values())
    return False


def _x_delete_response_valid(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get('errors'):
        return False
    data = payload.get('data')
    return isinstance(data, dict) and isinstance(data.get('delete_tweet'), dict)


def _x_delete_proof(receipts: list[dict], target: str, account_remote_id: str, operation_id: str,
                    *, identity_ok: bool, target_unavailable: bool, article_present: bool) -> dict:
    valid = [item for item in receipts if item.get('operation') == 'DeleteTweet'
             and item.get('target_id') == target and item.get('account_remote_id') == account_remote_id
             and item.get('status') == 200]
    if len(valid) != 1 or not identity_ok or not target_unavailable or article_present:
        return {}
    return {**valid[0], 'operation_id': operation_id, 'post_check': 'target_absent_after_reload'}


def _x_confirm_identity(adapter, page, expected: str) -> None:
    for _ in range(30):
        identity = adapter.identity_from_page(page)
        if identity.get('uid') == expected:
            return
        if identity.get('loggedIn'):
            raise RemoteBrowserError('account_mismatch')
        page.wait_for_timeout(350)
    raise RemoteBrowserError('login_required')


def _x_target_unavailable(value, target: str, depth: int = 0) -> bool:
    if depth > 30:
        return False
    if isinstance(value, list):
        return any(_x_target_unavailable(item, target, depth + 1) for item in value[:500])
    if isinstance(value, dict):
        if value.get('entryId') == 'tweet-' + target:
            item = (value.get('content') or {}).get('itemContent') or {}
            if 'tweet_results' in item and not item['tweet_results']:
                return True
        return any(_x_target_unavailable(item, target, depth + 1) for item in value.values())
    return False


def _x_run(adapter, options, action: str, params: dict) -> dict:
    expected = str(params.get('expected_account_remote_id') or '')
    if not expected.startswith('x-web:'):
        raise RemoteBrowserError('account_identity_missing')
    handle = expected.removeprefix('x-web:')
    target = str(params.get('remote_id') or '')
    if action in {'detail', 'deletion_check'} and not re.fullmatch(r'[0-9]{1,30}', target):
        raise RemoteBrowserError('invalid_post_id')
    manager, context, page = adapter._launch(options, headed=False)
    posts: dict[str, dict] = {}
    valid = 0
    target_unavailable = False
    timeline_end: dict[str, bool] = {}
    current_route = ''
    def capture(response):
        nonlocal valid, target_unavailable
        try:
            url = urlsplit(response.url)
            if url.hostname not in {'x.com', 'www.x.com'} or response.status != 200:
                return
            operation = url.path.rsplit('/', 1)[-1]
            if operation not in ({'UserTweets', 'UserTweetsAndReplies', 'UserOriginalsTimeline', 'UserRepliesTimeline'}
                                  if action == 'list' else {'TweetDetail'}):
                return
            data = response.json()
            if isinstance(data, dict) and not data.get('errors') and _has_timeline(data.get('data') or {}):
                valid += 1
                posts.update(extract_posts(data))
                if action == 'deletion_check' and _x_target_unavailable(data.get('data'), target):
                    target_unavailable = True
                if action == 'list':
                    timeline_end[current_route] = not _has_bottom_cursor(data.get('data'))
        except Exception:
            pass
    page.on('response', capture)
    try:
        page.goto('https://x.com/home', wait_until='domcontentloaded', timeout=30000)
        _x_confirm_identity(adapter, page, expected)
        routes = [f'https://x.com/{handle}/with_replies', f'https://x.com/{handle}'] if action == 'list' else [f'https://x.com/{handle}/status/{target}']
        offset = max(0, int(params.get('cursor') or 0))
        limit = max(1, min(int(params.get('limit') or 20), 50))
        for route in routes:
            current_route = route
            page.goto(route, wait_until='domcontentloaded', timeout=30000)
            stable = 0
            previous = -1
            for _ in range(45 if action == 'list' else 10):
                page.wait_for_timeout(700)
                _x_confirm_identity(adapter, page, expected)
                own = [p for p in posts.values() if p.get('author') == handle]
                if action in {'detail', 'deletion_check'} and (target in posts or target_unavailable) or action == 'list' and len(own) >= offset + limit + 1:
                    break
                if len(posts) == previous:
                    stable += 1
                else:
                    stable = 0
                if stable >= 8 and (action != 'list' or current_route in timeline_end):
                    break
                previous = len(posts)
                page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        if not valid:
            raise RemoteBrowserError('read_unconfirmed')
        if action == 'deletion_check':
            found = posts.get(target)
            return {'target_id': target, 'account_remote_id': expected,
                    'exists': bool(found and found.get('author') == handle),
                    'target_unavailable': target_unavailable, 'checked_at': _now()}
        if action == 'detail':
            post = posts.get(target)
            if not post or post.get('author') != handle:
                raise RemoteBrowserError('target_owner_unconfirmed')
            row = _x_row(post, expected)
            article = None
            for _ in range(20):
                article = page.evaluate_handle(ARTICLE_JS, target).as_element()
                if article is not None:
                    break
                page.wait_for_timeout(300)
            if article is not None:
                caret = None
                for _ in range(15):
                    caret = article.query_selector('[data-testid="caret"]')
                    if caret is not None:
                        break
                    page.wait_for_timeout(300)
                    article = page.evaluate_handle(ARTICLE_JS, target).as_element() or article
                if caret is not None:
                    caret.click()
                    edit_item = page.get_by_role('menuitem', name=re.compile(r'Edit|编辑', re.I))
                    for _ in range(10):
                        if edit_item.count() == 1:
                            break
                        page.wait_for_timeout(200)
                    row['edit_available'] = edit_item.count() == 1 and edit_item.is_enabled()
                    page.keyboard.press('Escape')
            row['detail_checked'] = True
            return {'post': row}
        rows = [_x_row(p, expected) for p in posts.values() if p.get('author') == handle]
        rows.sort(key=lambda p: int(p['remote_id']), reverse=True)
        page_rows = rows[offset:offset + limit]
        profile_heading = page.locator('main').inner_text()[:240] if page.locator('main').count() else ''
        count_match = re.search(r'\b([0-9][0-9,]*)\s+posts\b', profile_heading, re.I)
        profile_total = int(count_match.group(1).replace(',', '')) if count_match else None
        exhausted = (all(timeline_end.get(route) is True for route in routes)
                     or profile_total is not None and len(rows) >= profile_total
                     and all(route in timeline_end for route in routes))
        complete = len(rows) < offset + limit and stable >= 4 and exhausted
        return {'items': page_rows, 'next_cursor': None if complete or not page_rows else str(offset + len(page_rows)),
                'complete': complete, 'checked_at': _now(), 'account_remote_id': expected}
    finally:
        adapter._safe_close(manager, context)


def _xhs_status(text: str) -> str:
    if any(word in text for word in ('审核中', '审核处理中', '待审核')):
        return 'reviewing'
    if any(word in text for word in ('未通过', '审核失败', '违规', '已驳回')):
        return 'rejected'
    if any(word in text for word in ('已发布', '发布成功')):
        return 'published'
    if '仅自己可见' in text:
        return 'published'
    return 'unknown'


def _xhs_row(row: dict, account_remote_id: str, browser, api_note: dict | None = None) -> dict:
    api_note = api_note or {}
    remote_id = browser._note_id(str(row.get('href') or ''), str(row.get('noteId') or ''))
    text = str(row.get('text') or '')
    permission = str(api_note.get('permission_msg') or '')
    url = browser.public_note_url(str(row.get('href') or ''))
    media = [{'url': str(item.get('url') or '')[:2048]} for item in api_note.get('images_list') or []
             if isinstance(item, dict) and str(item.get('url') or '').startswith(('https://', 'http://'))]
    if not media and row.get('cover'):
        media = [{'url': str(row.get('cover'))[:2048]}]
    return {'platform': 'xiaohongshu', 'account_remote_id': account_remote_id, 'remote_id': remote_id,
            'version_ids': [remote_id], 'kind': 'video' if api_note.get('type') == 'video' or '视频' in text else 'image',
            'title': str(api_note.get('display_title') or row.get('title') or '')[:200], 'body': '', 'topics': [],
            'media': media, 'url': url,
            'remote_status': _xhs_status(text + permission) if _xhs_status(text + permission) != 'unknown'
                             else 'published' if api_note.get('tab_status') == 1 else 'unknown',
            'visibility': 'private' if api_note.get('permission_code') == 1 or '仅自己可见' in text + permission else 'unknown',
            'checked_at': _now(), 'creator_evidence': text[:200], 'detail_complete': False}


def _xhs_editor_fields(page) -> dict:
    return page.evaluate(r"""() => {
      const editor=document.querySelector('div.ql-editor, div.editor-container [contenteditable="true"]');
      if(!editor) return {body:'',topics:[]};
      const topics=[];
      for(const anchor of editor.querySelectorAll('a.tiptap-topic[data-topic]')) {
        try { const value=JSON.parse(anchor.getAttribute('data-topic')||'{}'); if(value.name) topics.push(String(value.name)); } catch(e) {}
      }
      const clone=editor.cloneNode(true);
      clone.querySelectorAll('a.tiptap-topic').forEach(node=>node.remove());
      const paragraphs=[...clone.querySelectorAll('p')];
      const body=(paragraphs.length?paragraphs.map(node=>node.textContent||'').join('\n'):clone.textContent||'').replace(/\u00a0/g,' ').trim();
      return {body,topics};
    }""") or {'body': '', 'topics': []}


def _xhs_video_reference(value: object) -> dict | None:
    try:
        parsed = urlsplit(str(value or ''))
    except ValueError:
        return None
    host = (parsed.hostname or '').lower()
    if parsed.scheme not in {'https', 'http'} or not (host == 'xhscdn.com' or host.endswith('.xhscdn.com')):
        return None
    return {'kind': 'video', 'url': urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))[:2048]}


def _xhs_deleted_status(page, browser, target: str, kind: str) -> dict:
    observed = []
    def capture(response):
        try:
            url = urlsplit(response.url)
            if (url.hostname != 'edith.xiaohongshu.com' or url.path != '/web_api/sns/capa/postgw/note/detail'
                    or target not in url.query or response.status != 200):
                return
            payload = response.json()
            if isinstance(payload, dict):
                observed.append({'target_id': target, 'status': response.status,
                                 'code': payload.get('code'), 'success': payload.get('success'),
                                 'message': str(payload.get('msg') or '')[:120]})
        except Exception:
            pass
    page.on('response', capture)
    note_type = 'video' if kind == 'video' else 'normal'
    browser._goto(page, f'https://creator.xiaohongshu.com/publish/update?id={target}&noteType={note_type}', wait=1800)
    if len(observed) != 1:
        raise RemoteBrowserError('target_status_unconfirmed')
    result = observed[0]
    result['deleted'] = result['code'] == -9106 and result['success'] is False and '删除' in result['message']
    return result


def _xhs_run(directory, action: str, params: dict) -> dict:
    from . import xhs_browser as browser
    from .xhs_reply import IDENTITY_JS
    expected = str(params.get('expected_account_remote_id') or '')
    if not expected:
        raise RemoteBrowserError('account_identity_missing')
    target = str(params.get('remote_id') or '')
    p, context, page = browser._launch(directory)
    api_rows: dict[str, dict] = {}
    api_pages = 0
    detail_payload: dict = {}
    def capture(response):
        nonlocal api_pages
        try:
            url = urlsplit(response.url)
            if url.hostname != 'creator.xiaohongshu.com' or '/creator/note/user/posted' not in url.path or response.status != 200:
                return
            payload = response.json()
            data = payload.get('data') if isinstance(payload, dict) else {}
            if payload.get('code') not in (0, '0') or not isinstance(data.get('notes'), list):
                return
            api_pages += 1
            for note in data['notes']:
                if isinstance(note, dict) and re.fullmatch(r'[0-9A-Za-z]{16,40}', str(note.get('id') or '')):
                    api_rows[str(note['id'])] = note
        except Exception:
            pass
    page.on('response', capture)
    def capture_detail(response):
        try:
            url = urlsplit(response.url)
            if url.hostname != 'edith.xiaohongshu.com' or url.path != '/web_api/sns/capa/postgw/note/detail' or response.status != 200:
                return
            payload = response.json()
            data = payload.get('data') if isinstance(payload, dict) else {}
            if isinstance(data, dict) and str(data.get('id') or '') == target and payload.get('success') is True:
                detail_payload.update(data)
        except Exception:
            pass
    page.on('response', capture_detail)
    try:
        browser._goto(page, 'https://www.xiaohongshu.com/explore', wait=1200)
        if page.evaluate(IDENTITY_JS) != [expected]:
            raise RemoteBrowserError('account_mismatch')
        if action == 'deletion_check':
            if not re.fullmatch(r'[0-9A-Za-z]{16,40}', target):
                raise RemoteBrowserError('invalid_note_id')
            result = _xhs_deleted_status(page, browser, target, str(params.get('kind') or 'image'))
            browser._goto(page, 'https://www.xiaohongshu.com/explore', wait=700)
            if page.evaluate(IDENTITY_JS) != [expected]:
                raise RemoteBrowserError('account_mismatch')
            return {'account_remote_id': expected, **result}
        browser._goto(page, 'https://creator.xiaohongshu.com/new/note-manager', wait=1500)
        heading = ' '.join(page.locator('.tab-item').all_inner_texts()[:1])
        total_match = re.search(r'全部\s*([0-9]+)', heading)
        expected_total = int(total_match.group(1)) if total_match else None
        offset = max(0, int(params.get('cursor') or 0))
        limit = max(1, min(int(params.get('limit') or 20), 50))
        stable = 0
        previous = -1
        raw = []
        for _ in range(70):
            browser._risk(page)
            raw = page.evaluate(browser.MY_NOTES_JS, 10000) or []
            count = len(set(api_rows) | {browser._note_id(str(r.get('href') or ''), str(r.get('noteId') or '')) for r in raw})
            if action == 'detail' and (target in api_rows or any(browser._note_id(str(r.get('href') or ''), str(r.get('noteId') or '')) == target for r in raw)):
                break
            if action == 'list' and expected_total is not None and count >= expected_total:
                break
            if action == 'list' and count >= offset + limit + 1:
                break
            stable = stable + 1 if count == previous else 0
            if stable >= 5:
                break
            previous = count
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            page.wait_for_timeout(700)
        if not raw and not api_rows:
            body = (page.locator('body').inner_text(timeout=1000) or '')[:3000]
            if not any(marker in body for marker in ('暂无作品', '暂无笔记', '还没有发布')):
                raise RemoteBrowserError('read_unconfirmed')
        if not api_pages:
            raise RemoteBrowserError('read_unconfirmed')
        rows = [_xhs_row(r, expected, browser,
                         api_rows.get(browser._note_id(str(r.get('href') or ''), str(r.get('noteId') or '')))) for r in raw]
        present = {row['remote_id'] for row in rows}
        for note_id, note in api_rows.items():
            if note_id not in present:
                rows.append(_xhs_row({'noteId': note_id, 'title': note.get('display_title') or ''}, expected, browser, note))
        rows = [r for r in rows if re.fullmatch(r'[0-9A-Za-z]{16,40}', r['remote_id'])]
        rows = list({row['remote_id']: row for row in rows}.values())
        if action == 'detail':
            row = next((r for r in rows if r['remote_id'] == target), None)
            if not row:
                raise RemoteBrowserError('target_owner_unconfirmed')
            card = page.locator(f'.note-card[data-impression*="{target}"]')
            if card.count() == 1:
                actions = card.locator('.note-card__action-btn')
                row['edit_available'] = actions.count() >= 2 and 'disabled' not in (actions.nth(actions.count() - 2).get_attribute('class') or '')
            if row.get('edit_available') and card.count() == 1:
                actions = card.locator('.note-card__action-btn')
                actions.nth(actions.count() - 2).click()
                page.wait_for_timeout(1200)
                title_input = page.locator('div.d-input input, input[placeholder*="标题"]').first
                body_input = page.locator('div.ql-editor, div.editor-container [contenteditable="true"]').first
                if title_input.count() and body_input.count() and title_input.input_value() == row['title']:
                    fields = _xhs_editor_fields(page)
                    row['body'] = str(fields.get('body') or '')[:10000]
                    row['topics'] = [str(topic)[:100] for topic in fields.get('topics') or []][:30]
                    video_ref = _xhs_video_reference(detail_payload.get('video'))
                    if row['kind'] == 'video' and video_ref:
                        row['media'] = [*row['media'], video_ref]
                    row['detail_complete'] = row['kind'] != 'video' or video_ref is not None
            elif row['url']:
                browser._goto(page, row['url'], wait=1200)
                raw_detail = page.evaluate(browser.NOTE_JS) or {}
                row['body'] = str(raw_detail.get('body') or '')[:10000]
                images = [str(url)[:2048] for url in raw_detail.get('images') or [] if str(url).startswith('https://')]
                if images:
                    row['media'] = [{'url': url} for url in images]
                row['topics'] = re.findall(r'(?<!\w)#([^\s#]+)', row['body'])[:30]
                row['detail_complete'] = bool(row['body'])
            row['detail_checked'] = True
            return {'post': row}
        page_rows = rows[offset:offset + limit]
        complete = expected_total is not None and len(rows) >= expected_total and offset + limit >= expected_total
        next_cursor = None if complete or not page_rows else str(offset + len(page_rows))
        return {'items': page_rows, 'next_cursor': next_cursor,
                'complete': complete, 'checked_at': _now(), 'account_remote_id': expected}
    except XhsBrowserError as exc:
        raise RemoteBrowserError(str(exc)) from exc
    finally:
        browser._close(p, context)


def _xhs_write(directory, action: str, params: dict) -> dict:
    from . import xhs_browser as browser
    from .xhs_reply import IDENTITY_JS
    from .browser_submission import mark_submission
    expected = str(params.get('expected_account_remote_id') or '')
    target = str(params.get('remote_id') or '')
    snapshot = params.get('snapshot') if isinstance(params.get('snapshot'), dict) else {}
    changes = params.get('changes') if isinstance(params.get('changes'), dict) else {}
    if not expected or not re.fullmatch(r'[0-9A-Za-z]{16,40}', target) or snapshot.get('remote_id') != target:
        return {'state': 'not_submitted', 'not_submitted': True, 'reason': '作品或账号标识无效。'}
    p, context, page = browser._launch(directory)
    submitted = False
    receipts = []
    raw_responses = []
    manager_media = None
    def capture_manager(response):
        nonlocal manager_media
        try:
            url = urlsplit(response.url)
            if url.hostname != 'creator.xiaohongshu.com' or '/creator/note/user/posted' not in url.path or response.status != 200:
                return
            payload = response.json()
            notes = ((payload.get('data') or {}).get('notes') or []) if isinstance(payload, dict) else []
            selected = [item for item in notes if isinstance(item, dict) and str(item.get('id') or '') == target]
            if len(selected) == 1:
                manager_media = [{'url': str(item.get('url') or '')[:2048]} for item in selected[0].get('images_list') or []
                                 if isinstance(item, dict) and str(item.get('url') or '').startswith(('https://', 'http://'))]
        except Exception:
            pass
    page.on('response', capture_manager)
    try:
        browser._goto(page, 'https://www.xiaohongshu.com/explore', wait=900)
        if page.evaluate(IDENTITY_JS) != [expected]:
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '浏览器账号与目标账号不一致。'}
        browser._goto(page, 'https://creator.xiaohongshu.com/new/note-manager', wait=1300)
        card = page.locator(f'.note-card[data-impression*="{target}"]')
        heading = ' '.join(page.locator('.tab-item').all_inner_texts()[:1])
        total_match = re.search(r'全部\s*([0-9]+)', heading)
        max_scrolls = min(500, max(70, (int(total_match.group(1)) if total_match else 30) * 2))
        for _ in range(max_scrolls):
            if card.count() == 1:
                break
            if card.count() > 1:
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '创作者列表中目标不唯一。'}
            page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
            page.wait_for_timeout(350)
        if card.count() != 1:
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '创作者列表中未找到唯一目标，无法执行写入。'}
        title = card.locator('.note-card__title').inner_text()
        if title != snapshot.get('title'):
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台作品标题已变化，请重新预览。'}
        expected_covers = [item for item in snapshot.get('media') or [] if item.get('kind') != 'video']
        if manager_media is None or manager_media != expected_covers:
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台媒体已变化或无法核实，请重新预览。'}
        def capture(response):
            try:
                url = urlsplit(response.url)
                request_data = response.request.post_data or ''
                if (not submitted or url.hostname not in {'creator.xiaohongshu.com', 'edith.xiaohongshu.com'}
                        or response.request.method not in {'POST', 'PUT'}):
                    return
                body = response.json() if response.status == 200 else {}
                if isinstance(body, dict):
                    raw_responses.append({'path': url.path, 'request': request_data[:8192],
                                          'status': response.status, 'response': body})
                    if target in request_data + url.path:
                        receipts.append({'target_id': target, 'path': url.path, 'status': response.status,
                                         'code': body.get('code'), 'success': body.get('success'),
                                         'operation_id': params.get('operation_id')})
            except Exception:
                pass
        page.on('response', capture)
        if action == 'delete':
            button = card.locator('.note-card__action-btn--del')
            if button.count() != 1:
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '目标删除入口不唯一。'}
            button.click()
            page.wait_for_timeout(400)
            dialog = page.locator('.d-modal:visible')
            if dialog.count() != 1 or '删除' not in dialog.inner_text() or title[:6] not in dialog.inner_text():
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '删除确认框与目标不匹配。'}
            confirm = dialog.locator('button.confirm-button')
            if confirm.count() != 1:
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '删除确认按钮不唯一。'}
            mark_submission(str(params.get('submission_file') or ''), {'operation_id': params.get('operation_id'),
                                                                      'target_id': target, 'account_remote_id': expected})
            submitted = True
            confirm.click()
            page.wait_for_timeout(1100)
            try:
                target_status = _xhs_deleted_status(page, browser, target, str(snapshot.get('kind') or 'image'))
            except RemoteBrowserError:
                target_status = {'deleted': False}
            browser._goto(page, 'https://www.xiaohongshu.com/explore', wait=800)
            if page.evaluate(IDENTITY_JS) != [expected]:
                return {'state': 'unknown_result', 'reason': '删除后账号身份无法确认。', 'evidence': receipts}
            valid = [item for item in receipts if item['status'] == 200 and item['code'] in (0, '0', None)
                     and item['success'] is not False and any(word in item['path'].lower() for word in ('delete', 'remove'))]
            proof = {**valid[0], 'kind': 'platform_receipt', 'account_remote_id': expected,
                     'post_check': 'platform_note_deleted_code', 'platform_code': -9106} if len(valid) == 1 and target_status.get('deleted') is True else {}
            return {'state': 'verified' if proof else 'unknown_result',
                    'evidence': proof if proof else {'responses': receipts[:5], 'target_status': target_status},
                    'reason': '' if proof else '缺少目标删除响应或目标详情未确认已删除。'}
        if action == 'edit':
            detail_video = []
            def capture_video(response):
                try:
                    url = urlsplit(response.url)
                    if url.hostname != 'edith.xiaohongshu.com' or url.path != '/web_api/sns/capa/postgw/note/detail' or response.status != 200:
                        return
                    payload = response.json()
                    data = payload.get('data') if isinstance(payload, dict) else {}
                    if isinstance(data, dict) and str(data.get('id') or '') == target:
                        value = _xhs_video_reference(data.get('video'))
                        if value:
                            detail_video.append(value)
                except Exception:
                    pass
            page.on('response', capture_video)
            actions = card.locator('.note-card__action-btn')
            if actions.count() < 2:
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '当前作品没有编辑入口。'}
            edit = actions.nth(actions.count() - 2)
            if 'disabled' in (edit.get_attribute('class') or ''):
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台禁用该作品的编辑入口。'}
            edit.click()
            page.wait_for_timeout(1200)
            if snapshot.get('kind') == 'video' and detail_video[-1:] != [item for item in snapshot.get('media') or [] if item.get('kind') == 'video']:
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '原视频引用已变化，未保存修改。'}
            guide = page.locator('.feature-guide__btn:visible')
            if guide.count() == 1:
                guide.click()
            title_input = page.locator('div.d-input input, input[placeholder*="标题"]').first
            body_input = page.locator('div.ql-editor, div.editor-container [contenteditable="true"]').first
            if not title_input.count() or not body_input.count():
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台编辑器未打开。'}
            fields = _xhs_editor_fields(page)
            if (title_input.input_value() != snapshot.get('title')
                    or str(fields.get('body') or '').strip() != str(snapshot.get('body') or '').strip()
                    or list(fields.get('topics') or []) != list(snapshot.get('topics') or [])):
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台作品内容已变化，请重新预览。'}
            if 'title' in changes:
                title_input.fill(str(changes['title']))
                # 标题编辑器失焦后才更新预览和最终提交状态。
                title_input.press('Tab')
                page.wait_for_timeout(500)
                if title_input.input_value() != str(changes['title']):
                    return {'state': 'not_submitted', 'not_submitted': True, 'reason': '标题未进入平台编辑器，未保存修改。'}
            if 'body' in changes:
                body_input.fill(str(changes['body']))
            if 'topics' in changes:
                # 话题必须经平台编辑器联想绑定；无法确认联想项时停止提交。
                base = str(changes.get('body', snapshot.get('body') or ''))
                body_input.fill(base.strip())
                body_input.click()
                page.keyboard.press('Control+End')
                for topic in changes['topics']:
                    name = str(topic).lstrip('#')
                    recommended = page.locator('.recommend-topic-wrapper .tag')
                    matches = [index for index, value in enumerate(recommended.all_inner_texts()) if value.strip() == '#' + name]
                    if len(matches) == 1:
                        recommended.nth(matches[0]).click()
                    else:
                        page.keyboard.type(' #')
                        page.wait_for_timeout(250)
                        for char in name:
                            page.keyboard.type(char)
                            page.wait_for_timeout(150)
                        page.wait_for_timeout(1000)
                        suggestions = page.locator('#creator-editor-topic-container .item')
                        exact = [index for index, value in enumerate(suggestions.all_inner_texts()) if value.splitlines()[0].strip() == '#' + name]
                        if len(exact) != 1:
                            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台没有返回准确的话题选项，未保存编辑。'}
                        suggestions.nth(exact[0]).click()
                    if name not in _xhs_editor_fields(page).get('topics', []):
                        return {'state': 'not_submitted', 'not_submitted': True, 'reason': '话题未绑定到编辑器，未保存编辑。'}
                if list(_xhs_editor_fields(page).get('topics') or []) != list(changes['topics']):
                    return {'state': 'not_submitted', 'not_submitted': True, 'reason': '编辑器话题与预览不一致，未保存编辑。'}
            save = page.locator('xhs-publish-btn[is-publish="true"]')
            if save.count() != 1 or save.get_attribute('submit-disabled') == 'true':
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台编辑提交入口不唯一或不可用。'}
            box = save.bounding_box()
            if not box or box['width'] < 40:
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台编辑提交入口位置无效。'}
            mark_submission(str(params.get('submission_file') or ''), {'operation_id': params.get('operation_id'),
                                                                      'target_id': target, 'account_remote_id': expected})
            submitted = True
            # 修改页的提交按钮位于 host 中央；右侧空白区域不会触发平台请求。
            page.mouse.click(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
            page.wait_for_timeout(1200)
            dialog = page.locator('.d-modal:visible')
            if dialog.count() == 1:
                confirm = dialog.locator('button.confirm-button')
                if confirm.count() == 1:
                    confirm.click()
                    page.wait_for_timeout(900)
            for _ in range(16):
                if receipts:
                    break
                page.wait_for_timeout(500)
            return {'state': 'unknown_result', 'evidence': {'responses': receipts[:5]},
                    'reason': '已尝试提交编辑；等待平台回读核对。' if raw_responses else '点击后未捕获平台写入响应，结果待核对。'}
        return {'state': 'not_submitted', 'not_submitted': True, 'reason': '不支持的操作。'}
    except Exception:
        return {'state': 'unknown_result' if submitted else 'not_submitted', 'not_submitted': not submitted,
                'reason': '提交后状态不明。' if submitted else '提交前检查失败。'}
    finally:
        if submitted:
            try:
                evidence_path = Path(str(params.get('submission_file') or '')).with_name('browser-evidence.json')
                evidence_path.write_text(json.dumps({'responses': raw_responses[:10]}, ensure_ascii=False), encoding='utf-8')
            except (OSError, ValueError):
                pass
        browser._close(p, context)


def _x_write(adapter, options, action: str, params: dict) -> dict:
    expected = str(params.get('expected_account_remote_id') or '')
    target = str(params.get('remote_id') or '')
    snapshot = params.get('snapshot') if isinstance(params.get('snapshot'), dict) else {}
    changes = params.get('changes') if isinstance(params.get('changes'), dict) else {}
    if not expected.startswith('x-web:') or not re.fullmatch(r'[0-9]{1,30}', target) or snapshot.get('remote_id') != target:
        return {'state': 'not_submitted', 'not_submitted': True, 'reason': '账号或作品 ID 无效。'}
    manager, context, page = adapter._launch(options, headed=False)
    posts = {}
    receipts = []
    raw_responses = []
    delete_requests = []
    submitted = False
    checking_deletion = False
    deleted_detail = False
    handle = expected.removeprefix('x-web:')
    def capture_request(request):
        try:
            url = urlsplit(request.url)
            if not submitted or action != 'delete' or url.hostname not in {'x.com', 'www.x.com'}:
                return
            if url.path.rsplit('/', 1)[-1] != 'DeleteTweet' or request.method != 'POST':
                return
            payload = request.post_data_json
            variables = payload.get('variables', {}) if isinstance(payload, dict) else {}
            bound = str(variables.get('tweet_id') or variables.get('tweetId') or '')
            if bound == target:
                delete_requests.append({'target_id': target, 'operation_id': params.get('operation_id')})
        except Exception:
            pass
    def capture(response):
        nonlocal deleted_detail
        try:
            url = urlsplit(response.url)
            if url.hostname not in {'x.com', 'www.x.com'}:
                return
            operation = url.path.rsplit('/', 1)[-1]
            if operation == 'TweetDetail' and response.status == 200:
                detail = response.json()
                posts.update(extract_posts(detail))
                if checking_deletion and _x_target_unavailable(detail.get('data'), target):
                    deleted_detail = True
            if not submitted or operation not in {'EditTweet', 'DeleteTweet'} or response.request.method != 'POST':
                return
            request = response.request.post_data_json
            variables = request.get('variables', {}) if isinstance(request, dict) else {}
            data = response.json() if response.status == 200 else {}
            raw_responses.append({'path': url.path, 'request': variables,
                                  'status': response.status, 'response': data})
            bound = str(variables.get('tweet_id') or variables.get('tweetId') or variables.get('edit_tweet_id') or '')
            if bound != target:
                return
            if not isinstance(data, dict) or data.get('errors'):
                return
            if operation == 'DeleteTweet' and not _x_delete_response_valid(data):
                return
            evidence = {'kind': 'platform_receipt', 'operation': operation, 'target_id': target,
                        'account_remote_id': expected, 'status': response.status}
            if operation == 'EditTweet':
                matches = [post for post in extract_posts(data).values()
                           if post.get('author') == handle and post.get('content') == changes.get('body')]
                if len(matches) == 1:
                    evidence['new_remote_id'] = matches[0]['id']
            receipts.append(evidence)
        except Exception:
            pass
    page.on('request', capture_request)
    page.on('response', capture)
    try:
        page.goto('https://x.com/home', wait_until='domcontentloaded', timeout=30000)
        try:
            _x_confirm_identity(adapter, page, expected)
        except RemoteBrowserError:
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '浏览器账号与目标不匹配或登录失效。'}
        page.goto(f'https://x.com/{handle}/status/{target}', wait_until='domcontentloaded', timeout=30000)
        try:
            _x_confirm_identity(adapter, page, expected)
        except RemoteBrowserError:
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '账号身份已变化或登录失效。'}
        current = None
        for _ in range(20):
            current = posts.get(target)
            if current is not None:
                break
            page.wait_for_timeout(300)
        if current is None:
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台作品详情尚未加载，请重新读取。'}
        if current.get('author') != handle or current.get('content') != snapshot.get('body'):
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '作品作者或正文已变化，请重新预览。'}
        if current.get('media') != snapshot.get('media'):
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '作品媒体已变化，请重新预览。'}
        article = None
        for _ in range(20):
            article = page.evaluate_handle(ARTICLE_JS, target).as_element()
            if article is not None:
                break
            page.wait_for_timeout(300)
        if article is None:
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '目标帖子尚未加载或不唯一。'}
        caret = None
        for _ in range(15):
            caret = article.query_selector('[data-testid="caret"]')
            if caret is not None:
                break
            page.wait_for_timeout(300)
            article = page.evaluate_handle(ARTICLE_JS, target).as_element() or article
        if caret is None:
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '帖子菜单不可用。'}
        caret.click()
        page.wait_for_timeout(200)
        menu = page.get_by_role('menuitem', name=re.compile(r'Edit|编辑', re.I) if action == 'edit' else re.compile(r'Delete|删除', re.I))
        if menu.count() != 1 or not menu.is_enabled():
            return {'state': 'not_submitted', 'not_submitted': True,
                    'reason': '平台未提供编辑入口；请检查订阅、1 小时期限、修改次数及发布设备。' if action == 'edit' else '平台未提供删除入口。'}
        menu.click()
        if action == 'delete':
            confirm = page.locator('[data-testid="confirmationSheetConfirm"]')
            if confirm.count() != 1 or not confirm.is_enabled():
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '删除确认框不唯一。'}
            mark_submission(str(params.get('submission_file') or ''), {'operation_id': params.get('operation_id'),
                                                                      'target_id': target, 'account_remote_id': expected})
            submitted = True
            confirm.click()
            # 等待删除请求的响应，避免过早导航中断慢速请求。
            for _ in range(100):
                if any(item['operation'] == 'DeleteTweet' and item['status'] == 200 for item in receipts):
                    break
                page.wait_for_timeout(300)
            checking_deletion = True
            page.goto(f'https://x.com/{handle}/status/{target}', wait_until='domcontentloaded', timeout=30000)
            for _ in range(30):
                if deleted_detail:
                    break
                page.wait_for_timeout(300)
            try:
                _x_confirm_identity(adapter, page, expected)
                identity_ok = True
            except RemoteBrowserError:
                identity_ok = False
            still_visible = page.evaluate_handle(ARTICLE_JS, target).as_element() is not None
            proof = _x_delete_proof(receipts, target, expected, str(params.get('operation_id') or ''),
                                    identity_ok=identity_ok, target_unavailable=deleted_detail,
                                    article_present=still_visible)
            return {'state': 'verified' if proof else 'unknown_result',
                    'evidence': proof if proof else {'target_id': target, 'responses': receipts[:5],
                                                    'request_seen': bool(delete_requests),
                                                    'target_unavailable': deleted_detail},
                    'reason': '' if proof else '目标删除结果尚未确认。'}
        editor = page.locator('[role="dialog"][aria-modal="true"] [data-testid="tweetTextarea_0"][contenteditable="true"]')
        for _ in range(18):
            heads_up = page.get_by_role('dialog').filter(has_text=re.compile(r'Heads up|请注意', re.I))
            if heads_up.count() == 1:
                acknowledge = heads_up.get_by_role('button', name=re.compile(r'Got it|知道了|明白', re.I))
                if acknowledge.count() != 1:
                    return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台编辑提示尚未确认。'}
                acknowledge.click()
            if editor.count() == 1:
                break
            page.wait_for_timeout(300)
        if editor.count() != 1:
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '编辑器未打开。'}
        if editor.inner_text() != snapshot.get('body'):
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '编辑器内容已变化，请重新预览。'}
        expected_media = snapshot.get('media') or []
        media_previews = page.locator('[role="dialog"][aria-modal="true"] [data-testid="attachments"] img[src^="blob:"]')
        if expected_media:
            for _ in range(36):
                if media_previews.count() == len(expected_media):
                    break
                page.wait_for_timeout(300)
            if media_previews.count() != len(expected_media):
                return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台编辑器未载入全部原媒体，未提交文字修改。'}
        editor.fill(str(changes.get('body') or ''))
        if expected_media and media_previews.count() != len(expected_media):
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '填入文字后原媒体未保留，未提交修改。'}
        update = page.locator('[role="dialog"][aria-modal="true"] [data-testid="tweetButton"]')
        for _ in range(10):
            if update.count() == 1 and update.is_enabled():
                break
            page.wait_for_timeout(300)
        if update.count() != 1 or not update.is_enabled():
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '更新按钮不可用。'}
        if not re.search(r'Update|更新', update.inner_text(), re.I):
            return {'state': 'not_submitted', 'not_submitted': True, 'reason': '平台更新按钮身份未确认。'}
        mark_submission(str(params.get('submission_file') or ''), {'operation_id': params.get('operation_id'),
                                                                  'target_id': target, 'account_remote_id': expected})
        submitted = True
        update.click()
        page.wait_for_timeout(1100)
        valid = [item for item in receipts if item['operation'] == 'EditTweet' and item.get('new_remote_id')]
        return {'state': 'unknown_result', 'evidence': valid[0] if len(valid) == 1 else {'target_id': target, 'receipt_count': len(receipts)},
                'reason': '已提交编辑，等待读取新版本及媒体确认。'}
    except Exception:
        return {'state': 'unknown_result' if submitted else 'not_submitted', 'not_submitted': not submitted,
                'reason': '提交后结果不明。' if submitted else '提交前检查失败。'}
    finally:
        if submitted:
            try:
                evidence_path = Path(str(params.get('submission_file') or '')).with_name('browser-evidence.json')
                evidence_path.write_text(json.dumps({'responses': raw_responses[:10],
                                                    'delete_requests': delete_requests[:10]}, ensure_ascii=False), encoding='utf-8')
            except (OSError, ValueError):
                pass
        adapter._safe_close(manager, context)


def run(platform: str, adapter, options, directory, action: str, params: dict) -> dict:
    if action not in {'list', 'detail', 'deletion_check', 'edit', 'delete'}:
        raise RemoteBrowserError('unsupported_action')
    if action in {'edit', 'delete'}:
        if platform == 'xiaohongshu':
            return _xhs_write(directory, action, params)
        if platform == 'x':
            return _x_write(adapter, options, action, params)
        raise RemoteBrowserError('unsupported_action')
    if platform == 'x':
        return _x_run(adapter, options, action, params)
    if platform == 'xiaohongshu':
        return _xhs_run(directory, action, params)
    raise RemoteBrowserError('unsupported_platform')
