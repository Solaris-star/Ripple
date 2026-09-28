"""平台作品与写操作记录，独立于本地稿件和互动记录。"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import re
from urllib.parse import urlsplit
import uuid
from typing import Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from .publishing import WorkflowError, fingerprint


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class RemotePost(TypedDict):
    id: str
    platform: Literal['x', 'xiaohongshu']
    account_id: str
    account_remote_id: str
    remote_id: str
    original_remote_id: str
    version_ids: list[str]
    linked_versions: list[str]
    kind: str
    title: str
    body: str
    topics: list[str]
    media: list[dict]
    url: str
    remote_status: str
    visibility: str
    checked_at: str
    created_at: str
    version: str
    task_ids: list[str]
    action_ids: list[str]
    capabilities: dict
    detail_complete: bool
    edit_available: NotRequired[bool | None]


class RemotePostAction(TypedDict):
    id: str
    post_id: str
    platform: str
    account_id: str
    remote_id: str
    kind: Literal['edit', 'delete']
    operation_id: str
    snapshot: RemotePost
    changes: dict
    confirmed_version: str
    auth_revision: int
    status: str
    evidence: dict | None
    created_at: str
    updated_at: str


class RemoteSyncInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    account_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    cursor: str = Field(default='', max_length=16)
    limit: int = Field(default=20, ge=1, le=50)


class RemoteActionInput(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    kind: str = Field(pattern=r'^(edit|delete)$')
    expected_version: str = Field(pattern=r'^[a-f0-9]{64}$')
    idempotency_key: str = Field(min_length=8, max_length=100)
    title: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=10000)
    topics: list[str] | None = None


class RemoteActionExecuteInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirmed: bool
    operation_id: str = Field(pattern=r'^[a-f0-9]{32}$')
    expected_version: str = Field(pattern=r'^[a-f0-9]{64}$')
    auth_revision: int = Field(ge=0)


class RemotePostService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.store = workspace.store
        self.accounts = workspace.accounts

    def _account(self, account_id: str) -> dict:
        account = self.accounts.get(account_id)
        if account.get('platform') not in {'x', 'xiaohongshu'} or account.get('adapter') == 'x-api':
            raise WorkflowError('请选择 X 或小红书浏览器账号。', 422)
        if account.get('status') != 'connected' or not (account.get('identity') or {}).get('remote_id'):
            raise WorkflowError('账号身份尚未连接或校验。', 409)
        if (account.get('operation') or {}).get('state') == 'running':
            raise WorkflowError('账号正在执行其他操作，请稍后读取。', 429)
        return account

    @staticmethod
    def _rows(state: dict) -> dict:
        return state.setdefault('remote_posts', {})

    @staticmethod
    def _post(state: dict, post_id: str) -> dict:
        row = state.get('remote_posts', {}).get(post_id)
        if not row:
            raise WorkflowError('平台作品不存在。', 404)
        return row

    def list(self, account_id: str = '', limit: int = 30, offset: int = 0) -> dict:
        with self.store.transaction(write=False) as state:
            values = [deepcopy(row) for row in state.get('remote_posts', {}).values()
                      if not account_id or row.get('account_id') == account_id]
            values.sort(key=lambda row: (row.get('created_at') or row.get('checked_at') or '', row['id']), reverse=True)
            sync = {k: deepcopy(v) for k, v in state.get('remote_post_sync', {}).get(account_id, {}).items()
                    if not k.startswith('_')} if account_id else {}
            return {'items': values[offset:offset + limit], 'total': len(values), 'sync': sync}

    def get(self, post_id: str) -> dict:
        with self.store.transaction(write=False) as state:
            return deepcopy(self._post(state, post_id))

    @staticmethod
    def _url_id(platform: str, value: str) -> str:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ''
        if parsed.scheme != 'https' or parsed.username or parsed.password or parsed.fragment:
            return ''
        if platform == 'x' and parsed.hostname in {'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com'}:
            match = re.fullmatch(r'/([A-Za-z0-9_]{1,30})/status/([0-9]{1,30})/?', parsed.path)
            return match.group(2) if match else ''
        if platform == 'xiaohongshu' and parsed.hostname in {'www.xiaohongshu.com', 'xiaohongshu.com'}:
            match = re.fullmatch(r'/(?:explore|discovery/item|item)/([0-9A-Za-z]{16,40})/?', parsed.path)
            return match.group(1) if match else ''
        return ''

    @staticmethod
    def _capabilities(row: dict, account: dict) -> dict:
        reason = ''
        if row.get('remote_status') == 'deleted':
            reason = '作品已删除。'
        elif row.get('remote_status') in {'rejected', 'unknown', 'not_found_in_sync'}:
            reason = '平台状态尚未确认。'
        elif account.get('status') != 'connected':
            reason = '账号未连接。'
        elif (account.get('operation') or {}).get('state') in {'running', 'recovery_required'}:
            reason = '该账号有写操作待核对，暂不能提交其他写操作。'
        if row['platform'] == 'x':
            edit_reason = reason
            if not edit_reason and row.get('kind') == 'reply':
                edit_reason = 'X 不支持编辑回复。'
            if not edit_reason and row.get('kind') not in {'original', 'quote'}:
                edit_reason = '当前帖子类型不支持编辑。'
            if not edit_reason:
                try:
                    posted = parsedate_to_datetime(row.get('created_at') or '')
                    if (datetime.now(timezone.utc) - posted).total_seconds() >= 3600:
                        edit_reason = 'X 仅允许发布后 1 小时内编辑。'
                except (TypeError, ValueError):
                    edit_reason = '无法确认 X 发帖时间。'
            if not edit_reason and row.get('edits_remaining') is not None and int(row['edits_remaining']) <= 0:
                edit_reason = 'X 已达到最多 5 次修改。'
            if not edit_reason and row.get('editable_until_msecs') is not None:
                try:
                    if int(row['editable_until_msecs']) <= int(datetime.now(timezone.utc).timestamp() * 1000):
                        edit_reason = 'X 编辑时间窗口已结束。'
                except (TypeError, ValueError):
                    edit_reason = '无法确认 X 编辑时间窗口。'
            if not edit_reason and row.get('edit_available') is not True:
                edit_reason = '当前 X 页面未提供编辑入口；可能是订阅、时间、次数或发布设备限制。'
            return {'edit': not edit_reason, 'edit_reason': edit_reason,
                    'delete': not reason, 'delete_reason': reason}
        edit_reason = reason or ('' if row.get('edit_available') is True and row.get('detail_complete') else '需先读取作品编辑器中的完整内容及可用入口。')
        return {'edit': not edit_reason, 'edit_reason': edit_reason,
                'delete': not reason, 'delete_reason': reason}

    def _worker(self, account: dict, action: str, *, cursor: str = '', limit: int = 20,
                remote_id: str = '', kind: str = '') -> dict:
        if account.get('execution_node_id', 'local') != 'local':
            raise WorkflowError('远程 Browser Node 作品管理尚未启用。', 409)
        result = self.accounts.run(account, 'remote_posts_read', uuid.uuid4().hex, headed=False,
                                   remote_action=action,
                                   remote_params={'expected_account_remote_id': (account.get('identity') or {}).get('remote_id'),
                                                  'cursor': cursor, 'limit': limit, 'remote_id': remote_id,
                                                  'kind': kind})
        if result.get('state') != 'success':
            reason = str(result.get('message') or result.get('state') or '平台读取失败。')[:120]
            raise WorkflowError('平台作品读取失败：' + reason, 409 if result.get('state') in {'login_required', 'account_mismatch'} else 502)
        data = result.get('data')
        if not isinstance(data, dict):
            raise WorkflowError('平台作品响应无效。', 502)
        return data

    def _upsert(self, state: dict, account: dict, data: dict) -> dict:
        remote_id = str(data.get('remote_id') or '')
        expected = str((account.get('identity') or {}).get('remote_id') or '')
        if not remote_id or data.get('platform') != account['platform'] or data.get('account_remote_id') != expected:
            raise WorkflowError('平台作品身份与所选账号不匹配。', 409)
        if not re.fullmatch(r'[0-9]{1,30}' if account['platform'] == 'x' else r'[0-9A-Za-z]{16,40}', remote_id):
            raise WorkflowError('平台作品 ID 无效。', 502)
        key = account['id'] + ':' + remote_id
        previous = self._rows(state).get(key, {})
        detailed = data.get('detail_complete') is True
        version_ids = list(dict.fromkeys([str(x) for x in data.get('version_ids') or [remote_id]]))
        row = {**previous, 'id': key, 'platform': account['platform'], 'account_id': account['id'],
               'account_remote_id': expected, 'remote_id': remote_id,
               'version_ids': version_ids, 'original_remote_id': version_ids[0],
               'kind': str(data.get('kind') or 'unknown')[:30], 'title': str(data.get('title') or '')[:200],
               'body': str(data.get('body') or '')[:10000] if detailed else str(previous.get('body') or ''),
               'topics': [str(x)[:100] for x in data.get('topics') or []][:30] if detailed else deepcopy(previous.get('topics') or []),
               'media': deepcopy((data.get('media') or [])[:20]) if detailed or not previous.get('media') else deepcopy(previous['media']),
               'url': str(data.get('url') or previous.get('url') or '')[:2048],
               'remote_status': str(data.get('remote_status') or 'unknown')[:40],
               'visibility': str(data.get('visibility') or 'unknown')[:40],
               'checked_at': str(data.get('checked_at') or _now()),
               'created_at': str(data.get('created_at') or previous.get('created_at') or ''),
               'edits_remaining': data.get('edits_remaining'), 'editable_until_msecs': data.get('editable_until_msecs'),
               'detail_complete': detailed if data.get('detail_checked') is True else (detailed or previous.get('detail_complete') is True),
               'edit_available': data.get('edit_available') if 'edit_available' in data else previous.get('edit_available'),
               'action_ids': deepcopy(previous.get('action_ids') or [])}
        row['version'] = fingerprint({name: row[name] for name in ('remote_id', 'version_ids', 'kind', 'title', 'body', 'topics', 'media', 'remote_status', 'visibility')})
        linked = []
        for task in state.get('tasks', {}).values():
            if task.get('content', {}).get('account_id') != account['id']:
                continue
            receipt = task.get('receipt') or {}
            candidate_id = str(receipt.get('remote_id') or receipt.get('post_id') or '')
            if candidate_id in version_ids or (not candidate_id and self._url_id(account['platform'], str(receipt.get('public_url') or receipt.get('candidate_url') or '')) in version_ids):
                linked.append(task['id'])
                receipt['remote_id'] = candidate_id or remote_id
                receipt['account_remote_id'] = expected
                task['receipt'] = receipt
        row['task_ids'] = list(dict.fromkeys([*(previous.get('task_ids') or []), *linked]))
        row['linked_versions'] = [account['id'] + ':' + item for item in version_ids if item != remote_id]
        row['capabilities'] = self._capabilities(row, account)
        self._rows(state)[key] = row
        return deepcopy(row)

    def sync(self, req: RemoteSyncInput) -> dict:
        account = self._account(req.account_id)
        cursor = req.cursor or '0'
        if not re.fullmatch(r'[0-9]{1,12}', cursor):
            raise WorkflowError('继续加载位置无效。', 422)
        data = self._worker(account, 'list', cursor=cursor, limit=req.limit)
        if data.get('account_remote_id') != (account.get('identity') or {}).get('remote_id') or not isinstance(data.get('items'), list):
            raise WorkflowError('平台作品列表身份不匹配。', 502)
        with self.store.transaction() as state:
            current = self.accounts._account(state, account['id'])
            if current.get('auth_revision') != account.get('auth_revision'):
                raise WorkflowError('账号授权已变化，请重新同步。', 409)
            previous_sync = state.get('remote_post_sync', {}).get(account['id'], {})
            if cursor != '0' and previous_sync.get('next_cursor') != cursor:
                raise WorkflowError('继续加载位置已变化，请从第一页重新同步。', 409)
            items = [self._upsert(state, current, item) for item in data['items']]
            seen = set() if cursor == '0' else set(previous_sync.get('_seen_ids') or [])
            seen.update(item['id'] for item in items)
            if data.get('complete') is True:
                for post in self._rows(state).values():
                    if post.get('account_id') == account['id'] and post['id'] not in seen and post.get('remote_status') != 'deleted':
                        post['remote_status'] = 'not_found_in_sync'
                        post['capabilities'] = {'edit': False, 'edit_reason': '本次完整同步未找到该作品，请先单独核对。',
                                                'delete': False, 'delete_reason': '本次完整同步未找到该作品，请先单独核对。'}
            sync = {'account_id': account['id'], 'checked_at': _now(),
                    'complete': data.get('complete') is True, 'next_cursor': data.get('next_cursor'),
                    'last_count': len(items), '_seen_ids': sorted(seen)}
            state.setdefault('remote_post_sync', {})[account['id']] = sync
            sync.pop('_seen_ids')
            return {'items': items, 'sync': deepcopy(sync)}

    def refresh(self, post_id: str) -> dict:
        row = self.get(post_id)
        account = self._account(row['account_id'])
        data = self._worker(account, 'detail', remote_id=row['remote_id'])
        fresh = data.get('post')
        if not isinstance(fresh, dict) or fresh.get('remote_id') != row['remote_id']:
            raise WorkflowError('平台未返回目标作品；当前不能推断作品已删除。', 502)
        with self.store.transaction() as state:
            current = self.accounts._account(state, account['id'])
            if current.get('auth_revision') != account.get('auth_revision'):
                raise WorkflowError('账号授权已变化，请重新读取。', 409)
            return self._upsert(state, current, fresh)

    def read_task(self, task: dict) -> dict | None:
        account = self._account(task['content']['account_id'])
        receipt = task.get('receipt') or {}
        remote_id = str(receipt.get('remote_id') or receipt.get('post_id') or '')
        if not remote_id:
            remote_id = self._url_id(account['platform'], str(receipt.get('public_url') or receipt.get('candidate_url') or ''))
        if not remote_id:
            return None
        data = self._worker(account, 'detail', remote_id=remote_id)
        fresh = data.get('post')
        if not isinstance(fresh, dict) or fresh.get('remote_id') != remote_id:
            return None
        with self.store.transaction() as state:
            current = self.accounts._account(state, account['id'])
            if current.get('auth_revision') != account.get('auth_revision'):
                raise WorkflowError('账号授权已变化，请重新读取。', 409)
            return self._upsert(state, current, fresh)

    @staticmethod
    def _actions(state: dict) -> dict:
        return state.setdefault('remote_post_actions', {})

    @staticmethod
    def _public_action(row: dict) -> dict:
        value = deepcopy(row)
        if isinstance(value.get('evidence'), dict):
            value['evidence'].pop('private_evidence', None)
        return value

    def _x_delete_submitted(self, row: dict) -> bool:
        path = self.accounts.directory(row['account_id']) / 'operations' / row['operation_id'] / 'submission.json'
        try:
            if path.stat().st_size > 4096:
                return False
            marker = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, TypeError):
            return False
        return (isinstance(marker, dict) and marker.get('operation_id') == row['operation_id']
                and marker.get('target_id') == row['remote_id']
                and marker.get('account_remote_id') == row['snapshot']['account_remote_id'])

    def _x_profile_absence(self, account: dict, row: dict) -> dict:
        cursor = '0'
        seen_cursors = set()
        target_versions = set(row['snapshot'].get('version_ids') or [row['remote_id']])
        result = {'complete': False, 'target_found': False, 'checked_at': '', 'pages': 0}
        for _ in range(100):
            if cursor in seen_cursors:
                break
            seen_cursors.add(cursor)
            try:
                listing = self._worker(account, 'list', cursor=cursor, limit=50)
            except WorkflowError:
                break
            if (listing.get('account_remote_id') != row['snapshot']['account_remote_id']
                    or not isinstance(listing.get('items'), list)):
                break
            result['pages'] += 1
            result['checked_at'] = listing.get('checked_at') or ''
            for item in listing['items']:
                if not isinstance(item, dict):
                    continue
                item_versions = {str(item.get('remote_id') or ''), *(item.get('version_ids') or [])}
                if target_versions.intersection(item_versions):
                    result['target_found'] = True
                    return result
            if listing.get('complete') is True:
                result['complete'] = True
                return result
            next_cursor = str(listing.get('next_cursor') or '')
            if not re.fullmatch(r'[0-9]{1,12}', next_cursor):
                break
            cursor = next_cursor
        return result

    def get_action(self, action_id: str) -> dict:
        with self.store.transaction(write=False) as state:
            row = state.get('remote_post_actions', {}).get(action_id)
            if not row:
                raise WorkflowError('平台操作记录不存在。', 404)
            return self._public_action(row)

    def preview(self, post_id: str, req: RemoteActionInput) -> dict:
        before = self.get(post_id)
        if before['version'] != req.expected_version:
            raise WorkflowError('作品已变化，请重新查看后再预览。', 409)
        fresh = self.refresh(post_id)
        if fresh['version'] != req.expected_version:
            raise WorkflowError('平台作品已变化，请重新查看预览。', 409)
        account = self._account(fresh['account_id'])
        capability = fresh.get('capabilities') or {}
        if not capability.get(req.kind):
            raise WorkflowError(str(capability.get(req.kind + '_reason') or '平台当前不支持该操作。'), 409)
        changes = {}
        if req.kind == 'edit':
            allowed = {'body'} if fresh['platform'] == 'x' else {'title', 'body', 'topics'}
            for name in allowed:
                value = getattr(req, name)
                if value is not None and value != fresh.get(name):
                    changes[name] = value
            if not changes:
                raise WorkflowError('没有需要保存的文字修改。', 422)
            if fresh['platform'] == 'x':
                from .x_text import validate_reply
                validate_reply(str(changes.get('body') or fresh['body']))
            else:
                title = str(changes.get('title') or fresh['title'])
                units = title.encode('utf-16-le')
                length = (sum(2 if int.from_bytes(units[i:i + 2], 'little') > 127 else 1 for i in range(0, len(units), 2)) + 1) // 2
                if length > 20 or len(str(changes.get('body') or fresh['body'])) > 1000:
                    raise WorkflowError('小红书标题或正文超过平台长度限制。', 422)
                if 'topics' in changes and (len(changes['topics']) > 20 or any(not topic or len(topic) > 100 for topic in changes['topics'])):
                    raise WorkflowError('小红书话题无效。', 422)
        digest = fingerprint({'post_id': post_id, 'kind': req.kind, 'version': req.expected_version, 'changes': changes})
        with self.store.transaction() as state:
            for existing in self._actions(state).values():
                if existing.get('idempotency_key') == req.idempotency_key:
                    if existing.get('request_digest') != digest:
                        raise WorkflowError('操作创建键已被其他内容使用。', 409)
                    return self._public_action(existing)
            now = _now()
            row = {'id': uuid.uuid4().hex, 'post_id': post_id, 'platform': fresh['platform'],
                   'account_id': fresh['account_id'], 'remote_id': fresh['remote_id'], 'kind': req.kind,
                   'operation_id': uuid.uuid4().hex, 'idempotency_key': req.idempotency_key,
                   'request_digest': digest, 'snapshot': deepcopy(fresh), 'changes': changes,
                   'confirmed_version': fresh['version'], 'auth_revision': account['auth_revision'],
                   'status': 'preview', 'evidence': None, 'created_at': now, 'updated_at': now}
            self._actions(state)[row['id']] = row
            self._post(state, post_id).setdefault('action_ids', []).append(row['id'])
            return self._public_action(row)

    def _settle_action(self, action_id: str, outcome: dict) -> dict:
        with self.store.transaction() as state:
            row = self._actions(state).get(action_id)
            if not row:
                raise WorkflowError('平台操作记录不存在。', 404)
            if row['status'] not in {'dispatching', 'unknown_result', 'reviewing'}:
                return self._public_action(row)
            status = str(outcome.get('state') or 'unknown_result')
            if status not in {'verified', 'not_submitted', 'unknown_result', 'reviewing', 'partial'}:
                status = 'unknown_result'
            evidence = outcome.get('evidence') if isinstance(outcome.get('evidence'), dict) else {}
            if status == 'verified' and row['kind'] == 'delete' and not (
                    evidence.get('kind') == 'platform_receipt'
                    and evidence.get('target_id') == row['remote_id']
                    and evidence.get('account_remote_id') == row['snapshot']['account_remote_id']
                    and evidence.get('operation_id') == row['operation_id']
                    and evidence.get('post_check') in {'target_absent_after_reload', 'platform_note_deleted_code'}
                    and (evidence.get('post_check') != 'platform_note_deleted_code' or evidence.get('platform_code') == -9106)
                    and evidence.get('status') == 200):
                status = 'unknown_result'
            row.update(status=status, evidence=deepcopy(outcome.get('evidence')),
                       reason=str(outcome.get('reason') or '')[:300], updated_at=_now())
            account = state.get('accounts', {}).get(row['account_id'])
            if account and (account.get('operation') or {}).get('id') == row['operation_id']:
                needs_reconciliation = (status == 'unknown_result' or
                                        status == 'reviewing' and evidence.get('kind') != 'platform_readback_reviewing')
                account['operation'] = None if not needs_reconciliation else {
                    **account['operation'], 'state': 'recovery_required'}
            if account:
                for post in state.get('remote_posts', {}).values():
                    if post.get('account_id') == row['account_id']:
                        post['capabilities'] = self._capabilities(post, account)
            if status == 'verified' and row['kind'] == 'delete':
                post = self._post(state, row['post_id'])
                versions = set(post.get('version_ids') or [post['remote_id']])
                for linked in state.get('remote_posts', {}).values():
                    if linked.get('account_id') != row['account_id'] or not versions.intersection(
                            linked.get('version_ids') or [linked.get('remote_id')]):
                        continue
                    linked.update(remote_status='deleted', visibility='none', checked_at=_now(),
                                  capabilities={'edit': False, 'edit_reason': '作品已删除。',
                                                'delete': False, 'delete_reason': '作品已删除。'})
                    linked['version'] = fingerprint({name: linked[name] for name in
                                                     ('remote_id', 'version_ids', 'kind', 'title', 'body',
                                                      'topics', 'media', 'remote_status', 'visibility')})
            return self._public_action(row)

    def execute(self, action_id: str, req: RemoteActionExecuteInput) -> dict:
        row = self.get_action(action_id)
        if row['status'] != 'preview':
            return row
        if not req.confirmed or req.operation_id != row['operation_id'] or req.expected_version != row['confirmed_version'] or req.auth_revision != row['auth_revision']:
            raise WorkflowError('确认内容、操作 ID 或账号授权版本已变化，请重新预览。', 409)
        account = self._account(row['account_id'])
        if account['auth_revision'] != row['auth_revision']:
            raise WorkflowError('账号授权版本已变化，请重新预览。', 409)
        fresh = self.refresh(row['post_id'])
        if fresh['version'] != row['confirmed_version']:
            raise WorkflowError('平台作品已有新修改，请重新预览。', 409)
        with self.store.transaction() as state:
            current = self._actions(state)[action_id]
            if current['status'] != 'preview':
                return self._public_action(current)
            current_account = self.accounts._account(state, row['account_id'])
            if current_account.get('auth_revision') != req.auth_revision or (current_account.get('operation') or {}).get('state') in {'running', 'recovery_required'}:
                raise WorkflowError('账号授权已变化或另有待核对的写操作。', 409)
            if self._post(state, row['post_id'])['version'] != row['confirmed_version']:
                raise WorkflowError('作品版本已变化，请重新预览。', 409)
            current.update(status='dispatching', updated_at=_now())
            current_account['operation'] = {'id': row['operation_id'], 'kind': 'remote_post_action',
                                            'state': 'running', 'action_id': action_id, 'started_at': _now()}
            work = deepcopy(current)
            account = deepcopy(current_account)
        try:
            response = self.accounts.run(account, 'remote_posts_write', row['operation_id'], headed=False, confirmed=True,
                                         remote_action=row['kind'],
                                         remote_params={'expected_account_remote_id': row['snapshot']['account_remote_id'],
                                                        'remote_id': row['remote_id'], 'snapshot': row['snapshot'],
                                                        'changes': row['changes'], 'operation_id': row['operation_id']})
            data = response.get('data') if isinstance(response.get('data'), dict) else {}
            outcome = data if response.get('state') == 'success' else {
                'state': 'not_submitted' if response.get('not_submitted') is True else 'unknown_result',
                'reason': response.get('message') or '平台写入结果未确认。'}
        except Exception:
            outcome = {'state': 'unknown_result', 'reason': '执行进程中断，结果待核对。'}
        settled = self._settle_action(action_id, outcome)
        if settled['kind'] == 'edit' and settled['status'] in {'verified', 'unknown_result'}:
            return self.query(action_id)
        return settled

    def query(self, action_id: str) -> dict:
        row = self.get_action(action_id)
        if row['status'] not in {'dispatching', 'unknown_result', 'reviewing'}:
            return row
        result = self.accounts.read_result(row['account_id'], row['operation_id'])
        data = result.get('data') if isinstance(result, dict) and isinstance(result.get('data'), dict) else {}
        if data.get('state') == 'verified' and row['kind'] == 'delete':
            return self._settle_action(action_id, data)
        if row['kind'] == 'delete':
            saved = row.get('evidence') if isinstance(row.get('evidence'), dict) else {}
            candidates = [saved] if saved.get('kind') == 'platform_receipt' else saved.get('responses') or []
            valid = [item for item in candidates if isinstance(item, dict)
                     and item.get('target_id') == row['remote_id'] and item.get('status') == 200
                     and item.get('code') in (None, 0, '0') and item.get('success') is not False
                     and any(word in str(item.get('path') or item.get('operation') or '').lower()
                             for word in ('delete', 'remove'))]
            if len(valid) == 1:
                try:
                    account = self._account(row['account_id'])
                    if row['snapshot']['platform'] == 'xiaohongshu':
                        target_status = self._worker(account, 'deletion_check', remote_id=row['remote_id'],
                                                     kind=row['snapshot'].get('kind') or 'image')
                except WorkflowError:
                    return row
                if row['snapshot']['platform'] == 'xiaohongshu':
                    if (target_status.get('account_remote_id') == row['snapshot']['account_remote_id']
                            and target_status.get('target_id') == row['remote_id']
                            and target_status.get('deleted') is True and target_status.get('code') == -9106):
                        proof = {**valid[0], 'kind': 'platform_receipt', 'target_id': row['remote_id'],
                                 'account_remote_id': row['snapshot']['account_remote_id'],
                                 'operation_id': row['operation_id'], 'post_check': 'platform_note_deleted_code',
                                 'platform_code': -9106}
                        return self._settle_action(action_id, {'state': 'verified', 'evidence': proof})
                else:
                    profile = self._x_profile_absence(account, row)
                    if profile['complete'] and not profile['target_found']:
                        proof = {**valid[0], 'kind': 'platform_receipt', 'target_id': row['remote_id'],
                                 'account_remote_id': row['snapshot']['account_remote_id'],
                                 'operation_id': row['operation_id'], 'post_check': 'target_absent_after_reload'}
                        return self._settle_action(action_id, {'state': 'verified', 'evidence': proof})
            elif (row.get('platform') or row.get('snapshot', {}).get('platform')) == 'x':
                try:
                    account = self._account(row['account_id'])
                    checked = self._worker(account, 'deletion_check', remote_id=row['remote_id'])
                except WorkflowError:
                    return row
                if (checked.get('target_id') == row['remote_id']
                        and checked.get('account_remote_id') == row['snapshot']['account_remote_id']):
                    profile = (self._x_profile_absence(account, row)
                               if checked.get('target_unavailable') is True and checked.get('exists') is False
                               and self._x_delete_submitted(row) else None)
                    absent = bool(profile and profile['complete'] and not profile['target_found'])
                    with self.store.transaction() as state:
                        current = self._actions(state).get(action_id)
                        if current and current.get('status') in {'dispatching', 'unknown_result'}:
                            summary = deepcopy(current.get('evidence') or {})
                            summary['readback'] = {name: checked.get(name) for name in
                                                   ('target_id', 'account_remote_id', 'exists',
                                                    'target_unavailable', 'checked_at')}
                            if profile:
                                summary['profile_check'] = profile
                            current['evidence'] = summary
                            current['status'] = 'unknown_result'
                            current['reason'] = ('目标详情和完整本人作品列表均未找到该 ID；缺少删除回执，不能确认删除。账号可继续处理其他作品。'
                                                 if absent else '目标详情当前不可用，但缺少平台删除回执；继续待核对。'
                                                 if checked.get('target_unavailable') else
                                                 '目标仍可读取或平台状态不明；继续待核对。')
                            current['updated_at'] = _now()
                            current_account = state.get('accounts', {}).get(row['account_id'])
                            if (absent and current_account
                                    and (current_account.get('operation') or {}).get('id') == row['operation_id']):
                                current_account['operation'] = None
                                versions = set(row['snapshot'].get('version_ids') or [row['remote_id']])
                                for post in state.get('remote_posts', {}).values():
                                    if post.get('account_id') == row['account_id']:
                                        if (post.get('remote_status') != 'deleted' and versions.intersection(
                                                post.get('version_ids') or [post.get('remote_id')])):
                                            post['remote_status'] = 'not_found_in_sync'
                                            post['checked_at'] = profile.get('checked_at') or checked.get('checked_at') or _now()
                                            post['version'] = fingerprint({name: post[name] for name in
                                                                         ('remote_id', 'version_ids', 'kind', 'title',
                                                                          'body', 'topics', 'media', 'remote_status', 'visibility')})
                                        post['capabilities'] = self._capabilities(post, current_account)
                            return self._public_action(current)
        if row['kind'] == 'edit':
            if row['status'] == 'reviewing':
                with self.store.transaction(write=False) as state:
                    deleted_post = state.get('remote_posts', {}).get(row['post_id']) or {}
                    later = [item for item in state.get('remote_post_actions', {}).values()
                             if item.get('post_id') == row['post_id'] and item.get('kind') == 'delete'
                             and str(item.get('created_at') or '') > str(row.get('created_at') or '')
                             and (item.get('snapshot') or {}).get('remote_status') == 'published']
                for item in sorted(later, key=lambda value: value['created_at']):
                    snapshot = item['snapshot']
                    if (deleted_post.get('remote_status') == 'deleted'
                            and snapshot.get('account_remote_id') == row['snapshot']['account_remote_id']
                            and snapshot.get('media') == row['snapshot'].get('media')
                            and all(snapshot.get(name) == value for name, value in row['changes'].items())):
                        return self._settle_action(action_id, {'state': 'verified',
                            'evidence': {'kind': 'later_platform_snapshot', 'target_id': row['remote_id'],
                                         'source_action_id': item['id'], 'checked_at': snapshot['checked_at']}})
            evidence = row.get('evidence') if isinstance(row.get('evidence'), dict) else {}
            new_remote_id = str(evidence.get('new_remote_id') or '')
            if (row.get('platform') or row.get('snapshot', {}).get('platform')) == 'x' and not new_remote_id:
                try:
                    account = self._account(row['account_id'])
                    listing = self._worker(account, 'list', cursor='0', limit=50)
                    candidates = [item for item in listing.get('items') or [] if isinstance(item, dict)
                                  and item.get('account_remote_id') == row['snapshot']['account_remote_id']
                                  and item.get('remote_id') != row['remote_id']
                                  and row['remote_id'] in (item.get('version_ids') or [])
                                  and item.get('kind') == row['snapshot'].get('kind')
                                  and all(item.get(name) == value for name, value in row['changes'].items())]
                    if len(candidates) == 1:
                        new_remote_id = str(candidates[0]['remote_id'])
                except WorkflowError:
                    pass
            try:
                if new_remote_id and new_remote_id != row['remote_id']:
                    account = self._account(row['account_id'])
                    data = self._worker(account, 'detail', remote_id=new_remote_id)
                    post_data = data.get('post')
                    if not isinstance(post_data, dict) or post_data.get('remote_id') != new_remote_id:
                        return row
                    with self.store.transaction() as state:
                        fresh = self._upsert(state, self.accounts._account(state, account['id']), post_data)
                else:
                    fresh = self.refresh(row['post_id'])
            except WorkflowError:
                return row
            if ((row.get('platform') or row.get('snapshot', {}).get('platform')) == 'x'
                    and new_remote_id and fresh.get('media') != row['snapshot'].get('media')
                    and all(fresh.get(name) == value for name, value in row['changes'].items())):
                return self._settle_action(action_id, {'state': 'partial',
                    'evidence': {'kind': 'platform_readback_media_changed', 'target_id': row['remote_id'],
                                 'result_post_id': fresh['id'], 'before_media': row['snapshot'].get('media'),
                                 'after_media': fresh.get('media'), 'checked_at': fresh['checked_at']},
                    'reason': '平台保存了文字修改，但原媒体发生变化；请核对并处理这条测试作品。'})
            if (fresh.get('media') == row['snapshot'].get('media')
                    and all(fresh.get(name) == value for name, value in row['changes'].items())):
                reviewing = (row.get('platform') or row.get('snapshot', {}).get('platform')) == 'xiaohongshu' and fresh.get('remote_status') == 'reviewing'
                settled = self._settle_action(action_id, {'state': 'reviewing' if reviewing else 'verified',
                    'evidence': {'kind': 'platform_readback_reviewing' if reviewing else 'platform_readback',
                                 'target_id': row['remote_id'], 'result_post_id': fresh['id'],
                                 'version': fresh['version'], 'checked_at': fresh['checked_at']},
                    'reason': '修改已提交，审核中。' if reviewing else ''})
                return settled
            if (row.get('platform') or row.get('snapshot', {}).get('platform')) == 'xiaohongshu' and fresh.get('remote_status') == 'reviewing':
                proof = row.get('evidence') if isinstance(row.get('evidence'), dict) else {}
                receipts = [item for item in proof.get('responses') or [] if isinstance(item, dict)
                            and item.get('target_id') == row['remote_id'] and item.get('status') == 200
                            and item.get('code') in (0, '0') and item.get('success') is not False
                            and any(word in str(item.get('path') or '').lower() for word in ('publish', 'update', 'edit'))]
                if len(receipts) == 1:
                    return self._settle_action(action_id, {'state': 'reviewing', 'evidence': proof,
                        'reason': '修改已提交，平台审核中；等待内容和媒体回读确认。'})
            if fresh.get('media') == row['snapshot'].get('media'):
                proof = row.get('evidence') if isinstance(row.get('evidence'), dict) else {}
                receipts = [item for item in proof.get('responses') or [] if isinstance(item, dict)
                            and item.get('target_id') == row['remote_id'] and item.get('status') == 200
                            and item.get('code') in (None, 0, '0') and item.get('success') is not False
                            and any(word in str(item.get('path') or '').lower() for word in ('update', 'edit'))]
                matched = [name for name, value in row['changes'].items() if fresh.get(name) == value]
                missing = [name for name in row['changes'] if name not in matched]
                try:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(row['created_at'])).total_seconds()
                except (KeyError, TypeError, ValueError):
                    elapsed = 0
                if len(receipts) == 1 and matched and missing and elapsed >= 30:
                    return self._settle_action(action_id, {'state': 'partial',
                        'evidence': {'kind': 'platform_readback_partial', 'target_id': row['remote_id'],
                                     'matched_fields': matched, 'missing_fields': missing,
                                     'media_unchanged': True, 'checked_at': fresh['checked_at']},
                        'reason': '平台只保存了部分文字字段，请查看差异后创建新的修改预览。'})
        return row

    def recover(self) -> int:
        changed = 0
        with self.store.transaction() as state:
            for row in state.get('remote_post_actions', {}).values():
                if row.get('status') == 'dispatching':
                    row.update(status='unknown_result', reason='进程中断，写入结果待核对。', updated_at=_now())
                    account = state.get('accounts', {}).get(row['account_id'])
                    if account and (account.get('operation') or {}).get('id') == row['operation_id']:
                        account['operation']['state'] = 'recovery_required'
                    changed += 1
        return changed
