from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from ripple.api import install
from ripple.publishing import CreateInput, WorkflowError
from ripple.workspace import WorkspaceService


def workspace(tmp_path: Path) -> WorkspaceService:
    return WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")


def model_json(payload):
    return lambda prompt: json.dumps(payload, ensure_ascii=False)


def topic_payload(score: int = 8):
    names = ["流量潜力", "账号匹配", "竞争差异化", "时效价值", "变现潜力", "制作成本", "合规风险"]
    return {
        "dimensions": [{"name": name, "score": score, "reason": f"{name}基于当前提供信息评估"} for name in names],
        "summary": "整体值得尝试，但没有实时平台热度数据。",
        "optimizations": ["强化具体受众", "收窄切入角度"],
        "alternatives": ["替代选题A", "替代选题B"],
        "assumptions": ["未提供实时平台搜索量"],
    }


def seed_remote_comments(service: WorkspaceService, source_id: str = "d" * 32):
    with service.store.transaction() as state:
        state["interaction_sources"] = {source_id: {
            "id": source_id, "kind": "remote", "platform": "xiaohongshu", "label": "真实同步作品",
            "account_id": "a" * 32, "account_label": "测试账号", "target_id": "note1", "target_url": "",
            "comments": [
                {"id": "c1", "nickname": "甲", "content": "参数是多少？", "time": 1, "time_str": "刚刚", "like": "2", "parent": ""},
                {"id": "c2", "nickname": "乙", "content": "价格太贵了，踩雷", "time": 2, "time_str": "1小时前", "like": "1", "parent": ""},
            ],
            "count": 2, "created_at": "2026-09-10T00:00:00+00:00", "updated_at": "2026-09-10T01:00:00+00:00",
        }}


def test_registry_has_exact_five_migrated_operations_and_readiness(tmp_path):
    service = workspace(tmp_path)
    rows = {row["id"]: row for row in service.operations.registry()["items"]}
    assert set(rows) == {"topic_evaluate", "text_polish", "comment_analysis", "template_apply", "publish_checklist"}
    assert rows["topic_evaluate"]["ready"] is False
    assert rows["text_polish"]["ready"] is False
    assert rows["comment_analysis"]["ready"] is True
    assert rows["template_apply"]["ready"] is True
    assert rows["publish_checklist"]["ready"] is True
    assert service.operations.operation_for_skill("skill-topic-evaluator")["module"] == "ideas"
    assert service.operations.operation_for_skill("text-polisher")["module"] == "contents"
    assert service.operations.operation_for_skill("not-migrated") is None


def test_topic_evaluation_strict_json_and_server_computed_score(tmp_path):
    service = workspace(tmp_path)
    service.operations.configure_model(model_json(topic_payload(8)), lambda: True)
    result = service.operations.execute("topic_evaluate", {
        "title": "AI 旅游图怎么做", "note": "教程型", "platform": "xiaohongshu", "persona": "",
    }, {"kind": "idea", "ref": "idea1", "version": "v1", "snapshot": {"title": "AI 旅游图怎么做"}})
    assert result["output"]["score"] == 80
    assert result["output"]["decision"] == "做"
    assert [x["name"] for x in result["output"]["dimensions"]] == ["流量潜力", "账号匹配", "竞争差异化", "时效价值", "变现潜力", "制作成本", "合规风险"]
    assert result["source"]["ref"] == "idea1" and len(result["source"]["digest"]) == 64

    service.operations.configure_model(lambda prompt: "```json\n{}\n```", lambda: True)
    with pytest.raises(WorkflowError, match="不符合操作契约"):
        service.operations.execute("topic_evaluate", {"title": "不能静默接受坏 JSON"})


def test_text_polish_is_preview_and_source_digest_changes_with_snapshot(tmp_path):
    service = workspace(tmp_path)
    service.operations.configure_model(model_json({
        "revised_text": "更自然的正文。", "changes": ["删掉套话"], "warnings": [],
    }), lambda: True)
    first = service.operations.execute("text_polish", {"title": "标题", "body": "原正文。", "mode": "natural"},
        {"kind": "content_draft", "ref": "content1", "version": "edit-1", "snapshot": {"body": "原正文。"}})
    second = service.operations.execute("text_polish", {"title": "标题", "body": "用户已改正文。", "mode": "natural"},
        {"kind": "content_draft", "ref": "content1", "version": "edit-2", "snapshot": {"body": "用户已改正文。"}})
    assert first["output"]["revised_text"] == "更自然的正文。"
    assert first["source"]["digest"] != second["source"]["digest"]
    assert service.library.list() == []  # operation execution itself did not create or overwrite content
    with pytest.raises(WorkflowError, match="输入不符合契约"):
        service.operations.execute("text_polish", {"body": "   ", "mode": "natural"})


def test_comment_analysis_reuses_remote_interaction_analysis(tmp_path):
    service = workspace(tmp_path)
    source_id = "d" * 32
    seed_remote_comments(service, source_id)
    expected = service.interactions.analyze_source(source_id)
    result = service.operations.execute("comment_analysis", {"source_id": source_id})
    assert result["output"] == expected
    assert result["source"]["kind"] == "interaction_source"
    assert result["source"]["version"] == "2026-09-10T01:00:00+00:00"

    with service.store.transaction() as state:
        state["interaction_sources"]["legacy01"] = {
            "id": "legacy01", "kind": "import", "platform": "generic", "label": "旧记录", "account_id": "",
            "comments": [{"id": "c", "content": "hello"}], "count": 1, "created_at": "", "updated_at": "",
        }
    with pytest.raises(WorkflowError, match="真实平台账号同步"):
        service.operations.execute("comment_analysis", {"source_id": "legacy01"})


def test_template_preview_reports_missing_and_never_increments_usage(tmp_path):
    service = workspace(tmp_path)
    catalog = service.operations.templates()["items"]
    template = next(row for row in catalog if row["id"] == "builtin:tutorial-steps")
    assert "title" in template["variables"] and "cta" in template["variables"]
    result = service.operations.execute("template_apply", {
        "template_id": template["id"], "values": {"title": "我的教程", "hook": "先说结论"},
    }, {"kind": "content_draft", "ref": "draft1", "version": "v1", "snapshot": {"body": "原文"}})
    assert result["output"]["preview"].startswith("我的教程")
    assert "step_1" in result["output"]["missing"]
    assert not (tmp_path / "templates" / "INDEX.json").exists()

    with pytest.raises(WorkflowError, match="未知变量"):
        service.operations.execute("template_apply", {"template_id": template["id"], "values": {"evil": "x"}})


def test_publish_checklist_is_read_only_and_separates_blockers_from_advice(tmp_path):
    service = workspace(tmp_path)
    task = service.create(CreateInput(
        title="测试", body="", platform="blog", account_id="local", mode="blog", media=[],
        timezone="Asia/Shanghai", tags="", idempotency_key="publish-check-op",
    ))
    before = service.get(task["id"])
    no_model = service.operations.execute("publish_checklist", {"task_id": task["id"], "expected_version": task["version_id"]})
    assert no_model["output"]["ready"] is False
    assert any("正文与素材均为空" in item for item in no_model["output"]["blocking_issues"])
    assert no_model["output"]["advisories"] == []
    assert no_model["output"]["advisory_available"] is False
    after = service.get(task["id"])
    assert after["status"] == before["status"] == "draft"
    assert after["version_id"] == before["version_id"]

    service.operations.configure_model(model_json({
        "advisories": [{"item": "CTA", "detail": "可考虑增加一个自然的互动问题。"}],
        "summary": "仅提供非阻塞优化建议。",
    }), lambda: True)
    advised = service.operations.execute("publish_checklist", {"task_id": task["id"], "expected_version": task["version_id"]})
    assert advised["output"]["blocking_issues"] == no_model["output"]["blocking_issues"]
    assert advised["output"]["advisories"][0]["item"] == "CTA"
    assert service.get(task["id"])["status"] == "draft"

    service.operations.configure_model(lambda prompt: "not-json", lambda: True)
    degraded = service.operations.execute("publish_checklist", {"task_id": task["id"], "expected_version": task["version_id"]})
    assert degraded["output"]["blocking_issues"] == no_model["output"]["blocking_issues"]
    assert degraded["output"]["advisories"] == []
    assert degraded["output"]["advisory_available"] is False
    assert "结构化结果不符合操作契约" in degraded["output"]["advisory_error"]
    assert service.get(task["id"])["status"] == "draft"


def test_result_history_is_bounded_projected_and_unknown_operation_fails(tmp_path):
    service = workspace(tmp_path)
    for i in range(105):
        service.operations.execute("template_apply", {"template_id": "builtin:problem-solution-proof", "values": {"title": str(i)}})
    rows = service.operations.results(operation_id="template_apply", limit=100)["items"]
    assert len(rows) == 100
    assert all("input_digest" in row and "output" in row for row in rows)
    with pytest.raises(WorkflowError, match="未知结构化操作"):
        service.operations.execute("does_not_exist", {})


def test_operation_api_exposes_registry_templates_and_executes_deterministic_operation(tmp_path):
    app = FastAPI()
    service = install(app, tmp_path / "outputs", private=tmp_path / "private")
    with TestClient(app, base_url="http://localhost") as client:
        registry = client.get("/api/ripple/operations")
        assert registry.status_code == 200 and len(registry.json()["items"]) == 5
        templates = client.get("/api/ripple/operations/templates")
        assert templates.status_code == 200 and len(templates.json()["items"]) >= 3
        result = client.post("/api/ripple/operations/template_apply/execute", json={
            "input": {"template_id": "builtin:comparison-decision", "values": {"title": "A 还是 B"}},
            "source": {"kind": "content_draft", "ref": "draft", "version": "1", "snapshot": {"body": "x"}},
        })
        assert result.status_code == 200
        assert "option_a" in result.json()["output"]["missing"]
        history = client.get("/api/ripple/operations/results?operation_id=template_apply")
        assert history.status_code == 200 and history.json()["items"][0]["id"] == result.json()["id"]


def test_agent_operation_tool_is_authenticated_and_uses_shared_service(tmp_path, monkeypatch):
    from web import app as webapp

    service = workspace(tmp_path)
    monkeypatch.setattr(webapp.app.state, "ripple", service)
    payload = {"operation": "template_apply", "input": {"template_id": "builtin:tutorial-steps", "values": {"title": "标题"}}, "source": {}}
    with TestClient(webapp.app, base_url="http://localhost") as client:
        denied = client.post("/api/ripple-agent/tools/operation", json=payload)
        assert denied.status_code == 403
        headers = {"X-Ripple-Agent-Token": webapp._AGENT_RUNTIME.tool_token}
        ok = client.post("/api/ripple-agent/tools/operation", headers=headers, json=payload)
        assert ok.status_code == 200
        body = ok.json()
        assert body["kind"] == "structured_operation"
        assert body["operation_id"] == "template_apply"
        assert "没有修改业务内容" in body["notice"]
        unknown = client.post("/api/ripple-agent/tools/operation", headers=headers, json={"operation": "unknown", "input": {}, "source": {}})
        assert unknown.status_code == 404


def test_migrated_skill_metadata_blocks_legacy_skill_execution(tmp_path, monkeypatch):
    from web import app as webapp

    service = workspace(tmp_path)
    monkeypatch.setattr(webapp.app.state, "ripple", service)
    monkeypatch.setenv("RIPPLE_ENABLE_AI", "1")
    with TestClient(webapp.app, base_url="http://localhost") as client:
        items = client.get("/api/skills")
        assert items.status_code == 200
        migrated = {row["name"]: row["structuredOperation"] for row in items.json() if row.get("structuredOperation")}
        assert migrated["skill-topic-evaluator"]["id"] == "topic_evaluate"
        assert migrated["text-polisher"]["id"] == "text_polish"
        detail = client.get("/api/skill/skill-publish-checklist")
        assert detail.status_code == 200
        assert detail.json()["structuredOperation"]["id"] == "publish_checklist"
        blocked = client.post("/api/skill", json={"skill": "skill-topic-evaluator", "input": "评估这个选题", "persona": None})
        assert blocked.status_code == 409
        assert "结构化操作" in blocked.json()["detail"]
