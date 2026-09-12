"""Xiaohongshu read, interaction-draft and metric coordination for Ripple.

Browser execution stays inside the account-isolated native worker. This service
owns public sanitization, short-lived note locators, interaction idempotency and
result reconciliation. Agent tools may read and create drafts but never execute
an external interaction without the user's explicit workspace confirmation.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import time
import uuid
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .publishing import WorkflowError


MAX_LOCATORS = 300
MAX_SNAPSHOTS = 300
XHS_PUBLIC_HOSTS = {"xiaohongshu.com", "www.xiaohongshu.com"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_url(value: str) -> str:
    try:
        parsed = urlsplit(str(value or '').strip())
    except ValueError:
        return ''
    host = (parsed.hostname or '').lower().rstrip('.')
    if parsed.scheme != 'https' or not any(host == root or host.endswith('.' + root) for root in XHS_PUBLIC_HOSTS):
        return ''
    if parsed.username or parsed.password or parsed.fragment:
        return ''
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))[:2048]


def _note_id_from_url(value: str) -> str:
    try:
        path = urlsplit(value).path
    except ValueError:
        return ''
    match = re.search(r"/(?:explore|discovery/item|item)/([0-9A-Za-z]+)", path)
    return match.group(1) if match else ''


class XhsOpsService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.accounts = workspace.accounts
        self.store = workspace.store

    def _account(self, account_id: str) -> dict[str, Any]:
        account = self.accounts.get(account_id)
        if account.get('platform') != 'xiaohongshu' or account.get('adapter') not in {None, 'native'}:
            raise WorkflowError("请选择 Ripple 中的小红书原生账号。", 422)
        if account.get('status') != 'connected':
            raise WorkflowError("小红书账号当前未连接，请先完成登录或重新校验。", 409)
        return account

    def _locator_path(self, account_id: str) -> Path:
        return self.accounts.directory(account_id) / "xhs-note-locators.json"

    def _read_locators(self, account_id: str) -> dict[str, dict[str, Any]]:
        path = self._locator_path(account_id)
        try:
            if not path.is_file() or path.stat().st_size > 1024 * 1024:
                return {}
            data = json.loads(path.read_text(encoding='utf-8'))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    def _save_locators(self, account_id: str, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        data = self._read_locators(account_id)
        now = time.time()
        for row in rows:
            note_id = str(row.get('note_id') or '')[:100]
            url = str(row.get('url') or '')[:4096]
            if note_id and _public_url(url):
                data[note_id] = {'url': url, 'at': now}
        if len(data) > MAX_LOCATORS:
            data = dict(sorted(data.items(), key=lambda item: float((item[1] or {}).get('at') or 0), reverse=True)[:MAX_LOCATORS])
        path = self._locator_path(account_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        os.replace(temp, path)

    def _remember_url(self, account_id: str, value: str) -> str:
        note_id = _note_id_from_url(value)
        if not note_id or not _public_url(value):
            raise WorkflowError("小红书笔记链接无效。", 422)
        self._save_locators(account_id, [{'note_id': note_id, 'url': value}])
        return note_id

    def _locator(self, account_id: str, *, note_id: str = '', url: str = '') -> tuple[str, str]:
        if url:
            note_id = self._remember_url(account_id, url)
            return note_id, url[:4096]
        note_id = str(note_id or '')[:100]
        row = self._read_locators(account_id).get(note_id) if note_id else None
        locator = str((row or {}).get('url') or '')
        if not note_id or not locator:
            raise WorkflowError("缺少可用的笔记定位信息。请先从 Ripple 读取该笔记，或粘贴完整小红书笔记链接。", 422)
        return note_id, locator

    def _record_snapshot(self, account_id: str, kind: str, data: dict[str, Any]) -> None:
        public = deepcopy(data)
        public.pop('_locators', None)
        entry = {'id': uuid.uuid4().hex, 'account_id': account_id, 'kind': kind, 'at': _now(), 'data': public}
        with self.store.transaction() as state:
            rows = state.setdefault('xhs_snapshots', [])
            if not isinstance(rows, list):
                rows = state['xhs_snapshots'] = []
            rows.append(entry)
            state['xhs_snapshots'] = rows[-MAX_SNAPSHOTS:]

    def _worker(self, account_id: str, action: str, params: dict[str, Any], *, interaction: bool = False, operation_id: str | None = None) -> dict[str, Any]:
        account = self._account(account_id)
        operation_id = operation_id or uuid.uuid4().hex
        if not interaction:
            operation = account.get('operation') or {}
            if operation.get('state') in {'running', 'recovery_required'}:
                if operation.get('state') == 'recovery_required':
                    raise WorkflowError("该账号有结果待核对的真实操作，请先处理回执。", 409)
                raise WorkflowError("该账号正在执行登录、发布或互动操作，请稍后再读。", 429)
        result = self.accounts.run(
            account, 'xhs_interact' if interaction else 'xhs_read', operation_id,
            headed=interaction, confirmed=bool(interaction), xhs_action=action, xhs_params=params,
        )
        state = str(result.get('state') or 'error')
        if interaction and result.get('not_submitted') is True:
            state = 'not_submitted'
        if state not in {'success', 'verified', 'partial', 'unknown_result', 'not_submitted'}:
            message = str(result.get('message') or '小红书操作未完成。')[:300]
            status = 429 if '正在使用' in message else 409 if state in {'login_required','verification_required'} else 502
            raise WorkflowError(message, status)
        data = result.get('data') if isinstance(result.get('data'), dict) else {}
        locators = data.pop('_locators', []) if isinstance(data, dict) else []
        if isinstance(locators, list):
            self._save_locators(account_id, [row for row in locators if isinstance(row, dict)])
        return {'state': state, 'data': data, 'operation_id': operation_id, 'message': str(result.get('message') or '')[:300]}

    def execute_interaction_action(self, account_id: str, action: str, locator: str,
                                   payload: dict[str, Any], operation_id: str) -> dict[str, Any]:
        if action not in {'reply', 'delete', 'comment'}:
            raise WorkflowError("不支持的小红书互动类型。", 422)
        return self._worker(account_id, action, {'url': locator, **payload}, interaction=True, operation_id=operation_id)

    def feed(self, account_id: str, limit: int = 12) -> dict[str, Any]:
        result = self._worker(account_id, 'feed', {'limit': max(1, min(limit, 30))})
        payload = {'source': 'recommended_feed', 'sample_scope': 'selected_account_personalized_feed', 'fetched_at': _now(), **result['data']}
        self._record_snapshot(account_id, 'feed', payload)
        return payload

    def search(self, account_id: str, query: str, limit: int = 12) -> dict[str, Any]:
        query = str(query or '').strip()
        if not query or len(query) > 100:
            raise WorkflowError("搜索关键词不能为空且不能超过 100 字符。", 422)
        result = self._worker(account_id, 'search', {'query': query, 'limit': max(1, min(limit, 30))})
        payload = {'source': 'keyword_search', 'sample_scope': f'query:{query}', 'query': query, 'fetched_at': _now(), **result['data']}
        self._record_snapshot(account_id, 'search', payload)
        return payload

    def notes(self, account_id: str, limit: int = 20) -> dict[str, Any]:
        result = self._worker(account_id, 'notes', {'limit': max(1, min(limit, 30))})
        payload = {'source': 'connected_account_notes', 'sample_scope': 'selected_account_creator_notes', 'fetched_at': _now(), **result['data']}
        self._record_snapshot(account_id, 'notes', payload)
        return payload

    def note(self, account_id: str, *, note_id: str = '', url: str = '') -> dict[str, Any]:
        note_id, locator = self._locator(account_id, note_id=note_id, url=url)
        result = self._worker(account_id, 'note', {'url': locator})
        payload = {'source': 'note_detail', 'sample_scope': f'note:{note_id}', 'fetched_at': _now(), **result['data']}
        self._record_snapshot(account_id, 'note', payload)
        return payload

    def comments(self, account_id: str, *, note_id: str = '', url: str = '', limit: int = 50) -> dict[str, Any]:
        note_id, locator = self._locator(account_id, note_id=note_id, url=url)
        result = self._worker(account_id, 'comments', {'url': locator, 'limit': max(1, min(limit, 100))})
        payload = {'source': 'note_comments', 'sample_scope': f'note:{note_id}', 'fetched_at': _now(), **result['data']}
        self._record_snapshot(account_id, 'comments', payload)
        return payload

    def snapshots(self, account_id: str, *, kind: str = '', limit: int = 30) -> dict[str, Any]:
        account = self.accounts.get(account_id)
        if account.get('platform') != 'xiaohongshu':
            raise WorkflowError("请选择 Ripple 中的小红书账号。", 422)
        with self.store.transaction(write=False) as state:
            rows = state.get('xhs_snapshots', []) if isinstance(state.get('xhs_snapshots', []), list) else []
            selected = [deepcopy(row) for row in rows if row.get('account_id') == account_id and (not kind or row.get('kind') == kind)]
        return {'items': selected[-max(1, min(limit, 100)):][::-1]}

    def draft_interaction(self, *, account_id: str, note_id: str = '', url: str = '', kind: str,
                          items: list[dict[str, Any]] | None = None, text: str = '', idempotency_key: str) -> dict[str, Any]:
        return self.workspace.interactions.create_draft(
            platform='xiaohongshu', account_id=account_id, target_id=note_id, target_url=url,
            kind=kind, items=items, text=text, idempotency_key=idempotency_key,
        )

    def interactions(self, account_id: str = '', limit: int = 100) -> dict[str, Any]:
        return self.workspace.interactions.list(platform='xiaohongshu', account_id=account_id, limit=limit)

    def execute_interaction(self, interaction_id: str, confirmed: bool) -> dict[str, Any]:
        return self.workspace.interactions.execute(interaction_id, confirmed)

    def query_interaction(self, interaction_id: str) -> dict[str, Any]:
        return self.workspace.interactions.refresh_result(interaction_id)['interaction']
