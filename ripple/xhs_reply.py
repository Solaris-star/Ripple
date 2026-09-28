"""小红书回复只执行一次最终点击，并校验目标、账号与回执。"""
from pathlib import Path
from urllib.parse import urlsplit

from .browser_submission import mark_submission


IDENTITY_JS = r"""() => {
  const ids = [...document.querySelectorAll('.main-container .user a[href*="/user/profile/"]')]
    .map(a => (a.getAttribute('href') || '').match(/\/user\/profile\/([^/?#]+)/)?.[1]).filter(Boolean);
  return [...new Set(ids)];
}"""
OWNER_JS = r"""(noteId) => {
  const state = window.__INITIAL_STATE__;
  const detail = state?.note?.noteDetailMap?.[noteId]?.note;
  if (detail && String(detail.noteId || detail.note_id || noteId) === noteId) {
    return String(detail.user?.userId || detail.user?.user_id || '');
  }
  const links = [...document.querySelectorAll('.note-detail-mask .author-wrapper a[href*="/user/profile/"], .note-detail .author-container a[href*="/user/profile/"]')];
  const ids = [...new Set(links.map(a => (a.getAttribute('href') || '').match(/\/user\/profile\/([^/?#]+)/)?.[1]).filter(Boolean))];
  return ids.length === 1 ? ids[0] : '';
}"""
TARGET_JS = r"""(id) => {
  const rows = [...document.querySelectorAll('[data-comment-id],[data-id],[id]')].filter(e =>
    e.getAttribute('data-comment-id') === id || e.getAttribute('data-id') === id || e.id === id || e.id === 'comment-' + id);
  return rows.length === 1 ? rows[0] : null;
}"""


def verify_owner(page, note_id: str, expected: str) -> None:
    from .xhs_browser import XhsBrowserError, _risk
    _risk(page)
    if not expected:
        raise XhsBrowserError('account_identity_missing')
    ids = page.evaluate(IDENTITY_JS)
    if ids != [expected]:
        raise XhsBrowserError('account_mismatch')
    if page.evaluate(OWNER_JS, note_id) != expected:
        raise XhsBrowserError('target_owner_unconfirmed')


def receipt_evidence(response, note_id: str, comment_id: str, expected: str, text: str) -> dict | None:
    """只接受本次请求的精确目标与服务端返回的发送者、回复 ID。"""
    parsed = urlsplit(response.url)
    if parsed.hostname not in {'edith.xiaohongshu.com', 'www.xiaohongshu.com'} or parsed.path != '/api/sns/web/v1/comment/post':
        return None
    if response.status != 200 or response.request.method != 'POST':
        return None
    request = response.request.post_data_json
    body = response.json()
    if not isinstance(request, dict) or not isinstance(body, dict) or body.get('success') is not True:
        return None
    if str(request.get('note_id') or '') != note_id or str(request.get('target_comment_id') or '') != comment_id or request.get('content') != text:
        return None
    data = body.get('data') or {}
    comment = data.get('comment') or data
    if not isinstance(comment, dict):
        return None
    author = comment.get('user_info') or comment.get('userInfo') or {}
    uid = str(author.get('user_id') or author.get('userId') or '')
    reply_id = str(comment.get('id') or comment.get('comment_id') or '')
    if not reply_id or uid != expected or comment.get('content') != text:
        return None
    return {'kind': 'platform_receipt', 'target_id': note_id, 'target_comment_id': comment_id,
            'account_remote_id': uid, 'reply_id': reply_id, 'text': text}


def reply(directory: Path, url: str, replies: list[dict], *, expected_account_remote_id: str, submission_file: str) -> dict:
    from .xhs_browser import XhsBrowserError, _close, _fill, _goto, _input, _launch, _risk, parse_note_url
    if len(replies) != 1:
        raise XhsBrowserError('one_reply_per_operation_required')
    item = replies[0]
    cid, text = str(item.get('id') or ''), str(item.get('reply') or '').strip()
    outcome = {'id': cid, 'status': 'not_submitted', 'reason': ''}
    if not cid or not text or len(text) > 1000:
        return {'results': [{**outcome, 'reason': '评论 ID 或回复内容无效。'}]}
    note_id, _, locator = parse_note_url(url)
    p, context, page = _launch(directory, headed=False)
    submitted = False
    receipts = []
    try:
        _goto(page, locator, wait=1800)
        verify_owner(page, note_id, expected_account_remote_id)
        container = page.evaluate_handle(TARGET_JS, cid).as_element()
        if container is None:
            return {'results': [{**outcome, 'reason': '评论 ID 无法唯一定位，请重新同步。'}]}
        controls = [node for node in container.query_selector_all('*') if (node.text_content() or '').strip() == '回复' and node.is_visible()]
        if not controls:
            return {'results': [{**outcome, 'reason': '未找到目标评论的回复按钮。'}]}
        controls[0].click()
        page.wait_for_timeout(500)
        inp = _input(page)
        if inp is None:
            return {'results': [{**outcome, 'reason': '未找到回复输入框。'}]}
        _fill(inp, text)
        send = None
        for selector in ("button:has-text('发送')", "[class*='send-btn']"):
            candidates = page.locator(selector)
            visible = [candidates.nth(index) for index in range(candidates.count()) if candidates.nth(index).is_visible()]
            if len(visible) == 1:
                send = visible[0]
                break
        if send is None:
            return {'results': [{**outcome, 'reason': '无法唯一定位发送按钮。'}]}
        def capture(response):
            try:
                evidence = receipt_evidence(response, note_id, cid, expected_account_remote_id, text)
                if submitted and evidence:
                    receipts.append(evidence)
            except Exception:
                pass
        page.on('response', capture)
        verify_owner(page, note_id, expected_account_remote_id)
        mark_submission(submission_file, {'target_id': note_id, 'comment_id': cid, 'account_remote_id': expected_account_remote_id})
        submitted = True
        send.click()
        page.wait_for_timeout(1600)
        _risk(page)
        outcome.update(status='verified' if len(receipts) == 1 else 'unknown_result', evidence=receipts[0] if len(receipts) == 1 else {},
                       reason='' if len(receipts) == 1 else '缺少绑定目标和账号的回执，请人工核对。')
    except FileExistsError:
        outcome.update(status='unknown_result', reason='本条已有提交记录，不能再次点击发送。')
    except Exception as exc:
        code = str(exc)
        outcome.update(status='unknown_result' if submitted else 'not_submitted',
                       reason='提交结果待核对。' if submitted else {
                           'account_identity_missing': '缺少账号身份，请在账号与平台重新校验。',
                           'account_mismatch': '浏览器账号与所选账号不一致。',
                           'target_owner_unconfirmed': '无法确认作品属于当前账号。',
                           'login_required': '登录已失效，请主动打开登录窗口处理。',
                           'risk_control': '平台要求验证，请主动打开登录窗口处理。',
                       }.get(code, '提交前检查失败，本条未发送。'),
                       stop_batch=code in {'login_required', 'risk_control', 'account_mismatch', 'account_identity_missing'})
    finally:
        _close(p, context)
    return {'results': [outcome]}
