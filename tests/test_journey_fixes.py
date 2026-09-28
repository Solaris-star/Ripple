import io
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from ripple.agent_credentials import agent_tool_token
from ripple.api import install
from ripple.library import MotherCreate, MotherRevision
from ripple.variants import VariantBatchInput
from ripple.workspace import WorkspaceService
from ripple.ideation_engine import candidate_failure_message, parse_candidates, parse_brief


def test_agent_credentials_survive_restart_and_concurrent_start(tmp_path, monkeypatch):
    monkeypatch.setenv("RIPPLE_SECRET_KEY_FILE", str(tmp_path / "machine.key"))
    private = tmp_path / "private"
    with ThreadPoolExecutor(max_workers=4) as pool:
        tokens = list(pool.map(lambda _: agent_tool_token(private), range(8)))
    assert len(set(tokens)) == 1
    assert agent_tool_token(private) == tokens[0]
    assert tokens[0] not in (private / "agent-tool-auth.json").read_text()
    assert agent_tool_token(tmp_path / "another") != tokens[0]


def test_corrupt_credentials_are_not_silently_rotated(tmp_path):
    path = tmp_path / "agent-tool-auth.json"
    path.write_text('{"protected":"broken"}')
    with pytest.raises(Exception):
        agent_tool_token(tmp_path)
    assert json.loads(path.read_text())["protected"] == "broken"


def test_old_runtime_token_is_valid_after_web_restart(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from web import app as webapp
    monkeypatch.setenv("RIPPLE_SECRET_KEY_FILE", str(tmp_path / "machine.key"))
    inherited_token = agent_tool_token(tmp_path / "private")
    monkeypatch.setattr(webapp, '_read_ideas', lambda: [])
    for _ in range(2):
        monkeypatch.setattr(webapp, '_AGENT_RUNTIME', SimpleNamespace(tool_token=agent_tool_token(tmp_path / "private")))
        with TestClient(webapp.app, base_url='http://127.0.0.1') as client:
            response = client.post('/api/ripple-agent/tools/ideas/list', json={'limit': 1}, headers={'X-Ripple-Agent-Token': inherited_token})
            assert response.status_code == 200
            assert client.post('/api/ripple-agent/tools/ideas/list', json={'limit': 1}, headers={'X-Ripple-Agent-Token': 'wrong-token'}).status_code == 403


def test_same_model_does_not_authorize_unrelated_opencode(tmp_path, monkeypatch):
    from ripple.opencode_runtime import OpenCodeAgentAdapter, OpenCodeError
    runtime = OpenCodeAgentAdapter(tmp_path, tmp_path / 'private', lambda: {})
    monkeypatch.setattr(runtime, '_settings', lambda: {'model': 'fixture', 'port': '4096'})
    monkeypatch.setattr(runtime, '_health', lambda _: {'healthy': True})
    monkeypatch.setattr(runtime, '_request', lambda *args, **kwargs: {'model': 'ripple/fixture', 'instructions': ['/another/project.md']})
    with pytest.raises(OpenCodeError, match='端口已被其他配置占用'):
        runtime.ensure_started('http://127.0.0.1:7860')


def test_save_and_read_keep_variants_and_update_staleness(tmp_path):
    work = WorkspaceService(tmp_path / "outputs", private=tmp_path / "private")
    try:
        first = work.library.create(MotherCreate(title="稿件", body="正文", idempotency_key="journey-content"))
        assert first["variants"] == []
        variant = work.variants.create_many(first["id"], VariantBatchInput(
            expected_source_version=first["version_id"], idempotency_key="journey-variant",
            targets=[{"platform": "xiaohongshu"}],
        ))["items"][0]
        assert work.library.get(first["id"])["variants"][0]["stale"] is False
        saved = work.library.revise(first["id"], MotherRevision(
            **{**first["content"], "tags": "待办清单"}, expected_version=first["version_id"],
        ))
        assert saved["variants"][0]["id"] == variant["id"]
        assert saved["variants"][0]["stale"] is True
        assert saved["variants"] == work.library.get(first["id"])["variants"] == work.library.list()[0]["variants"]
        assert work.variants.get(variant["id"])["content"]["tags"] == ""
    finally:
        work.close()


@pytest.mark.parametrize('filename, expected', [('../test-cover.png', 'test-cover.png'), ('CON.png', 'media-CON.png'), ('_cover.png', 'media-_cover.png'), ('封面.png', '封面.png')])
def test_upload_keeps_safe_original_filename_and_avoids_overwrite(tmp_path, filename, expected):
    app = FastAPI()
    install(app, outputs=tmp_path / "outputs", private=tmp_path / "private")
    stream = io.BytesIO()
    Image.new("RGB", (4, 4)).save(stream, format="PNG")
    with TestClient(app, base_url="http://127.0.0.1") as client:
        paths = []
        for _ in range(2):
            response = client.post('/api/ripple/media', files={"file": (filename, stream.getvalue(), "image/png")})
            assert response.status_code == 201, response.text
            paths.append(response.json()["path"])
        assert paths[0] != paths[1]
        assert all(Path(path).name == expected and '..' not in path for path in paths)


def test_failed_candidates_explain_actual_rejection():
    diagnostics = {}
    parse_candidates('bad json', 3, [], allowed_source_refs=set(), target_platforms=['xiaohongshu'], source_title_refs={}, campaign_by_ref={}, diagnostics=diagnostics)
    assert '可读取' in candidate_failure_message(diagnostics)
    diagnostics = {}
    row = {'title': '重复标题', 'angle': '角度', 'reason': '理由'}
    assert parse_candidates(json.dumps({'recommendations': [row]}), 3, ['重复标题'], allowed_source_refs=set(), target_platforms=['xiaohongshu'], source_title_refs={}, campaign_by_ref={}, diagnostics=diagnostics) == []
    assert '重复' in candidate_failure_message(diagnostics)


def test_brief_marks_unverified_personal_and_effect_claims():
    brief = parse_brief(json.dumps({'title_directions': ['我的清单终于清空了，完成率翻倍']}), {}, allowed_refs=set())
    assert brief['evidence_checks'][0].startswith('经历与效果待核实：')
    plain = parse_brief(json.dumps({'title_directions': ['可以尝试每天列三件事']}), {}, allowed_refs=set())
    assert plain['evidence_checks'] == []


def test_recommendation_keeps_user_constraints_even_if_model_omits_them(tmp_path, monkeypatch):
    import asyncio
    from web import app as webapp
    from ripple.ideation import IdeationService
    service = IdeationService(tmp_path / 'ideas.sqlite3')
    monkeypatch.setattr(webapp, '_IDEATION', service)
    request = {'persona': '测试', 'goal': '手机整理待办', 'instruction': '不编造亲测经历，不使用付费软件', 'limit': 3}
    run, _ = service.create_run('recommend', request, 'preserve-constraints')
    async def context(_request):
        return {}, [], [], {}, ['xiaohongshu'], []
    async def generate(_prompt):
        return json.dumps({'recommendations': [{'title': '待办整理方法', 'angle': '少量目标', 'reason': '易于开始'}]}), 'direct'
    monkeypatch.setattr(webapp, '_idea_run_context', context)
    monkeypatch.setattr(webapp, '_idea_call_model', generate)
    asyncio.run(webapp._run_idea_job(run['id']))
    saved = service.get_run(run['id'])
    assert saved['status'] == 'succeeded'
    idea = service.get_idea(saved['result']['idea_ids'][0])
    assert request['instruction'] in idea['requirements']
