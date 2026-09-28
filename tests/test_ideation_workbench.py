from __future__ import annotations

import json
from pathlib import Path

import pytest

from ripple.ideation import IdeationError, IdeationService
from ripple.ideation_engine import parse_brief, parse_candidates, persona_platforms, platform_key, source_ref


def make_service(tmp_path: Path, legacy=None) -> IdeationService:
    legacy_path = tmp_path / "_ideas.json"
    if legacy is not None:
        legacy_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
    return IdeationService(tmp_path / "_ideation" / "ideas.sqlite3", legacy_path=legacy_path)


def test_legacy_migration_preserves_identity_fields_and_backup(tmp_path):
    legacy = [{
        "id": "legacy001", "title": "旧选题", "note": "备注", "source": "微博热点",
        "status": "doing", "created": 1700000000, "angle": "旧角度", "reason": "旧理由",
        "campaign_id": "campaign1", "campaign_rule_version": 3,
        "trend_refs": ["热点 A"], "target_platforms": ["xiaohongshu"],
        "requirements": ["需要实测"], "pending_checks": ["核验数据"],
    }]
    raw = json.dumps(legacy, ensure_ascii=False).encode()
    (tmp_path / "_ideas.json").write_bytes(raw)
    service = make_service(tmp_path)
    rows = service.list_ideas()
    assert len(rows) == 1
    idea = rows[0]
    assert idea["id"] == "legacy001"
    assert idea["title"] == "旧选题"
    assert idea["created"] == 1700000000
    assert idea["stage"] == "production"
    assert idea["target_platforms"] == ["xiaohongshu"]
    assert idea["campaign_rule_version"] == 3
    assert (tmp_path / "_ideas.json").read_bytes() == raw
    backups = list((tmp_path / "_ideation").glob("legacy-_ideas-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw

    # Restarting the service never duplicates the migrated record.
    restarted = make_service(tmp_path)
    assert [row["id"] for row in restarted.list_ideas()] == ["legacy001"]


def test_run_idempotency_conflict_and_restart_interruption(tmp_path):
    service = make_service(tmp_path, [])
    request = {"persona": "测试号", "target_platforms": ["douyin"], "trend_sources": [], "limit": 6}
    first, created = service.create_run("recommend", request, "run-same-key")
    assert created and first["status"] == "queued"
    same, created = service.create_run("recommend", request, "run-same-key")
    assert not created and same["id"] == first["id"]
    with pytest.raises(IdeationError) as exc:
        service.create_run("recommend", {**request, "limit": 3}, "run-same-key")
    assert exc.value.status == 409

    restarted = make_service(tmp_path)
    interrupted = restarted.get_run(first["id"])
    assert interrupted["status"] == "interrupted"
    assert "没有自动重复调用模型" in interrupted["error"]


def test_fixed_trend_title_filters_sources_without_changing_target_platform(monkeypatch):
    import asyncio
    from web import app as upstream

    monkeypatch.setattr(upstream, "profile_exists", lambda name: True)
    monkeypatch.setattr(upstream, "load_profile_text", lambda name: "## 小红书\n科技工具账号")
    monkeypatch.setattr(upstream, "_read_ideas", lambda: [])
    monkeypatch.setattr(upstream._TREND_SERVICE, "get_group", lambda platform, limit: {
        "platform": platform, "label": "微博", "status": "fresh", "source": "fixture", "fetched_at": 123,
        "items": [
            {"title": "不相关热点", "hot": "100", "url": "https://example.test/a"},
            {"title": "指定热点", "hot": "200", "url": "https://example.test/b"},
        ],
    })
    context, sources, existing, campaigns, targets, summary = asyncio.run(upstream._idea_run_context({
        "persona": "测试画像",
        "target_platforms": ["xiaohongshu"],
        "trend_sources": ["weibo"],
        "trend_titles": ["指定热点"],
        "include_trends": True,
        "include_campaigns": False,
        "campaign_ids": [],
        "instruction": "",
        "goal": "",
        "effort_minutes": 0,
        "limit": 6,
    }))
    assert targets == ["xiaohongshu"]
    assert [row["title"] for row in sources] == ["指定热点"]
    assert [row["title"] for row in context["sources"]] == ["指定热点"]
    assert context["fixed_trend_titles"] == ["指定热点"]
    assert summary == [{"platform": "weibo", "label": "微博", "status": "fresh", "count": 1}]
    assert existing == [] and campaigns == {}


def test_candidate_parser_rejects_fake_sources_and_out_of_scope_platforms():
    trend = source_ref("trend", "weibo", "https://example.test/a", "热点 A")
    campaign = source_ref("campaign", "douyin", "campaign-1", "活动一")
    raw = json.dumps({"recommendations": [{
        "title": "一个可靠选题",
        "angle": "用实测对照回答一个具体问题",
        "reason": "与账号定位一致，且资料范围明确",
        "score": 81,
        "platforms": ["小红书", "微博"],
        "source_refs": [trend, campaign, "trend:fake:evil"],
        "trend_refs": ["热点 A", "模型捏造热点"],
        "requirements": ["保留实测条件"],
        "pending_checks": [],
        "platform_plans": [
            {"platform": "小红书", "angle": "图文对照", "format": "图文", "hook": "先展示结果", "adaptation": "强调收藏价值"},
            {"platform": "微博", "angle": "越权平台", "format": "短帖", "hook": "", "adaptation": ""},
        ],
    }]}, ensure_ascii=False)
    rows = parse_candidates(
        raw, 6, [],
        allowed_source_refs={trend, campaign},
        target_platforms=["xiaohongshu", "douyin"],
        source_title_refs={"热点 A": trend},
        campaign_by_ref={campaign: {
            "id": "campaign-1", "rule_version": 4, "content_requirements": ["必须原创"],
            "required_topics": ["活动话题"], "submit_deadline": "2026-10-10",
            "qualification_state": "unknown",
        }},
    )
    assert len(rows) == 1
    row = rows[0]
    assert row["platforms"] == ["xiaohongshu"]
    assert "trend:fake:evil" not in row["source_refs"]
    assert set(row["source_refs"]) == {trend, campaign}
    assert row["campaign_id"] == "campaign-1" and row["campaign_rule_version"] == 4
    assert "必须原创" in row["requirements"]
    assert "必须带话题：活动话题" in row["requirements"]
    assert "投稿截止：2026-10-10" in row["requirements"]
    assert any("资格" in value for value in row["pending_checks"])
    assert [value["platform"] for value in row["platform_plans"]] == ["xiaohongshu"]


def test_candidate_parser_supports_evergreen_without_source():
    raw = json.dumps({"recommendations": [{
        "title": "常青教程",
        "angle": "一个可复用的操作流程",
        "reason": "来自账号长期内容支柱",
        "score": 70,
        "platforms": ["douyin"],
        "source_refs": [],
        "trend_refs": [],
        "requirements": [],
        "pending_checks": ["需要补一次真实录屏"],
        "platform_plans": [],
    }]}, ensure_ascii=False)
    rows = parse_candidates(
        raw, 6, [],
        allowed_source_refs=set(), target_platforms=["douyin"],
        source_title_refs={}, campaign_by_ref={},
    )
    assert rows[0]["source_refs"] == []
    assert rows[0]["campaign_id"] == ""
    assert rows[0]["platform_plans"][0]["platform"] == "douyin"


def test_persona_platform_parser_and_aliases_are_normalized():
    profile = "# 平台运营\n\n## 抖音\n做短视频\n\n## 小红书\n做图文\n"
    assert persona_platforms(profile) == ["xiaohongshu", "douyin"]
    assert platform_key("小红书") == "xiaohongshu"
    assert platform_key("B站") == "bilibili"
    assert platform_key("不存在平台") == ""


def test_brief_parser_filters_sources_and_platforms():
    raw = json.dumps({
        "audience": "想提升效率的知识工作者",
        "objective": "让用户能复现实测方法",
        "core_thesis": "先验证准确率，再比较速度",
        "differentiation": "公开测试条件",
        "title_directions": ["标题一", "标题二"],
        "hook": "先展示一个失败案例",
        "outline": [{"title": "测试条件", "purpose": "说明边界", "evidence_needed": ["录屏"]}],
        "platform_plans": [
            {"platform": "抖音", "title": "短视频版", "hook": "失败案例", "format": "60s 视频", "adaptation": "快节奏"},
            {"platform": "微博", "title": "越权版", "hook": "", "format": "短帖", "adaptation": ""},
        ],
        "evidence_checks": ["核对版本号"],
        "production_tasks": ["录一次屏"],
        "open_questions": ["是否有第二组样本"],
        "source_refs": ["trend:ok", "fake:ref"],
    }, ensure_ascii=False)
    brief = parse_brief(raw, {"target_platforms": ["douyin"], "source_refs": ["trend:ok"]}, allowed_refs={"trend:ok"})
    assert brief["platform_plans"][0]["platform"] == "douyin"
    assert len(brief["platform_plans"]) == 1
    assert brief["source_refs"] == ["trend:ok"]
    assert brief["outline"][0]["evidence_needed"] == ["录屏"]


def test_brief_revision_conflict_confirm_and_content_link(tmp_path):
    service = make_service(tmp_path, [])
    idea = service.create_idea({
        "title": "待深化选题", "status": "pending", "persona": "测试号",
        "target_platforms": ["douyin"], "source_refs": [],
    }, stage="saved")
    first = service.save_brief(
        idea["id"],
        {"audience": "A", "objective": "B", "core_thesis": "C", "differentiation": "",
         "title_directions": [], "hook": "", "outline": [], "platform_plans": [],
         "evidence_checks": [], "production_tasks": [], "open_questions": [], "source_refs": []},
        source="agent", expected_revision=0,
    )
    assert first["revision"] == 1
    with pytest.raises(IdeationError) as exc:
        service.save_brief(idea["id"], first["data"], source="user", expected_revision=0)
    assert exc.value.status == 409
    confirmed = service.confirm_brief(idea["id"], 1)
    assert confirmed["status"] == "confirmed"
    linked = service.attach_content(idea["id"], "content-123")
    assert linked["content_id"] == "content-123" and linked["stage"] == "production"


def test_start_content_is_idempotent_for_confirmed_brief(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from web import app as upstream

    service = make_service(tmp_path, [])
    idea = service.create_idea({"title": "幂等交接", "status": "pending"}, stage="saved")
    brief = service.save_brief(
        idea["id"],
        {"audience": "A", "objective": "B", "core_thesis": "C", "differentiation": "",
         "title_directions": [], "hook": "", "outline": [], "platform_plans": [],
         "evidence_checks": [], "production_tasks": [], "open_questions": [], "source_refs": []},
        source="user", expected_revision=0,
    )
    service.confirm_brief(idea["id"], brief["revision"])

    created = []
    class Library:
        def create(self, request):
            created.append(request.idempotency_key)
            return {"id": "content-one", "version_id": "v1", "content": {"title": request.title}}
        def get(self, content_id):
            assert content_id == "content-one"
            return {"id": content_id, "version_id": "v1", "content": {"title": "幂等交接"}}
    monkeypatch.setattr(upstream, "_IDEATION", service)
    monkeypatch.setattr(upstream, "_RIPPLE_WORKSPACE", SimpleNamespace(
        library=Library(),
        plans=SimpleNamespace(get=lambda *_: (_ for _ in ()).throw(AssertionError("no plan expected"))),
    ))
    request = upstream.IdeaStartContentInput(expected_revision=brief["revision"], idempotency_key="idea-content-test")
    first = asyncio.run(upstream.api_idea_start_content(idea["id"], request))
    second = asyncio.run(upstream.api_idea_start_content(idea["id"], request))
    assert first["created"] is True and second["created"] is False
    assert first["content"]["id"] == second["content"]["id"] == "content-one"
    assert created == ["idea-content-test"]


def test_manual_start_content_without_ai_or_brief_preserves_idea(tmp_path, monkeypatch):
    import asyncio
    from web import app as upstream
    from ripple.workspace import WorkspaceService

    service = make_service(tmp_path, [])
    idea = service.create_idea({"title": "手动写稿", "note": "先归类，再清理", "persona": "测试画像",
                                "target_platforms": ["xiaohongshu"], "status": "pending"}, stage="saved")
    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    monkeypatch.setattr(upstream, "_IDEATION", service)
    monkeypatch.setattr(upstream, "_RIPPLE_WORKSPACE", work)
    monkeypatch.setattr(upstream, "_recommendation_ai_backend", lambda: None)
    try:
        request = upstream.IdeaStartContentInput(manual=True, idempotency_key="manual-content-test")
        first = asyncio.run(upstream.api_idea_start_content(idea["id"], request))
        again = asyncio.run(upstream.api_idea_start_content(idea["id"], request))
        assert first["created"] is True and again["created"] is False
        assert first["content"]["id"] == again["content"]["id"]
        assert first["content"]["content"]["body"] == "先归类，再清理"
        assert first["idea"]["target_platforms"] == ["xiaohongshu"]
        assert first["idea"]["persona"] == "测试画像"
        assert first["idea"]["stage"] == "production"
        assert first["idea"]["content_id"] == first["content"]["id"]
    finally:
        work.close()


def test_ai_start_still_requires_confirmed_current_brief(tmp_path, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from web import app as upstream

    service = make_service(tmp_path, [])
    idea = service.create_idea({"title": "未确认策划", "status": "pending"}, stage="saved")
    monkeypatch.setattr(upstream, "_IDEATION", service)
    with pytest.raises(HTTPException) as error:
        asyncio.run(upstream.api_idea_start_content(idea["id"], upstream.IdeaStartContentInput(
            expected_revision=1, idempotency_key="still-require-brief")))
    assert error.value.status_code == 409


def test_idea_plan_requires_and_preserves_user_selected_time(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from web import app as upstream

    service = make_service(tmp_path, [])
    idea = service.create_idea({"title": "明确排期", "status": "pending"}, stage="saved")
    observed = {}
    class Plans:
        def create(self, request):
            observed.update(request.model_dump())
            return {"id": "plan-one", "version": 1, "scheduled_local": request.scheduled_local,
                    "timezone": request.timezone, "source_id": request.source_id}
        def get(self, *_):
            raise AssertionError("create path should not read an existing plan")
    monkeypatch.setattr(upstream, "_IDEATION", service)
    monkeypatch.setattr(upstream, "_RIPPLE_WORKSPACE", SimpleNamespace(plans=Plans()))
    request = upstream.IdeaPlanInput(
        scheduled_local="2026-10-08T14:30", timezone="America/Chicago", fold=None,
        idempotency_key="idea-plan-test",
    )
    result = asyncio.run(upstream.api_idea_plan(idea["id"], request))
    assert result["plan"]["scheduled_local"] == "2026-10-08T14:30"
    assert observed["scheduled_local"] == "2026-10-08T14:30"
    assert observed["timezone"] == "America/Chicago"
    assert service.get_idea(idea["id"])["plan_id"] == "plan-one"


def test_feedback_hides_rejected_but_can_restore(tmp_path):
    service = make_service(tmp_path, [])
    idea = service.create_idea({"title": "候选", "status": "pending"}, stage="candidate")
    service.feedback(idea["id"], "reject", "方向不合适")
    assert service.list_ideas() == []
    assert service.list_ideas(include_rejected=True)[0]["stage"] == "rejected"
    restored = service.feedback(idea["id"], "reopen")
    assert restored["stage"] == "candidate"
