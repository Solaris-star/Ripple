from __future__ import annotations

import uuid

from ripple import native_worker
from ripple.douyin_browser import _candidates


def test_douyin_creator_json_parser_extracts_activity_fields():
    rows = _candidates({"status_code": 0, "list": [{
        "activity_id": "7681577746870326281", "show_name": "九月创作激励",
        "show_start_time": 1789000000, "show_end_time": 1791000000,
        "jump_link": "https://creator.douyin.com/activity/t1", "challenge_ids": [0, 12345],
    }]})
    assert rows[0]["external_id"] == "7681577746870326281"
    assert rows[0]["title"] == "九月创作激励"
    assert rows[0]["url"] == "https://creator.douyin.com/activity/t1"
    assert rows[0]["display_starts_at"].endswith("+08:00")
    assert rows[0]["challenge_ids"] == ["12345"]
    assert "submit_deadline" not in rows[0]


def test_native_worker_douyin_campaign_read_does_not_require_removed_legacy_publisher(tmp_path, monkeypatch):
    from ripple import douyin_browser
    monkeypatch.setattr(douyin_browser, "run", lambda action, directory, params: {
        "source": "creator_activity_api",
        "items": [{"external_id": "t1", "title": "创作活动"}],
    })
    result = native_worker.execute({
        "platform": "douyin",
        "operation": "campaign_read",
        "operation_id": uuid.uuid4().hex,
        "private_dir": str(tmp_path / "account"),
        "headed": False,
        "profile_label": "测试抖音",
        "browser_channel": "",
        "campaign_action": "events",
        "campaign_params": {"limit": 10},
    })
    assert result["state"] == "success"
    assert result["not_submitted"] is True
    assert result["data"]["items"][0]["title"] == "创作活动"


def test_native_worker_douyin_login_and_probe_use_new_profile_adapter(tmp_path, monkeypatch):
    from ripple import douyin_browser
    monkeypatch.setattr(douyin_browser, "login", lambda directory, **kwargs: {
        "logged_in": True, "name": "creator", "remote_id": "",
    })
    monkeypatch.setattr(douyin_browser, "probe", lambda directory, **kwargs: {
        "logged_in": True, "name": "creator", "remote_id": "",
    })
    base = {
        "platform": "douyin",
        "private_dir": str(tmp_path / "account"),
        "headed": True,
        "profile_label": "测试抖音",
        "browser_channel": "",
    }
    login = native_worker.execute({**base, "operation": "login", "operation_id": uuid.uuid4().hex})
    probe = native_worker.execute({**base, "operation": "probe", "operation_id": uuid.uuid4().hex})
    assert login["state"] == "connected"
    assert probe["state"] == "connected"
