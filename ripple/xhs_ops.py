"""Xiaohongshu read, interaction-draft and metric coordination for Ripple.

Browser execution stays inside the account-isolated native worker. This service
owns public sanitization, short-lived note locators, interaction idempotency and
result reconciliation. Agent tools may read and create drafts but never execute
an external interaction without the user's explicit workspace confirmation.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
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


def _compact_snapshot_data(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    public = deepcopy(data)
    public.pop('_locators', None)
    if kind != 'events':
        return public
    items = public.pop('items', None)
    if isinstance(items, list):
        item_count = len(items)
    else:
        try:
            item_count = int(public.get('item_count') if public.get('item_count') is not None else public.get('listed_count') or 0)
        except (TypeError, ValueError):
            item_count = 0
    public['item_count'] = max(0, item_count)
    public['snapshot_schema'] = 2
    diagnostics = public.get('diagnostics')
    if isinstance(diagnostics, dict):
        public['diagnostics'] = {
            'code': str(diagnostics.get('code') or '')[:80],
            'stage': str(diagnostics.get('stage') or '')[:40],
            'error_type': str(diagnostics.get('error_type') or '')[:80],
            'final_page': _public_url(str(diagnostics.get('final_page') or '')),
            'body_state': str(diagnostics.get('body_state') or '')[:20],
            'api_list_responses_seen': max(0, int(diagnostics.get('api_list_responses_seen') or 0)),
            'api_responses_accepted': max(0, int(diagnostics.get('api_responses_accepted') or 0)),
            'query_mismatch_count': max(0, int(diagnostics.get('query_mismatch_count') or 0)),
            'non_json_count': max(0, int(diagnostics.get('non_json_count') or 0)),
            'invalid_payload_count': max(0, int(diagnostics.get('invalid_payload_count') or 0)),
            'parse_error_count': max(0, int(diagnostics.get('parse_error_count') or 0)),
            'http_statuses': [int(v) for v in diagnostics.get('http_statuses', []) if isinstance(v, int)][:6],
            'sort_errors': {
                str(k)[:24]: str(v)[:80]
                for k, v in (diagnostics.get('sort_errors') or {}).items()
            } if isinstance(diagnostics.get('sort_errors'), dict) else {},
        }
    return public


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

    def _event_archive_dir(self, account_id: str) -> Path:
        return self.accounts.directory(account_id) / "xhs-event-snapshots"

    @staticmethod
    def _event_archive_name(snapshot_id: str) -> str:
        return hashlib.sha256(str(snapshot_id or "").encode("utf-8")).hexdigest()[:32] + ".json.gz"

    def _write_event_archive(self, account_id: str, snapshot_id: str, data: dict[str, Any]) -> str:
        archive_dir = self._event_archive_dir(account_id)
        archive_dir.mkdir(parents=True, exist_ok=True)
        name = self._event_archive_name(snapshot_id)
        path = archive_dir / name
        if path.is_file() and path.stat().st_size > 0:
            return name
        public = deepcopy(data)
        public.pop('_locators', None)
        for item in public.get('items', []) if isinstance(public.get('items'), list) else []:
            if not isinstance(item, dict):
                continue
            for field in ('url', 'publish_url'):
                if item.get(field):
                    item[field] = _public_url(str(item.get(field) or ''))
            for topic in item.get('topics', []) if isinstance(item.get('topics'), list) else []:
                if isinstance(topic, dict) and topic.get('link'):
                    topic['link'] = _public_url(str(topic.get('link') or ''))
        if public.get('page_url'):
            public['page_url'] = _public_url(str(public.get('page_url') or ''))
        compact = _compact_snapshot_data('events', public)
        if isinstance(compact.get('diagnostics'), dict):
            public['diagnostics'] = compact['diagnostics']
        if public.get('page_url'):
            public['page_url'] = _public_url(str(public.get('page_url') or ''))
        temp = path.with_suffix(path.suffix + ".tmp")
        with gzip.open(temp, "wt", encoding="utf-8") as handle:
            json.dump(public, handle, ensure_ascii=False, separators=(",", ":"))
        os.replace(temp, path)
        return name

    def _read_event_archive(self, account_id: str, archive_ref: str) -> dict[str, Any] | None:
        ref = str(archive_ref or "")
        if not re.fullmatch(r"[0-9a-f]{32}\.json\.gz", ref):
            return None
        path = self._event_archive_dir(account_id) / ref
        try:
            if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
                return None
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                raw = handle.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                return None
            value = json.loads(raw)
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _hydrate_event_snapshot(self, row: dict[str, Any]) -> dict[str, Any]:
        current = deepcopy(row)
        data = current.get('data')
        if current.get('kind') != 'events' or not isinstance(data, dict):
            return current
        archive = self._read_event_archive(str(current.get('account_id') or ''), str(data.get('archive_ref') or ''))
        if archive is not None:
            current['data'] = archive
        return current

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
        snapshot_id = uuid.uuid4().hex
        public = _compact_snapshot_data(kind, data)
        if kind == 'events':
            public['archive_ref'] = self._write_event_archive(account_id, snapshot_id, data)
        entry = {'id': snapshot_id, 'account_id': account_id, 'kind': kind, 'at': _now(), 'data': public}
        with self.store.transaction() as state:
            rows = state.setdefault('xhs_snapshots', [])
            if not isinstance(rows, list):
                rows = state['xhs_snapshots'] = []
            updated = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                current = deepcopy(row)
                if kind == 'events' and current.get('account_id') == account_id and current.get('kind') == 'events' and isinstance(current.get('data'), dict):
                    current_data = current['data']
                    if isinstance(current_data.get('items'), list):
                        current_data['archive_ref'] = self._write_event_archive(
                            account_id, str(current.get('id') or uuid.uuid4().hex), current_data,
                        )
                    current['data'] = _compact_snapshot_data('events', current_data)
                    if current_data.get('archive_ref'):
                        current['data']['archive_ref'] = str(current_data.get('archive_ref'))
                updated.append(current)
            updated.append(entry)
            state['xhs_snapshots'] = updated[-MAX_SNAPSHOTS:]

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

    def events(self, account_id: str, limit: int = 500, *, detail_limit: int = 12) -> dict[str, Any]:
        result = self._worker(account_id, 'events', {'limit': max(1, min(limit, 500)),
                                                     'detail_limit': max(0, min(detail_limit, 20))})
        payload = {'source': str(result['data'].get('source') or 'creator_events'),
                   'sample_scope': 'selected_account_creator_events', 'fetched_at': _now(), **result['data']}
        self._record_snapshot(account_id, 'events', payload)
        return payload

    def event_detail(self, account_id: str, *, url: str, activity_id: str = '', force: bool = False) -> dict[str, Any]:
        url = str(url or '').strip()
        if not url or len(url) > 2048:
            raise WorkflowError("小红书活动详情链接无效。", 422)
        result = self._worker(account_id, 'event_detail', {
            'url': url, 'activity_id': str(activity_id or '')[:160], 'force': bool(force),
        })
        payload = {
            'source': 'creator_event_detail',
            'sample_scope': f'creator_event:{str(activity_id or "")[:160]}',
            'fetched_at': _now(),
            **result['data'],
        }
        self._record_snapshot(account_id, 'event_detail', payload)
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
        hydrated = [self._hydrate_event_snapshot(row) for row in selected[-max(1, min(limit, 100)):][::-1]]
        return {'items': hydrated}

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
