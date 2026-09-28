from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ripple.accounts import AccountInput
from ripple.campaign_sources import _xhs_empty_failure, _xhs_transient_empty
from ripple.workspace import WorkspaceService
from ripple import xhs_browser
from ripple.xhs_ops import _compact_snapshot_data


def _connected_account(service: WorkspaceService, label: str) -> dict:
    account = service.accounts.create(AccountInput(
        platform="xiaohongshu",
        label=label,
        idempotency_key=f"xhs-review-{label}",
    ))
    with service.store.transaction() as state:
        state["accounts"][account["id"]].update(
            status="connected",
            identity={"logged_in": True, "name": label, "remote_id": label},
        )
    return service.accounts.get(account["id"])


def _event_payload(marker: str, count: int = 3) -> dict:
    return {
        "source": "creator_activity_center_api",
        "api_observed": True,
        "raw_count": count,
        "listed_count": count,
        "orders": {},
        "page_url": "https://creator.xiaohongshu.com/new/events?token=PRIVATE",
        "items": [
            {
                "external_id": f"{marker}-{index}",
                "title": f"{marker}活动{index}",
                "description": f"{marker}详情{index}",
                "url": f"https://fe.xiaohongshu.com/ditto/{marker}/{index}?xsec_token=PRIVATE",
                "publish_url": f"https://creator.xiaohongshu.com/publish?xsec_token=PRIVATE",
                "topics": [{
                    "id": str(index),
                    "name": f"话题{index}",
                    "link": f"https://www.xiaohongshu.com/page/topics/{index}?xsec_token=PRIVATE",
                }],
            }
            for index in range(count)
        ],
    }


def test_event_snapshot_archive_preserves_history_and_unrelated_account_reads(tmp_path):
    service = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    try:
        account_a = _connected_account(service, "A")
        account_b = _connected_account(service, "B")
        legacy = {
            "id": "legacy-b",
            "account_id": account_b["id"],
            "kind": "events",
            "at": "fixture",
            "data": _event_payload("HISTORIC", 4),
        }
        with service.store.transaction() as state:
            state["xhs_snapshots"] = [deepcopy(legacy)]

        # A normal read for another account must not opportunistically rewrite B.
        service.xhs_ops._record_snapshot(account_a["id"], "feed", {"items": [{"title": "feed"}]})
        with service.store.transaction(write=False) as state:
            raw_b = next(row for row in state["xhs_snapshots"] if row.get("id") == "legacy-b")
        assert len(raw_b["data"]["items"]) == 4

        # B's next event snapshot migrates only B's old event row after archiving it.
        service.xhs_ops._record_snapshot(account_b["id"], "events", _event_payload("CURRENT", 2))
        with service.store.transaction(write=False) as state:
            b_rows = [
                deepcopy(row)
                for row in state["xhs_snapshots"]
                if row.get("account_id") == account_b["id"] and row.get("kind") == "events"
            ]
        assert len(b_rows) == 2
        assert all("items" not in row["data"] for row in b_rows)
        assert [row["data"]["item_count"] for row in b_rows] == [4, 2]
        assert all(row["data"].get("archive_ref") for row in b_rows)

        hydrated = service.xhs_ops.snapshots(account_b["id"], kind="events", limit=10)["items"]
        assert len(hydrated) == 2
        assert [len(row["data"]["items"]) for row in hydrated] == [2, 4]
        assert hydrated[1]["data"]["items"][0]["title"] == "HISTORIC活动0"
        serialized = json.dumps(hydrated, ensure_ascii=False)
        assert "PRIVATE" not in serialized
        assert "xsec_token" not in serialized

        # Later unrelated writes do not mutate B's lightweight metadata or archive.
        service.xhs_ops._record_snapshot(account_a["id"], "notes", {"items": [{"title": "note"}]})
        with service.store.transaction(write=False) as state:
            after = [
                deepcopy(row)
                for row in state["xhs_snapshots"]
                if row.get("account_id") == account_b["id"] and row.get("kind") == "events"
            ]
        assert [row["data"]["item_count"] for row in after] == [4, 2]
        assert [row["data"]["archive_ref"] for row in after] == [row["data"]["archive_ref"] for row in b_rows]
    finally:
        service.close()


def test_event_snapshot_compaction_is_idempotent():
    first = _compact_snapshot_data("events", {
        "items": [{"external_id": str(index)} for index in range(250)],
        "listed_count": 250,
    })
    second = _compact_snapshot_data("events", first)
    third = _compact_snapshot_data("events", second)
    assert first["item_count"] == second["item_count"] == third["item_count"] == 250
    assert first["snapshot_schema"] == second["snapshot_schema"] == third["snapshot_schema"] == 2


class _Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1_790_328_000 + self.now


class _EmptyMatches:
    @property
    def first(self):
        return self

    def count(self):
        return 0


class _Response:
    def __init__(
        self,
        *,
        status: int = 200,
        query: str = "sort=1&type=1&source=3&topic_activity=0",
        mode: str = "json",
        payload: dict | None = None,
    ):
        self.status = status
        self.url = "https://creator.xiaohongshu.com/api/galaxy/v2/creator/activity_center/list?" + query
        self.headers = {
            "content-type": "application/json" if mode != "nonjson" else "text/html",
            "set-cookie": "PRIVATE_COOKIE",
        }
        self.mode = mode
        self.payload = payload if payload is not None else {"success": True, "data": {"activity_list": []}}

    def json(self):
        if self.mode == "badjson":
            raise ValueError("PRIVATE_RESPONSE_BODY")
        return deepcopy(self.payload)


class _Page:
    def __init__(self, clock: _Clock, response: _Response, *, navigation_error: bool = False):
        self.clock = clock
        self.response = response
        self.navigation_error = navigation_error
        self.handlers = []
        self.url = "about:blank"
        self.reload_count = 0

    def on(self, name, callback):
        if name == "response":
            self.handlers.append(callback)

    def remove_listener(self, name, callback):
        if callback in self.handlers:
            self.handlers.remove(callback)

    def _emit(self):
        for handler in self.handlers[:]:
            handler(self.response)

    def goto(self, url, **_kwargs):
        self.url = url + "?private_token=SECRET"
        if self.navigation_error:
            raise TimeoutError("navigation timeout at ?private_token=SECRET")
        self._emit()

    def reload(self, **_kwargs):
        self.reload_count += 1
        self._emit()

    def wait_for_timeout(self, ms):
        self.clock.now += ms / 1000

    def locator(self, selector):
        if selector == ".login-container":
            return _EmptyMatches()
        return SimpleNamespace(inner_text=lambda **_kwargs: "创作者中心 活动广场")

    def get_by_text(self, *_args, **_kwargs):
        return _EmptyMatches()

    def evaluate(self, *_args):
        return []


def _collect(tmp_path: Path, monkeypatch, response: _Response, *, navigation_error: bool = False):
    clock = _Clock()
    page = _Page(clock, response, navigation_error=navigation_error)
    process = SimpleNamespace(stop=lambda: None)
    context = SimpleNamespace(close=lambda: None)
    monkeypatch.setattr(xhs_browser, "_launch", lambda _directory: (process, context, page))
    monkeypatch.setattr(xhs_browser, "time", clock)
    result = xhs_browser.creator_events(tmp_path, detail_limit=0)
    return result, page


@pytest.mark.parametrize("status", [401, 403, 429])
def test_blocking_activity_http_status_stops_browser_reload(tmp_path, monkeypatch, status):
    result, page = _collect(tmp_path, monkeypatch, _Response(status=status))
    assert page.reload_count == 0
    assert result["diagnostics"]["http_statuses"] == [status]
    assert result["diagnostics"]["code"] == f"http_{status}"
    assert not _xhs_transient_empty(result)
    serialized = json.dumps(result, ensure_ascii=False)
    assert "PRIVATE_COOKIE" not in serialized
    assert "SECRET" not in serialized


def test_blocking_status_is_recorded_before_query_validation(tmp_path, monkeypatch):
    result, page = _collect(
        tmp_path,
        monkeypatch,
        _Response(status=429, query="sort=1&type=1&source=3"),
    )
    assert page.reload_count == 0
    assert result["diagnostics"]["http_statuses"] == [429]
    assert result["diagnostics"]["code"] == "http_429"
    assert result["diagnostics"]["query_mismatch_count"] == 0
    assert not _xhs_transient_empty(result)


def test_navigation_timeout_returns_typed_sanitized_diagnostics(tmp_path, monkeypatch):
    result, page = _collect(tmp_path, monkeypatch, _Response(), navigation_error=True)
    assert page.reload_count == 0
    assert result["diagnostics"]["code"] == "navigation_timeout"
    assert result["diagnostics"]["stage"] == "initial_navigation"
    assert result["diagnostics"]["error_type"] == "TimeoutError"
    serialized = json.dumps(result, ensure_ascii=False)
    assert "private_token" not in serialized
    assert "SECRET" not in serialized

    message, status = _xhs_empty_failure(result, attempts=2)
    assert status == 502
    assert "加载超时" in message
    assert "未观察到" not in message


def test_http_503_is_reported_as_server_response(tmp_path, monkeypatch):
    result, page = _collect(tmp_path, monkeypatch, _Response(status=503))
    assert page.reload_count == 0
    assert result["diagnostics"]["http_statuses"] == [503]
    message, status = _xhs_empty_failure(result, attempts=2)
    assert status == 502
    assert "HTTP 503" in message
    assert "服务端响应异常" in message
    assert "未观察到" not in message
