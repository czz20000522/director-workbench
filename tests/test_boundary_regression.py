"""Regression tests for the deterministic-tool boundary (Issue #118).

The workbench stores, validates, renders, compares and assembles. It does not
understand, recommend, choose, adopt, or silently fall back for the caller.
These tests pin the observable contract rather than implementation wording.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from backend import app as backend


ROOT = Path(__file__).resolve().parents[1]


def _write_plan(*, segments: list[dict], revision: int = 1) -> None:
    document, plan_path = backend.load_project_plan('preset-test')
    plan_path.write_text(json.dumps({**document, 'revision': revision, 'segments': segments},
                                    ensure_ascii=False), encoding='utf-8')


def test_state_readiness_requires_an_explicit_stage_and_offers_facts_without_writing(preset_client):
    client, _ = preset_client
    _write_plan(segments=[{'id': 'S01', 'duration_seconds': 5, 'status': 'planned'}])
    state = client.get('/api/projects/preset-test/state')
    assert state.status_code == 200, state.text
    row = state.json()['readiness']['segments'][0]
    blocker = row['blockers'][0]
    assert blocker['code'] == 'stage_required'
    assert blocker['action']['path'] == '/api/projects/preset-test/pipeline'
    # Selection facts are supplied so an Agent can choose explicitly.
    assert 'available_presets' in blocker['options']
    assert 'installed_pipeline_stages' in blocker['options']
    assert row['stage_id'] is None and not row['ready'] and not row['queueable']
    assert 'validate' not in {action['id'] for action in row['allowed_actions']}
    assert 'submit' not in {action['id'] for action in row['allowed_actions']}
    # Reading readiness is not allowed to create a task or invent a selection.
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_state_readiness_reports_recipe_unresolved_for_registered_workflow(preset_client):
    client, _ = preset_client
    installed = client.post('/api/projects/preset-test/production-presets/h3-guided')
    assert installed.status_code == 200, installed.text
    _write_plan(segments=[{'id': 'S01', 'duration_seconds': 5,
                           'workflow': 'workflows/not-installed.json'}])
    state = client.get('/api/projects/preset-test/state')
    assert state.status_code == 200, state.text
    row = state.json()['readiness']['segments'][0]
    blocker = row['blockers'][0]
    assert blocker['code'] == 'recipe_unresolved'
    assert row['stage_id'] is None
    # The actually installed stage is exposed as a fact, not auto-applied.
    assert blocker['options']['installed_pipeline_stages']
    assert blocker['options']['installed_stages']
    assert client.get('/api/projects/preset-test/tasks').json()['tasks'] == []


def test_model_status_names_observations_not_platform_understanding(preset_client):
    client, _ = preset_client
    status = client.get('/api/models/reverse-analysis')
    assert status.status_code == 200
    primary = status.json()['primary']
    assert primary['role'] == 'observation_candidates'
    # Adding a second model does not grant the platform an automatic analysis step.
    assert status.json()['fast_optional']['role'] == 'optional_fast_visual_prepass'


def test_ui_copy_does_not_claim_understanding_recommendation_or_default_plan():
    concatenated = '\n'.join(
        (ROOT / 'src' / name).read_text(encoding='utf-8')
        for name in ('App.tsx', 'ReverseWorkbench.tsx', 'QuickStudioPrototype.tsx')
    )
    for phrase in ('统一完成视觉理解', '推荐配置', '可直接复用默认方案', '平台已理解', '平台推荐'):
        assert phrase not in concatenated
    # The template list must read the factual key, not a recommended one.
    assert 'data.available_templates' in (ROOT / 'src/App.tsx').read_text(encoding='utf-8')


def test_reference_naming_uses_observation_candidate_contract():
    semantic = (ROOT / 'tools/reference_semantic.py').read_text(encoding='utf-8')
    decompose = (ROOT / 'tools/reference_decompose.py').read_text(encoding='utf-8')
    combined = semantic + decompose
    for stale in ('primary_multimodal_analysis_and_prompt_writing',
                  'vision_story_analysis_prompt_writing'):
        assert stale not in combined
    # Candidate/observation wording is visible; model artifacts stay reviewed.
    assert '可迁移机制候选' in decompose
    assert '制作草案种子候选' in decompose


def test_mcp_read_project_exposes_template_facts_and_readiness_sections():
    pytest.importorskip('mcp')
    from mcp import Client
    from mcp_server.server import create_server

    seen: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={'available_templates': []})

    async def verify() -> None:
        server = create_server('http://localhost:4100', httpx.MockTransport(respond))
        async with Client(server) as client:
            templates = await client.call_tool('read_project', {
                'project_id': 'work-a', 'section': 'reusable-templates'})
            state = await client.call_tool('read_project', {
                'project_id': 'work-a', 'section': 'state'})
            assert templates.structured_content['ok']
            assert state.structured_content['ok']

    asyncio.run(verify())
    assert ('GET', '/api/projects/work-a/reusable-templates') in seen
    assert ('GET', '/api/projects/work-a/state') in seen


def test_mcp_tool_surface_has_no_automatic_or_recommendation_entry():
    pytest.importorskip('mcp')
    from mcp import Client
    from mcp_server.server import create_server

    async def verify() -> None:
        server = create_server('http://localhost:4100', httpx.MockTransport(
            lambda request: httpx.Response(200, json={})))
        async with Client(server) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert not [name for name in tools if 'auto' in name or 'recommend' in name]
            approve = tools['approve_candidate'].input_schema
            assert 'confirm_adoption' in approve.get('required', [])
            read_project = json.dumps(tools['read_project'].input_schema, ensure_ascii=False)
            assert 'reusable-templates' in read_project

    asyncio.run(verify())
