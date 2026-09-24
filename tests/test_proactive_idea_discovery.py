from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import time

from ripple.idea_discovery import (
    IdeaDiscoveryService, build_opportunities, source_digest, source_health,
)
from ripple.ideation import IdeationService
from ripple.trends import TrendService


def ideation(tmp_path: Path) -> IdeationService:
    return IdeationService(tmp_path / "ideas.sqlite3", legacy_path=tmp_path / "legacy.json")


def discovery(tmp_path: Path) -> IdeaDiscoveryService:
    IdeationService(tmp_path / "ideas.sqlite3", legacy_path=tmp_path / "legacy.json")
    return IdeaDiscoveryService(tmp_path / "ideas.sqlite3")


def policy_payload(**overrides):
    value = {
        "enabled": True,
        "target_platforms": ["xiaohongshu", "douyin"],
        "trend_sources": ["weibo"],
        "account_ids": [],
        "mode": "balanced",
        "focus_keywords": ["AI工具", "办公效率"],
        "effort_minutes": 120,
        "max_daily_runs": 2,
        "max_daily_candidates": 2,
        "max_daily_notices": 2,
        "min_candidate_score": 68,
        "timezone": "America/Chicago",
        "quiet_start": "22:00",
        "quiet_end": "08:00",
        "important_notifications": False,
    }
    value.update(overrides)
    return value


def opportunity(key="opp_one", evidence="ev1"):
    return {
        "key": key,
        "kind": "combo",
        "evidence_digest": evidence,
        "trend_titles": ["AI会议纪要工具更新"],
        "trend_sources": ["weibo"],
        "campaign_ids": ["campaign-one"],
        "trigger_summary": "热点与活动自然关联",
        "source_reason": "热点与活动组合",
    }


def candidate(title="候选一", score=82):
    return {
        "title": title,
        "angle": "用同一份脱敏会议纪要对照测试",
        "reason": "符合账号方向且可制作",
        "score": score,
        "platforms": ["xiaohongshu"],
        "target_platforms": ["xiaohongshu"],
        "trend_refs": ["AI会议纪要工具更新"],
        "source_refs": ["trend:weibo:abc"],
        "requirements": [],
        "pending_checks": [],
        "platform_plans": [],
        "campaign_id": "",
        "campaign_rule_version": 0,
    }


def test_source_snapshots_are_scoped_by_run(tmp_path):
    service = ideation(tmp_path)
    request = {"persona": "p", "target_platforms": ["xiaohongshu"], "trend_sources": ["weibo"]}
    first, _ = service.create_run("recommend", request, "run-source-one")
    second, _ = service.create_run("recommend", request, "run-source-two")
    source = [{
        "id": "trend:weibo:same",
        "kind": "trend",
        "source_id": "same",
        "title": "同一热点",
        "platform": "weibo",
        "url": "https://example.test/trend",
        "fetched_at": 1,
        "version": "1",
        "access_scope": "public",
        "data": {},
    }]
    assert service.replace_sources(first["id"], source)[0]["id"] == "trend:weibo:same"
    assert service.replace_sources(second["id"], source)[0]["id"] == "trend:weibo:same"
    assert service.list_sources(first["id"])[0]["title"] == "同一热点"
    assert service.list_sources(second["id"])[0]["title"] == "同一热点"


def test_manual_and_automatic_runs_are_isolated(tmp_path):
    service = ideation(tmp_path)
    manual, _ = service.create_run("recommend", {
        "persona": "p", "target_platforms": [], "trend_sources": [], "origin": "manual",
    }, "manual-run")
    automatic, _ = service.create_run("recommend", {
        "persona": "p", "target_platforms": [], "trend_sources": [], "origin": "automatic",
        "policy_id": "policy-one", "opportunity_key": "opp-one",
    }, "auto-run")
    assert [row["id"] for row in service.list_runs(origin="manual")] == [manual["id"]]
    assert [row["id"] for row in service.list_runs(origin="automatic")] == [automatic["id"]]
    assert automatic["origin"] == "automatic"
    assert automatic["policy_id"] == "policy-one"


def test_automatic_same_opportunity_updates_existing_candidate(tmp_path):
    service = ideation(tmp_path)
    request = {
        "persona": "p", "target_platforms": ["xiaohongshu"], "trend_sources": ["weibo"],
        "origin": "automatic", "policy_id": "policy-one", "opportunity_key": "opp-one",
    }
    first_run, _ = service.create_run("recommend", request, "auto-candidate-one")
    first = service.store_candidates(
        first_run["id"], "p", [candidate("第一版")],
        origin="automatic", opportunity_key="opp-one", trigger_summary="第一次发现",
    )[0]
    second_run, _ = service.create_run("recommend", request, "auto-candidate-two")
    second = service.store_candidates(
        second_run["id"], "p", [candidate("更新后的角度")],
        origin="automatic", opportunity_key="opp-one", trigger_summary="规则有实质更新",
    )[0]
    rows = service.list_ideas(persona="p")
    assert len(rows) == 1
    assert first["id"] == second["id"]
    assert rows[0]["title"] == "更新后的角度"
    assert rows[0]["run_id"] == second_run["id"]
    assert rows[0]["unread"] is True
    assert rows[0]["trigger_summary"] == "规则有实质更新"


def test_trend_peek_never_calls_provider(tmp_path, monkeypatch):
    service = TrendService(tmp_path / "trend-cache.json", tmp_path / "private")
    called = []
    monkeypatch.setattr(service, "_providers", lambda platform: (_ for _ in ()).throw(AssertionError("provider called")))
    missing = service.peek_group("weibo", 10)
    assert missing["status"] == "missing"
    assert missing["items"] == []

    service._cache["weibo:public"] = {
        "fetched_at": int(time.time()),
        "source": "fixture-cache",
        "items": [{"title": "AI 办公效率", "hot": "123", "url": "https://example.test"}],
    }
    cached = service.peek_group("weibo", 10)
    assert cached["status"] == "fresh"
    assert cached["cached"] is True
    assert [row["title"] for row in cached["items"]] == ["AI 办公效率"]
    assert called == []


def test_source_digest_ignores_timestamp_and_minor_heat_changes():
    campaigns = [{
        "id": "c1", "platform": "xiaohongshu", "title": "AI工具活动", "rule_version": 1,
        "status": "active", "submit_deadline": "2026-10-10", "qualification_state": "unknown",
        "content_requirements": ["实测"], "required_topics": ["AI效率"], "ai_policy": "",
    }]
    first = [{"platform": "weibo", "status": "fresh", "fetched_at": 100,
              "items": [{"title": "AI办公效率", "url": "https://example.test/a", "hot": "100"}]}]
    second = [{"platform": "weibo", "status": "stale", "fetched_at": 200,
               "items": [{"title": "AI办公效率", "url": "https://example.test/a", "hot": "101"}]}]
    assert source_digest(first, campaigns) == source_digest(second, campaigns)
    assert source_health(second, campaigns)["degraded"] is False


def test_combo_opportunity_requires_relevance_and_time_window():
    now = datetime_ts = 1790200000.0
    trends = [{
        "platform": "weibo", "status": "fresh", "fetched_at": int(now),
        "items": [{"title": "AI会议纪要效率工具更新", "url": "https://example.test/a", "hot": "热"}],
    }]
    good_campaign = {
        "id": "c1", "platform": "xiaohongshu", "title": "AI办公效率实测征集",
        "summary": "分享AI工具真实体验", "activity_type": "创作投稿", "status": "active",
        "qualification_state": "unknown", "ai_policy": "", "submit_deadline": "2026-12-01",
        "content_requirements": ["真实实测"], "required_topics": ["AI效率"], "reward_summary": "流量激励",
        "rule_version": 1,
    }
    rows = build_opportunities(
        profile_text="科技工具账号，关注AI工具和办公效率",
        focus_keywords=["AI工具", "办公效率"],
        target_platforms=["xiaohongshu"],
        trend_groups=trends,
        campaigns=[good_campaign],
        mode="combo_only",
        effort_minutes=120,
        now=now,
    )
    assert rows and rows[0]["kind"] == "combo"
    assert rows[0]["campaign_ids"] == ["c1"]

    forbidden = {**good_campaign, "id": "c2", "ai_policy": "禁止使用AI生成内容"}
    assert not build_opportunities(
        profile_text="科技工具账号，关注AI工具和办公效率",
        focus_keywords=["AI工具"], target_platforms=["xiaohongshu"],
        trend_groups=trends, campaigns=[forbidden], mode="combo_only",
        effort_minutes=120, now=now,
    )
    urgent = {**good_campaign, "id": "c3", "submit_deadline": "2026-09-23T15:00:00+00:00"}
    assert not build_opportunities(
        profile_text="科技工具账号，关注AI工具和办公效率",
        focus_keywords=["AI工具"], target_platforms=["xiaohongshu"],
        trend_groups=trends, campaigns=[urgent], mode="combo_only",
        effort_minutes=600, now=now,
    )


def test_policy_budget_duplicate_pause_and_state(tmp_path):
    service = discovery(tmp_path)
    now = 1790200000.0
    policy = service.configure("科技工具号", policy_payload(max_daily_runs=1, max_daily_candidates=1), now=now)
    assert policy["enabled"] is True
    first = service.admit(policy, opportunity(), source_digest_value="digest-one", trigger_type="bootstrap", now=now)
    assert first and first["status"] == "queued"
    assert service.admit(policy, opportunity(), source_digest_value="digest-one", trigger_type="bootstrap", now=now) is None
    assert service.admit(policy, opportunity("opp_two", "ev2"), source_digest_value="digest-two", trigger_type="source_change", now=now) is None
    state = service.state("科技工具号", now=now)
    assert state["today"]["runs"] == 1
    assert state["today"]["candidates"] == 1

    paused = service.configure("科技工具号", policy_payload(enabled=False), now=now + 1)
    assert paused["enabled"] is False
    assert service.claim_next(owner="worker", now=now + 2) is None


def test_claim_recovery_and_unknown_outcome_are_conservative(tmp_path):
    service = discovery(tmp_path)
    now = 1790200000.0
    policy = service.configure("科技工具号", policy_payload(), now=now)
    job = service.admit(policy, opportunity(), source_digest_value="d1", trigger_type="bootstrap", now=now)
    claimed = service.claim_next(owner="worker", now=now, lease_seconds=30)
    assert claimed and claimed["id"] == job["id"]
    with sqlite3.connect(tmp_path / "ideas.sqlite3") as db:
        db.execute("UPDATE discovery_jobs SET lease_until=? WHERE id=?", (int(now) - 1, job["id"]))
        db.commit()
    recovered = IdeaDiscoveryService(tmp_path / "ideas.sqlite3")
    assert recovered.claim_next(owner="worker2", now=now + 1) is not None

    job2 = recovered.admit(policy, opportunity("opp_two", "e2"), source_digest_value="d2", trigger_type="source_change", now=now + 2)
    claimed2 = recovered.claim_next(owner="worker3", now=now + 2)
    # First recovered job may still be claimed; attach whichever was returned then verify startup is conservative.
    recovered.attach_run(claimed2["id"], "run-x", now=now + 2)
    restarted = IdeaDiscoveryService(tmp_path / "ideas.sqlite3")
    states = {row["id"]: row["status"] for row in restarted.state("科技工具号", now=now + 3)["jobs"]}
    assert states[claimed2["id"]] == "outcome_unknown"


def test_finish_creates_inbox_notice_and_seen_clears_it(tmp_path):
    service = discovery(tmp_path)
    now = 1790200000.0
    policy = service.configure("科技工具号", policy_payload(), now=now)
    job = service.admit(policy, opportunity(), source_digest_value="d1", trigger_type="bootstrap", now=now)
    claimed = service.claim_next(owner="worker", now=now)
    service.attach_run(claimed["id"], "run-one", now=now)
    service.finish(claimed["id"], status="succeeded", idea_ids=["idea-one"], now=now + 5)
    assert service.state("科技工具号", now=now + 5)["unread"] == 1
    service.mark_idea_read("idea-one")
    assert service.state("科技工具号", now=now + 5)["unread"] == 0


def test_notice_cap_does_not_drop_extra_candidate(tmp_path):
    ideas = ideation(tmp_path)
    service = IdeaDiscoveryService(tmp_path / "ideas.sqlite3")
    now = 1790200000.0
    policy = service.configure("科技工具号", policy_payload(max_daily_notices=1, max_daily_runs=3, max_daily_candidates=3), now=now)
    first_idea = ideas.create_idea({"title": "机会一", "origin": "automatic", "unread": True}, stage="candidate")
    second_idea = ideas.create_idea({"title": "机会二", "origin": "automatic", "unread": True}, stage="candidate")
    for index, idea in enumerate((first_idea, second_idea), 1):
        job = service.admit(policy, opportunity(f"opp_{index}", f"ev{index}"), source_digest_value=f"d{index}", trigger_type="source_change", now=now + index)
        claimed = service.claim_next(owner=f"worker{index}", now=now + index)
        service.attach_run(claimed["id"], f"run-{index}", now=now + index)
        service.finish(claimed["id"], status="succeeded", idea_ids=[idea["id"]], now=now + index + 1)
    state = service.state("科技工具号", now=now + 10)
    assert state["unread"] == 1
    assert state["today"]["notices"] == 1
    stored = {row["title"]: row for row in ideas.list_ideas()}
    assert set(stored) == {"机会一", "机会二"}
    assert sum(bool(row["unread"]) for row in stored.values()) == 1


def test_scheduler_is_cache_only_and_never_triggers_trend_collection(tmp_path, monkeypatch):
    import asyncio
    from web import app as upstream

    policy = {**policy_payload(), "id": "policy-one", "persona": "测试画像", "revision": 1, "trend_sources": ["weibo", "douyin"]}
    scanned = []
    monkeypatch.setattr(upstream, "_DISCOVERY_TICK_LOCK", asyncio.Lock())
    monkeypatch.setattr(upstream._DISCOVERY, "list_enabled", lambda: [policy])
    monkeypatch.setattr(upstream, "_discovery_policy_context_status", lambda p: {"requires_review": False, "review_reason": "", "current_profile_revision": 1})
    monkeypatch.setattr(upstream, "_reconcile_discovery_jobs", lambda: asyncio.sleep(0))
    monkeypatch.setattr(upstream, "_dispatch_discovery_job", lambda now: asyncio.sleep(0))
    monkeypatch.setattr(upstream, "_scan_discovery_policy", lambda p, now: scanned.append((p["id"], now)) or {"admitted": 0})
    monkeypatch.setattr(upstream._TREND_SERVICE, "get_group", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("discovery must not collect trends")))
    asyncio.run(upstream._idea_discovery_scheduler_tick())
    assert len(scanned) == 1 and scanned[0][0] == "policy-one"


def test_scheduler_skips_scan_when_profile_context_requires_review(monkeypatch):
    import asyncio
    from web import app as upstream

    policy = {**policy_payload(), "id": "policy-review", "persona": "测试画像", "revision": 1, "trend_sources": ["weibo"]}
    scanned = []
    monkeypatch.setattr(upstream, "_DISCOVERY_TICK_LOCK", asyncio.Lock())
    monkeypatch.setattr(upstream._DISCOVERY, "list_enabled", lambda: [policy])
    monkeypatch.setattr(upstream, "_reconcile_discovery_jobs", lambda: asyncio.sleep(0))
    monkeypatch.setattr(upstream, "_dispatch_discovery_job", lambda now: asyncio.sleep(0))
    monkeypatch.setattr(upstream, "_discovery_policy_context_status", lambda p: {"requires_review": True, "review_reason": "画像已更新", "current_profile_revision": 2})
    monkeypatch.setattr(upstream, "_scan_discovery_policy", lambda p, now: scanned.append((p["id"], now)))
    asyncio.run(upstream._idea_discovery_scheduler_tick())
    assert scanned == []


def test_source_change_waits_for_stable_window_before_scan(tmp_path):
    service = discovery(tmp_path)
    now = 1790200000.0
    policy = service.configure("科技工具号", policy_payload(), now=now)
    assert service.source_change_due(policy, "digest-one", now, coalesce_seconds=120) is False
    policy = service.get_policy("科技工具号")
    assert policy and policy["pending_source_digest"] == "digest-one"
    assert service.source_change_due(policy, "digest-one", now + 119, coalesce_seconds=120) is False
    assert service.source_change_due(policy, "digest-one", now + 120, coalesce_seconds=120) is True


def test_dispatch_creates_automatic_run_but_does_not_execute_model_in_test(tmp_path, monkeypatch):
    import asyncio
    from web import app as upstream

    ideas = ideation(tmp_path)
    discovery_service = IdeaDiscoveryService(tmp_path / "ideas.sqlite3")
    now = 1790200000.0
    policy = discovery_service.configure("测试画像", policy_payload(), now=now)
    request = {
        "persona": "测试画像", "target_platforms": ["xiaohongshu"], "trend_sources": ["weibo"],
        "trend_titles": ["AI办公效率"], "include_trends": True, "include_campaigns": False,
        "campaign_ids": [], "instruction": "只生成一个候选", "goal": "主动发现", "effort_minutes": 120,
        "limit": 1, "origin": "automatic", "policy_id": policy["id"], "opportunity_key": "opp-auto",
        "trigger_summary": "来源变化", "min_candidate_score": 68,
    }
    job = discovery_service.admit(policy, {**opportunity("opp-auto", "ev-auto"), "request": request}, source_digest_value="digest", trigger_type="source_change", now=now)
    spawned = []
    monkeypatch.setattr(upstream, "_DISCOVERY", discovery_service)
    monkeypatch.setattr(upstream, "_IDEATION", ideas)
    monkeypatch.setattr(upstream, "_recommendation_ai_backend", lambda: "direct")
    monkeypatch.setattr(upstream, "_discovery_policy_context_status", lambda p: {"requires_review": False, "review_reason": "", "current_profile_revision": 1})
    monkeypatch.setattr(upstream, "_spawn_idea_job", lambda run_id: spawned.append(run_id))
    asyncio.run(upstream._dispatch_discovery_job(now + 1))
    stored_job = discovery_service.state("测试画像", now=now + 1)["jobs"][0]
    assert stored_job["status"] == "running"
    run = ideas.get_run(stored_job["run_id"])
    assert run["origin"] == "automatic"
    assert run["policy_id"] == policy["id"]
    assert run["opportunity_key"] == "opp-auto"
    assert spawned == [run["id"]]


def test_dispatch_cancels_claimed_job_when_profile_context_changed(tmp_path, monkeypatch):
    import asyncio
    from web import app as upstream

    ideas = ideation(tmp_path)
    discovery_service = IdeaDiscoveryService(tmp_path / "ideas.sqlite3")
    now = 1790201000.0
    policy = discovery_service.configure("测试画像", policy_payload(), now=now)
    request = {
        "persona": "测试画像", "target_platforms": ["xiaohongshu"], "trend_sources": ["weibo"],
        "trend_titles": ["AI办公效率"], "include_trends": True, "include_campaigns": False,
        "campaign_ids": [], "instruction": "只生成一个候选", "goal": "主动发现", "effort_minutes": 120,
        "limit": 1, "origin": "automatic", "policy_id": policy["id"], "opportunity_key": "opp-review",
        "trigger_summary": "来源变化", "min_candidate_score": 68,
    }
    job = discovery_service.admit(policy, {**opportunity("opp-review", "ev-review"), "request": request}, source_digest_value="digest-review", trigger_type="source_change", now=now)
    assert job is not None
    spawned = []
    monkeypatch.setattr(upstream, "_DISCOVERY", discovery_service)
    monkeypatch.setattr(upstream, "_IDEATION", ideas)
    monkeypatch.setattr(upstream, "_recommendation_ai_backend", lambda: "direct")
    monkeypatch.setattr(upstream, "_discovery_policy_context_status", lambda p: {"requires_review": True, "review_reason": "画像已更新，请复核", "current_profile_revision": 2})
    monkeypatch.setattr(upstream, "_spawn_idea_job", lambda run_id: spawned.append(run_id))
    asyncio.run(upstream._dispatch_discovery_job(now + 1))
    stored_job = discovery_service.state("测试画像", now=now + 1)["jobs"][0]
    assert stored_job["status"] == "cancelled"
    assert "画像已更新" in stored_job["error"]
    assert spawned == []
    assert ideas.list_runs(10, persona="测试画像") == []
    assert ideas.list_runs(origin="manual") == []
